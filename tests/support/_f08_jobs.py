"""Job-side helpers of the impl 08 fault suite (T08-24: FT08-06, FT08-07, FT08-09, FT08-11).

`child` runs ``tests.support._f08_children`` as a real subprocess (list argv, no shell) and
waits for it to end (a kill ends it at the fault point); `killed` checks it died there. The
fault plan is handed to the child only: `child_env` snapshots the environment `fault_env` set
up and then removes `HERNESS_FAULTS` from this process, so the parent (which claims and
resumes the same work) never loads the kill rule (R-40 plans stay per process).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Final

import pytest
from tests.support._f08_children import RESULT_PREFIX
from tests.support.config_tree import REPO
from tests.support.sync_kill import KILLED_RETURNCODE

from herness.core.resilience import ProcessState
from herness.core.resilience._state import NOT_LOADED
from herness.store.ops.core import read_all

CHILD_TIMEOUT_S: Final = 300


def child_env(monkeypatch: pytest.MonkeyPatch, state: ProcessState) -> dict[str, str]:
    """The current environment (with the plan of `fault_env`) for a child; this process
    forgets the plan."""
    env = dict(os.environ, PYTHONUTF8="1", HERNESS_ENV="test")
    assert "HERNESS_FAULTS" in env, "call fault_env first"
    monkeypatch.delenv("HERNESS_FAULTS")
    state.fault_plan = NOT_LOADED
    state.faults_enabled = False
    return env


def child_env_without_plan() -> dict[str, str]:
    """The current environment for a child, without any fault plan."""
    env = dict(os.environ, PYTHONUTF8="1", HERNESS_ENV="test")
    env.pop("HERNESS_FAULTS", None)
    return env


def child(
    env: dict[str, str], *args: str, module: str = "tests.support._f08_children"
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m <module> <args>`` (default the suite's children) to its end."""
    argv = [sys.executable, "-m", module, *args]
    return subprocess.run(  # noqa: S603 - fixed argv list, no shell
        argv,
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CHILD_TIMEOUT_S,
        check=False,
    )


def killed(done: subprocess.CompletedProcess[str], prefix: str = RESULT_PREFIX) -> None:
    """The child died at a `kill` fault point of a plan it loaded from `HERNESS_FAULTS`."""
    logs = done.stdout + done.stderr
    assert done.returncode == KILLED_RETURNCODE, (done.returncode, logs[-3000:])
    assert prefix not in done.stdout
    assert "resilience.faults.enabled" in logs
    assert "resilience.faults.kill" in logs


def result(done: subprocess.CompletedProcess[str], prefix: str = RESULT_PREFIX) -> dict[str, Any]:
    """The child's result line (it must have exited 0)."""
    logs = done.stdout + done.stderr
    assert done.returncode == 0, logs[-3000:]
    [line] = [x for x in done.stdout.splitlines() if x.startswith(prefix)]
    out: dict[str, Any] = json.loads(line.removeprefix(prefix))
    return out


def job_events(kind: str) -> list[dict[str, Any]]:
    """Stored `resilience_event` rows of `kind` with ts, job id and the decoded detail."""
    rows = read_all(
        "SELECT ts, job_id, target, detail FROM resilience_event WHERE kind = ?"
        " ORDER BY ts, event_id",
        (kind,),
    )
    return [
        {"ts": r["ts"], "job_id": r["job_id"], "target": r["target"]} | json.loads(r["detail"])
        for r in rows
    ]
