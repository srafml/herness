"""Skeptic execution, challenge rounds, cross-check evaluation and gate 1 (impl 06 U06-108,
U06-109, U06-113, U06-114; design 06 §5.7).

`execute_skeptic` runs one Skeptic task with self-consistency and commits the verdict with
the task status; `run_challenge_rounds` drives the rounds with revisions and the large stage;
`evaluate_crosschecks` builds the `CrossCheck` records of one finding and `run_gate1` /
`execute_verifier` run Verifier gate 1 (private sibling `_challenge_gate1`, re-exported here).
The pure rules live in `adversarial`. The scheduler dispatches skeptic and verifier tasks to
this module, which it imports on first use (this module imports the scheduler).
"""

from __future__ import annotations

import asyncio
import functools
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

from pydantic import JsonValue, ValidationError

from herness.core.errors import ConfigError, FatalError, HernessError
from herness.core.ids import IdKind, new_id
from herness.core.logging import get_logger
from herness.core.types import Challenge, RejectReason, Role
from herness.harness.blackboard import Blackboard
from herness.harness.loop import run_agent
from herness.harness.swarm import scheduler as sch
from herness.harness.swarm._challenge_gate1 import (
    CrosscheckOutcome,
    CrosscheckReader,
    Writes,
    evaluate_crosschecks,
    execute_verifier,
    finish,
    guard,
    insert,
    plain_result,
    run_gate1,
)
from herness.harness.swarm._dedup_read import read_proposed
from herness.harness.swarm.adversarial import (
    build_revision_task,
    build_skeptic_task,
    majority_verdict,
    normalize_verdict,
    select_for_challenge,
)
from herness.harness.swarm.hybrid import Pseudonymizer, build_evidence_pack
from herness.harness.swarm.routing import (
    build_hooks,
    build_task_input,
    build_tool_context,
    map_agent_result,
    route_task,
)
from herness.harness.swarm.scheduler import PhaseOutcome
from herness.harness.swarm.tools import build_task_tools
from herness.store.ops import (
    count_tasks,
    get_findings,
    get_task,
    run_write,
    select_tasks,
    update_run_fields,
)

if TYPE_CHECKING:
    from herness.core.types import AgentResult, Depth, Finding, TaskSpec, ToolContext
    from herness.harness.budget import RunBudget
    from herness.harness.loop import HarnessHooks
    from herness.harness.swarm.routing import Route, RunEnv
    from herness.harness.swarm.scheduler import PhaseRunner
    from herness.store.ops.runs import TaskRow

__all__ = [
    "CrosscheckOutcome",
    "CrosscheckReader",
    "evaluate_crosschecks",
    "execute_skeptic",
    "execute_verifier",
    "run_challenge_rounds",
    "run_gate1",
]

_log = get_logger("harness.swarm")
_ROUND_ROLES: Final[frozenset[Role]] = frozenset({"skeptic", "analyst"})


def _new_task_id() -> str:
    return new_id(IdKind.TASK)


# --- U06-108 Skeptic --------------------------------------------------------------------------


@dataclass(slots=True)
class _Skeptic:
    """One Skeptic task's routed agent, its sample input and its sample results."""

    env: RunEnv
    row: TaskRow
    route: Route
    ctx: ToolContext
    hooks: HarnessHooks
    base: dict[str, object]
    pseudo: Pseudonymizer | None
    runs: list[AgentResult] = field(default_factory=list)

    async def sample(self, i: int) -> Challenge | None:
        """One sample: a normalized verdict, or None when the output is not usable."""
        spec, route = self.row.spec, self.route
        task_input = build_task_input("skeptic", self.base | {"sample_index": i})
        resume = sch._resume_from(self.row.checkpoint) if spec.k_samples == 1 and i == 0 else None
        res = await run_agent(
            route.role_spec, cast("dict[str, JsonValue]", task_input), self.ctx, route.client,
            route.config, self.hooks, resume_from=resume,
        )  # fmt: skip
        sch._add_usage(self.env, "skeptic", res)
        self.runs.append(res)
        if res.status == "failed" or res.output is None:
            return None
        out = res.output if self.pseudo is None else self.pseudo.restore_obj(res.output)
        try:
            ch = Challenge.model_validate(
                {**cast("dict[str, object]", out), "finding_id": spec.inputs.finding_ids[0]}
            )
        except ValidationError:
            return None
        return normalize_verdict(ch)


async def execute_skeptic(env: RunEnv, row: TaskRow, ledger: RunBudget) -> None:
    """Run one Skeptic task with self-consistency; commit its verdict with the task (U06-108).

    Samples run sequentially; an off-network route gets the evidence pack (U06-123) of the
    target under a run-local `Pseudonymizer` and the outputs are restored before the verdict
    rule (TH06-06). A pack that cannot be built fails the task closed (`FatalError`).
    """
    await guard(env, row, functools.partial(_skeptic, env, row, ledger))


async def _skeptic(env: RunEnv, row: TaskRow, ledger: RunBudget) -> sch._Outcome:
    spec = row.spec
    fid = spec.inputs.finding_ids[0]
    target = (await asyncio.to_thread(get_findings, [fid])).get(fid)
    if target is None:
        msg = f"skeptic target not found: finding_id={fid}"
        raise FatalError(msg)
    if target.status != "proposed":
        await finish(env, row, plain_result("target no longer open", []), None)
        return "done"
    depth = cast("Depth", env.run.depth)
    route = route_task(spec, llms=env.llms, depth=depth, profile=env.run.profile, ledger=ledger)
    base, pseudo = await _skeptic_input(env, spec, target, route)
    tools = build_task_tools(spec, bb=env.bb, broker=env.broker)
    ctx = build_tool_context(env, spec, route, task_tools=tools, ledger=ledger, now=env.clock())
    s = _Skeptic(env, row, route, ctx, build_hooks(env, spec, route, ctx), base, pseudo)
    k, on_reject = spec.k_samples, env.knobs.k_samples.on_reject
    try:
        async with asyncio.timeout(spec.budget.wall_clock_s + sch.WALL_CLOCK_GRACE_S) as backstop:
            chs = [ch for i in range(k) if (ch := await s.sample(i)) is not None]
            if on_reject > k and chs and majority_verdict([c.verdict for c in chs])[0] == "reject":
                chs += [ch for i in range(k, on_reject) if (ch := await s.sample(i)) is not None]
    except TimeoutError:
        if backstop.expired():
            raise sch._Backstop from None
        raise
    if not chs:
        msg = "skeptic produced no verdict"
        raise FatalError(msg)
    verdict, votes = majority_verdict([c.verdict for c in chs])
    final = next(c for c in chs if c.verdict == verdict).model_copy(
        update={"finding_id": fid, "round": spec.round, "skeptic_task_id": spec.task_id,
                "votes": votes if len(chs) > 1 else None, "model": route.client_key}
    )  # fmt: skip
    writes = await asyncio.to_thread(_verdict_writes, env, spec, target, final)
    result = map_agent_result(s.runs[-1], extra={"verdict": verdict, "votes": votes})
    result["tokens"] = {
        key: sum(cast("dict[str, int]", map_agent_result(r)["tokens"])[key] for r in s.runs)
        for key in ("input", "output")
    }
    result["cost_usd"] = str(sum(r.cost_usd for r in s.runs))
    await finish(env, row, result, writes)
    return "done"


async def _skeptic_input(
    env: RunEnv, spec: TaskSpec, target: Finding, route: Route
) -> tuple[dict[str, object], Pseudonymizer | None]:
    """The sample input without `sample_index`: spec + finding, or the hybrid evidence pack."""
    if not route.off_network:
        return spec.model_dump(mode="json") | {"finding": target.model_dump(mode="json")}, None
    kind, eid = target.entity_type, target.entity_id
    try:
        name = (await asyncio.to_thread(env.catalog.names, kind, [eid])).get(eid)
        pseudo = Pseudonymizer([(kind, eid, name)])
        pack = build_evidence_pack(
            spec, findings=[target], inp={}, pseudo=pseudo,
            max_tokens=env.cfg.hybrid.max_input_tokens_per_call, cfg=route.config,
        )  # fmt: skip
    except (HernessError, ValueError) as exc:
        msg = f"evidence pack unavailable: task_id={spec.task_id}"
        raise FatalError(msg) from exc
    if not pack["findings"]:
        msg = f"evidence pack dropped the target: task_id={spec.task_id}"
        raise FatalError(msg)
    return pack, pseudo


def _verdict_writes(env: RunEnv, spec: TaskSpec, target: Finding, final: Challenge) -> Writes:
    """The challenge append or rejection (and revision task) for `final` (U06-108 step 5)."""
    fid, now, revision = target.finding_id, env.clock(), None
    if final.verdict == "revise":
        used = count_tasks(spec.run_id, roles={"analyst", "skeptic"})
        author = get_task(target.task_id)
        if used < env.knobs.max_tasks_per_run and author is not None:
            revision = build_revision_task(
                target, final, skeptic=spec, author=author.spec, knobs=env.knobs,
                new_task_id=_new_task_id,
            )  # fmt: skip

    def writes(conn: sqlite3.Connection) -> None:
        if final.verdict == "uphold" or revision is not None:
            Blackboard.tx_challenge(conn, fid, final, revision, now)
        else:
            reason: RejectReason = (
                "skeptic_reject" if final.verdict == "reject" else "revision_dead"
            )
            Blackboard.tx_reject(conn, fid, reason, None, final)

    return writes


# --- U06-109 rounds ---------------------------------------------------------------------------


def _first_round(
    env: RunEnv, findings: Sequence[Finding], tasks: Sequence[TaskRow]
) -> list[TaskSpec]:
    """Round 1 Skeptic tasks for the selected findings (U06-109 step 2)."""
    plan_ctx, pipeline, knobs = env.plan_ctx, env.pipeline, env.knobs
    if plan_ctx is None:
        msg = f"run {env.run.run_id} has no plan context"
        raise ConfigError(msg)
    by = {
        s: {t.task_id for t in tasks if t.spec.specialty == s}
        for s in ("retrospective", "crosscheck")
    }
    selected = select_for_challenge(
        findings, score=lambda f: pipeline.challenge_priority(f, plan_ctx),
        top_n=knobs.skeptic_top_n, must_cover=pipeline.must_cover(plan_ctx),
        retrospective_task_ids=by["retrospective"], excluded_task_ids=by["crosscheck"],
    )  # fmt: skip
    final = knobs.skeptic_rounds == 1
    return [
        build_skeptic_task(f, run=env.run, round_no=1, final_round=final, score=score,
                           knobs=knobs, new_task_id=_new_task_id)
        for f, score in selected
    ]  # fmt: skip


async def _next_round(env: RunEnv, rnd: int) -> None:
    """Skeptic tasks of round `rnd + 1` for the revisions made in round `rnd` (step 3d)."""
    findings = await read_proposed(env.bb, env.run.run_id)
    tasks = {t.task_id: t.spec for t in await asyncio.to_thread(select_tasks, env.run.run_id)}
    final = rnd + 1 == env.knobs.skeptic_rounds
    specs = [
        build_skeptic_task(f, run=env.run, round_no=rnd + 1, final_round=final,
                           score=tasks[f.task_id].priority, knobs=env.knobs,
                           new_task_id=_new_task_id)
        for f in findings
        if f.supersedes is not None and f.task_id in tasks and tasks[f.task_id].round == rnd + 1
    ]  # fmt: skip
    await insert(env, specs, "swarm_skeptic_round")


async def _large_stage(env: RunEnv) -> None:
    """Switch to the large GPU class for the last round (U06-109 step 3a, F06-14)."""
    if not env.knobs.large_stage or env.run.profile != "local" or env.large_scope is not None:
        return
    if env.job is None:
        _log.warning("harness.stage.skipped", run_id=env.run.run_id, stage="large")
        return
    patch = functools.partial(
        update_run_fields, run_id=env.run.run_id, meta_patch={"stage": "large"}
    )
    await env.bb.run_on_writer(lambda: run_write(patch, op="swarm_run_stage"))
    scope = env.job.gpu_scope("large")
    await asyncio.to_thread(scope.__enter__)
    env.large_scope = scope


def _log_round(run_id: str, rnd: int) -> None:
    done = select_tasks(run_id, roles={"skeptic"}, statuses={"done"})
    tally = Counter((t.result or {}).get("verdict") for t in done if t.spec.round == rnd)
    _log.info(
        "harness.challenge.round_completed", run_id=run_id, round=rnd,
        upheld=tally["uphold"], revised=tally["revise"], rejected=tally["reject"],
    )  # fmt: skip


async def run_challenge_rounds(env: RunEnv, runner: PhaseRunner) -> PhaseOutcome:
    """Skeptic rounds with revisions (U06-109, design 06 §5.7 Rounds, §5.11 large stage).

    Resumes at the highest Skeptic round stored. Revisions made in the last round stay
    `proposed` and go to gate 1.
    """
    run_id, last = env.run.run_id, env.knobs.skeptic_rounds
    tasks = await asyncio.to_thread(select_tasks, run_id)
    r = max((t.spec.round for t in tasks if t.role == "skeptic"), default=0)
    if r == 0:
        findings = await read_proposed(env.bb, run_id)
        specs = await asyncio.to_thread(_first_round, env, findings, tasks)
        await insert(env, specs, "swarm_skeptic_round")
        r = 1
    out = PhaseOutcome(frozenset(), env.stop())
    for rnd in range(r, last + 1):
        if rnd == last:
            await _large_stage(env)
        out = await runner.run_phase(_ROUND_ROLES, ledger=env.analysis)
        if out.stopped or out.blocked or env.analysis.exhausted:
            return out
        if rnd < last:
            await _next_round(env, rnd)
        await asyncio.to_thread(_log_round, run_id, rnd)
    return out
