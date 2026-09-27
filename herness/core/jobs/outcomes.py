"""Failure policy and outcome application (impl 08 U08-49, U08-50; design 08 §5.2, §5.7).

Every completion statement is guarded by `lease_owner` in the bound `JobsBackend` (TH08-08):
a statement that changes 0 rows means the lease was lost, which is logged at WARNING and
changes nothing else (no event, metric or chain advance).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random
from typing import Final, Literal, NamedTuple

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import (
    CircuitOpen,
    HernessError,
    JobStateError,
    RateLimited,
    RetryableError,
)
from herness.core.jobs.ports import JobRow, StopReason, require_jobs_backend
from herness.core.jobs.scheduler import advance_chain
from herness.core.jobs.windows import next_window_allowing
from herness.core.logging import get_logger
from herness.core.redact import redact_text
from herness.core.resilience._state import process_state
from herness.core.resilience.events import record_event
from herness.core.resilience.faults import fault_point
from herness.core.resilience.metrics import record_counter, record_histogram
from herness.core.resilience.policies import job_backoff_delay
from herness.core.types import JobKind, JobOutcome

type FinishResult = Literal["done", "queued", "failed", "canceled", "lease_lost"]

# Kinds an open circuit finishes `done` partial; the next scheduled run retries (R-39).
SKIP_ON_OPEN_CIRCUIT: Final[frozenset[JobKind]] = frozenset({"sync", "reconcile"})
MESSAGE_MAX_CHARS: Final = 2048
# Stored in `last_error.message` when redaction fails: the raw text is never kept (fail closed).
MESSAGE_WITHHELD: Final = "[message withheld: redaction failed]"
_ID_CHARS: Final = 64  # ids and owners are cut to this length in log lines (T08-12)
_FINISHED: Final = "herness_jobs_finished_total"
_RUN_SECONDS: Final = "herness_jobs_run_seconds"

_log: Final = get_logger("jobs")


@dataclass(frozen=True, slots=True)
class FailureAction:
    """What `finish_job` does with a job whose handler raised (U08-49)."""

    action: Literal["requeue", "failed", "done"]
    scheduled_for: datetime | None = None
    result: dict[str, JsonValue] | None = None


_FAILED: Final = FailureAction("failed")


def decide_failure(err: HernessError, row: JobRow, now: datetime, *, rng: Random) -> FailureAction:
    """The "Job" column of the design 08 §5.2 error table (U08-49); pure given config.

    ``row`` is the claimed row, so `row.attempts` already counts this attempt.
    """
    if isinstance(err, CircuitOpen):
        if row.kind in SKIP_ON_OPEN_CIRCUIT:
            result: dict[str, JsonValue] = {
                "partial": True,
                "outcome": "skipped_open_circuit",
                "skipped_open_circuit": [err.key],
            }
            return FailureAction("done", None, result)
        at = max(err.retry_at, now)
    elif isinstance(err, RateLimited) and err.retry_after is not None:
        at = now + timedelta(seconds=err.retry_after)
    elif isinstance(err, RetryableError):
        if row.attempts >= row.max_attempts:
            return _FAILED
        at = now + timedelta(seconds=job_backoff_delay(row.attempts, rng=rng))
    else:
        return _FAILED  # every RecoverableError and FatalError, BudgetExceeded included
    return FailureAction("requeue", at) if row.attempts < row.max_attempts else _FAILED


class _Run(NamedTuple):
    """One attempt being finished: the claimed row, its owner and the finish time."""

    row: JobRow
    owner: str
    now: datetime
    duration_s: float


def finish_job(
    row: JobRow,
    owner: str,
    outcome: JobOutcome | HernessError,
    *,
    attempt_started_at: datetime,
    stop_reason: StopReason | None,
) -> FinishResult:
    """Apply a job's outcome with the lease-owner guard (U08-50; TH08-08).

    A job missing on re-read is treated like a lost lease: nothing is written or recorded.
    """
    fault_point("job.before_complete", kind=row.kind)
    backend = require_jobs_backend()
    now = clock.now()
    current = backend.get_job(row.job_id)
    if current is None:
        return _lease_lost(row, owner)
    if current.status == "canceled":
        if not backend.finalize_canceled(row.job_id, owner, now):
            return _lease_lost(row, owner)
        _log.info("jobs.job.canceled", job_id=row.job_id[:_ID_CHARS], owner=owner[:_ID_CHARS])
        return "canceled"
    duration = max((now - attempt_started_at).total_seconds(), 0.0)
    run = _Run(row, owner, now, duration)
    if isinstance(outcome, HernessError):
        return _apply_error(run, outcome)
    if outcome.status == "done":
        return _apply_done(run, outcome.result)
    return _apply_yield(run, stop_reason)


def _lease_lost(row: JobRow, owner: str) -> FinishResult:
    _log.warning("jobs.job.lease_lost", job_id=row.job_id[:_ID_CHARS], owner=owner[:_ID_CHARS])
    return "lease_lost"


def _record_finish(run: _Run, status: str) -> None:
    """`herness_jobs_finished_total{kind, status}` and `herness_jobs_run_seconds{kind}`."""
    kind = run.row.kind
    record_counter(_FINISHED, component="jobs", labels={"kind": kind, "status": status})
    record_histogram(_RUN_SECONDS, run.duration_s, component="jobs", labels={"kind": kind})


def _event(kind: str, run: _Run, detail: Mapping[str, object]) -> None:
    row = run.row
    record_event(kind, component="jobs", target=row.kind, job_id=row.job_id, detail=detail)


def _apply_done(run: _Run, result: Mapping[str, JsonValue]) -> FinishResult:
    """U08-50 step 3: store the result without its `state` key, then advance the chain."""
    row = run.row
    stored = {key: value for key, value in result.items() if key != "state"}
    if not require_jobs_backend().finish_done(row.job_id, run.owner, stored, run.now):
        return _lease_lost(row, run.owner)
    detail = {
        "kind": row.kind,
        "attempt": row.attempts,
        "duration_s": run.duration_s,
        "partial": result.get("partial", False),
    }
    _event("job_done", run, detail)
    _record_finish(run, "done")
    _advance(run)
    return "done"


def _apply_yield(run: _Run, stop_reason: StopReason | None) -> FinishResult:
    """U08-50 step 4: back to `queued` with no attempt charge; preempt waits for a window."""
    row = run.row
    resume_at = run.now
    if stop_reason == "preempt":
        resume_at = next_window_allowing(row.gpu_class, run.now)
    if not require_jobs_backend().finish_yield(row.job_id, run.owner, resume_at, run.now):
        return _lease_lost(row, run.owner)
    detail = {"kind": row.kind, "attempt": row.attempts, "stop_reason": stop_reason}
    _event("job_yield", run, detail)
    _record_finish(run, "yield")
    return "queued"


def _last_error(err: HernessError, run: _Run) -> dict[str, JsonValue]:
    """`last_error` (U08-50 step 5): the message is redacted, then cut to 2048 chars."""
    clean = redact_text(str(err))
    message = MESSAGE_WITHHELD if clean is None else clean[:MESSAGE_MAX_CHARS]
    return {
        "class": type(err).__name__,
        "message": message,
        "at": clock.format_utc(run.now),
        "attempt": run.row.attempts,
    }


def _apply_error(run: _Run, err: HernessError) -> FinishResult:
    """U08-50 step 5: the U08-49 decision, then requeue, `done` partial or `failed`."""
    decision = decide_failure(err, run.row, run.now, rng=process_state().rng)
    if decision.action == "done":
        return _apply_done(run, decision.result or {})
    if decision.action == "requeue":
        if decision.scheduled_for is None:  # U08-49 always sets it; never fail silently
            msg = "requeue decision without scheduled_for"
            raise JobStateError(msg, job_id=run.row.job_id[:_ID_CHARS])
        return _apply_requeue(run, err, decision.scheduled_for)
    row, error_type = run.row, type(err).__name__
    backend = require_jobs_backend()
    if not backend.finish_failed(row.job_id, run.owner, _last_error(err, run), run.now):
        return _lease_lost(row, run.owner)
    _event("job_failed", run, {"kind": row.kind, "attempt": row.attempts, "error_type": error_type})
    _record_finish(run, "failed")
    _advance(run)  # failure mode: `chain_broken` for a scheduled job (U08-73)
    return "failed"


def _apply_requeue(run: _Run, err: HernessError, at: datetime) -> FinishResult:
    row, error_type = run.row, type(err).__name__
    if not require_jobs_backend().finish_requeue(row.job_id, run.owner, at, _last_error(err, run)):
        return _lease_lost(row, run.owner)
    detail = {
        "target": "job",
        "policy": "job_backoff",
        "attempt": row.attempts,
        "error_type": error_type,
        "wait_s": max((at - run.now).total_seconds(), 0.0),
    }
    _event("retry", run, detail)
    _log.warning(
        "jobs.job.rescheduled",
        job_id=row.job_id[:_ID_CHARS],
        error_type=error_type,
        scheduled_for=clock.format_utc(at),
    )
    _record_finish(run, "requeued")
    return "queued"


def _advance(run: _Run) -> None:
    """`advance_chain` on the row re-read after the completion write (U08-73).

    A chain error is logged as ERROR `jobs.schedule.error` and not raised: the job is already
    finished and the scheduler's chain repair re-runs the advance (F08-11).
    """
    schedule = run.row.payload.get("schedule")
    if schedule is None:
        return
    after = require_jobs_backend().get_job(run.row.job_id)
    if after is None:
        return
    try:
        advance_chain(after)
    except HernessError as exc:
        name = schedule[:_ID_CHARS] if isinstance(schedule, str) else None
        _log.error("jobs.schedule.error", schedule=name, error_type=type(exc).__name__)
