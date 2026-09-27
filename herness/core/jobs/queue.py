"""Job queue API (impl 08 U08-43 to U08-48, U08-51 to U08-54; design 08 §3.4, §4.1, §5.7).

Every statement runs in the bound `JobsBackend` (U08-41, SQL in `herness.store.ops.jobs`);
this module adds validation, defaults, logging and metrics. Metrics are recorded after the
backend call returns, never inside a `run_write` callback (T08-11 m3 handoff).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final, NoReturn, cast, get_args

from pydantic import JsonValue, ValidationError

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError, JobStateError, SchemaViolation
from herness.core.ids import IdKind, canonical_json, is_valid_id, new_ulid
from herness.core.jobs.ports import (
    CancelResult,
    ClaimSlot,
    JobRow,
    JobStatus,
    NewJob,
    SchedCheck,
    require_jobs_backend,
)
from herness.core.logging import get_logger
from herness.core.redact import RedactionFailed, get_redactor
from herness.core.resilience.faults import fault_point
from herness.core.resilience.metrics import record_counter, record_histogram
from herness.core.resilience.settings import JobsSettings
from herness.core.secrets import known_values
from herness.core.types import GpuClass, JobKind, JobSpec

DEFAULT_PRIORITY: Final[Mapping[JobKind, int]] = MappingProxyType(
    {
        "chat": 75,
        "sync": 60,
        "build_pipeline": 60,
        "review": 40,
        "reconcile": 40,
        "outcome_measure": 30,
        "memory_maintenance": 30,
        "maintenance": 30,
        "eval": 20,
        "distill": 40,  # not in design 08 §4.1 (open item O08-06)
    }
)
MANUAL_PRIORITY: Final = 80
# Kinds that start as class `none` and switch classes in-job: GPU slot only (R-43, D08-20).
GPU_SLOT_KINDS: Final[frozenset[JobKind]] = frozenset({"build_pipeline"})

_OWNER_RE: Final = re.compile(r"^[^:\s]{1,64}:\d{1,10}:(gpu|cpu\d{1,2}|cli)$")
_KEY_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_PAYLOAD_BYTES: Final = 65_536
_MAX_DEPTH: Final = 8  # the payload object itself is depth 1
_MIN_SECRET_CHARS: Final = 8
_SECRET_SPANS: Final = frozenset({"CREDENTIAL", "URL_TOKEN"})
_STATUSES: Final[frozenset[str]] = frozenset(get_args(JobStatus.__value__))
_KINDS: Final[frozenset[str]] = frozenset(get_args(JobKind.__value__))
_CLASSES: Final[frozenset[str]] = frozenset(get_args(GpuClass.__value__))
_LIVE_WORKER: Final = frozenset({"starting", "running", "draining"})
_LIST_MAX: Final = 1000
_ID_CHARS: Final = 64  # caller-supplied ids are cut to this length in log lines
_RETRY_ERRORS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "not_failed": "job is not failed",
        "conflict": "an active job with the same idem_key exists",
        "missing": "unknown job",
    }
)

_log: Final = get_logger("jobs")


def _jobs_cfg() -> JobsSettings:
    return get_config().resilience.resilience.jobs


def default_idem_key(kind: JobKind, payload: Mapping[str, JsonValue]) -> str:
    """`kind:` + the first 16 hex chars of sha256(canonical JSON of payload) (U08-44)."""
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{kind}:{digest[:16]}"


def _reject(reason: str) -> NoReturn:
    """Raise the U08-45 error; no exception context is kept (it could hold the value)."""
    msg = f"job payload rejected: {reason}"
    raise SchemaViolation(msg) from None


def _walk(value: object, depth: int, strings: list[str]) -> None:
    """Check JSON types, key names and depth; collect every string value (U08-45 step 1, 3)."""
    if isinstance(value, str):
        strings.append(value)
        return
    if not isinstance(value, Mapping | list | tuple):
        _check_scalar(value)
        return
    if depth > _MAX_DEPTH:
        _reject(f"nested deeper than {_MAX_DEPTH}")
    items = value.items() if isinstance(value, Mapping) else enumerate(value)
    for key, item in items:
        if isinstance(value, Mapping) and not _valid_key(key):
            _reject("invalid key name")
        _walk(item, depth + 1, strings)


def _valid_key(key: object) -> bool:
    return isinstance(key, str) and _KEY_RE.fullmatch(key) is not None


def _check_scalar(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        _reject("non-finite number")
    if value is not None and not isinstance(value, bool | int | float):
        _reject(f"not JSON: {type(value).__name__}")


def _scan_secrets(strings: Sequence[str]) -> None:
    """Refuse known secret values and credential or URL-token spans (U08-45 step 4)."""
    secrets = [s for s in known_values() if len(s) >= _MIN_SECRET_CHARS]
    redactor = get_redactor()
    for text in strings:
        if any(secret in text for secret in secrets):
            _reject("contains a known secret value")
        try:
            spans = redactor.scan(text)
        except RedactionFailed:
            _reject("string could not be scanned")
        if any(span.type in _SECRET_SPANS for span in spans):
            _reject("contains a credential or URL token")


def validate_payload(payload: Mapping[str, object]) -> bytes:
    """Canonical JSON bytes of ``payload``: JSON only, <= 64 KiB, depth <= 8, key regex, no
    secret values, credentials or URL tokens (U08-45; TH08-03, TH08-10).

    Raises SchemaViolation("job payload rejected: <reason>"); the reason never has the value.
    """
    if not isinstance(payload, Mapping):
        _reject("payload is not an object")
    strings: list[str] = []
    _walk(payload, 1, strings)
    try:
        data = canonical_json(payload).encode("utf-8")
    except SchemaViolation:
        _reject("not canonical JSON")
    if len(data) > _MAX_PAYLOAD_BYTES:
        _reject(f"larger than {_MAX_PAYLOAD_BYTES} bytes")
    _scan_secrets(strings)
    return data


def submit(spec: JobSpec, *, sched_check: SchedCheck | None = None) -> tuple[str, bool]:
    """Idempotent enqueue in one transaction (U08-46): `(job_id, created)`."""
    try:
        payload_bytes = validate_payload(spec.payload)
    except SchemaViolation as exc:
        _log.warning("jobs.job.rejected", kind=spec.kind, reason=exc.message)
        raise
    now = clock.now()
    priority = DEFAULT_PRIORITY[spec.kind] if spec.priority is None else spec.priority
    scheduled_for = spec.scheduled_for or now
    new_job = NewJob(
        job_id="job_" + new_ulid(),
        kind=spec.kind,
        gpu_class=spec.gpu_class,
        status="queued",
        priority=priority,
        payload=payload_bytes,
        idem_key=spec.idem_key or default_idem_key(spec.kind, spec.payload),
        attempts=0,
        max_attempts=spec.max_attempts or _jobs_cfg().max_attempts[spec.kind],
        scheduled_for=scheduled_for,
        created_at=now,
    )
    job_id, created = require_jobs_backend().insert_job(new_job, sched_check=sched_check)
    emit = _log.info if created else _log.debug
    emit(
        "jobs.job.enqueued",
        job_id=job_id,
        kind=spec.kind,
        priority=priority,
        created=created,
        scheduled_for=clock.format_utc(scheduled_for),
    )
    if created:
        record_counter("herness_jobs_enqueued_total", component="jobs", labels={"kind": spec.kind})
    return job_id, created


def enqueue(  # noqa: PLR0913 - design 08 §3.4 signature (R-41)
    kind: JobKind,
    payload: dict[str, JsonValue],
    gpu_class: GpuClass,
    priority: int | None = None,
    scheduled_for: datetime | None = None,
    *,
    max_attempts: int | None = None,
    idem_key: str | None = None,
) -> str:
    """Build a `JobSpec` and `submit` it (U08-47); returns the job id (the existing one when
    deduped). An invalid field raises SchemaViolation naming the field, not its value."""
    try:
        spec = JobSpec(
            kind=kind,
            payload=payload,
            gpu_class=gpu_class,
            priority=priority,
            max_attempts=max_attempts,
            scheduled_for=scheduled_for,
            idem_key=idem_key,
        )
    except ValidationError as exc:
        loc = exc.errors()[0]["loc"]
        field = str(loc[0]) if loc else "spec"
        msg = f"invalid job spec field: {field}"
        raise SchemaViolation(msg) from None
    return submit(spec)[0]


def _slot(owner: object) -> ClaimSlot:
    """The claim slot of an owner `<host>:<pid>:<gpu|cpuN|cli>` (U08-48, R-43)."""
    match = _OWNER_RE.fullmatch(owner) if isinstance(owner, str) else None
    if match is None:
        msg = "invalid claim owner; expected <host>:<pid>:<gpu|cpuN|cli>"
        raise ConfigError(msg)
    suffix = match.group(1)
    return "cpu" if suffix.startswith("cpu") else "gpu" if suffix == "gpu" else "cli"


def _classes(allowed: object) -> list[GpuClass]:
    if isinstance(allowed, str) or not isinstance(allowed, Sequence) or not allowed:
        msg = "allowed_classes must be a non-empty sequence of GPU classes"
        raise ConfigError(msg)
    if not set(allowed) <= _CLASSES:
        msg = "allowed_classes has an unknown GPU class"
        raise ConfigError(msg)
    return list(allowed)


def claim(
    *, owner: str, allowed_classes: Sequence[GpuClass], job_id: str | None = None
) -> JobRow | None:
    """Atomically claim the best eligible job for ``owner`` (U08-48); None when none is
    claimable. Invalid owner, classes or job id → ConfigError."""
    return _claim(
        owner=owner,
        allowed_classes=allowed_classes,
        job_id=job_id,
        min_priority=None,
        priority_exempt_kinds=(),
    )


def _claim(
    *,
    owner: str,
    allowed_classes: Sequence[GpuClass],
    job_id: str | None,
    min_priority: int | None,
    priority_exempt_kinds: Sequence[JobKind],
) -> JobRow | None:
    """`claim` with the supervisor's chat-window filter (U08-48)."""
    slot = _slot(owner)
    classes = _classes(allowed_classes)
    if job_id is not None and not is_valid_id(IdKind.JOB, job_id):
        msg = "invalid job_id; expected job_<ulid>"
        raise ConfigError(msg)
    cfg = _jobs_cfg()
    now = clock.now()
    row = require_jobs_backend().claim_job(
        owner=owner,
        now=now,
        lease_until=now + timedelta(seconds=cfg.lease_s),
        allowed_classes=classes,
        exclusive_kinds=cfg.exclusive_kinds,
        job_id=job_id,
        min_priority=min_priority,
        priority_exempt_kinds=priority_exempt_kinds,
        slot=slot,
        gpu_slot_kinds=sorted(GPU_SLOT_KINDS),
    )
    if row is None:
        return None
    fault_point("job.after_claim", kind=row.kind)
    due = row.scheduled_for or now
    wait_s = max(0.0, (now - due).total_seconds())
    _log.info(
        "jobs.job.claimed",
        job_id=row.job_id,
        kind=row.kind,
        attempt=row.attempts,
        owner=owner,
        wait_s=round(wait_s, 3),
    )
    labels = {"kind": row.kind}
    record_histogram("herness_jobs_queue_wait_seconds", wait_s, component="jobs", labels=labels)
    return row


def cancel(job_id: str) -> CancelResult:
    """Cancel a queued job, or request cancel of a running one (U08-51; TH08-11)."""
    result = require_jobs_backend().cancel_job(job_id, clock.now())
    _log.info("jobs.job.cancel_requested", job_id=job_id[:_ID_CHARS], result=result)
    return result


def retry(job_id: str) -> None:
    """Requeue a failed job with attempts 0 (U08-52; TH08-11).

    Raises JobStateError when the job is unknown, not failed, or its idem key is active.
    """
    result = require_jobs_backend().retry_job(job_id, clock.now())
    _log.info("jobs.job.retry_requested", job_id=job_id[:_ID_CHARS], result=result)
    if result != "queued":
        raise JobStateError(_RETRY_ERRORS[result], job_id=job_id[:_ID_CHARS])


def get(job_id: str) -> JobRow:
    """The job row (U08-53); unknown → JobStateError."""
    row = require_jobs_backend().get_job(job_id)
    if row is None:
        msg = "unknown job"
        raise JobStateError(msg, job_id=job_id[:_ID_CHARS])
    return row


def list_jobs(
    *, status: str | None = None, kind: str | None = None, limit: int = 50
) -> list[JobRow]:
    """Jobs newest first, optionally filtered (U08-53); invalid filter → ConfigError."""
    if status is not None and status not in _STATUSES:
        msg = "invalid job status filter"
        raise ConfigError(msg)
    if kind is not None and kind not in _KINDS:
        msg = "invalid job kind filter"
        raise ConfigError(msg)
    if type(limit) is not int or not 1 <= limit <= _LIST_MAX:
        msg = f"limit must be an integer from 1 to {_LIST_MAX}"
        raise ConfigError(msg)
    return require_jobs_backend().list_jobs(
        status=cast("JobStatus | None", status), kind=cast("JobKind | None", kind), limit=limit
    )


def worker_alive() -> bool:
    """True when a starting, running or draining worker heartbeat is newer than
    3 * `heartbeat_s` (U08-54, R-44)."""
    cutoff = clock.now() - timedelta(seconds=3 * _jobs_cfg().heartbeat_s)
    workers = require_jobs_backend().list_workers()
    return any(w.status in _LIVE_WORKER and w.heartbeat_at > cutoff for w in workers)
