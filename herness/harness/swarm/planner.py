"""Planner step of the swarm: plan context, planner and judge, post-processing (impl 06 §3.9).

`build_plan_context` (U06-87) assembles the `PlanContext`; `run_planning` (U06-88) runs the
Planner best-of-N with the deep-mode judge, post-processes the chosen proposal and commits the
plan with the run's `running` status in one transaction. `postprocess_plan` (U06-89) is the
TH06-04 boundary: a model-proposed task can only add notes to a deterministic task or become a
capped analyst hypothesis whose budget, tools, priority and model role are set here.
`judge_select` (U06-90) and `base_priority` (U06-91) are pure.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Final, Literal, cast

from pydantic import JsonValue, ValidationError

from herness.core.errors import (
    ConfigError,
    FatalError,
    HernessError,
    ModelUnavailable,
    NotFound,
)
from herness.core.ids import IdKind, new_id
from herness.core.jobs import claim_task, complete_task, fail_task, save_checkpoint
from herness.core.logging import get_logger
from herness.core.resilience import fault_point
from herness.core.types import (
    Depth,
    EntityScope,
    SwarmTaskState,
    TaskSpec,
)
from herness.harness.findings import compute_dedup_key
from herness.harness.loop import run_agent
from herness.harness.roles.judge import JudgeOutput
from herness.harness.roles.planner import PlannerOutput
from herness.harness.swarm._plan_ctx import build_plan_context
from herness.harness.swarm._plan_post import PlanDecision, postprocess_plan
from herness.harness.swarm.routing import (
    RunEnv,
    build_hooks,
    build_task_input,
    build_tool_context,
    role_budget,
    route_task,
)
from herness.metrics.catalog import load_catalog
from herness.store.ops import get_task_by_dedup, insert_tasks, run_write, set_run_status

if TYPE_CHECKING:
    import sqlite3

    from herness.core.types import AgentResult
    from herness.harness.pipelines.base import PlanContext
    from herness.harness.pipelines.settings import DepthKnobs
    from herness.store.ops.runs import TaskRow

__all__ = [
    "JUDGE_CRITERIA",
    "PlanDecision",
    "base_priority",
    "build_plan_context",
    "judge_select",
    "planner_dedup_key",
    "postprocess_plan",
    "run_planning",
]

_log = get_logger("harness.swarm")

SCORE_MAX: Final = 5.0
JUDGE_CRITERIA: Final = (
    "must-cover framing present",
    "hypotheses testable",
    "no overlap",
    "DQ warnings addressed",
)
JUDGE_OBJECTIVE: Final = "Judge the planner proposals."
_USABLE: Final = frozenset({"completed", "partial"})


def base_priority(rank: int | None, must_cover: bool) -> float:
    """U06-91 (design 06 §5.4): `(100 - rank if rank is not None else 0) + (50 if must_cover)`."""
    return float((100 - rank if rank is not None else 0) + (50 if must_cover else 0))


def judge_select(samples: Sequence[Sequence[float]], n: int) -> tuple[int, list[float]]:
    """Choice and mean score per proposal over judge samples (U06-90); pure.

    Samples whose length is not `n` or holding a non-finite score are dropped; scores are
    clipped to 0-5; ties go to the lower index; no kept sample gives `(0, [0.0] * n)`.
    """
    kept = [
        [min(max(float(s), 0.0), SCORE_MAX) for s in sample]
        for sample in samples
        if len(sample) == n and all(math.isfinite(float(s)) for s in sample)
    ]
    if not kept:
        return 0, [0.0] * n
    means = [sum(sample[i] for sample in kept) / len(kept) for i in range(n)]
    best = max(means)
    return means.index(best), means


# --- planner step (U06-88)


@dataclass(slots=True)
class _Usage:
    """Token and cost totals of the planner's own samples."""

    tokens_in: int = 0
    tokens_out: int = 0
    cost: Decimal = Decimal(0)

    def add(self, res: AgentResult) -> None:
        u = res.usage
        self.tokens_in += u.input_tokens + u.cache_read_tokens + u.cache_write_tokens
        self.tokens_out += u.output_tokens
        self.cost += res.cost_usd


def planner_dedup_key(run_id: str, kind: str) -> str:
    """The `dedup_key` of a run's planner task, as `create_run_record` (U06-137) writes it."""
    scope = EntityScope(entity_type="run", entity_ids=[run_id])
    return compute_dedup_key("planner", "general", scope, f"Plan the {kind} review.")


def metric_names() -> frozenset[str]:
    """Spec 04 metric catalog names (the U06-89 `metric_names` input)."""
    return frozenset(load_catalog().names())


def _plan_context(env: RunEnv) -> PlanContext:
    if env.plan_ctx is None:
        msg = f"run {env.run.run_id} has no plan context"
        raise ConfigError(msg)
    return env.plan_ctx


async def run_planning(env: RunEnv) -> Literal["planned", "failed"]:
    """The planner step (U06-88): proposals, judge, post-processing and the plan commit.

    `planned` also means "re-read the planner status": a failed attempt leaves the task
    `pending` until spec 08 sets it `dead`, which returns `failed`.
    """
    run, ctx = env.run, _plan_context(env)
    key = planner_dedup_key(run.run_id, run.kind)
    row = await asyncio.to_thread(get_task_by_dedup, run.run_id, key)
    if row is None:
        msg = f"run {run.run_id} has no planner task"
        raise NotFound(msg)
    if row.status in {"done", "dead"}:
        return "planned" if row.status == "done" else "failed"
    if not await env.bb.run_on_writer(partial(claim_task, row.task_id)):
        return "planned"
    fault_point("swarm.after_task_claim", role="planner")
    deterministic = await asyncio.to_thread(env.pipeline.deterministic_tasks, ctx)
    usage = _Usage()
    if env.force and env.knobs.H_wildcards == 0 and row.last_error is not None:
        await _commit(env, row, (deterministic, []), rationale="", usage=usage)
        return "planned"
    try:
        proposals = await _proposals(env, row, ctx, deterministic, usage)
        if not proposals:
            await _fail(env, row, ModelUnavailable("planner produced no plan"))
            return "planned"
        choice = await _judge(env, proposals) if len(proposals) > 1 else 0
    except FatalError as err:  # U06-96 mapping, as the scheduler (U06-93): dead at once
        return "failed" if await _fail(env, row, err) == "dead" else "planned"
    chosen = proposals[choice if choice < len(proposals) else 0]
    planned = await asyncio.to_thread(
        partial(
            postprocess_plan, deterministic, chosen.tasks, knobs=env.knobs,
            depth_mode=cast("Depth", run.depth), run_id=run.run_id,
            entity_missing=env.catalog.missing, metric_names=metric_names(),
            new_task_id=partial(new_id, IdKind.TASK),
        )
    )  # fmt: skip
    for d in planned[1]:
        env.tracer.emit(
            "plan_decision", task_id=row.task_id, role="planner", action=d.action,
            reason=d.reason, dedup_key=d.dedup_key, specialty=d.specialty,
            entity_type=d.entity_type, n_entities=d.n_entities,
        )  # fmt: skip
    await _commit(env, row, planned, rationale=chosen.rationale, usage=usage)
    return "planned"


async def _fail(env: RunEnv, row: TaskRow, err: HernessError) -> str:
    """`fail_task` on the writer with the original error (a FatalError is dead at once)."""
    attempts = env.cfg.swarm.max_task_attempts
    outcome: str = await env.bb.run_on_writer(
        partial(fail_task, row.task_id, err, max_task_attempts=attempts)
    )
    _log.warning(
        "harness.task.failed", run_id=row.spec.run_id, task_id=row.task_id, role=row.spec.role,
        error_type=type(err).__name__, outcome=outcome,
    )  # fmt: skip
    return outcome


async def _proposals(
    env: RunEnv, row: TaskRow, ctx: PlanContext, deterministic: list[TaskSpec], usage: _Usage
) -> list[PlannerOutput]:
    """Stored proposals of the checkpoint (R-21), else `plans_best_of` fresh samples."""
    state = SwarmTaskState.from_envelope(row.checkpoint or {}, task_id=row.task_id)
    if state is not None and state.proposals:
        return [PlannerOutput.model_validate(p) for p in state.proposals]
    n = env.knobs.plans_best_of
    planner_input = await asyncio.to_thread(env.pipeline.planner_input, ctx, deterministic)
    proposals: list[PlannerOutput] = []
    for i in range(n):
        payload = {"planner_input": planner_input, "best_of_n": n, "sample_index": i}
        res = await _agent(env, row.spec, build_task_input("planner", payload))
        out = _parsed(PlannerOutput, res, usage)
        if out is not None:
            proposals.append(out)
    if proposals:
        state = SwarmTaskState(
            phase="planning", proposals=[p.model_dump(mode="json") for p in proposals]
        )
        await env.bb.run_on_writer(
            partial(save_checkpoint, row.task_id, "state", state.model_dump(mode="json"))
        )
    return proposals


def _parsed[M: (PlannerOutput, JudgeOutput)](
    model: type[M], res: AgentResult | None, usage: _Usage | None
) -> M | None:
    """The validated output of a usable (`completed` or `partial`) agent result, else None."""
    if res is None:
        return None
    if usage is not None:
        usage.add(res)
    if res.status not in _USABLE or res.output is None:
        return None
    try:
        return model.model_validate(res.output)
    except ValidationError:
        return None


async def _agent(env: RunEnv, spec: TaskSpec, task_input: dict[str, object]) -> AgentResult | None:
    """One fresh `run_agent` call for `spec` (U06-99 hooks).

    A retryable or recoverable agent error makes the sample unusable (None); a `FatalError`
    (budget, egress, config, permission) propagates and fails the task (U06-96).
    """
    run = env.run
    depth = cast("Depth", run.depth)
    try:
        route = route_task(
            spec, llms=env.llms, depth=depth, profile=run.profile, ledger=env.analysis
        )
        ctx = build_tool_context(
            env, spec, route, task_tools={}, ledger=env.analysis, now=env.clock()
        )
        hooks = build_hooks(env, spec, route, ctx)
        res = await run_agent(
            route.role_spec, cast("dict[str, JsonValue]", task_input), ctx, route.client,
            route.config, hooks, resume_from=None,
        )  # fmt: skip
    except FatalError:
        raise
    except HernessError as err:
        _log.warning(
            "harness.plan.sample_failed", run_id=run.run_id, task_id=spec.task_id,
            role=spec.role, error_type=type(err).__name__,
        )  # fmt: skip
        return None
    with env.by_role_lock:
        acc = env.by_role.setdefault(spec.role, {})
        u = res.usage
        tokens_in = u.input_tokens + u.cache_read_tokens + u.cache_write_tokens
        acc["input"] = acc.get("input", 0) + tokens_in
        acc["output"] = acc.get("output", 0) + u.output_tokens
    return res


def _judge_spec(env: RunEnv, knobs: DepthKnobs) -> TaskSpec:
    run_id = env.run.run_id
    scope = EntityScope(entity_type="run", entity_ids=[run_id])
    return TaskSpec(
        task_id=new_id(IdKind.TASK), run_id=run_id, role="judge", objective=JUDGE_OBJECTIVE,
        scope=scope, tools=[], budget=role_budget("judge", knobs, writer_tokens=0),
        model_role="judge", k_samples=knobs.k_samples.default,
        dedup_key=compute_dedup_key("judge", "general", scope, JUDGE_OBJECTIVE),
    )  # fmt: skip


async def _judge(env: RunEnv, proposals: list[PlannerOutput]) -> int:
    """The judge choice: stored when the judge task is `done`, else `k_samples` new samples."""
    spec = _judge_spec(env, env.knobs)
    now = env.clock()

    def insert(conn: sqlite3.Connection) -> None:
        insert_tasks(conn, [spec], now=now)

    await env.bb.run_on_writer(partial(run_write, insert, op="swarm_judge_task"))
    row = await asyncio.to_thread(get_task_by_dedup, spec.run_id, spec.dedup_key)
    if row is not None and row.status == "done" and row.result is not None:
        return int(cast("int", row.result.get("choice", 0)))
    if row is None or not await env.bb.run_on_writer(partial(claim_task, row.task_id)):
        _log.warning("harness.plan.judge_unavailable", run_id=spec.run_id)
        return 0
    payload = {
        "proposals": [p.model_dump(mode="json") for p in proposals],
        "criteria": list(JUDGE_CRITERIA),
    }
    samples: list[list[float]] = []
    usage = _Usage()
    try:
        for i in range(row.spec.k_samples):
            task_input = build_task_input("planner", payload | {"sample_index": i})  # R-20
            out = _parsed(JudgeOutput, await _agent(env, row.spec, task_input), usage)
            if out is not None:
                samples.append(list(out.scores))
    except FatalError as err:
        await _fail(env, row, err)  # the judge dies with the original error; so does the planner
        raise
    choice, means = judge_select(samples, len(proposals))
    if not samples:
        _log.warning("harness.plan.judge_failed", run_id=spec.run_id, task_id=row.task_id)
    result = _result("", usage) | {"choice": choice, "scores": means}
    await env.bb.run_on_writer(partial(complete_task, row.task_id, result))
    return choice


def _result(summary: str, usage: _Usage) -> dict[str, object]:
    return {
        "summary": summary, "finding_ids": [], "partial": False, "stop_cause": "final",
        "tokens": {"input": usage.tokens_in, "output": usage.tokens_out},
        "cost_usd": str(usage.cost), "subtasks": [],
    }  # fmt: skip


async def _commit(
    env: RunEnv,
    row: TaskRow,
    planned: tuple[list[TaskSpec], list[PlanDecision]],
    *,
    rationale: str,
    usage: _Usage,
) -> None:
    """Complete the planner with its plan, the plan's task rows and `running` (U06-88 step 8)."""
    plan, decisions = planned
    run_id, now = env.run.run_id, env.clock()

    def writes(conn: sqlite3.Connection) -> None:
        insert_tasks(conn, plan, now=now)
        set_run_status(conn, run_id, "running", {"planning"}, now=now)

    result = _result(rationale, usage) | {"plan": [t.model_dump(mode="json") for t in plan]}
    await env.bb.run_on_writer(partial(complete_task, row.task_id, result, writes=writes))
    kept = sum(d.action == "kept" for d in decisions)
    _log.info(
        "harness.plan.completed", run_id=run_id,
        deterministic=len(plan) - kept, hypotheses_kept=kept,
        hypotheses_dropped=sum(d.action == "dropped" for d in decisions),
    )  # fmt: skip
