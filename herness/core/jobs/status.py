"""Status snapshot, component health and resume enqueue (impl 08 U08-91 to U08-93; §3.17).

`status_snapshot` is the read-only data behind `herness status` and the dashboard (design 08
§5.14); `health` is the `herness doctor` verdict (ENG §4); `enqueue_resume` is the logic of
`herness resume RUN_ID` (design 08 §5.12). Every read goes through the bound ports with bounded
queries: the worker rows, `queue_stats`, at most `ROWS_MAX` running or queued maintenance jobs,
the not-closed `source_health` rows, `event_counts` and two `latest_event` reads. Instants are
aware UTC `datetime`s (spec 09 renders them). TH08-06: an enabled fault plan shows in
`faults_enabled` and degrades `health()`.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal, TypedDict

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import JobStateError
from herness.core.jobs._status_views import (
    BreakerView,
    QueueView,
    RekeyView,
    RunningView,
    ScheduleView,
    WindowView,
    WorkerView,
    next_job_view,
    window_view,
)
from herness.core.jobs.chat_policy import chat_next_live_at, chat_policy
from herness.core.jobs.gpu import ALIVE_HEARTBEATS, ALIVE_STATUSES, gpu_state
from herness.core.jobs.ports import require_jobs_backend
from herness.core.jobs.queue import MANUAL_PRIORITY, submit
from herness.core.jobs.scheduler import collect_schedules
from herness.core.jobs.windows import window_at
from herness.core.resilience._state import process_state, require_ops_backend
from herness.core.resilience.breaker import _family, probe_due
from herness.core.types import JobSpec

if TYPE_CHECKING:
    from collections.abc import Sequence

    from herness.core.jobs.ports import JobRow, JobsBackend, WorkerRow
    from herness.core.types import ChatMode

__all__ = [
    "ComponentHealth",
    "ResumeResult",
    "StatusSnapshot",
    "enqueue_resume",
    "health",
    "status_snapshot",
]

type GpuAvailability = Literal["no_worker", "unavailable", "available"]

DAY: Final = timedelta(hours=24)
ROWS_MAX: Final = 200  # bound of the running-job and queued-maintenance reads
COUNTER_KINDS: Final = (
    "retry", "fallback", "repair", "guard_stop", "breaker_open", "lease_expired",
    "gpu_swap", "gpu_swap_failed", "job_done", "job_failed", "job_yield",
)  # fmt: skip
FINISHED_RUN: Final = frozenset({"done", "partial", "failed", "canceled"})
NO_WORKER: Final = "no worker heartbeat"
_ID_CHARS: Final = 64


class StatusSnapshot(TypedDict):
    """U08-91: the keys of the §3.17 table (design 08 §5.14)."""

    workers: list[WorkerView]
    gpu: GpuAvailability
    window: WindowView
    next_window: WindowView
    chat_mode: ChatMode
    chat_next_live_at: datetime | None
    running: list[RunningView]
    planned_rekey: RekeyView | None
    queue: QueueView
    failed_24h: int
    dead_letters: int
    breakers: list[BreakerView]
    counters_24h: dict[str, int]
    schedules: list[ScheduleView]
    faults_enabled: bool


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """U08-92: the jobs component verdict for `herness doctor`."""

    status: Literal["ok", "degraded", "down"]
    reason: str


@dataclass(frozen=True, slots=True)
class ResumeResult:
    """U08-93: the resume job (None when the run is finished and not forced)."""

    job_id: str | None
    created: bool
    run_status: str


def _alive(row: WorkerRow, now: datetime) -> bool:
    """The U08-54 liveness rule at `now` (R-44)."""
    heartbeat_s = get_config().resilience.resilience.jobs.heartbeat_s
    fresh = row.heartbeat_at > now - timedelta(seconds=ALIVE_HEARTBEATS * heartbeat_s)
    return row.status in ALIVE_STATUSES and fresh


def _gpu_worker(alive: Sequence[WorkerRow]) -> WorkerRow | None:
    return max((w for w in alive if w.gpu_slot == 1), key=lambda w: w.heartbeat_at, default=None)


def _swap_failed_last() -> bool:
    """The latest `gpu_swap_failed` event is newer than the latest `gpu_swap`."""
    ops = require_ops_backend()
    failed = ops.latest_event("gpu_swap_failed")
    if failed is None:
        return False
    swapped = ops.latest_event("gpu_swap")
    return swapped is None or failed.ts > swapped.ts


def _gpu_status(alive: Sequence[WorkerRow]) -> GpuAvailability:
    worker = _gpu_worker(alive)
    if worker is None:
        return "no_worker"
    if worker.gpu_class_loaded == "none" and _swap_failed_last():
        return "unavailable"
    return "available"


def _faults(alive: Sequence[WorkerRow]) -> bool:
    return process_state().faults_enabled or any(w.faults_enabled for w in alive)


def _services(row: WorkerRow, local: WorkerRow | None) -> dict[str, bool]:
    """Service health of the loaded class, only for the local host's alive GPU worker."""
    if local is None or row.worker_id != local.worker_id:
        return {}
    classes = get_config().resilience.resilience.gpu.classes
    names = [n for c, spec in classes.items() if c == row.gpu_class_loaded for n in spec.services]
    reader = gpu_state()
    return {name: reader.service_healthy(name) for name in names}


def _workers(
    rows: Sequence[WorkerRow], alive: Sequence[WorkerRow], now: datetime
) -> list[WorkerView]:
    host = socket.gethostname().lower()
    local = _gpu_worker([w for w in alive if w.host == host])
    return [
        WorkerView(
            worker_id=w.worker_id,
            host=w.host,
            pid=w.pid,
            status=w.status,
            heartbeat_age_s=max((now - w.heartbeat_at).total_seconds(), 0.0),
            gpu_slot=w.gpu_slot,
            cpu_slots=w.cpu_slots,
            gpu_class_loaded=w.gpu_class_loaded,
            requested_class=w.requested_class,
            services=_services(w, local),
            faults_enabled=w.faults_enabled,
        )
        for w in rows
    ]


def _notes(workers: Sequence[WorkerRow]) -> dict[str, JsonValue]:
    """`job_id` → the last heartbeat note in the workers' `current_jobs`."""
    return {str(j.get("job_id")): j.get("note") for w in workers for j in w.current_jobs}


def _running(jobs: JobsBackend, workers: Sequence[WorkerRow], now: datetime) -> list[RunningView]:
    notes = _notes(workers)
    views: list[RunningView] = []
    for row in jobs.list_jobs(status="running", kind=None, limit=ROWS_MAX):
        expires = row.lease_expires_at
        note = notes.get(row.job_id)
        views.append(
            RunningView(
                job_id=row.job_id,
                kind=row.kind,
                attempt=row.attempts,
                max_attempts=row.max_attempts,
                lease_left_s=None if expires is None else (expires - now).total_seconds(),
                slot=None if row.lease_owner is None else row.lease_owner.rsplit(":", 1)[-1],
                heartbeat_note=note if isinstance(note, str) else None,
            )
        )
    return views


def _planned_rekey(jobs: JobsBackend) -> RekeyView | None:
    """The earliest queued `maintenance` job with `payload.action == "rekey"`."""
    rows = jobs.list_jobs(status="queued", kind="maintenance", limit=ROWS_MAX)
    rekeys = [r for r in rows if r.payload.get("action") == "rekey"]
    first = min(rekeys, key=_due, default=None)
    return (
        None if first is None else RekeyView(job_id=first.job_id, scheduled_for=first.scheduled_for)
    )


def _due(row: JobRow) -> datetime:
    return row.scheduled_for or datetime.max.replace(tzinfo=UTC)


def _breakers() -> list[BreakerView]:
    rows = require_ops_backend().health_list(["open", "half_open"])
    return [
        BreakerView(
            key=r.source,
            state=r.state,
            since=r.opened_at,
            trips=r.trips,
            probe_due=probe_due(r, _family(r.source)) if r.state == "open" else None,
        )
        for r in rows
    ]


def _schedules(now: datetime) -> list[ScheduleView]:
    tz = clock.zone(get_config().weights.business_timezone)
    return [
        ScheduleView(name=e.name, next_fire_at=e.cron.next_after(now, tz))
        for e in collect_schedules()
    ]


def status_snapshot(now: datetime | None = None) -> StatusSnapshot:
    """The §3.17 snapshot at `now` (default: the clock); read-only (U08-91)."""
    now = clock.now() if now is None else now
    jobs = require_jobs_backend()
    workers = jobs.list_workers()
    alive = [w for w in workers if _alive(w, now)]
    stats = jobs.queue_stats(now, now - DAY)
    active = window_at(now)
    head = stats.next_job
    counts = require_ops_backend().event_counts(now - DAY, COUNTER_KINDS)
    return StatusSnapshot(
        workers=_workers(workers, alive, now),
        gpu=_gpu_status(alive),
        window=window_view(active),
        next_window=window_view(window_at(active.end_at)),
        chat_mode=chat_policy(now),
        chat_next_live_at=chat_next_live_at(now),
        running=_running(jobs, workers, now),
        planned_rekey=_planned_rekey(jobs),
        queue=QueueView(
            queued=stats.queued,
            next_job=None if head is None else next_job_view(head),
        ),
        failed_24h=stats.failed_24h,
        dead_letters=stats.dead_letters,
        breakers=_breakers(),
        counters_24h={kind: counts.get(kind, 0) for kind in COUNTER_KINDS},
        schedules=_schedules(now),
        faults_enabled=_faults(alive),
    )


def health(now: datetime | None = None) -> ComponentHealth:
    """`down` without a live worker, `degraded` with the listed conditions, else `ok` (U08-92)."""
    now = clock.now() if now is None else now
    jobs = require_jobs_backend()
    alive = [w for w in jobs.list_workers() if _alive(w, now)]
    if not alive:
        return ComponentHealth("down", NO_WORKER)
    reasons: list[str] = []
    if require_ops_backend().health_list(["open"]):
        reasons.append("breaker open")
    if jobs.queue_stats(now, now - DAY).failed_24h > 0:
        reasons.append("dead letters in 24 h")
    if _faults(alive):
        reasons.append("faults enabled")
    if _gpu_status(alive) == "unavailable":
        reasons.append("gpu unavailable")
    if reasons:
        return ComponentHealth("degraded", "; ".join(reasons))
    return ComponentHealth("ok", "ok")


def enqueue_resume(run_id: str, *, force: bool = False, retry_dead: bool = False) -> ResumeResult:
    """Enqueue the `review` job that resumes run `run_id` (U08-93; design 08 §5.12).

    Raises JobStateError for an unknown run or a chat run.
    """
    found = require_jobs_backend().run_row(run_id)
    if found is None:
        msg = "unknown run"
        raise JobStateError(msg, run_id=run_id[:_ID_CHARS])
    kind, status = found
    if kind == "chat":
        msg = "chat runs resume through their chat job"
        raise JobStateError(msg, run_id=run_id[:_ID_CHARS])
    if status in FINISHED_RUN and not force:
        return ResumeResult(None, False, status)
    payload: dict[str, JsonValue] = {"run_id": run_id, "resume": True}
    if force:
        payload["force"] = True
    if retry_dead:
        payload["retry_dead"] = True
    spec = JobSpec(
        kind="review",
        payload=payload,
        gpu_class="reasoning",
        priority=MANUAL_PRIORITY,
        idem_key=f"resume:{run_id}",
    )
    job_id, created = submit(spec)
    return ResumeResult(job_id, created, status)
