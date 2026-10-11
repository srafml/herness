"""Fault row FT07-03 (impl 07 F07-06, U07-78; T07-24): a kill inside the
`write_recommendations` transaction.

A child process (`tests.support._f07_children recs`) runs a standard funding review through
the T06-22 run driver (scripted agents, real run steps, Blackboard, findings and drafts) with
the real spec 07 `MemoryStore` on the parent's ops store, and dies in the record step's
`write_recommendations` call: by the JSON fault plan `sqlite.write kill
kind=write_recommendations` (the named point of that transaction, R-40, through
`tests.support.faults.write_fault_plan`), or by an OS-level kill while the rows are inserted
and the `BEGIN IMMEDIATE` transaction is still open. The parent then resumes the run left in
`recording` (the rerun) with its own `MemoryStore` and calls `write_recommendations` once
more, which takes the "same ids" path of U07-78 step 6.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
import structlog
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.integration.memory._it07_env import (
    memory_store,
    record_portfolio,
    run_summaries,
    swarm_for,
)
from tests.support import faults
from tests.support._f07_children import IN_TX, SURVIVED
from tests.support.config_tree import REPO
from tests.support.sync_kill import KILLED_RETURNCODE
from tests.unit.harness._blackboard_env import test_redactor

from herness.harness.memory import MemoryStore
from herness.store import ops
from herness.store.ops import core, get_run, select_runs

pytestmark = pytest.mark.fault

drv = e.drv  # fixtures
__all__ = ["drv", "test_redactor"]

CHILD_TIMEOUT_S: Final = 300
RULE: Final = {"point": "sqlite.write", "action": "kill", "kind": "write_recommendations"}


class Recorder:
    """The real facade, recording each `write_recommendations` call and its result."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store
        self.calls: list[tuple[str, list[Any], list[str]]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def write_recommendations(self, run_id: str, drafts: list[Any]) -> list[str]:
        ids = self._store.write_recommendations(run_id, drafts)
        self.calls.append((run_id, list(drafts), ids))
        return ids


def _argv(drv: Drv, tmp_path: Path, mode: str) -> list[str]:
    return [sys.executable, "-m", "tests.support._f07_children", "recs", str(drv.cfg_root),
            str(drv.hcfg.paths.data), str(tmp_path / "mem"), mode]  # fmt: skip


def _env(extra: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ, PYTHONUTF8="1")
    env.pop("HERNESS_FAULTS", None)
    env.update(extra)
    return env


def _killed_in_recording() -> str:
    """Nothing of the record step was written: the run is `recording` with no
    recommendation, no `run_summary` and no memory item of its own."""
    [run] = select_runs(statuses={"recording"})
    assert ops.run_recommendations(run.run_id) == []
    assert run_summaries(run.run_id) == []
    assert core.read_all("SELECT memory_id FROM memory_item") == []
    return run.run_id


def _rerun(drv: Drv, tmp_path: Path, run_id: str) -> None:
    """Resume the run (the rerun inserts the rows once), then call `write_recommendations`
    again with the same drafts: the same ids come back from the existing rows."""
    memory = Recorder(memory_store(tmp_path, Path(drv.hcfg.paths.data)).store)
    res = asyncio.run(swarm_for(drv, memory).resume(run_id))
    assert res.status == "done"
    run = get_run(run_id)
    assert run is not None
    assert run.status == "done"
    [(called_run, drafts, ids)] = memory.calls
    assert called_run == run_id
    rows = ops.run_recommendations(run_id)
    assert sorted(r["rec_id"] for r in rows) == sorted(ids)
    assert len(run_summaries(run_id)) == 1
    with structlog.testing.capture_logs() as logs:
        again = memory.write_recommendations(run_id, drafts)
    assert again == ids  # the same-ids path
    assert ops.run_recommendations(run_id) == rows
    assert len(run_summaries(run_id)) == 1
    [written] = [x for x in logs if x["event"] == "memory.recommendations.written"]
    assert written["reused"] is True


def test_ft07_03_kill_at_write_recommendations_point_writes_nothing_rerun_same_ids(
    drv: Drv, tmp_path: Path
) -> None:
    """FT07-03 the fault plan kills the child at the `sqlite.write` point of the
    `write_recommendations` transaction: nothing is written; the rerun writes one set of
    rows and a further call returns the same ids."""
    record_portfolio()
    plan = faults.write_fault_plan(tmp_path, [RULE])
    out = subprocess.run(  # noqa: S603 - fixed interpreter and module
        _argv(drv, tmp_path, "plan"), cwd=REPO, env=_env(faults.fault_env(plan)),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=CHILD_TIMEOUT_S, check=False,
    )  # fmt: skip
    assert out.returncode == KILLED_RETURNCODE, out.stdout[-800:] + out.stderr[-1500:]
    assert SURVIVED not in out.stdout
    assert "resilience.faults.kill" in out.stdout + out.stderr
    _rerun(drv, tmp_path, _killed_in_recording())


def test_ft07_03_os_kill_inside_open_transaction_rolls_back_rerun_same_ids(
    drv: Drv, tmp_path: Path
) -> None:
    """FT07-03 the child is killed (OS level) after its recommendation rows were inserted
    inside the open `write_recommendations` transaction: nothing is written; the rerun writes
    one set of rows and a further call returns the same ids."""
    record_portfolio()
    errors = (tmp_path / "child_stderr.txt").open("w", encoding="utf-8")
    proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and module
        _argv(drv, tmp_path, "hold"), cwd=REPO, env=_env({}), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8", errors="replace",
    )  # fmt: skip
    try:
        assert proc.stdout is not None
        held = next((line for line in proc.stdout if line.startswith(IN_TX)), None)
        assert held is not None, (tmp_path / "child_stderr.txt").read_text("utf-8")[-1500:]
        proc.kill()  # inside the transaction: rows inserted, no COMMIT
    finally:
        proc.wait(timeout=CHILD_TIMEOUT_S)
        errors.close()
    assert proc.returncode != 0
    run_id = held[len(IN_TX) :].strip()
    assert _killed_in_recording() == run_id
    _rerun(drv, tmp_path, run_id)
