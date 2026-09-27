"""UT06-46, UT06-47, ST06-02, ST06-03: the spawn broker (U06-65, design 06 §5.5).

Runs against a migrated ops store with one review run (`bb_env`), the real `Blackboard` writer,
the real `EntityCatalog` over the fake warehouse (teams `t1`, `t2`), a real `RunBudget` and the
real metric buffer; the tracer records events and `wake` counts calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from tests.support.harness_fakes import RecordingTracer
from tests.unit.harness import _blackboard_env as env_mod
from tests.unit.harness._blackboard_env import BbEnv

from herness.core import time as clock
from herness.core.ids import IdKind, new_id
from herness.core.resilience import ProcessState, process_state
from herness.core.types import EntityScope, PlannedTask, TaskBudget, TaskSpec
from herness.harness.budget import RunBudget
from herness.harness.findings import EntityCatalog, compute_dedup_key
from herness.harness.pipelines.settings import (
    DepthKnobs,
    PipelinesConfig,
    SwarmSettings,
    resolve_knobs,
)
from herness.harness.swarm import spawn as spawn_mod
from herness.harness.swarm.routing import default_tools
from herness.harness.swarm.spawn import SpawnBroker, SpawnDecision
from herness.store.ops import count_tasks, get_task, insert_tasks, run_write

pytestmark = pytest.mark.unit

bb_env = env_mod.bb_env  # fixtures
test_redactor = env_mod.test_redactor
T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
METRIC = "herness_harness_spawn_decisions_total"
MODEL_TEXT = "objective text the trace must never carry"


def _knobs(depth: str, **update: object) -> DepthKnobs:
    knobs = resolve_knobs(PipelinesConfig(), "funding_review", depth)  # type: ignore[arg-type]
    return knobs.model_copy(update=update)


def _insert(spec: TaskSpec) -> list[str]:
    return run_write(lambda conn: insert_tasks(conn, [spec], now=clock.now()), op="test_task")


def _parent(env: BbEnv, *, depth: int = 0, specialty: str = "general", **kw: Any) -> TaskSpec:
    tools = kw.pop("tools", None) or default_tools(
        "analyst",
        specialty,
        "deep",
        child_depth=0,
        knobs=_knobs("deep"),  # type: ignore[arg-type]
    )
    scope = EntityScope(entity_type="team", entity_ids=["t1"], period_start=None, period_end=None)
    spec = TaskSpec(
        task_id=new_id(IdKind.TASK),
        run_id=env.run_id,
        role="analyst",
        specialty=specialty,  # type: ignore[arg-type]
        objective=f"parent {new_id(IdKind.TASK)}",
        scope=scope,
        tools=tools,
        budget=TaskBudget(max_steps=20, max_tokens=80_000, wall_clock_s=600),
        depth=depth,
        priority=kw.pop("priority", 100.0),
        model_role="analyst",
        dedup_key=compute_dedup_key("analyst", specialty, scope, new_id(IdKind.TASK)),
        round=kw.pop("round", 0),
    )
    assert _insert(spec) == [spec.task_id]
    return spec


def _args(objective: str = "incidents of team t2", ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "objective": objective,
        "specialty": "ops",
        "entity_type": "team",
        "entity_ids": ids or ["t2"],
        "reason": MODEL_TEXT,
    }


@dataclass
class Harness:
    """One broker plus the fakes it was built with."""

    env: BbEnv
    broker: SpawnBroker
    budget: RunBudget
    tracer: RecordingTracer = field(default_factory=RecordingTracer)
    wakes: list[int] = field(default_factory=list)

    def request(self, parent: TaskSpec, args: dict[str, Any]) -> SpawnDecision:
        return asyncio.run(self.broker.request(parent, args))


def _harness(
    env: BbEnv,
    depth: str = "standard",
    *,
    knobs: DepthKnobs | None = None,
    settings: SwarmSettings | None = None,
    tokens_cap: int = 10_000_000,
) -> Harness:
    budget = RunBudget("analysis", tokens_cap, run_id=env.run_id)
    tracer = RecordingTracer(env.run_id)
    wakes: list[int] = []
    broker = SpawnBroker(
        run_id=env.run_id,
        depth_mode=depth,  # type: ignore[arg-type]
        knobs=knobs or _knobs(depth),
        settings=settings or SwarmSettings(),
        budget=budget,
        bb=env.bb,
        catalog=EntityCatalog(env.warehouse),
        tracer=tracer,
        wake=lambda: wakes.append(1),
        clock=lambda: T0,
    )
    return Harness(env, broker, budget, tracer, wakes)


def _counter(approved: str, reason: str) -> float:
    key = (METRIC, (("approved", approved), ("reason", reason)), "harness")
    return process_state().metric_buffer.counters.get(key, 0.0)


# --- UT06-46 denial reasons in rule order -------------------------------------------------


def test_ut06_46_denials_in_rule_order(bb_env: BbEnv, reset_process_state: ProcessState) -> None:
    """UT06-46 each step fails its rule AND every later rule; relaxing one at a time walks §5.5.

    So a request that fails several rules must report the first in design order: swapping two
    rules or moving a check (e.g. the catalog check after the dedup lookup) turns this red.
    """
    del reset_process_state
    narrow = ["describe_table", "post_finding", "run_sql"]  # crosscheck-like: rule 8 fails
    template = _parent(bb_env, tools=narrow)
    dups = {}
    for ids in (["t2"], ["zz"]):  # rule 7 fails for the good scope and for the unknown one
        scope = EntityScope(entity_type="team", entity_ids=ids)
        key = compute_dedup_key("analyst", "ops", scope, _args()["objective"])
        dup = template.model_copy(update={"task_id": new_id(IdKind.TASK), "dedup_key": key})
        _insert(dup)
        dups[ids[0]] = dup.task_id
    deep_parent = _parent(bb_env, depth=1, tools=narrow)  # rule 2 fails in standard
    parent = _parent(bb_env, tools=narrow)
    assert count_tasks(bb_env.run_id, roles={"analyst", "skeptic"}) > 1
    no_children = SwarmSettings(max_children_per_task=0)  # rule 3 fails (0 >= 0)
    many = SwarmSettings(max_children_per_task=3)
    one_task = {"max_tasks_per_run": 1}  # rule 4 fails (the run already has more)
    zz = _args(ids=["zz"])  # rule 6 fails; its dedup key also exists (rule 7)
    steps: list[tuple[Harness, TaskSpec, dict[str, Any], str]] = [
        (_harness(bb_env, "fast", knobs=_knobs("fast", **one_task), settings=no_children,
                  tokens_cap=1), deep_parent, zz, "spawn_disabled"),
        (_harness(bb_env, knobs=_knobs("standard", **one_task), settings=no_children,
                  tokens_cap=1), deep_parent, zz, "max_depth"),
        (_harness(bb_env, knobs=_knobs("standard", **one_task), settings=no_children,
                  tokens_cap=1), parent, zz, "max_children"),
        (_harness(bb_env, knobs=_knobs("standard", **one_task), settings=many, tokens_cap=1),
         parent, zz, "max_tasks"),
        (_harness(bb_env, settings=many, tokens_cap=1), parent, zz, "budget"),
        (_harness(bb_env, settings=many), parent, zz, "bad_scope"),
        (_harness(bb_env, settings=many), parent, _args(), f"duplicate:{dups['t2']}"),
        (_harness(bb_env, settings=many), parent, _args("new objective"), "tool_escalation"),
    ]  # fmt: skip
    reasons = []
    for harness, par, request, expected in steps:
        decision = harness.request(par, request)
        assert decision == SpawnDecision(approved=False, reason=expected, task_id=None)
        assert harness.wakes == []
        reasons.append(decision.reason)
    assert reasons == [s[3] for s in steps]
    for rule in ("spawn_disabled", "max_depth", "max_children", "max_tasks", "budget",
                 "bad_scope", "duplicate", "tool_escalation"):  # fmt: skip
        assert _counter("false", rule) == 1.0
    full = _parent(bb_env)
    assert _harness(bb_env, settings=many).request(full, _args("new objective")).approved


@pytest.mark.parametrize(
    "request_args",
    [
        {**_args(), "entity_ids": "t2"},
        {**_args(), "entity_ids": ["t2", "t2"]},
        {**_args(), "entity_ids": [f"t{i}" for i in range(51)]},
        {**_args(), "entity_type": "galaxy"},
        {**_args(), "specialty": "astrology"},
        {**_args(), "objective": None},
        {**_args(), "objective": ""},
        {**_args(), "objective": "o" * 2_001},
    ],
)
def test_ut06_46_malformed_arguments_are_bad_scope(
    bb_env: BbEnv, request_args: dict[str, Any]
) -> None:
    """UT06-46 arguments outside the schema are a bad_scope denial, never an exception."""
    decision = _harness(bb_env).request(_parent(bb_env), request_args)
    assert decision.reason == "bad_scope"


def test_ut06_46_lost_race_is_duplicate(bb_env: BbEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT06-46 insert skipped by the (run_id, dedup_key) index -> duplicate:<existing id>."""
    h = _harness(bb_env)
    parent = _parent(bb_env)
    first = h.request(parent, _args())
    real = spawn_mod.get_task_by_dedup
    calls: list[str] = []

    def racing(run_id: str, key: str) -> Any:
        calls.append(key)
        return None if len(calls) == 1 else real(run_id, key)  # the rule 7 lookup misses

    monkeypatch.setattr(spawn_mod, "get_task_by_dedup", racing)
    decision = h.request(parent, _args())
    assert decision.reason == f"duplicate:{first.task_id}"
    monkeypatch.setattr(spawn_mod, "get_task_by_dedup", lambda _r, _k: None)
    assert h.request(parent, _args()).reason == "duplicate"
    assert h.wakes == [1]


# --- UT06-47 approval ----------------------------------------------------------------------


def test_ut06_47_approved_child(bb_env: BbEnv, reset_process_state: ProcessState) -> None:
    """UT06-47 depth +1, budget x0.5, priority x0.9, pending row, trace, metric, wake."""
    del reset_process_state
    h = _harness(bb_env, "deep")
    parent = _parent(bb_env, round=2, priority=50.0)
    decision = h.request(parent, _args())
    assert decision.approved
    assert decision.reason is None
    row = get_task(str(decision.task_id))
    assert row is not None
    assert row.status == "pending"
    child = row.spec
    assert child.depth == parent.depth + 1
    assert child.parent_task_id == parent.task_id
    assert child.budget == TaskBudget(max_steps=10, max_tokens=40_000, wall_clock_s=300)
    assert child.budget == parent.budget.scaled(0.5)
    assert child.priority == pytest.approx(45.0)
    assert (child.role, child.model_role, child.specialty) == ("analyst", "analyst", "ops")
    assert (child.round, child.k_samples) == (2, 1)
    assert child.inputs.notes == MODEL_TEXT
    assert child.scope == EntityScope(entity_type="team", entity_ids=["t2"])
    assert set(child.tools) <= set(parent.tools)
    assert "request_subtask" in child.tools  # deep: depth 1 < max_spawn_depth 2
    assert h.wakes == [1]
    [(kind, _span, fields)] = h.tracer.events
    assert kind == "spawn_decision"
    assert fields == {
        "task_id": parent.task_id,
        "parent_task_id": parent.task_id,
        "approved": True,
        "reason": None,
        "child_task_id": child.task_id,
        "depth": 1,
        "dedup_key": child.dedup_key,
        "specialty": "ops",
        "entity_type": "team",
        "n_entities": 1,
    }
    assert _counter("true", "none") == 1.0


def test_ut06_47_denial_trace_has_structured_values_only(bb_env: BbEnv) -> None:
    """UT06-47 a denial trace carries the rule and ids, never the model's text."""
    h = _harness(bb_env)
    bad = {**_args(), "specialty": "not-a-specialty", "entity_type": "x", "entity_ids": 3}
    h.request(_parent(bb_env), bad)
    [(_kind, _span, fields)] = h.tracer.events
    assert fields["reason"] == "bad_scope"
    assert (fields["specialty"], fields["entity_type"], fields["n_entities"]) == (None, None, 0)
    assert MODEL_TEXT not in repr(fields)
    assert _args()["objective"] not in repr(fields)


# --- ST06-02 no tool escalation --------------------------------------------------------------


def test_st06_02_crosscheck_parent_cannot_spawn_general_child(bb_env: BbEnv) -> None:
    """ST06-02 a crosscheck parent asking for a general child is denied tool_escalation."""
    crosscheck = default_tools("analyst", "crosscheck", "deep", child_depth=0, knobs=_knobs("deep"))
    parent = _parent(bb_env, specialty="crosscheck", tools=crosscheck)
    h = _harness(bb_env, "deep")
    decision = h.request(parent, {**_args(), "specialty": "general"})
    assert decision == SpawnDecision(approved=False, reason="tool_escalation", task_id=None)
    assert count_tasks(bb_env.run_id, parent_task_id=parent.task_id) == 0
    assert h.wakes == []


def test_st06_02_planned_task_with_tools_is_rejected() -> None:
    """ST06-02 a planner item carrying a tools (or budget) field fails validation."""
    item = {
        "dedup_key": None,
        "specialty": "ops",
        "objective": "o",
        "entity_type": "team",
        "entity_ids": ["t1"],
        "notes": None,
    }
    PlannedTask.model_validate(item)
    for extra in ({"tools": ["run_sql", "request_subtask"]}, {"budget": {"max_steps": 200}}):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            PlannedTask.model_validate({**item, **extra})


# --- ST06-03 runaway spawning stops at the caps ----------------------------------------------


def test_st06_03_children_and_depth_caps(bb_env: BbEnv) -> None:
    """ST06-03 a parent looping on request_subtask gets 3 children; children cannot recurse."""
    h = _harness(bb_env)
    parent = _parent(bb_env)
    decisions = [h.request(parent, _args(f"loop {i}")) for i in range(10)]
    assert [d.approved for d in decisions] == [True] * 3 + [False] * 7
    assert {d.reason for d in decisions[3:]} == {"max_children"}
    assert count_tasks(bb_env.run_id, parent_task_id=parent.task_id) == 3
    child = get_task(str(decisions[0].task_id))
    assert child is not None
    assert h.request(child.spec, _args("grandchild")).reason == "max_depth"
    assert len(h.wakes) == 3


def test_st06_03_concurrent_requests_respect_caps(bb_env: BbEnv) -> None:
    """ST06-03 concurrent requests are serialized: exactly max_children approvals."""
    h = _harness(bb_env)
    parent = _parent(bb_env)

    async def burst() -> list[SpawnDecision]:
        return await asyncio.gather(*(h.broker.request(parent, _args(f"b{i}")) for i in range(8)))

    decisions = asyncio.run(burst())
    assert sum(d.approved for d in decisions) == 3
    assert count_tasks(bb_env.run_id, parent_task_id=parent.task_id) == 3


def test_st06_03_task_and_budget_caps_end_the_loop(bb_env: BbEnv) -> None:
    """ST06-03 run task cap and the phase budget stop spawning across parents."""
    knobs = _knobs("deep", max_tasks_per_run=6)
    h = _harness(bb_env, "deep", knobs=knobs)
    parents = [_parent(bb_env) for _ in range(3)]  # + the bb_env task = 4 analyst tasks
    decisions = [h.request(p, _args(f"{p.task_id} {i}")) for p in parents for i in range(3)]
    assert sum(d.approved for d in decisions) == 2
    assert {d.reason for d in decisions if not d.approved} == {"max_tasks"}
    assert count_tasks(bb_env.run_id, roles={"analyst", "skeptic"}) == 6
    poor = _harness(bb_env, "deep", tokens_cap=100_000)
    fresh = _parent(bb_env)
    poor.budget.charge(50_000, 20_000, Decimal(0))  # 30,000 left < child 40,000
    assert poor.request(fresh, _args("costly")).reason == "budget"
    assert poor.budget.snapshot()["tokens_used"] == 70_000  # the check charges nothing
