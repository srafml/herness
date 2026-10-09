"""Privacy deletion: the seven steps of design 10 §5.5 plus memory step 3b (impl 10 U10-99;
F10-13; R-54, R-57, R-77; TH10-42, TH10-50).

Step results go on the ``deletion_request`` row (U10-105); a ``done`` step is skipped on retry.
A failing step records ``failed`` with the error class only, leaves the request ``running``
and re-raises (``StoreBusy``, ``ModelUnavailable`` and ``FatalError`` as they are, anything
else as ``FatalError``). Messages and logs never carry the record ID or record text. Steps
run with ``secure_delete`` on and steps 4 and 7 checkpoint the WAL, so deleted ops-store bytes
survive neither in freed pages nor in old WAL frames (TH10-42). Step 5 waits for the rebuild
through continuation jobs (payload adds ``request_id``, ``build_job_id``, ``wait``,
``rebuilds``, ``rebuild_at``).
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, Literal

from pydantic import JsonValue

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
from herness.enrich import purge_record
from herness.model.promote import cleanup_builds
from herness.store.lake_purge import purge_record_ids
from herness.store.layout import data_layout
from herness.store.ops.core import connection, run_write
from herness.store.ops.evidence import scrub_record_from_evidence
from herness.store.ops.findings import scrub_record_from_findings
from herness.store.ops.privacy import (
    DeletionRequest,
    create_deletion_request,
    get_deletion_request,
    open_deletion_request,
    record_deletion_step,
    set_deletion_status,
)
from herness.store.warehouse import read_current

__all__ = ["TRACE_CHUNK", "run_privacy_delete"]

type _Step = Literal["1", "2", "3", "3b", "4", "5", "6", "7"]
type _Counts = dict[str, int]

TRACE_CHUNK: Final = 1 << 20
_WAIT: Final = datetime.timedelta(minutes=30)
_MAX_REBUILDS: Final = 3
_PRIORITY: Final = 80  # the U10-73 priority of the request's own job
_BUILD_PRIORITY: Final = 60
_NIGHTLY: Final[dict[str, JsonValue]] = {"stages": ["build", "enrich", "score", "dq", "promote"]}
_BUILD_TIME: Final = "%Y%m%d-%H%M%S"
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


def _escalate(run_id: str, step: str, exc: Exception) -> FatalError | None:
    """Log the failure; None when ``exc`` is re-raised as is, else the FatalError to raise."""
    error = type(exc).__name__
    _log.warning("admin.privacy.step_failed", request_id=run_id, step=step, error_type=error)
    if isinstance(exc, StoreBusy | ModelUnavailable | FatalError):
        return None
    msg = f"privacy deletion step {step} failed: {error}"
    return FatalError(msg)


def _step(
    run: _Run, step: _Step, work: Callable[[_Counts], JobOutcome | None]
) -> JobOutcome | None:
    """Run ``work`` unless ``step`` is done; record ``done`` (unless it returned an outcome)
    or ``failed`` with the counts gathered so far and the error class, then re-raise."""
    if run.done(step):
        return None
    counts: _Counts = {}
    try:
        connection().execute("PRAGMA secure_delete = ON")  # again: run_write may reopen it
        outcome = work(counts)
    except Exception as exc:
        rid, err = run.request.request_id, type(exc).__name__
        record_deletion_step(rid, step, status="failed", at=run.now, counts=counts, error=err)
        fatal = _escalate(rid, step, exc)
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


def _lake(run: _Run, counts: _Counts) -> None:
    result = purge_record_ids([run.record_id])
    counts.update({k: int(v) for k, v in dataclasses.asdict(result).items()})


def _seal_ops() -> None:
    """Fold the WAL into the main file and truncate it (old frames hold deleted rows)."""
    busy = connection().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    if busy:
        msg = "ops store checkpoint busy"
        raise StoreBusy(msg)


def _caches(run: _Run, counts: _Counts) -> None:
    counts.update({k: int(v) for k, v in purge_record(run.record_id).items()})


def _memory(run: _Run, counts: _Counts) -> None:
    counts["memory_items"] = int(run.memory_purge(run.record_id))


def _holds(path: Path, needle: bytes) -> bool:
    """Whether the bytes of ``path`` contain ``needle`` (chunked; overlap spans chunk ends)."""
    tail = b""
    with path.open("rb") as fh:
        while chunk := fh.read(TRACE_CHUNK):
            if needle in tail + chunk:
                return True
            tail = chunk[-(len(needle) - 1) :] if len(needle) > 1 else b""
    return False


def _traces(cfg: HernessConfig, record_id: str) -> int:
    needle, deleted = record_id.encode("utf-8"), 0
    for path in sorted((Path(cfg.paths.data) / "traces").glob("*.jsonl")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if _holds(path, needle):
                path.unlink()
                deleted += 1
        except PermissionError as exc:  # a trace writer or scanner holds the file
            msg = "trace file busy"
            raise StoreBusy(msg) from exc
    return deleted


def _scrub(run: _Run, counts: _Counts) -> None:
    rid = run.record_id
    counts["evidence_rows"] = scrub_record_from_evidence(rid)
    counts["finding_rows"] = run_write(
        lambda conn: scrub_record_from_findings(rid, conn=conn), op="privacy_scrub_findings"
    )
    counts["trace_files"] = _traces(run.cfg, rid)
    _seal_ops()


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
    if build is None:
        return False
    built = datetime.datetime.strptime(build[:15], _BUILD_TIME).replace(tzinfo=datetime.UTC)
    return built >= since.replace(microsecond=0)


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
        report = cleanup_builds(
            mode="post", keep_last=1, protect=frozenset(), layout=data_layout(cfg=run.cfg),
            now=run.now,
        )  # fmt: skip
        counts.update(builds_deleted=len(report.deleted), builds_retired=len(report.retired))
        if report.deferred:
            msg = "build cleanup deferred"
            raise StoreBusy(msg, request_id=run.request.request_id)
        return None
    if rebuilds >= _MAX_REBUILDS:
        set_deletion_status(run.request.request_id, "failed")
        msg = "privacy rebuild failed"
        raise FatalError(msg, request_id=run.request.request_id)
    nxt = _enqueue_build(run)
    return _continue(run, nxt, wait=wait + 1, rebuilds=rebuilds + 1, since=run.now)


def _finish(run: _Run) -> None:
    request = get_deletion_request(run.request.request_id)
    if request.status == "running":
        set_deletion_status(request.request_id, "done", completed_at=run.now)
    audit("admin_action", "system", action="privacy_delete", target=run.record_id)


def _second_pass(run: _Run, counts: _Counts) -> None:
    """Lake pass 2; when it (or a failed earlier attempt) removed rows, repeat 3 and 3b."""
    earlier = run.request.steps.get("7", {}).get("counts", {}).get("rows_removed", 0)
    _lake(run, counts)
    counts["rows_removed"] += earlier if isinstance(earlier, int) else 0
    if counts["rows_removed"]:
        _caches(run, counts)
        _memory(run, counts)
    _seal_ops()


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
    _step(run, "1", lambda _c: _start(run))
    _step(run, "2", lambda c: _lake(run, c))
    _step(run, "3", lambda c: _caches(run, c))
    _step(run, "3b", lambda c: _memory(run, c))
    _step(run, "4", lambda c: _scrub(run, c))
    waiting = _step(run, "5", lambda c: _rebuild(run, c))
    if waiting is not None:
        return waiting
    _step(run, "6", lambda _c: _finish(run))
    _step(run, "7", lambda c: _second_pass(run, c))
    return JobOutcome(status="done", result={"request_id": request.request_id})
