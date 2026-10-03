"""Promotion, retirement and build cleanup (impl 02 U02-104, U02-105; design 02 §7, spec 00 §4).

``promote_build`` is the blue/green switch: the build is marked ``promoted`` and closed, then
``CURRENT`` is replaced atomically, the previous build is retired and retention runs.
``cleanup_builds`` deletes orphans before a build (``pre``) and applies retention after a
promotion (``post``). It never deletes ``CURRENT``, a protected build, a build pinned by a
non-terminal run, or a build a non-terminal ``build_pipeline`` job will resume (T02-21 spec
note under U02-105: a yielded ``enrich`` / ``score`` build looks like an orphan otherwise).
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Final, Literal

import duckdb

from herness.core.errors import ConfigError, HernessError, StoreBusy
from herness.core.logging import get_logger
from herness.core.resilience import fault_point
from herness.model import meta
from herness.store import ops, warehouse
from herness.store._warehouse_rw import open_for_build, write_current

if TYPE_CHECKING:
    from herness.core.jobs.ports import JobRow
    from herness.model.settings import BuildSettings
    from herness.store.layout import DataLayout
    from herness.store.warehouse import BuildInfo

# `run.status` CHECK list (§4.3) minus the terminal set done, partial, failed, canceled.
NON_TERMINAL_RUN_STATUSES: Final = (
    "created",
    "planning",
    "running",
    "challenging",
    "verifying",
    "writing",
    "recording",
)
ORPHAN_UNREADABLE_AGE_S: Final = 3600
_ACTIVE_JOB_STATUSES: Final = ("queued", "running")  # a yielded job is queued again (impl 08)
_JOBS_LIMIT: Final = 1000
_KEEP_LAST: Final = range(1, 21)

_log = get_logger("model.build")


@dataclasses.dataclass(frozen=True, slots=True)
class CleanupReport:
    """Build IDs deleted, whose deletion was deferred (file in use), and retired."""

    deleted: tuple[str, ...]
    deferred: tuple[str, ...]
    retired: tuple[str, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class _RetireDb:
    """``open_for_build`` settings for the one-row status update of a retirement."""

    threads: int | None = 1
    memory_limit: str = "256MiB"


def _retire(build_id: str, layout: DataLayout) -> bool:
    """Set ``meta.build.status = 'retired'``; False (logged) when the file is in use."""
    try:
        con = open_for_build(build_id, create=False, cfg=_RetireDb(), layout=layout)
    except StoreBusy:
        _log.info("model.build.retire_deferred", build_id=build_id)
        return False
    try:
        meta.update_build_row(con, status="retired")
    finally:
        con.close()
    return True


def promote_build(
    con: duckdb.DuckDBPyConnection,
    build_id: str,
    *,
    build_cfg: BuildSettings,
    layout: DataLayout,
    now: datetime.datetime,
) -> str | None:
    """Promote ``build_id``: status, ``CURRENT``, retirement, retention (U02-104).

    Precondition: the DQ gate passed in this job. ``con`` is the build connection and is
    closed here before ``CURRENT`` changes. Returns the previous ``CURRENT``. Raises
    StoreBusy from ``write_current`` (the job retries) and SchemaViolation.
    """
    fault_point("pipeline.before_promote")
    meta.update_build_row(con, status="promoted", finished_at=now)
    con.execute("CHECKPOINT")
    con.close()
    previous = write_current(build_id, layout=layout)
    if previous is not None and previous != build_id:
        _retire_quietly(previous, layout)  # deferred or failed: the next cleanup retries
    protect = frozenset({build_id})
    cleanup_builds(
        mode="post", keep_last=build_cfg.keep_last, protect=protect, layout=layout, now=now
    )
    _log.info("model.build.promoted", build_id=build_id, previous=previous)
    return previous


def _job_builds(job: JobRow) -> set[str]:
    """Builds ``job`` resumes (U02-98 step 3): payload ``build_id``, or the state's build
    when its stage ``build`` is done (a yield inside ``build`` leaves a true orphan)."""
    named = [job.payload.get("build_id")]
    state = (job.result or {}).get("state")
    if isinstance(state, dict):
        done = state.get("stages_done")
        if isinstance(done, list) and "build" in done:
            named.append(state.get("build_id"))
    return {name for name in named if isinstance(name, str)}


def _active_job_builds() -> set[str]:
    """Builds named by queued (incl. yielded) or running ``build_pipeline`` jobs."""
    backend = ops.SqliteJobsBackend()
    jobs = [
        job
        for status in _ACTIVE_JOB_STATUSES
        for job in backend.list_jobs(status=status, kind="build_pipeline", limit=_JOBS_LIMIT)
    ]
    return {build_id for job in jobs for build_id in _job_builds(job)}


def _pinned_by_runs() -> set[str]:
    runs = ops.select_runs(statuses=NON_TERMINAL_RUN_STATUSES)
    return {run.build_id for run in runs if run.build_id}


def _stale_unreadable(info: BuildInfo, now: datetime.datetime) -> bool:
    try:
        mtime = info.path.stat().st_mtime
    except OSError:
        return False
    return now.timestamp() - mtime > ORPHAN_UNREADABLE_AGE_S


def _orphans(builds: Sequence[BuildInfo], now: datetime.datetime) -> list[BuildInfo]:
    """Rule (2), both modes: killed builds, old unreadable files, all but the newest failed."""
    failed = [b for b in builds if b.status == "failed"]  # newest first (list_builds)
    return [
        b
        for b in builds
        if (b.status == "building" and b.finished_at is None)
        or (b.status == "unreadable" and _stale_unreadable(b, now))
        or b in failed[1:]
    ]


def _retention(builds: Sequence[BuildInfo], keep_last: int) -> list[BuildInfo]:
    """Rule (3), ``post`` only: old promoted / retired builds and superseded builds."""
    kept = [b for b in builds if b.status in ("promoted", "retired") and not b.is_current]
    current = next((b.build_id for b in builds if b.is_current), None)
    superseded = [
        b
        for b in builds
        if b.status == "building"
        and b.finished_at is not None
        and current is not None
        and b.build_id < current
    ]
    return kept[keep_last - 1 :] + superseded


def _delete(
    targets: Iterable[BuildInfo], layout: DataLayout, *, mode: str
) -> tuple[list[str], list[str]]:
    deleted: list[str] = []
    deferred: list[str] = []
    for info in targets:
        outcome = warehouse.delete_build_files(info.build_id, layout=layout)
        if outcome == "deferred":
            deferred.append(info.build_id)
        elif outcome == "deleted":
            deleted.append(info.build_id)
            if mode == "pre":
                _log.info("model.build.orphan_deleted", build_id=info.build_id, status=info.status)
    return deleted, deferred


def cleanup_builds(
    *,
    mode: Literal["pre", "post"],
    keep_last: int,
    protect: frozenset[str],
    layout: DataLayout,
    now: datetime.datetime,
) -> CleanupReport:
    """Delete orphans (``pre`` and ``post``) and apply retention (``post``) (U02-105).

    Never deletes ``CURRENT``, ``protect``, builds pinned by a non-terminal run
    (``select_runs``) or builds a queued / running ``build_pipeline`` job resumes; ``locked``
    files are never deleted. ``post`` also retires every other non-current ``promoted``
    build. Raises ConfigError for a bad ``mode`` / ``keep_last``; StoreBusy from the ops reads.
    """
    if mode not in ("pre", "post") or keep_last not in _KEEP_LAST:
        msg = "cleanup_builds: mode must be pre or post and keep_last 1-20"
        raise ConfigError(msg)
    builds = warehouse.list_builds(layout=layout)
    keep = protect | _pinned_by_runs() | _active_job_builds()
    keep |= {b.build_id for b in builds if b.is_current}
    targets = _orphans(builds, now)
    if mode == "post":
        targets += [b for b in _retention(builds, keep_last) if b not in targets]
    deleted, deferred = _delete([b for b in targets if b.build_id not in keep], layout, mode=mode)
    retired: list[str] = []
    if mode == "post":
        gone = set(deleted) | set(deferred)
        for info in builds:
            stale = info.status == "promoted" and not info.is_current
            if stale and info.build_id not in gone and _retire_quietly(info.build_id, layout):
                retired.append(info.build_id)
    _log.info(
        "model.build.cleanup",
        mode=mode,
        deleted=len(deleted),
        deferred=len(deferred),
        retired=len(retired),
    )
    return CleanupReport(tuple(deleted), tuple(deferred), tuple(retired))


def _retire_quietly(build_id: str, layout: DataLayout) -> bool:
    """Rule (4): a retirement that fails is retried by the next cleanup, never raised."""
    try:
        return _retire(build_id, layout)
    except HernessError as exc:
        _log.warning("model.build.retire_failed", build_id=build_id, error_class=type(exc).__name__)
        return False
