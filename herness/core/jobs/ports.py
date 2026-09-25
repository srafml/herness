"""Jobs backend port, row models and handler-facing protocols (U08-04, U08-41, U08-42).

Handlers are `(ctx: JobContext) -> JobOutcome` (R-02, R-42); `JobsBackend` is implemented at
L1 by `herness.store.ops.jobs.SqliteJobsBackend` and bound at the composition root (R-04).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, NamedTuple, Protocol

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
)

from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.resilience._state import bind_port, process_state
from herness.core.types import GpuClass, JobKind, ServiceName

type JobStatus = Literal["queued", "running", "done", "failed", "canceled"]
type WorkerStatus = Literal["starting", "running", "draining", "stopped"]
type StopReason = Literal["cancel", "preempt", "shutdown"]
type RequeueResult = list[tuple[str, JobKind, Literal["queued", "failed"]]]
type SqlWrites = Callable[[sqlite3.Connection], None]
type JsonMap = Mapping[str, JsonValue]
type CheckpointKey = Literal["loop", "state", "scratchpad"]
type CancelResult = Literal["canceled", "cancel_requested", "not_active"]
type RetryResult = Literal["queued", "not_failed", "conflict", "missing"]
# Placeholder: U08-41 names `QueueStats` without defining it (T08-11 or T08-22 will).
type QueueStats = Any

JOBS_UNBOUND: Final = (
    "jobs backend not bound; call herness.store.ops.resilience.bind_core_backends()"
)


def _json_text(value: object) -> object:
    """Decode a JSON TEXT column; other values pass through to field validation."""
    if isinstance(value, str | bytes):
        try:
            return json.loads(value)
        except ValueError as exc:
            msg = "invalid JSON text"
            raise ValueError(msg) from exc
    return value


def _utc(value: object) -> datetime:
    """Parse ts text (spec 00 §8) and normalise aware datetimes to UTC; reject naive ones."""
    if isinstance(value, str):
        try:
            return clock.parse_utc(value)
        except SchemaViolation as exc:
            msg = "bad timestamp text"
            raise ValueError(msg) from exc
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC)
    msg = "expected ts text or an aware datetime"
    raise ValueError(msg)


def _read_only(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    return MappingProxyType(dict(value))


# Column types: ts text and JSON TEXT decode before validation (sqlite rows parse directly).
DbTs = Annotated[datetime, BeforeValidator(_utc)]
DbJson = Annotated[dict[str, JsonValue], BeforeValidator(_json_text)]
Payload = Annotated[
    Mapping[str, JsonValue],
    BeforeValidator(_json_text),
    AfterValidator(_read_only),
    PlainSerializer(dict, return_type=dict[str, JsonValue]),
]


class JobRow(BaseModel):
    """Parsed `job` row (U08-42); frozen, with a read-only payload mapping (R-42)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    kind: JobKind
    gpu_class: GpuClass
    status: JobStatus
    priority: int
    attempts: int
    max_attempts: int
    payload: Payload
    idem_key: str | None = None
    result: DbJson | None = None
    last_error: DbJson | None = None
    lease_owner: str | None = None
    lease_expires_at: DbTs | None = None
    scheduled_for: DbTs | None = None
    created_at: DbTs | None = None
    started_at: DbTs | None = None
    finished_at: DbTs | None = None


class NewJob(BaseModel):
    """All insert columns of `job` (U08-41); built by `submit` (U08-46)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    job_id: str
    kind: JobKind
    gpu_class: GpuClass
    status: Literal["queued"] = "queued"
    priority: int = Field(ge=0, le=100)
    payload: bytes
    idem_key: str | None
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(ge=1, le=20)
    scheduled_for: DbTs
    created_at: DbTs


class WorkerRow(BaseModel):
    """All `worker` columns with JSON parsed (U08-41, §4.1.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    worker_id: str
    host: str
    pid: int
    gpu_slot: int = Field(ge=0, le=1)
    cpu_slots: int = Field(ge=0, le=16)
    gpu_class_loaded: GpuClass | Literal["swapping"]
    requested_class: GpuClass | None = None
    status: WorkerStatus
    current_jobs: Annotated[list[dict[str, JsonValue]], BeforeValidator(_json_text)] = []
    started_at: DbTs
    heartbeat_at: DbTs
    version: str
    faults_enabled: bool = False


class SchedCheck(NamedTuple):
    """Scheduler dedupe key: a finished job for this fire blocks re-creation (U08-46)."""

    schedule: str
    fire_at: str


class ServiceControl(Protocol):
    """In-class GPU service control handed to handlers (U08-04)."""

    def start(self, name: ServiceName, *, timeout_s: float | None = None) -> None: ...
    def stop(self, name: ServiceName) -> None: ...
    def healthy(self, name: ServiceName) -> bool: ...


class JobContext(Protocol):
    """The only argument of every job handler (U08-04; R-02, R-42)."""

    @property
    def job(self) -> JobRow: ...
    @property
    def job_id(self) -> str: ...
    @property
    def kind(self) -> JobKind: ...
    @property
    def attempt(self) -> int: ...
    @property
    def services(self) -> ServiceControl: ...
    @property
    def stop_reason(self) -> StopReason | None: ...
    def should_yield(self) -> bool: ...
    def heartbeat(self, note: str | None = None) -> None: ...
    def require_gpu_class(self, cls: GpuClass, *, timeout_s: float | None = None) -> None: ...
    def gpu_scope(self, cls: GpuClass) -> AbstractContextManager[None]: ...
    def save_state(self, state: dict[str, JsonValue]) -> None: ...
    def load_state(self) -> dict[str, JsonValue]: ...


class JobsBackend(Protocol):
    """SQL port of `herness.core.jobs` (U08-41; SQL in U08-95 to U08-97)."""

    def insert_job(self, job: NewJob, *, sched_check: SchedCheck | None) -> tuple[str, bool]: ...
    def claim_job(  # noqa: PLR0913 - U08-41 signature
        self,
        *,
        owner: str,
        now: datetime,
        lease_until: datetime,
        allowed_classes: Sequence[GpuClass],
        exclusive_kinds: Sequence[JobKind],
        job_id: str | None,
        min_priority: int | None,
        priority_exempt_kinds: Sequence[JobKind],
    ) -> JobRow | None: ...
    def claimable_counts(
        self,
        *,
        now: datetime,
        classes: Sequence[GpuClass],
        exclusive_kinds: Sequence[JobKind],
        min_priority: int | None,
        priority_exempt_kinds: Sequence[JobKind],
    ) -> dict[GpuClass, int]: ...
    def heartbeat_job(self, job_id: str, owner: str, lease_until: datetime) -> bool: ...
    def finish_done(self, job_id: str, owner: str, result: JsonMap, now: datetime) -> bool: ...
    def finish_yield(self, job_id: str, owner: str, resume_at: datetime, now: datetime) -> bool: ...
    def finish_requeue(
        self, job_id: str, owner: str, at: datetime, last_error: JsonMap
    ) -> bool: ...
    def finish_failed(
        self, job_id: str, owner: str, last_error: JsonMap, now: datetime
    ) -> bool: ...
    def finalize_canceled(self, job_id: str, owner: str, now: datetime) -> bool: ...
    def save_job_state(self, job_id: str, owner: str, state_json: bytes) -> bool: ...
    def load_job_state(self, job_id: str) -> dict[str, JsonValue]: ...
    def cancel_job(self, job_id: str, now: datetime) -> CancelResult: ...
    def retry_job(self, job_id: str, now: datetime) -> RetryResult: ...
    def get_job(self, job_id: str) -> JobRow | None: ...
    def list_jobs(
        self, *, status: JobStatus | None, kind: JobKind | None, limit: int
    ) -> list[JobRow]: ...
    def reap_expired(self, now: datetime) -> RequeueResult: ...
    def requeue_owned(self, owners: Sequence[str], now: datetime) -> RequeueResult: ...
    def running_on_host(self, host: str) -> list[JobRow]: ...
    def postpone_class(self, gpu_class: GpuClass, now: datetime, until: datetime) -> int: ...
    def sched_fired(self, schedule: str, fire_at_ts: str) -> bool: ...
    def recent_scheduled(self, since: datetime) -> list[JobRow]: ...
    def rekey_on(self, start: datetime, end: datetime) -> JobRow | None: ...
    def queue_stats(self, now: datetime, since: datetime) -> QueueStats: ...
    def upsert_worker(self, row: WorkerRow) -> None: ...
    def update_worker(self, worker_id: str, **fields: object) -> None: ...
    def list_workers(self) -> list[WorkerRow]: ...
    def set_requested_class(self, worker_id: str, cls: GpuClass | None) -> None: ...
    def run_row(self, run_id: str) -> tuple[str, str] | None: ...
    def recover_tasks(
        self, run_id: str, *, max_task_attempts: int, retry_dead: bool, now: datetime
    ) -> tuple[int, int, int]: ...
    def claim_task(self, task_id: str, now: datetime) -> bool: ...
    def save_checkpoint(
        self,
        task_id: str,
        key: CheckpointKey,
        value: Mapping[str, object],
        writes: SqlWrites | None,
    ) -> bool: ...
    def complete_task(
        self, task_id: str, result: Mapping[str, object], *, writes: SqlWrites | None, now: datetime
    ) -> None: ...
    def fail_task(
        self,
        task_id: str,
        *,
        retryable: bool,
        max_task_attempts: int,
        last_error: JsonMap,
        now: datetime,
    ) -> Literal["pending", "dead"]: ...
    def release_task(self, task_id: str, now: datetime) -> bool: ...


def bind_jobs_backend(backend: JobsBackend) -> None:
    """Bind the jobs SQL port (U08-41); rebinding replaces the previous backend."""
    bind_port("jobs", backend)


def require_jobs_backend() -> JobsBackend:
    """Return the bound jobs backend; unbound → `ConfigError` (U08-41 Errors)."""
    jobs = process_state().jobs
    if jobs is None:
        raise ConfigError(JOBS_UNBOUND)
    return jobs
