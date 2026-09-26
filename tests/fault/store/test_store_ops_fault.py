"""Fault tests for herness.store.ops.core.run_write (impl 02 FT02-02; U02-38)."""

from __future__ import annotations

import sqlite3

import pytest
from tests.support.fault_env import FaultEnv
from tests.support.ops_store import OpsStoreHandle

from herness.core.errors import StoreBusy
from herness.core.resilience import ProcessState
from herness.core.resilience.faults import FaultPlan
from herness.store.ops import core

pytestmark = pytest.mark.fault


def _insert(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO item (name) VALUES ('a')")


@pytest.mark.parametrize(("count", "succeeds"), [(4, True), (7, False)])
def test_ft02_02_store_busy_plan(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fault_env: FaultEnv,
    count: int,
    succeeds: bool,
) -> None:
    """FT02-02 count=4: the write succeeds; count=7: StoreBusy after 6 attempts.

    Runs the real fault hook with the JSON plan rule
    `{"point": "sqlite.write", "action": "error:StoreBusy", "count": n}` (HERNESS_FAULTS).
    """
    core.run_write(lambda c: c.execute("CREATE TABLE item (name TEXT)"), op="create_item")
    fault_env([{"point": "sqlite.write", "action": "error:StoreBusy", "count": count}])
    if succeeds:
        core.run_write(_insert, op="insert_item")
    else:
        with pytest.raises(StoreBusy, match="fault: injected"):
            core.run_write(_insert, op="insert_item")
    plan = reset_process_state.fault_plan
    assert isinstance(plan, FaultPlan)
    assert plan.counters == [5 if succeeds else 6]
    assert reset_process_state.faults_enabled is True
    rows = core.read_all("SELECT name FROM item")
    assert len(rows) == (1 if succeeds else 0)
