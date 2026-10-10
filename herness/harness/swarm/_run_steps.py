"""State-machine steps of the run driver (impl 06 U06-82 steps 2, 3, 6 and 7, U06-77 `_result`).

Private sibling of `herness.harness.swarm.run` (T06-22 split). One coroutine per run status
calls the owner step (U06-88, F06-04 with U06-103 and U06-110, U06-109, U06-114, U06-115,
U06-122) and ends with the status compare-and-set of design 06 §5.1 (`cas`, which logs
`harness.run.status_changed`). Every step is idempotent: it computes what is missing from the
`task` and `finding` tables, so a kill at any step boundary resumes at the stored status.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from herness.core.errors import HernessError
from herness.core.ids import IdKind, new_id
from herness.core.jobs import claim_task, fail_task, list_jobs, release_task
from herness.core.logging import get_logger
from herness.core.types import Coverage, ReportDraft
from herness.harness.swarm import record
from herness.harness.swarm._dedup_read import read_proposed
from herness.harness.swarm.adversarial import plan_crosschecks, select_for_challenge
from herness.harness.swarm.challenge import run_challenge_rounds, run_gate1
from herness.harness.swarm.dedup import run_dedup
from herness.harness.swarm.lifecycle import OPEN_STATUSES, RunResult, handle_budget_exhausted
from herness.harness.swarm.planner import build_plan_context, planner_dedup_key, run_planning
from herness.harness.swarm.writer import draft_path, run_writer_step
from herness.metrics.settings import unconfirmed_blocks
from herness.store.ops import (
    get_task_by_dedup,
    query_findings,
    run_write,
    select_tasks,
    set_run_status,
    update_run_fields,
)

if TYPE_CHECKING:
    import sqlite3

    from herness.core.types import Role
    from herness.harness.blackboard import Blackboard
    from herness.harness.pipelines.base import RecordedReader
    from herness.harness.swarm.routing import RunEnv
    from herness.harness.swarm.scheduler import PhaseOutcome, PhaseRunner
    from herness.store.ops import RunRow

__all__ = [
    "Steps", "build_result", "cas", "exit_large_scope", "load_draft", "persist_usage",
    "release_running", "retire_build", "review_jobs",
]  # fmt: skip

_log = get_logger("harness.swarm")

ANALYSTS: Final[frozenset[Role]] = frozenset({"analyst"})
JOBS_LIMIT: Final = 1000  # spec 08 `list_jobs` maximum
COUNT_LIMIT: Final = 500  # findings counted for a result without a draft (store page limit)

type Render = Callable[[str, Sequence[str]], object]


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


@dataclass(frozen=True, slots=True)
class Steps:
    """The status handlers of one drive (U06-82 step 2)."""

    env: RunEnv
    runner: PhaseRunner
    render: Render
    portfolio_fn: Callable[..., Any]
    reader: RecordedReader

    @property
    def _id(self) -> str:
        return self.env.run.run_id

    async def _cas(self, frm: str, to: str) -> bool:
        return await cas(self.env.bb, self._id, frm, to, {frm}, self.env.clock(), log=False)

    async def created(self) -> None:
        await self._cas("created", "planning")

    async def _plan_ctx(self) -> bool:
        """F06-03 step 1: build the plan context; a failure fails the planner task."""
        env = self.env
        key = planner_dedup_key(self._id, env.run.kind)
        row = await asyncio.to_thread(get_task_by_dedup, self._id, key)
        task_id = "" if row is None else row.task_id
        weights = env.hcfg.weights
        try:
            env.plan_ctx = await build_plan_context(
                env.run, env.knobs, reader=self.reader, memory=env.memory,
                portfolio_fn=self.portfolio_fn, pipelines_cfg=env.cfg, planner_task_id=task_id,
                unconfirmed_weights=unconfirmed_blocks(weights, weights.blocks()),
            )  # fmt: skip
        except HernessError as err:
            if row is None or row.status != "pending":
                raise
            if await env.bb.run_on_writer(functools.partial(claim_task, task_id)):
                attempts = env.cfg.swarm.max_task_attempts
                fail = functools.partial(fail_task, task_id, err, max_task_attempts=attempts)
                await env.bb.run_on_writer(fail)
            return False
        return True

    async def ensure_plan_ctx(self) -> bool:
        """The plan context of the run, rebuilt on resume (U06-139)."""
        return self.env.plan_ctx is not None or await self._plan_ctx()

    async def planning(self) -> None:
        if not await self.ensure_plan_ctx():
            return  # the planner task was failed: the loop re-reads it (F06-03)
        if await run_planning(self.env) == "failed":
            await self._cas("planning", "failed")
            return
        key = planner_dedup_key(self._id, self.env.run.kind)
        row = await asyncio.to_thread(get_task_by_dedup, self._id, key)
        if row is not None and row.status == "done":  # a forced restart of a planned run
            await self._cas("planning", "running")

    async def _exhausted(self, out: PhaseOutcome) -> bool:
        """U06-82 step 3: an exhausted ledger or blocked tasks end the analysis (U06-84)."""
        env = self.env
        if not (env.analysis.exhausted or out.blocked):
            return False
        await handle_budget_exhausted(
            env.bb, self._id, max_task_attempts=env.cfg.swarm.max_task_attempts, now=env.clock()
        )
        return True

    async def _phase(self, roles: frozenset[Role]) -> bool:
        """One phase; True when the step should go on (no stop, no exhaustion)."""
        out = await self.runner.run_phase(roles, ledger=self.env.analysis)
        await persist_usage(self.env)
        return not out.stopped and not await self._exhausted(out)

    async def running(self) -> None:
        """F06-04 and F06-07: fan-out, dedup, cross-check tasks, second fan-out."""
        if not await self._phase(ANALYSTS):
            return
        await run_dedup(self.env)
        await self._crosschecks()
        if await self._phase(ANALYSTS):
            await self._cas("running", "challenging")

    async def _crosschecks(self) -> None:
        """U06-110 cross-check tasks, planned once per run (their dedup keys are stable)."""
        env = self.env
        tasks = await asyncio.to_thread(select_tasks, self._id)
        if any(t.spec.specialty == "crosscheck" for t in tasks) or env.plan_ctx is None:
            return
        ctx, pipeline = env.plan_ctx, env.pipeline
        findings = await read_proposed(env.bb, self._id)
        must = pipeline.must_cover(ctx)
        selected = select_for_challenge(
            findings, score=lambda f: pipeline.challenge_priority(f, ctx),
            top_n=env.knobs.skeptic_top_n, must_cover=must, retrospective_task_ids=set(),
            excluded_task_ids=set(),
        )  # fmt: skip
        specs, skipped = plan_crosschecks(
            findings, run=env.run, knobs=env.knobs, must_cover=must,
            selected_ids={f.finding_id for f, _ in selected}, task_count=len(tasks),
            new_task_id=functools.partial(new_id, IdKind.TASK),
        )  # fmt: skip
        if specs:
            from herness.harness.swarm._challenge_gate1 import insert  # noqa: PLC0415

            await insert(env, specs, "swarm_crosscheck_tasks")
        if skipped:  # O06-10: `crosscheck_skipped:<id>` notes; no owner field yet (T06-22 note)
            _log.info("harness.crosscheck.skipped", run_id=self._id, findings=len(skipped))

    async def challenging(self) -> None:
        out = await run_challenge_rounds(self.env, self.runner)
        await persist_usage(self.env)
        if not out.stopped and not await self._exhausted(out):
            await self._cas("challenging", "verifying")

    async def verifying(self) -> None:
        out = await run_gate1(self.env, self.runner)
        if not out.stopped:
            await self._cas("verifying", "writing")

    async def writing(self) -> None:
        """F06-10: Writer step, scope exit, then `recording` or `partial` plus render."""
        publishable = await run_writer_step(self.env)
        await persist_usage(self.env)
        await exit_large_scope(self.env)
        if publishable:
            await self._cas("writing", "recording")
        elif await self._cas("writing", "partial"):
            await self.render_only()

    async def recording(self) -> None:
        await self._clear_render_error()
        await record.run_record_step(self.env, self.render)

    async def render_only(self) -> None:
        """U06-82: a `partial` run gets only U06-122 step 6 (render, `meta.render_error`)."""
        await self._clear_render_error()
        await record._render(self.env, self.render)

    async def _clear_render_error(self) -> None:
        """A stale `meta.render_error` from an earlier attempt is cleared before rendering."""
        if self.env.run.meta.get("render_error") is None:
            return
        patch = functools.partial(
            update_run_fields, run_id=self._id, meta_patch={"render_error": None}
        )
        await self.env.bb.run_on_writer(lambda: run_write(patch, op="swarm_render_error"))


async def exit_large_scope(env: RunEnv) -> None:
    """F06-10 step 3: leave the large-stage GPU scope when it is open (in a thread)."""
    scope, env.large_scope = env.large_scope, None
    if scope is not None:
        await asyncio.to_thread(scope.__exit__, None, None, None)
