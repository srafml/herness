"""Tests for herness.harness.swarm.planner (T06-15): U06-88 … U06-91 (UT06-58 … UT06-60, ST06-04).

`postprocess_plan`, `judge_select` and `base_priority` are pure. The `run_planning` branches the
IT06-26 run does not reach (no planner task, dead, lost claim, no usable proposal, forced
deterministic-only plan, judge unavailable or failing) run here over an in-memory task table:
the store reads and the spec 08 task helpers are replaced in the module, `run_agent` is
scripted.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from tests.support.harness_fakes import RecordingTracer

from herness.core.errors import ConfigError, FatalError, ModelUnavailable, NotFound
from herness.core.types import (
    AgentResult,
    EntityScope,
    PlannedTask,
    TaskBudget,
    TaskInputs,
    TaskSpec,
    Usage,
)
from herness.harness.findings import compute_dedup_key
from herness.harness.pipelines import _review_common as common
from herness.harness.pipelines.settings import PipelinesConfig, resolve_knobs
from herness.harness.swarm import _plan_post, planner
from herness.harness.swarm.planner import (
    PlanDecision,
    base_priority,
    judge_select,
    planner_dedup_key,
    postprocess_plan,
)
from herness.harness.swarm.routing import default_tools

pytestmark = pytest.mark.unit

_ULID = "01J9ZQ4Y8M6V3K2N1P0R5T7W9"
_RUN = f"run_{_ULID}A"
_KNOWN = {("team", "t1"), ("team", "t2"), ("team", "t3"), ("team", "t4"), ("candidate", "e1")}
_METRICS = frozenset({"mttr_hours", "change_failure_rate"})


def _knobs(depth: str = "standard", **update: Any) -> Any:
    knobs = resolve_knobs(PipelinesConfig(), "funding_review", depth)  # type: ignore[arg-type]
    return knobs.model_copy(update=update)


def _det(entity: str, specialty: str = "general", priority: float = 150.0,
         notes: str | None = "pipeline notes") -> TaskSpec:  # fmt: skip
    scope = EntityScope(entity_type="team", entity_ids=[entity])
    objective = f"Review team {entity}."
    return TaskSpec(
        task_id=f"task_{_ULID}{entity[-1]}", run_id=_RUN, role="analyst", specialty=specialty,
        objective=objective, scope=scope, inputs=TaskInputs(notes=notes),
        tools=["run_sql"], budget=TaskBudget(max_steps=5, max_tokens=5_000, wall_clock_s=60),
        priority=priority, model_role="analyst", must_cover=True,
        dedup_key=compute_dedup_key("analyst", specialty, scope, objective),
    )  # fmt: skip


def _item(objective: str, *, ids: tuple[str, ...] = ("t3",), specialty: str = "ops",
          key: str | None = None, notes: str | None = None, entity_type: str = "team",
          ) -> PlannedTask:  # fmt: skip
    return PlannedTask.model_validate(
        {
            "dedup_key": key,
            "specialty": specialty,
            "objective": objective,
            "entity_type": entity_type,
            "entity_ids": list(ids),
            "notes": notes,
        }
    )


def _missing(entity_type: str, ids: list[str]) -> set[str]:
    return {i for i in ids if (entity_type, i) not in _KNOWN}


def _post(det: list[TaskSpec], items: list[PlannedTask], knobs: Any = None,
          ) -> tuple[list[TaskSpec], list[PlanDecision]]:  # fmt: skip
    counter = iter(range(10, 99))
    return postprocess_plan(
        det, items, knobs=knobs or _knobs(), depth_mode="standard", run_id=_RUN,
        entity_missing=_missing, metric_names=_METRICS,  # type: ignore[arg-type]
        new_task_id=lambda: f"task_{_ULID[:-2]}{next(counter)}X",
    )  # fmt: skip


# --- UT06-58 / ST06-04


def test_ut06_58_notes_only_on_deterministic_and_cut_to_400() -> None:
    """UT06-58 a planned item with a deterministic dedup key sets only its notes (<= 400);
    a second item with the same key is a hypothesis, not a second framing."""
    det = [_det("t1"), _det("t2")]
    long = "n" * 900
    items = [_item("ignored", ids=("t9",), key=det[0].dedup_key, notes=long, specialty="org"),
             _item("ignored again", key=det[0].dedup_key, notes="second")]  # fmt: skip
    plan, decisions = _post(det, items)
    assert plan[0].inputs.notes == "n" * 400
    assert plan[0].model_dump(exclude={"inputs"}) == det[0].model_dump(exclude={"inputs"})
    assert plan[1] == det[1]
    assert [(d.action, d.reason) for d in decisions] == [
        ("notes_applied", ""),
        ("dropped", "no_metric_or_table"),
    ]
    assert decisions[0] == PlanDecision("notes_applied", "", det[0].dedup_key, "org", "team", 1)


def test_ut06_58_empty_notes_leave_the_deterministic_task_unchanged() -> None:
    """UT06-58 a framing item with no notes is recorded but changes nothing."""
    det = [_det("t1")]
    plan, decisions = _post(det, [_item("x", key=det[0].dedup_key, notes=None)])
    assert plan == det
    assert decisions[0].action == "notes_applied"


def test_ut06_58_each_drop_reason_in_order() -> None:
    """UT06-58 over_limit, unknown_entity, no_metric_or_table, already_covered, duplicate_key."""
    det = [_det("t1", specialty="ops")]
    kept_obj = "Check mttr_hours of team t3."
    items = [
        _item("MTTR_Hours trend for t9", ids=("t9",)),  # unknown entity
        _item("Look at team t3 generally"),  # no metric or table
        _item("mttr_hours_extra of t3"),  # whole-word only: no metric
        _item("Incidents in core.incident for t1", ids=("t1",)),  # covered by deterministic
        _item(kept_obj),  # kept
        _item(kept_obj, ids=("t3",), specialty="delivery"),  # same objective other specialty
        _item("Change_Failure_Rate of t4", ids=("t4",), specialty="change"),  # kept (case)
        _item("score.funding for e1", ids=("e1",), entity_type="candidate"),  # over limit
    ]
    plan, decisions = _post(det, items, _knobs(H_wildcards=3))
    reasons = [d.reason or d.action for d in decisions]
    assert reasons == ["unknown_entity", "no_metric_or_table", "no_metric_or_table",
                       "already_covered", "kept", "kept", "kept", "over_limit"]  # fmt: skip
    assert [t.objective for t in plan[1:]] == [kept_obj, kept_obj, "Change_Failure_Rate of t4"]


def test_ut06_58_duplicate_key_and_covered_by_kept() -> None:
    """UT06-58 a hypothesis repeating a kept one is `already_covered`; one whose key equals
    an existing key but covers new triples is `duplicate_key`."""
    det = [_det("t1")]
    first = _item("Check mttr_hours of team t3.", ids=("t3",))
    _plan, decisions = _post(det, [first, first])
    assert [d.reason for d in decisions] == ["", "already_covered"]
    st = _plan_post._Post(tasks=[], by_key={}, framed=set(), covered=set(), keys={"k"}, kept=[])
    reason = _plan_post._drop_reason(first, "k", st, 3, _missing, _METRICS)
    assert reason == "duplicate_key"


def test_ut06_58_cap_h_wildcards_zero_keeps_no_hypothesis() -> None:
    """UT06-58 `H_wildcards = 0`: every hypothesis is dropped `over_limit`."""
    plan, decisions = _post([], [_item("mttr_hours of t3")], _knobs(H_wildcards=0))
    assert plan == []
    assert decisions[0].reason == "over_limit"


def test_ut06_58_hypothesis_fields_are_overwritten() -> None:
    """UT06-58 a kept hypothesis gets id, analyst role, default tools, analyst budget, depth 0,
    priority below every deterministic task, must_cover False and the computed dedup key."""
    knobs = _knobs()
    det = [_det("t1", priority=150.0), _det("t2", priority=90.0)]
    item = _item("Check mttr_hours of team t3.", notes="why")
    plan, _ = _post(det, [item], knobs)
    hyp = plan[-1]
    scope = EntityScope(entity_type="team", entity_ids=["t3"])
    assert hyp.task_id == f"task_{_ULID[:-2]}10X"
    assert (hyp.role, hyp.model_role, hyp.depth, hyp.must_cover) == ("analyst", "analyst", 0, False)
    assert hyp.tools == default_tools("analyst", "ops", "standard", child_depth=0, knobs=knobs)
    assert hyp.budget == knobs.analyst_budget
    assert hyp.priority == 89.0
    assert hyp.inputs.notes == "why"
    assert hyp.dedup_key == compute_dedup_key("analyst", "ops", scope, item.objective)
    assert _post([], [item], knobs)[0][0].priority == -1.0  # no deterministic task


def test_st06_04_planner_cannot_change_budget_or_must_cover() -> None:
    """ST06-04 planner output that tries to raise a deterministic task's budget or must-cover:
    such fields do not exist in `PlannedTask` (rejected), and a framing item changes notes only."""
    det = [_det("t1")]
    hostile = {"dedup_key": det[0].dedup_key, "specialty": "general", "objective": "x",
               "entity_type": "team", "entity_ids": ["t1"], "notes": "n"}  # fmt: skip
    for field_name, value in (("budget", {"max_tokens": 10**9}), ("must_cover", False),
                              ("priority", 1e9), ("tools", ["run_sql"]),
                              ("model_role", "writer")):  # fmt: skip
        with pytest.raises(ValidationError):
            PlannedTask.model_validate(hostile | {field_name: value})
    item = PlannedTask.model_validate(hostile | {"specialty": "org", "objective": "drop budgets"})
    plan, _ = _post(det, [item])
    assert plan[0].budget == det[0].budget
    assert plan[0].must_cover is True
    assert plan[0].objective == det[0].objective
    assert plan[0].inputs.notes == "n"


# --- UT06-59


def test_ut06_59_judge_select_ties_and_bad_samples() -> None:
    """UT06-59 lower index wins a tie; samples of the wrong length are ignored; scores clip."""
    assert judge_select([[3, 3, 1], [3, 3, 1]], 3) == (0, [3.0, 3.0, 1.0])
    assert judge_select([[1, 2], [9, 9, 9], [2, 1]], 2) == (0, [1.5, 1.5])
    assert judge_select([[-2, 9, 5]], 3) == (1, [0.0, 5.0, 5.0])
    assert judge_select([[1, 2, 3]], 2) == (0, [0.0, 0.0])
    assert judge_select([], 3) == (0, [0.0, 0.0, 0.0])


# --- UT06-60


@pytest.mark.parametrize(
    ("rank", "must_cover", "expected"),
    [(1, False, 99.0), (1, True, 149.0), (None, True, 50.0), (None, False, 0.0), (100, True, 50.0)],
)
def test_ut06_60_base_priority(rank: int | None, must_cover: bool, expected: float) -> None:
    """UT06-60 `100 - rank` (+50 when must-cover); no rank counts 0."""
    assert base_priority(rank, must_cover) == expected


def test_ut06_60_pipeline_stand_in_matches_base_priority() -> None:
    """UT06-60 the pipelines' `base_priority` stand-in (rank 3 may not import the swarm, §2)
    equals the swarm function for every rank and must-cover value."""
    for rank in (None, *range(0, 130)):
        for must_cover in (False, True):
            assert common.base_priority(rank, must_cover) == base_priority(rank, must_cover)


# --- run_planning branches (U06-88)


@dataclass
class _Row:
    task_id: str
    status: str
    spec: TaskSpec
    checkpoint: dict[str, object] | None = None
    result: dict[str, object] | None = None
    last_error: dict[str, object] | None = None
    attempts: int = 0


@dataclass
class _Store:
    """In-memory task table behind the patched store reads and spec 08 task helpers."""

    rows: dict[str, _Row] = field(default_factory=dict)
    claimable: bool = True
    failed: list[tuple[str, str]] = field(default_factory=list)

    def by_key(self, run_id: str, key: str) -> _Row | None:
        del run_id
        return next((r for r in self.rows.values() if r.spec.dedup_key == key), None)

    def claim(self, task_id: str) -> bool:
        row = self.rows[task_id]
        if not self.claimable or row.status != "pending":
            return False
        row.status = "running"
        return True

    def fail(self, task_id: str, err: Exception, *, max_task_attempts: int) -> str:
        del max_task_attempts
        self.failed.append((task_id, type(err).__name__))
        self.rows[task_id].status = "pending"
        return "pending"

    def complete(self, task_id: str, result: dict[str, object], *, writes: Any = None) -> None:
        del writes
        row = self.rows[task_id]
        row.status, row.result = "done", result

    def checkpoint(self, task_id: str, key: str, value: dict[str, object]) -> None:
        self.rows[task_id].checkpoint = {"schema_version": 1, key: value}

    def insert(self, fn: Any, op: str) -> None:
        del op
        conn = SimpleNamespace()
        fn(conn)


class _Bb:
    async def run_on_writer(self, fn: Any) -> Any:
        return fn()


def _planner_spec(kind: str = "funding_review") -> TaskSpec:
    scope = EntityScope(entity_type="run", entity_ids=[_RUN])
    return TaskSpec(
        task_id=f"task_{_ULID}P", run_id=_RUN, role="planner",
        objective=f"Plan the {kind} review.", scope=scope, tools=[],
        budget=TaskBudget(max_steps=5, max_tokens=5_000, wall_clock_s=60), model_role="planner",
        dedup_key=planner_dedup_key(_RUN, kind),
    )  # fmt: skip


def _agent_result(output: dict[str, Any] | None, status: str = "completed") -> AgentResult:
    return AgentResult(
        status=status, output=output, steps=1,  # type: ignore[arg-type]
        stop_reason="final" if status == "completed" else "error",
        usage=Usage(input_tokens=10, output_tokens=5), cost_usd=Decimal("0.01"),
        query_ids=[], finding_ids=[],
    )  # fmt: skip


_PROPOSAL = {"tasks": [{"dedup_key": None, "specialty": "ops", "objective": "mttr_hours of t3",
                        "entity_type": "team", "entity_ids": ["t3"], "notes": None}],
             "rationale": "r", "unknowns": []}  # fmt: skip


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    st = _Store()
    st.rows["p"] = _Row(f"task_{_ULID}P", "pending", _planner_spec())
    st.rows = {row.task_id: row for row in st.rows.values()}
    monkeypatch.setattr(planner, "get_task_by_dedup", st.by_key)
    monkeypatch.setattr(planner, "claim_task", st.claim)
    monkeypatch.setattr(planner, "fail_task", st.fail)
    monkeypatch.setattr(planner, "complete_task", st.complete)
    monkeypatch.setattr(planner, "save_checkpoint", st.checkpoint)
    monkeypatch.setattr(planner, "run_write", st.insert)
    monkeypatch.setattr(planner, "metric_names", lambda: _METRICS)

    def insert(conn: Any, specs: list[TaskSpec], *, now: Any) -> list[str]:
        del conn, now
        for spec in specs:
            if st.by_key(_RUN, spec.dedup_key) is None:
                st.rows[spec.task_id] = _Row(spec.task_id, "pending", spec)
        return []

    monkeypatch.setattr(planner, "insert_tasks", insert)
    route = SimpleNamespace(role_spec=None, client=None, config=None)
    monkeypatch.setattr(planner, "route_task", lambda *a, **kw: route)
    monkeypatch.setattr(planner, "build_tool_context", lambda *a, **kw: None)
    monkeypatch.setattr(planner, "build_hooks", lambda *a, **kw: None)
    return st


def _env(depth: str = "standard", *, force: bool = False, plan_ctx: object = "ctx",
         **knob_update: Any) -> Any:  # fmt: skip
    pipeline = SimpleNamespace(
        deterministic_tasks=lambda ctx: [_det("t1")], planner_input=lambda ctx, tasks: {"k": 1}
    )
    return SimpleNamespace(
        run=SimpleNamespace(run_id=_RUN, kind="funding_review", depth=depth, profile="local"),
        knobs=_knobs(depth, **knob_update), cfg=PipelinesConfig(), bb=_Bb(),
        catalog=SimpleNamespace(missing=_missing), pipeline=pipeline, tracer=RecordingTracer(),
        clock=lambda: None, force=force, plan_ctx=plan_ctx, llms=None, analysis=None,
        by_role={}, by_role_lock=threading.Lock(),
    )  # fmt: skip


def _script(monkeypatch: pytest.MonkeyPatch, *results: Any) -> list[str]:
    queue = list(results)
    roles: list[str] = []

    async def fake(role: Any, task_input: Any, ctx: Any, *a: Any, **kw: Any) -> AgentResult:
        del role, ctx, a, kw
        roles.append("judge" if "criteria" in task_input else "planner")
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt  # type: ignore[no-any-return]

    monkeypatch.setattr(planner, "run_agent", fake)
    return roles


def _go(env: Any) -> str:
    return asyncio.run(planner.run_planning(env))


def test_ut06_58_run_planning_status_branches(store: _Store) -> None:
    """U06-88 step 1-2: no plan context or planner task raises; done → planned; dead → failed;
    a lost claim → planned without running anything."""
    with pytest.raises(ConfigError):
        _go(_env(plan_ctx=None))
    row = next(iter(store.rows.values()))
    for status, expected in (("done", "planned"), ("dead", "failed")):
        row.status = status
        assert _go(_env()) == expected
    row.status = "pending"
    store.claimable = False
    assert _go(_env()) == "planned"
    assert row.status == "pending"
    store.rows.clear()
    with pytest.raises(NotFound):
        _go(_env())


def test_ut06_58_no_usable_proposal_fails_the_planner(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U06-88 step 5: failed, invalid and raising samples leave no proposal: fail_task with
    ModelUnavailable, outcome planned (the loop retries until dead)."""
    _script(monkeypatch, _agent_result(None, "failed"))
    assert _go(_env()) == "planned"
    _script(monkeypatch, _agent_result({"tasks": []}))
    assert _go(_env()) == "planned"
    _script(monkeypatch, FatalError("boom"))
    assert _go(_env()) == "planned"
    assert [name for _, name in store.failed] == [ModelUnavailable.__name__] * 3


def test_ut06_58_single_sample_commits_without_judge(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U06-88 N == 1: choice 0, no judge task; the plan is committed and decisions traced."""
    roles = _script(monkeypatch, _agent_result(_PROPOSAL, "partial"))
    env = _env()
    assert _go(env) == "planned"
    assert roles == ["planner"]
    (planner_row,) = store.rows.values()
    assert planner_row.status == "done"
    assert planner_row.result is not None
    assert [t["objective"] for t in planner_row.result["plan"]] == [  # type: ignore[index, union-attr]
        "Review team t1.",
        "mttr_hours of t3",
    ]
    assert planner_row.result["tokens"] == {"input": 10, "output": 5}
    assert env.by_role["planner"] == {"input": 10, "output": 5}
    assert [e[0] for e in env.tracer.events] == ["plan_decision"]


def test_ut06_58_forced_resume_plans_deterministic_only(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U06-88 step 4: `force` on a planner that failed before with `H_wildcards == 0` commits
    the deterministic tasks without calling a model."""
    roles = _script(monkeypatch)
    row = next(iter(store.rows.values()))
    row.last_error = {"class": "ModelUnavailable"}
    assert _go(_env(force=True, H_wildcards=0)) == "planned"
    assert roles == []
    assert row.result is not None
    assert len(row.result["plan"]) == 1  # type: ignore[arg-type]


def test_ut06_59_judge_failures_choose_zero(store: _Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """U06-88 step 6: every judge sample failing → choice 0 (WARNING), judge still completed;
    a judge task that cannot be claimed → choice 0; a done judge's choice is reused."""
    proposals = [_agent_result(_PROPOSAL | {"rationale": f"r{i}"}) for i in range(3)]
    bad = [
        _agent_result({"choice": 0, "scores": [1.0]}), FatalError("x"),
        _agent_result(None, "failed"), _agent_result({"choice": 9}), FatalError("y"),
    ]  # fmt: skip
    roles = _script(monkeypatch, *proposals, *bad)
    assert _go(_env("deep")) == "planned"
    assert roles == ["planner"] * 3 + ["judge"] * 5
    judge = next(r for r in store.rows.values() if r.spec.role == "judge")
    assert judge.result is not None
    assert (judge.result["choice"], judge.result["scores"]) == (0, [0.0, 0.0, 0.0])
    planner_row = next(r for r in store.rows.values() if r.spec.role == "planner")
    assert planner_row.result is not None
    assert planner_row.result["summary"] == "r0"

    # stored proposals + a judge that cannot be claimed: no model call at all
    planner_row.status, planner_row.result = "pending", None
    judge.status = "running"
    roles = _script(monkeypatch)
    assert _go(_env("deep")) == "planned"
    assert roles == []
    assert planner_row.result is not None
    assert planner_row.result["summary"] == "r0"

    # a done judge with choice 2 is reused
    planner_row.status = "pending"
    judge.status, judge.result = "done", {"choice": 2}
    assert _go(_env("deep")) == "planned"
    assert planner_row.result["summary"] == "r2"


def test_ut06_58_planner_dedup_key_matches_run_creation() -> None:
    """U06-88 step 1 finds the planner by the dedup key `create_run_record` writes."""
    from herness.harness.swarm.lifecycle import _planner_spec  # noqa: PLC0415 - test only

    spec = _planner_spec(_RUN, "org_review", "standard", _knobs())
    assert spec.dedup_key == planner_dedup_key(_RUN, "org_review")
