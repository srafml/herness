"""Privacy deletion: the seven steps of design 10 §5.5 plus memory step 3b (impl 10 U10-99;
F10-13; R-54, R-57, R-77; TH10-42, TH10-50).

Step results go on the ``deletion_request`` row (U10-105); a ``done`` step is skipped on retry.
A failing step records ``failed`` with the error class only, leaves the request ``running``
and re-raises (``StoreBusy``, ``ModelUnavailable`` and ``FatalError`` as they are, anything
else as ``FatalError``); a failure to record it is logged by class and the step error still
propagates. Messages and logs never carry the record ID or record text. Ops-store hygiene
(``secure_delete``, WAL truncation, ``VACUUM`` after a mid-step reopen) and the step bodies
live in ``_privacy_steps``. Step 5 waits for the rebuild through continuation jobs (payload
adds ``request_id``, ``build_job_id``, ``wait``, ``rebuilds``, ``rebuild_at``) and is done
only when no build file older than the step-5 time remains besides the new ``CURRENT``.
Step 7 (spec note SN-T10-29-a) repeats steps 3, 3b and 4 unconditionally after lake pass 2:
memory, evidence, findings and traces can cite the record again during the rebuild wait.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Callable, Mapping
from typing import Final, Literal

from pydantic import JsonValue

from herness.admin import _privacy_steps as steps
from herness.admin.commands_data import _validate as validate_request
from herness.core import time as clock
from herness.core.audit import audit
from herness.core.config import HernessConfig
from herness.core.errors import ConfigError, FatalError, ModelUnavailable, StoreBusy
from herness.core.ids import IdKind, is_valid_id
from herness.core.jobs import enqueue
from herness.core.jobs import get as get_job
from herness.core.jobs.ports import JobContext
from herness.core.logging import get_logger
from herness.core.types import JobOutcome
from herness.model.promote import cleanup_builds
from herness.store.layout import data_layout
from herness.store.ops.privacy import (
    DeletionRequest,
    create_deletion_request,
    get_deletion_request,
    open_deletion_request,
    record_deletion_step,
    set_deletion_status,
)
from herness.store.warehouse import read_current

__all__ = ["run_privacy_delete"]

type _Step = Literal["1", "2", "3", "3b", "4", "5", "6", "7"]
type _Counts = dict[str, int]

_WAIT: Final = datetime.timedelta(minutes=30)
_MAX_REBUILDS: Final = 3
_PRIORITY: Final = 80  # the U10-73 priority of the request's own job
_BUILD_PRIORITY: Final = 60
_NIGHTLY: Final[dict[str, JsonValue]] = {"stages": ["build", "enrich", "score", "dq", "promote"]}
_log = get_logger("admin.privacy")


@dataclasses.dataclass
class _Run:
    """One job's view of the request: payload, clock and the steps already ``done``."""

    cfg: HernessConfig
    payload: Mapping[str, JsonValue]
    now: datetime.datetime
    memory_purge: Callable[[str], int]
    request: DeletionRequest

    @property
    def record_id(self) -> str:
        return self.request.record_id

    def done(self, step: _Step) -> bool:
        return self.request.steps.get(step, {}).get("status") == "done"

    def vacuum_owed(self, step: _Step) -> bool:
        """A failed earlier attempt of ``step`` deleted on a connection without secure_delete."""
        prior = self.request.steps.get(step, {})
        return prior.get("status") == "failed" and prior.get("counts", {}).get("vacuum_owed") == 1


def _escalate(run_id: str, step: str, exc: Exception) -> FatalError | None:
    """Log the failure; None when ``exc`` is re-raised as is, else the FatalError to raise."""
    error = type(exc).__name__
    _log.warning("admin.privacy.step_failed", request_id=run_id, step=step, error_type=error)
    if isinstance(exc, StoreBusy | ModelUnavailable | FatalError):
        return None
    msg = f"privacy deletion step {step} failed: {error}"
    return FatalError(msg)


def _record_failure(run: _Run, step: _Step, counts: _Counts, exc: Exception) -> None:
    """Record ``failed``; a failure to record is logged by class, never raised over ``exc``."""
    rid = run.request.request_id
    try:
        record_deletion_step(
            rid, step, status="failed", at=run.now, counts=counts, error=type(exc).__name__
        )
    except Exception as secondary:  # noqa: BLE001 - the step error is the one that propagates
        error = type(secondary).__name__
        _log.error("admin.privacy.step_record_failed", request_id=rid, step=step, error_type=error)


def _step(
    run: _Run, step: _Step, work: Callable[[_Counts], JobOutcome | None]
) -> JobOutcome | None:
    """Run ``work`` unless ``step`` is done, with ``secure_delete`` on; a connection reopened
    mid-step is followed by ``VACUUM`` (owed to the retry when the step fails). Record ``done``
    (unless it returned an outcome) or ``failed`` with the counts so far, then re-raise."""
    if run.done(step):
        return None
    counts: _Counts = {}
    conn = None
    try:
        if run.vacuum_owed(step):
            steps.vacuum()
            counts["vacuumed"] = 1
        conn = steps.secure()
        outcome = work(counts)
        if steps.reopened(conn):
            steps.vacuum()
            counts["vacuumed"] = 1
    except Exception as exc:
        if run.vacuum_owed(step) if conn is None else steps.reopened(conn):
            counts["vacuum_owed"] = 1
        _record_failure(run, step, counts, exc)
        fatal = _escalate(run.request.request_id, step, exc)
        if fatal is None:
            raise
        raise fatal from exc
    if outcome is None:
        record_deletion_step(run.request.request_id, step, status="done", at=run.now, counts=counts)
    return outcome


def _text(payload: Mapping[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        msg = f"invalid {key}"
        raise ConfigError(msg)
    return value


def _open(payload: Mapping[str, JsonValue], now: datetime.datetime) -> DeletionRequest:
    """Re-validate the payload (U10-73) and find, or create, the request (step 1 lookup)."""
    record_id, reason_ref = _text(payload, "record_id"), _text(payload, "reason_ref")
    requested_by = _text(payload, "requested_by")
    validate_request([record_id], reason_ref)
    if "request_id" in payload:
        request_id = _text(payload, "request_id")
        if not is_valid_id(IdKind.REQUEST, request_id):
            msg = "invalid request_id"
            raise ConfigError(msg)
        request = get_deletion_request(request_id)
        if request.record_id != record_id:
            msg = "deletion request does not match the record"
            raise ConfigError(msg)
        if request.status == "failed":
            msg = "privacy deletion request failed"
            raise FatalError(msg, request_id=request_id)
        return request
    return open_deletion_request(record_id) or create_deletion_request(
        record_id=record_id, requested_by=requested_by, reason_ref=reason_ref, now=now
    )


def _start(run: _Run) -> None:
    if run.request.status == "pending":
        set_deletion_status(run.request.request_id, "running")


def _nightly_payload(cfg: HernessConfig) -> dict[str, JsonValue]:
    """The ``job.payload`` of the configured ``nightly`` build (impl 08), else its default."""
    for job in cfg.resilience.schedule.jobs:
        if job.name == "nightly" and job.job.kind == "build_pipeline":
            return dict(job.job.payload)
    return dict(_NIGHTLY)


def _int(payload: Mapping[str, JsonValue], key: str, high: int) -> int:
    value = payload.get(key)
    if type(value) is not int or not 0 <= value <= high:
        msg = f"invalid {key}"
        raise ConfigError(msg)
    return value


def _continue(
    run: _Run, build_job: str, *, wait: int, rebuilds: int, since: datetime.datetime
) -> JobOutcome:
    """Enqueue the next continuation in 30 minutes and finish this job as ``waiting_for``."""
    rid = run.request.request_id
    keys = ("action", "record_id", "reason_ref", "requested_by")
    payload: dict[str, JsonValue] = {key: run.payload[key] for key in keys}
    payload |= {"request_id": rid, "build_job_id": build_job, "wait": wait}
    payload |= {"rebuilds": rebuilds, "rebuild_at": clock.format_utc(since)}
    at, key = run.now + _WAIT, f"privacy_delete:{rid}:wait:{wait}"
    enqueue("maintenance", payload, "none", priority=_PRIORITY, scheduled_for=at, idem_key=key)
    return JobOutcome(status="done", result={"waiting_for": build_job})


def _enqueue_build(run: _Run) -> str:
    """The rebuild (``build_pipeline`` starts with no GPU class, R-43)."""
    payload, key = _nightly_payload(run.cfg), "privacy_rebuild:" + run.request.request_id
    return enqueue("build_pipeline", payload, "none", priority=_BUILD_PRIORITY, idem_key=key)


def _promoted_since(run: _Run, since: datetime.datetime) -> bool:
    build = read_current(layout=data_layout(cfg=run.cfg))
    return build is not None and steps.built_at(build) >= since.replace(microsecond=0)


def _cleanup(run: _Run, since: datetime.datetime, counts: _Counts) -> None:
    """``cleanup_builds`` post; any build file older than ``since`` left besides ``CURRENT``
    (deferred, pinned by a run or job, the kept failed build) fails the step, retryably."""
    layout = data_layout(cfg=run.cfg)
    report = cleanup_builds(
        mode="post", keep_last=1, protect=frozenset(), layout=layout, now=run.now
    )
    counts.update(builds_deleted=len(report.deleted), builds_retired=len(report.retired))
    counts["builds_left"] = steps.older_builds(layout, since)
    if report.deferred or counts["builds_left"]:
        msg = "older warehouse builds remain"
        raise StoreBusy(msg, request_id=run.request.request_id, builds=counts["builds_left"])


def _rebuild(run: _Run, counts: _Counts) -> JobOutcome | None:
    payload = run.payload
    if "build_job_id" not in payload:
        return _continue(run, _enqueue_build(run), wait=1, rebuilds=0, since=run.now)
    build_job = _text(payload, "build_job_id")
    if not is_valid_id(IdKind.JOB, build_job):
        msg = "invalid build_job_id"
        raise ConfigError(msg)
    wait, rebuilds = _int(payload, "wait", 10_000), _int(payload, "rebuilds", _MAX_REBUILDS)
    since = clock.parse_utc(_text(payload, "rebuild_at"))
    status = get_job(build_job).status
    if status in {"queued", "running"}:
        return _continue(run, build_job, wait=wait + 1, rebuilds=rebuilds, since=since)
    if status == "done" and _promoted_since(run, since):
        _cleanup(run, since, counts)
        return None
    if rebuilds >= _MAX_REBUILDS:
        set_deletion_status(run.request.request_id, "failed")
        msg = "privacy rebuild failed"
        raise FatalError(msg, request_id=run.request.request_id)
    nxt = _enqueue_build(run)  # keep the original step-5 time: a failed rebuild is no leftover
    return _continue(run, nxt, wait=wait + 1, rebuilds=rebuilds + 1, since=since)


def _finish(run: _Run) -> None:
    request = get_deletion_request(run.request.request_id)
    if request.status == "running":
        set_deletion_status(request.request_id, "done", completed_at=run.now)
    audit("admin_action", "system", action="privacy_delete", target=run.record_id)


def _second_pass(run: _Run, counts: _Counts) -> None:
    """Lake pass 2, then steps 3, 3b and 4 again, whatever pass 2 removed (SN-T10-29-a)."""
    rid = run.record_id
    steps.lake(rid, counts)
    steps.caches(rid, counts)
    steps.memory(run.memory_purge, rid, counts)
    steps.scrub(run.cfg, rid, counts)


def run_privacy_delete(
    cfg: HernessConfig,
    ctx: JobContext,
    *,
    now: datetime.datetime,
    memory_purge: Callable[[str], int],
) -> JobOutcome:
    """Privacy deletion of ``ctx.job.payload["record_id"]`` (U10-99, R-42, R-54).

    Returns ``JobOutcome(done, {"waiting_for": <build job>})`` while the rebuild runs, else
    ``JobOutcome(done, {"request_id": ...})`` after step 7. Raises ConfigError for a bad
    payload; step errors as the module docstring says."""
    request = _open(ctx.job.payload, now)
    run = _Run(cfg, ctx.job.payload, now, memory_purge, request)
    rid = request.record_id
    _step(run, "1", lambda _c: _start(run))
    _step(run, "2", lambda c: steps.lake(rid, c))
    _step(run, "3", lambda c: steps.caches(rid, c))
    _step(run, "3b", lambda c: steps.memory(memory_purge, rid, c))
    _step(run, "4", lambda c: steps.scrub(cfg, rid, c))
    waiting = _step(run, "5", lambda c: _rebuild(run, c))
    if waiting is not None:
        return waiting
    _step(run, "6", lambda _c: _finish(run))
    _step(run, "7", lambda c: _second_pass(run, c))
    return JobOutcome(status="done", result={"request_id": request.request_id})
