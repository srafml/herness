"""Run-row and task-table helpers of the run driver (impl 06 U06-77, U06-80, U06-81, U06-82).

Private sibling of `herness.harness.swarm.run` (T06-22 review split of `_run_steps`): the logged
status compare-and-set, `meta.no_wildcards` (a forced dead-planner restart, design 06 §6.2),
usage persistence (U06-82 step 6), the release of tasks left `running` after a stop (R-36),
build retirement (U06-80 step 4), the review-job lookup of U06-81, `_result` (U06-77) and the
large-stage scope exit (F06-10 step 3).
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

from herness.core.jobs import list_jobs, release_task
from herness.core.logging import get_logger
from herness.core.types import Coverage, ReportDraft
from herness.harness.swarm.lifecycle import OPEN_STATUSES, RunResult
from herness.harness.swarm.writer import draft_path
from herness.store.ops import (
    query_findings,
    run_write,
    select_tasks,
    set_run_status,
    update_run_fields,
)

if TYPE_CHECKING:
    import sqlite3

    from herness.harness.blackboard import Blackboard
    from herness.harness.swarm.routing import RunEnv
    from herness.store.ops import RunRow

__all__ = [
    "NO_WILDCARDS", "build_result", "cas", "exit_large_scope", "load_draft",
    "mark_no_wildcards", "persist_usage", "release_running", "retire_build", "review_jobs",
]  # fmt: skip

_log = get_logger("harness.swarm")

JOBS_LIMIT: Final = 1000  # spec 08 `list_jobs` maximum
NO_WILDCARDS: Final = "no_wildcards"  # meta key: forced dead-planner restart (design 06 §6.2)
COUNT_LIMIT: Final = 500  # findings counted for a result without a draft (store page limit)


async def cas(  # noqa: PLR0913 - one status compare-and-set with its log line
    bb: Blackboard,
    run_id: str,
    frm: str,
    to: str,
    allowed: Collection[str],
    now: datetime,
    *,
    log: bool = True,
) -> bool:
    """`set_run_status` on the writer; a win logs `harness.run.status_changed` (§8.1) unless
    `log` is False (the drive loop logs the changes it observes, step handlers included)."""

    def fn(conn: sqlite3.Connection) -> bool:
        return set_run_status(conn, run_id, to, allowed, now=now)

    won: bool = await bb.run_on_writer(lambda: run_write(fn, op="swarm_run_status"))
    if won and log:
        _log.info("harness.run.status_changed", run_id=run_id, **{"from": frm, "to": to})
    return won


async def retire_build(bb: Blackboard, run: RunRow, now: datetime) -> None:
    """U06-80 step 4: `failed` with `meta.blocked_reason = "build_retired"`, one write."""
    allowed = OPEN_STATUSES | {"canceled"}

    def fn(conn: sqlite3.Connection) -> bool:
        won = set_run_status(conn, run.run_id, "failed", allowed, now=now)
        if won:
            update_run_fields(conn, run.run_id, meta_patch={"blocked_reason": "build_retired"})
        return won

    if await bb.run_on_writer(lambda: run_write(fn, op="swarm_build_retired")):
        changed = {"from": run.status, "to": "failed"}
        _log.info("harness.run.status_changed", run_id=run.run_id, **changed)
        _log.error("harness.run.failed", run_id=run.run_id, error_type="build_retired")


def review_jobs(run: RunRow) -> list[str]:
    """Queued or running `review` jobs of `run` (payload `run_id` or `meta.job_id`, U06-81)."""
    job_id = run.meta.get("job_id")
    return [
        job.job_id
        for status in ("queued", "running")
        for job in list_jobs(status=status, kind="review", limit=JOBS_LIMIT)
        if job.payload.get("run_id") == run.run_id or (job_id and job.job_id == job_id)
    ]


async def mark_no_wildcards(bb: Blackboard, run_id: str, *, value: bool) -> None:
    """Set or clear `meta.no_wildcards` (a forced restart plans without hypotheses)."""
    patch = functools.partial(update_run_fields, run_id=run_id, meta_patch={NO_WILDCARDS: value})
    await bb.run_on_writer(lambda: run_write(patch, op="swarm_run_no_wildcards"))


async def persist_usage(env: RunEnv) -> None:
    """U06-82 step 6: both ledger snapshots, the role totals and the paid cost on the run."""
    with env.by_role_lock:
        by_role = {role: dict(counts) for role, counts in env.by_role.items()}
    usage = {
        "analysis": env.analysis.snapshot(),
        "writer": env.writer.snapshot(),
        "by_role": by_role,
    }
    cost = env.analysis.cost_used + env.writer.cost_used
    patch = functools.partial(
        update_run_fields, run_id=env.run.run_id, token_usage=usage, cost_usd=cost
    )
    await env.bb.run_on_writer(lambda: run_write(patch, op="swarm_run_usage"))


async def release_running(env: RunEnv) -> int:
    """Return every still `running` task of the run to `pending` (R-36) after a stop ended a
    step: nothing runs for this run any more in this process."""
    rows = await asyncio.to_thread(select_tasks, env.run.run_id, statuses={"running"})
    for row in rows:
        await env.bb.run_on_writer(functools.partial(release_task, row.task_id))
    return len(rows)


def load_draft(run_id: str, reports_root: Path) -> ReportDraft | None:
    """The stored `draft.json` of the run, or None."""
    path = draft_path(run_id, reports_root)
    return ReportDraft.model_validate_json(path.read_bytes()) if path.is_file() else None


def _task_coverage(run_id: str) -> Coverage:
    """Coverage from the task table when no draft exists (U06-77): never publishable."""
    tasks = select_tasks(run_id)
    planned = [t for t in tasks if t.role == "analyst" and t.spec.round == 0]
    planned = [t for t in planned if t.spec.specialty != "crosscheck"]
    must = {
        f"{t.spec.scope.entity_type}:{e}" for t in tasks if t.spec.must_cover
        for e in t.spec.scope.entity_ids
    }  # fmt: skip
    undone = {
        f"{t.spec.scope.entity_type}:{e}" for t in tasks if t.spec.must_cover
        and t.status != "done" for e in t.spec.scope.entity_ids
    }  # fmt: skip

    def count(status: str) -> int:
        return len(query_findings(run_id, statuses={status}, limit=COUNT_LIMIT))

    return Coverage(
        planned_tasks=len(planned), done_tasks=sum(t.status == "done" for t in planned),
        dead_tasks=sum(t.status == "dead" for t in tasks), must_cover_total=len(must),
        must_cover_done=len(must - undone), verified_findings=count("verified"),
        rejected_findings=count("rejected"), publishable=False,
    )  # fmt: skip


def build_result(run: RunRow, reports_root: Path) -> RunResult:
    """`_result(run)` (U06-77): run row, draft coverage when present, dead task ids."""
    draft = load_draft(run.run_id, reports_root)
    dead = [t.task_id for t in select_tasks(run.run_id, statuses={"dead"})]
    return RunResult(
        run_id=run.run_id, status=run.status,
        draft_path=None if draft is None else str(draft_path(run.run_id, reports_root)),
        coverage=_task_coverage(run.run_id) if draft is None else draft.coverage,
        dead_task_ids=dead, token_usage=dict(run.token_usage), cost_usd=Decimal(run.cost_usd),
    )  # fmt: skip


async def exit_large_scope(env: RunEnv) -> None:
    """F06-10 step 3: leave the large-stage GPU scope when it is open (in a thread)."""
    scope, env.large_scope = env.large_scope, None
    if scope is not None:
        await asyncio.to_thread(scope.__exit__, None, None, None)
