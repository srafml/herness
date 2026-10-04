"""IT06-26: the planner step over the real store, writer, loop and scripted fake LLM (T06-15).

A migrated ops store with a deep `funding_review` run in `planning` and its pending planner task
(`tests.support.planner_env`). `run_planning` runs the real `run_agent` with real `HarnessHooks`
over spec 11's `FakeLLMClient` (script `it06_26_planner.yaml`): three planner samples, a judge
task with five samples, post-processing and the plan commit. The kill test runs the step in a
child process (`tests.support.planner_kill`), terminates it after the judge task completed
(an OS-level kill, R-40) and resumes in a second child: the planner and the judge are not
called again, the stored proposals and the stored judge choice make the plan.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.support import planner_env as pe
from tests.support import planner_kill as pk
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core.resilience import ProcessState
from herness.core.types import SwarmTaskState
from herness.harness.roles.base import get_role
from herness.harness.swarm import planner
from herness.harness.swarm.planner import JUDGE_OBJECTIVE, planner_dedup_key
from herness.store.ops import get_run, get_task_by_dedup, select_tasks

pytestmark = pytest.mark.integration

_CHILD_TIMEOUT_S = 120


@pytest.fixture
def planning(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Any, str]]:
    del ops_store, reset_process_state
    hcfg = pe.configure(tmp_path / "cfg", monkeypatch.setattr)
    yield hcfg, pe.create_run()
    c.reset_config()
    for name in ("planner", "judge"):
        get_role(name).__dict__.pop("_prompts", None)


def _judge_row(run_id: str) -> Any:
    rows = [t for t in select_tasks(run_id) if t.role == "judge"]
    assert len(rows) == 1
    assert rows[0].spec.objective == JUDGE_OBJECTIVE
    return rows[0]


def _assert_plan(run_id: str) -> None:
    """Deterministic task framed by proposal 2, then proposal 2's hypothesis; run `running`."""
    row = get_task_by_dedup(run_id, planner_dedup_key(run_id, "funding_review"))
    assert row is not None
    assert row.status == "done"
    assert row.result is not None
    assert row.result["summary"] == "rationale 2"
    analysts = [t for t in select_tasks(run_id) if t.role == "analyst"]
    by_key = {t.spec.dedup_key: t.spec for t in analysts}
    assert by_key[pe.DET_KEY].inputs.notes == "framing from proposal 2"
    assert by_key[pe.DET_KEY].must_cover is True
    (hyp,) = [s for s in by_key.values() if s.dedup_key != pe.DET_KEY]
    assert hyp.objective.startswith("Hypothesis 2:")
    assert (hyp.must_cover, hyp.priority) == (False, pe.DET_PRIORITY - 1)
    run = get_run(run_id)
    assert run is not None
    assert run.status == "running"


def test_it06_26_best_of_three_with_judge(planning: tuple[Any, str]) -> None:
    """IT06-26 deep best-of-3: three planner samples, proposals persisted in the checkpoint,
    a judge task with five samples whose choice (proposal 2) makes the plan; one
    `plan_decision` trace per planned item."""
    hcfg, run_id = planning
    client = pe.CountingClient(pe.SCRIPT)
    env, bb = pe.make_env(run_id, hcfg, client, setattr)
    try:
        assert asyncio.run(planner.run_planning(env)) == "planned"
    finally:
        bb.close()
    assert client.roles == {"planner": 6, "judge": 10}
    row = get_task_by_dedup(run_id, planner_dedup_key(run_id, "funding_review"))
    assert row is not None
    assert row.checkpoint is not None
    state = SwarmTaskState.from_envelope(row.checkpoint, task_id=row.task_id)
    assert state is not None
    assert state.proposals is not None
    assert [p["rationale"] for p in state.proposals] == [
        "rationale 0",
        "rationale 1",
        "rationale 2",
    ]
    judge = _judge_row(run_id)
    assert judge.status == "done"
    assert judge.result is not None
    assert judge.result["choice"] == 2
    assert judge.result["scores"] == [1.0, 2.0, 4.0]
    _assert_plan(run_id)
    decisions = [f for kind, _, f in env.tracer.events if kind == "plan_decision"]  # type: ignore[attr-defined]
    assert [(d["action"], d["reason"]) for d in decisions] == [("notes_applied", ""), ("kept", "")]
    assert asyncio.run(planner.run_planning(env)) == "planned"  # done: nothing runs again
    assert client.roles == {"planner": 6, "judge": 10}


def _child(args: list[str]) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env.pop("HERNESS_FAULTS", None)
    return subprocess.Popen(  # noqa: S603 - fixed argv of the test's own child module
        [sys.executable, "-m", "tests.support.planner_kill", *args],
        cwd=pe.ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8",
    )  # fmt: skip


def _wait_blocked(proc: subprocess.Popen[str]) -> bool:
    """Read the child's stdout (logs included) until the blocked line or end of output."""
    assert proc.stdout is not None
    return any(line.strip() == pk.BLOCKED_LINE for line in proc.stdout)


def test_it06_26_resume_after_kill_reuses_judge_choice(
    planning: tuple[Any, str], ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """IT06-26 acceptance: a worker killed after the judge task completed (before the plan
    commit) resumes without calling the planner or the judge again and commits the plan of
    the stored judge choice from the stored proposals."""
    _hcfg, run_id = planning
    db, cfg = str(ops_store.db_path), tmp_path / "child_cfg"
    first = _child([str(cfg / "first"), db, run_id, "block"])
    watchdog = threading.Timer(_CHILD_TIMEOUT_S, first.kill)  # never hang on a stuck child
    watchdog.start()
    try:
        blocked = _wait_blocked(first)
        first.kill()  # OS-level kill while blocked after the judge completed
        _out, err = first.communicate(timeout=_CHILD_TIMEOUT_S)
    finally:
        watchdog.cancel()
    assert blocked, err[-2000:]
    assert first.returncode != 0
    planner_row = get_task_by_dedup(run_id, planner_dedup_key(run_id, "funding_review"))
    assert planner_row is not None
    assert planner_row.status == "running"  # killed mid-step
    assert planner_row.checkpoint is not None
    state = SwarmTaskState.from_envelope(planner_row.checkpoint, task_id=planner_row.task_id)
    assert state is not None
    assert state.proposals is not None
    assert len(state.proposals) == 3  # proposals persisted before the judge ran
    judge = _judge_row(run_id)
    assert judge.status == "done"
    assert judge.result is not None
    assert judge.result["choice"] == 2
    assert [t.role for t in select_tasks(run_id) if t.role == "analyst"] == []  # no plan yet

    second = _child([str(cfg / "resume"), db, run_id, "resume"])
    out, err = second.communicate(timeout=_CHILD_TIMEOUT_S)
    assert second.returncode == 0, err[-2000:]
    (result_line,) = [ln for ln in out.splitlines() if ln.startswith(pk.RESULT_PREFIX)]
    result = json.loads(result_line.removeprefix(pk.RESULT_PREFIX))
    assert result == {"outcome": "planned", "calls": {}, "running_reset": 1}
    _assert_plan(run_id)
    assert _judge_row(run_id).attempts == 1  # the judge never ran again
