"""UT06-61, UT06-62: budget admission and in-flight caps of the phase scheduler (U06-94, U06-95)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from herness.core.errors import BudgetExceeded
from herness.core.ids import IdKind, new_id
from herness.core.types import EntityScope, TaskBudget, TaskSpec
from herness.harness.budget import RunBudget
from herness.harness.findings import compute_dedup_key
from herness.harness.swarm.scheduler import CapTracker, PhaseOutcome, budget_admits
from herness.store.ops.runs import TaskRow

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
RUN_ID = new_id(IdKind.RUN)


def _spec(
    role: str = "analyst",
    *,
    max_tokens: int = 80_000,
    ids: tuple[str, ...] = ("t1",),
    parent: str | None = None,
) -> TaskSpec:
    scope = EntityScope(entity_type="team", entity_ids=list(ids))
    task_id = new_id(IdKind.TASK)
    return TaskSpec(
        task_id=task_id,
        run_id=RUN_ID,
        role=role,  # type: ignore[arg-type]
        objective=f"objective {task_id}",
        scope=scope,
        tools=[],
        budget=TaskBudget(max_steps=5, max_tokens=max_tokens, wall_clock_s=60),
        depth=0 if parent is None else 1,
        parent_task_id=parent,
        model_role=role,
        dedup_key=compute_dedup_key(role, "general", scope, task_id),
    )


def _row(spec: TaskSpec) -> TaskRow:
    return TaskRow(
        task_id=spec.task_id, run_id=spec.run_id, parent_task_id=spec.parent_task_id,
        role=spec.role, spec=spec, status="running", attempts=1, last_error=None,
        checkpoint=None, result=None, created_at=NOW, updated_at=NOW,
    )  # fmt: skip


def _ledger(remaining: int, *, cap: int = 100_000) -> RunBudget:
    ledger = RunBudget("analysis", cap, run_id=RUN_ID)
    ledger.charge(cap - remaining, 0, ledger.cost_used)
    assert ledger.snapshot()["tokens_remaining"] == remaining
    return ledger


# --- UT06-61


def test_ut06_61_admission_needs_a_fraction_of_the_task_budget() -> None:
    """UT06-61 remaining 10k, task 80k: refused at admit_fraction 0.25; admitted at 20k."""
    spec = _spec(max_tokens=80_000)
    assert budget_admits(_ledger(10_000), spec, 0.25) is False
    assert budget_admits(_ledger(20_000), spec, 0.25) is True
    assert budget_admits(_ledger(19_999), spec, 0.25) is False


def test_ut06_61_exhausted_ledger_refuses_and_verifier_always_passes() -> None:
    """UT06-61 an exhausted ledger admits no model task; a verifier task always passes."""
    ledger = RunBudget("analysis", 1_000, run_id=RUN_ID)
    with pytest.raises(BudgetExceeded):
        ledger.charge(1_000, 0, ledger.cost_used)
    assert ledger.exhausted
    assert budget_admits(ledger, _spec(max_tokens=1_000), 0.0) is False
    assert budget_admits(ledger, _spec("verifier", max_tokens=1_000), 0.25) is True


# --- UT06-62


def test_ut06_62_third_child_refused_while_two_run() -> None:
    """UT06-62 three children of one parent: the third is refused while two run."""
    caps = CapTracker(max_children_per_parent=2, max_per_entity=2)
    parent = new_id(IdKind.TASK)
    kids = [_row(_spec(ids=(f"t{i}",), parent=parent)) for i in range(3)]
    for kid in kids[:2]:
        assert caps.can_start(kid)
        caps.start(kid)
    assert caps.can_start(kids[2]) is False
    caps.finish(kids[0])
    assert caps.can_start(kids[2]) is True


def test_ut06_62_entity_cap_counts_every_scope_id() -> None:
    """UT06-62 per-entity cap: a task touching an entity at its cap is refused until one ends."""
    caps = CapTracker(max_children_per_parent=2, max_per_entity=2)
    a, b = _row(_spec(ids=("t1", "t2"))), _row(_spec(ids=("t1",)))
    c = _row(_spec(ids=("t3", "t1")))
    caps.start(a)
    caps.start(b)
    assert caps.can_start(c) is False
    assert caps.can_start(_row(_spec(ids=("t2",)))) is True
    caps.finish(b)
    assert caps.can_start(c) is True
    caps.finish(a)
    caps.start(c)
    assert caps.can_start(_row(_spec(ids=("t1",))))


def test_ut06_62_verifier_tasks_are_exempt() -> None:
    """UT06-62 verifier tasks bypass and never consume the caps."""
    caps = CapTracker(max_children_per_parent=1, max_per_entity=1)
    parent = new_id(IdKind.TASK)
    first = _row(_spec(ids=("t1",), parent=parent))
    caps.start(first)
    verifier = _row(_spec("verifier", ids=("t1",), parent=parent))
    assert caps.can_start(verifier)
    caps.start(verifier)
    caps.finish(verifier)
    assert caps.can_start(_row(_spec(ids=("t1",)))) is False
    caps.finish(first)
    assert caps.can_start(_row(_spec(ids=("t1",), parent=parent)))


def test_ut06_62_phase_outcome_is_frozen() -> None:
    """UT06-62 `PhaseOutcome` is an immutable value."""
    out = PhaseOutcome(blocked=frozenset({"task_x"}), stopped=False)
    with pytest.raises(AttributeError):
        out.stopped = True  # type: ignore[misc]
    assert out == PhaseOutcome(frozenset({"task_x"}), False)
