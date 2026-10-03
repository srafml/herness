"""Integration test IT05-12 (flow F05-06, checkpoint and resume after a crash) (T05-27).

A real child process (`tests.support.loop_kill`) runs one analyst task through the real loop
with `HarnessHooks`, a real spec 08 `ModelChain` (where the `llm.call` fault point fires) and
the real warehouse tools recording into the migrated ops store; every step checkpoints the
`loop` key through `herness.core.jobs.save_checkpoint`. A JSON fault plan (`HERNESS_ENV=test`)
kills the worker on the fourth `llm.call` hit, after step 3. The rerun without a plan resumes
from the checkpoint: no evidence use is duplicated, the envelope's `state` and `scratchpad`
keys are never touched by the loop's saves (R-21) and the task finishes `completed`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
from tests.support import warehouse_tools_build as wb
from tests.support.config_tree import write_full_config
from tests.support.loop_kill import RESULT_PREFIX
from tests.support.ops_store import OpsStoreHandle
from tests.support.sync_kill import KILLED_RETURNCODE

from herness.core import time as clock
from herness.core.ids import IdKind, canonical_json, new_id
from herness.core.jobs.tasks import CHECKPOINT_SCHEMA_VERSION
from herness.core.resilience import ProcessState
from herness.store.ops.core import read_all, read_one, run_write

pytestmark = pytest.mark.integration

REPO: Final = Path(__file__).resolve().parents[3]
SCRIPTS: Final = REPO / "tests" / "fixtures" / "llm_scripts"
CHILD_TIMEOUT_S: Final = 240
KILL_RULE: Final = {"point": "llm.call", "action": "kill", "nth": 4}
STATE: Final = {"phase": "analysis", "notes": ["kept by spec 06"]}
SCRATCHPAD: Final = {"plan": "count incidents, then by service and priority"}


def _insert_run_and_task() -> tuple[str, str]:
    """A running run and a running task whose envelope already holds `state`, `scratchpad`."""
    run_id, task_id = new_id(IdKind.RUN), new_id(IdKind.TASK)
    now = clock.format_utc(clock.now())
    envelope = {"schema_version": CHECKPOINT_SCHEMA_VERSION, "state": STATE}
    envelope["scratchpad"] = SCRATCHPAD

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at)"
            " VALUES (?, 'org_review', 'standard', 'local', 'h', 'running', ?)",
            (run_id, now),
        )
        conn.execute(
            "INSERT INTO task (task_id, run_id, role, spec, status, attempts, checkpoint,"
            " created_at, updated_at) VALUES (?, ?, 'analyst', ?, 'running', 1, ?, ?, ?)",
            (task_id, run_id, json.dumps({"dedup_key": task_id}), canonical_json(envelope),
             now, now),
        )  # fmt: skip

    run_write(insert, op="test_setup")
    return run_id, task_id


def _envelope(task_id: str) -> dict[str, Any]:
    row = read_one("SELECT checkpoint FROM task WHERE task_id = ?", (task_id,))
    assert row is not None
    envelope: dict[str, Any] = json.loads(row["checkpoint"])
    return envelope


def _uses() -> list[tuple[str, str, str | None]]:
    rows = read_all("SELECT query_id, run_id, task_id FROM evidence_use ORDER BY used_at", ())
    return [(row["query_id"], row["run_id"], row["task_id"]) for row in rows]


def _child(args: list[str], plan: Path | None, script: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HERNESS_ENV"] = "test"
    env.pop("HERNESS_FAULTS", None)
    if plan is not None:
        env["HERNESS_FAULTS"] = str(plan)
    env["PYTHONUTF8"] = "1"
    argv = [sys.executable, "-m", "tests.support.loop_kill", *args, str(SCRIPTS / script)]
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=CHILD_TIMEOUT_S, check=False,
    )  # fmt: skip


def _outcome(done: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert done.returncode == 0, done.stderr[-3000:]
    [line] = [x for x in done.stdout.splitlines() if x.startswith(RESULT_PREFIX)]
    outcome: dict[str, Any] = json.loads(line.removeprefix(RESULT_PREFIX))
    return outcome


def test_it05_12_kill_on_fourth_llm_call_then_resume_completes(
    tmp_path: Path, ops_store: OpsStoreHandle, reset_process_state: ProcessState
) -> None:
    """IT05-12 kill on the 4th `llm.call` hit (after step 3), `HERNESS_ENV=test`, then resume:
    the checkpoint holds step 3 and its three queries; the resumed run re-runs step 1's query
    and one new query without duplicating any evidence use; `state` and `scratchpad` stay as
    stored before the task ran; the task finishes `completed`."""
    del reset_process_state
    config_dir = write_full_config(tmp_path / "cfg")
    wb.make_build(tmp_path / "wh")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "_common.md").write_text("# Common rules\nCite every number.\n", encoding="utf-8")
    (prompts / "demo.md").write_text("# Demo analyst\nAnswer the task.\n", encoding="utf-8")
    run_id, task_id = _insert_run_and_task()
    plan = tmp_path / "kill_plan.json"
    plan.write_text(json.dumps([KILL_RULE]), encoding="utf-8")
    args = [str(config_dir), str(ops_store.db_path), str(tmp_path / "wh"), str(prompts)]
    args += [run_id, task_id]

    # 1. the killed run: three steps, then the kill inside the chain on the 4th llm.call
    killed = _child(args, plan, "it05_12_first_run.yaml")
    assert killed.returncode == KILLED_RETURNCODE, (killed.returncode, killed.stderr[-3000:])
    assert RESULT_PREFIX not in killed.stdout
    logs = killed.stdout + killed.stderr
    assert "resilience.faults.enabled" in logs
    assert "resilience.faults.kill" in logs
    envelope = _envelope(task_id)
    assert (envelope["state"], envelope["scratchpad"]) == (STATE, SCRATCHPAD)
    loop_cp = envelope["loop"]
    assert loop_cp["state"]["step"] == 3
    first_queries = loop_cp["query_ids"]
    assert len(first_queries) == 3
    assert _uses() == [(qid, run_id, task_id) for qid in first_queries]

    # 2. the resumed run (no plan): continues at step 3 and completes
    outcome = _outcome(_child(args, None, "it05_12_resume.yaml"))
    assert outcome["resumed_at"] == 3
    assert (outcome["status"], outcome["output"]) == ("completed", {"answer": "40 incidents"})
    assert outcome["query_ids"][:3] == first_queries
    assert len(outcome["query_ids"]) == len(set(outcome["query_ids"])) == 4
    uses = _uses()
    assert len(uses) == len({use[0] for use in uses}) == 4
    assert {use[0] for use in uses} == set(outcome["query_ids"])
    assert all(use[1:] == (run_id, task_id) for use in uses)
    evidence = read_all("SELECT query_id FROM evidence", ())
    assert {row["query_id"] for row in evidence} == set(outcome["query_ids"])
    final = _envelope(task_id)
    assert (final["state"], final["scratchpad"]) == (STATE, SCRATCHPAD)
    assert final["loop"]["state"]["step"] >= loop_cp["state"]["step"]
    assert set(final) == {"schema_version", "loop", "state", "scratchpad"}
