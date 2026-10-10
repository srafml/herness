"""State-machine steps of the run driver (impl 06 U06-82 steps 2, 3 and 6; F06-03, F06-07).

Private sibling of `herness.harness.swarm.run` (T06-22 split). One coroutine per run status
calls the owner step (U06-88, F06-04 with U06-103 and U06-110, U06-109, U06-114, U06-115,
U06-122) and ends with the status compare-and-set of design 06 §5.1 (`cas`, which logs
`harness.run.status_changed`; run-row helpers live in `_run_ops`). Every step is idempotent:
it computes what is missing from the `task` and `finding` tables, so a kill at any step
boundary resumes at the stored status.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from herness.core.errors import HernessError, RetryableError
from herness.core.ids import IdKind, new_id
from herness.core.jobs import claim_task, fail_task
from herness.core.logging import get_logger
from herness.harness.swarm import record
from herness.harness.swarm._dedup_read import read_proposed
from herness.harness.swarm._run_ops import (
    NO_WILDCARDS,
    cas,
    exit_large_scope,
    mark_no_wildcards,
    persist_usage,
)
from herness.harness.swarm.adversarial import plan_crosschecks, select_for_challenge
from herness.harness.swarm.challenge import run_challenge_rounds, run_gate1
from herness.harness.swarm.dedup import run_dedup
from herness.harness.swarm.lifecycle import handle_budget_exhausted
from herness.harness.swarm.planner import build_plan_context, planner_dedup_key, run_planning
from herness.harness.swarm.writer import run_writer_step
from herness.metrics.settings import unconfirmed_blocks
from herness.store.ops import (
    get_task_by_dedup,
    run_write,
    select_tasks,
    update_run_fields,
)

if TYPE_CHECKING:
    from herness.core.types import Role
    from herness.harness.pipelines.base import RecordedReader
    from herness.harness.swarm.routing import RunEnv
    from herness.harness.swarm.scheduler import PhaseOutcome, PhaseRunner

__all__ = ["Steps"]

_log = get_logger("harness.swarm")

ANALYSTS: Final[frozenset[Role]] = frozenset({"analyst"})

type Render = Callable[[str, Sequence[str]], object]


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
        except RetryableError:
            raise  # U06-82 step 8: no status change, no planner attempt charged
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

    async def _planner_status(self) -> str | None:
        key = planner_dedup_key(self._id, self.env.run.kind)
        row = await asyncio.to_thread(get_task_by_dedup, self._id, key)
        return None if row is None else row.status

    async def planning(self) -> None:
        """F06-03; a dead planner fails the run, a done one (forced restart) moves on."""
        if await self._planner_status() == "dead":
            await self._cas("planning", "failed")
            return
        if not await self.ensure_plan_ctx():
            return  # the planner task was failed: the loop re-reads it (F06-03)
        outcome = await run_planning(self.env)
        await persist_usage(self.env)
        if outcome == "failed":
            await self._cas("planning", "failed")
        elif await self._planner_status() == "done":  # also a forced restart of a planned run
            if self.env.run.meta.get(NO_WILDCARDS) is True:
                await mark_no_wildcards(self.env.bb, self._id, value=False)
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
        await persist_usage(self.env)
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
