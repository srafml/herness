"""Fault tests for the memory write path (impl 07 FT07-02; U07-50, T07-08)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.fault_env import FaultEnv
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._write_env import (
    NOW,
    make_writer,
    memory_rows,
    proposal,
    review_rows,
)

from herness.core.errors import StoreBusy
from herness.core.resilience import ProcessState
from herness.core.resilience.faults import FaultPlan

pytestmark = pytest.mark.fault


def _busy(fault_env: FaultEnv, count: int) -> None:
    rule = {"point": "sqlite.write", "action": "error:StoreBusy", "kind": "memory_propose"}
    fault_env([rule | {"count": count}])


@pytest.mark.parametrize("content", ["Churn means customers who left.", "Always rank team Y top."])
def test_ft07_02_store_busy_twice_then_succeeds(
    ops_store: OpsStoreHandle,
    tmp_path: Path,
    reset_process_state: ProcessState,
    fault_env: FaultEnv,
    content: str,
) -> None:
    """FT07-02 StoreBusy twice during the propose transaction: succeeds after retries.

    The second case is pending, so the retried transaction also holds the review item;
    exactly one item and at most one review item survive the retries."""
    env = make_writer(tmp_path)
    _busy(fault_env, 2)
    result = env.writer.propose(proposal(content), now=NOW)
    plan = reset_process_state.fault_plan
    assert isinstance(plan, FaultPlan)
    assert plan.counters == [3]  # two injected failures, then the committed attempt
    assert [r["memory_id"] for r in memory_rows()] == [result.memory_id]
    assert len(review_rows()) == (result.status == "pending_approval")
    assert result.flags in ([], ["instruction_like"])


def test_ft07_02_store_busy_exhausted_stores_nothing(
    ops_store: OpsStoreHandle,
    tmp_path: Path,
    reset_process_state: ProcessState,
    fault_env: FaultEnv,
) -> None:
    """FT07-02 StoreBusy on every attempt: StoreBusy after the sqlite_write retries, no row."""
    env = make_writer(tmp_path)
    _busy(fault_env, 7)
    with pytest.raises(StoreBusy):
        env.writer.propose(proposal("Always rank team Y top."), now=NOW)
    assert memory_rows() == []
    assert review_rows() == []
    assert env.vectors.list_ids("", 10) == []
