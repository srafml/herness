"""Fault tests for herness.store.ops.core.run_write (impl 02 FT02-02; U02-38)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from herness.core.errors import StoreBusy
from herness.core.resilience import ProcessState
from herness.store.ops import _shims, core

pytestmark = pytest.mark.fault


class _BusyPlan:
    """Counting stand-in for the plan rule `sqlite.write error:StoreBusy count=<n>`."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, name: str, **labels: str) -> None:
        self.calls.append((name, labels))
        if name == "sqlite.write" and len(self.calls) <= self.count:
            msg = "injected"
            raise StoreBusy(msg)


def _insert(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO item (name) VALUES ('a')")


@pytest.mark.parametrize(("count", "succeeds"), [(4, True), (7, False)])
def test_ft02_02_store_busy_plan(
    ops_store: Path,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    succeeds: bool,
) -> None:
    """FT02-02 count=4: the write succeeds; count=7: StoreBusy after 6 attempts.

    Runs under a monkeypatched fault_point shim; re-point to the `fault_plan` fixture
    (HERNESS_FAULTS plan) when T08-08 lands.
    """
    core.run_write(lambda c: c.execute("CREATE TABLE item (name TEXT)"), op="create_item")
    plan = _BusyPlan(count)
    monkeypatch.setattr(_shims, "fault_point", plan)
    if succeeds:
        core.run_write(_insert, op="insert_item")
        assert len(plan.calls) == 5
    else:
        with pytest.raises(StoreBusy):
            core.run_write(_insert, op="insert_item")
        assert len(plan.calls) == 6
    assert plan.calls[0] == ("sqlite.write", {"kind": "insert_item"})
    rows = core.read_all("SELECT name FROM item")
    assert len(rows) == (1 if succeeds else 0)
