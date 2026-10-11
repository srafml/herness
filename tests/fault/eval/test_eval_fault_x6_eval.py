"""Spec 11 fault case X6 (impl 11 FT11-06; F11-10, F11-11; R-40, R-45; T11-33).

A mock `eval` job (two chat questions and one funding review on `tiny_build`, `--mock-llm`
dir, no judge) is enqueued and run by `run_inline` in a worker subprocess
(`tests.support._f11_children eval`) under the JSON plan `job.before_complete kill kind=eval`.
`herness eval` / `herness eval --resume` are T09-24's CLI (herness/cli.py is frozen for this
card): the resume runs the handler the CLI would run, in-process, with `resume_run_id`
(`--resume <run_id>`), and the orphaned job itself is then recovered as a restarted worker
would (`requeue_owned(dead_owners(host))`) and re-run from its saved state
(`{"eval_run_id"}`). The child logs every scripted call (`chat` and each agent role) to a
call log, because the kill ends it before it can report.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Final

import pytest
from tests.integration.eval._eval_it_env import EvalEnv, eval_env, question
from tests.integration.harness import _swarm_run_env as e
from tests.support import faults
from tests.support._f11_children import RESULT_PREFIX
from tests.support.config_tree import REPO
from tests.support.sync_kill import KILLED_RETURNCODE
from tests.unit.eval._runner_env import lines, settings
from tests.unit.harness._blackboard_env import test_redactor

from herness.core import time as clock
from herness.core.jobs import enqueue, run_inline
from herness.core.jobs._supervisor_boot import dead_owners
from herness.core.jobs.ports import require_jobs_backend
from herness.eval import runner
from herness.eval.runner import build_eval_payload, eval_gpu_class, exit_code
from herness.store.ops import get_run, read_all

pytestmark = pytest.mark.fault

drv = e.drv  # fixtures
__all__ = ["drv", "eval_env", "test_redactor"]

CHILD_TIMEOUT_S: Final = 600  # F11-11 step 3
X6_RULE: Final = {"point": "job.before_complete", "action": "kill", "kind": "eval"}
IDS: Final = ("G01", "G02", "F01")


def _child(env_extra: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONUTF8="1", HERNESS_ENV="test")
    env.pop("HERNESS_FAULTS", None)
    env.update(env_extra)
    argv = [sys.executable, "-m", "tests.support._f11_children", *args]
    return subprocess.run(  # noqa: S603 - fixed argv list, no shell
        argv, cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=CHILD_TIMEOUT_S, check=False,
    )  # fmt: skip


def _payload(env: EvalEnv, **extra: Any) -> dict[str, Any]:
    suite = env.suite([question("G01"), question("G02"), question("F01", "funding")])
    mock = env.tmp / "mock"
    mock.mkdir(exist_ok=True)
    (mock / "judge.yaml").write_text(
        "- match: {model_role: eval_judge}\n  turns:\n    - final: {text: ok}\n", encoding="utf-8"
    )
    return build_eval_payload(suite=str(suite), profile="synth", depth="fast", ids=list(IDS),
                              no_judge=True, mock_llm_dir=str(mock), settings=settings(),
                              **extra)  # fmt: skip


def _calls(log: Path) -> Counter[str]:
    return Counter(log.read_text(encoding="utf-8").splitlines()) if log.exists() else Counter()


def _review_runs() -> list[str]:
    return [str(r["run_id"]) for r in read_all("SELECT run_id FROM run WHERE kind != 'eval'", ())]


def test_ft11_06_eval_killed_before_complete_resume_reruns_no_question(
    eval_env: EvalEnv, tmp_path: Path
) -> None:
    """FT11-06 X6 `job.before_complete kill kind=eval` in a worker subprocess, then
    `--resume <run_id>`: the questions in `results.jsonl` are not re-run (script call counts:
    the killed run's equal a clean run's, the resume adds none)."""
    payload = _payload(eval_env)
    job_id = enqueue("eval", payload, eval_gpu_class(payload))
    plan = faults.write_fault_plan(tmp_path, [X6_RULE])
    call_log = tmp_path / "calls.log"
    warehouse = eval_env.data_root / "warehouse" / eval_env.build.warehouse.name
    args = ("eval", str(eval_env.drv.cfg_root), str(eval_env.data_root),
            eval_env.build.build_id, str(warehouse), job_id, str(call_log))  # fmt: skip

    killed = _child(faults.fault_env(plan), *args)
    logs = killed.stdout + killed.stderr
    assert killed.returncode == KILLED_RETURNCODE, (killed.returncode, logs[-3000:])
    assert RESULT_PREFIX not in killed.stdout
    assert "resilience.faults.enabled" in logs
    assert "resilience.faults.kill" in logs
    job = require_jobs_backend().get_job(job_id)
    assert job is not None
    assert (job.status, job.attempts) == ("running", 1)  # never completed
    [eval_row] = read_all("SELECT run_id, status FROM run WHERE kind = 'eval'", ())
    run_id = str(eval_row["run_id"])
    assert eval_row["status"] == "done"
    out = eval_env.out_dir(run_id)
    before = (out / "results.jsonl").read_bytes()
    assert [r["id"] for r in lines(out / "results.jsonl")] == list(IDS)
    killed_calls = _calls(call_log)
    assert killed_calls["chat"] == 2
    reviews = _review_runs()
    assert len(reviews) == 1

    # `herness eval --resume <run_id>`: the handler with resume_run_id, in-process (R-45)
    _, outcome = eval_env.handle(_payload(eval_env, resume_run_id=run_id))
    assert outcome.status == "done"
    assert outcome.result["run_id"] == run_id
    assert exit_code(outcome.model_dump()) == 0
    assert (eval_env.chat.calls, eval_env.drv.agents.calls) == ([], {})  # nothing re-run
    assert (out / "results.jsonl").read_bytes() == before
    assert _review_runs() == reviews
    run = get_run(run_id)
    assert run is not None
    assert run.status == "done"

    # the orphaned job: a restarted worker requeues it, its saved state resumes the run
    requeued = require_jobs_backend().requeue_owned(
        dead_owners(socket.gethostname().lower()), clock.now()
    )
    assert [r[0] for r in requeued] == [job_id]
    runner.register_handlers()
    again = run_inline(job_id)
    assert (again.status, again.result["run_id"]) == ("done", run_id)
    assert (eval_env.chat.calls, eval_env.drv.agents.calls) == ([], {})
    assert (out / "results.jsonl").read_bytes() == before
    _assert_call_counts_equal_a_clean_run(eval_env, killed_calls)


def _assert_call_counts_equal_a_clean_run(env: EvalEnv, killed: Counter[str]) -> None:
    """A clean run of the same suite in this process makes the same scripted calls."""
    _, outcome = env.handle(_payload(env))
    assert outcome.status == "done"
    clean = Counter({"chat": len(env.chat.calls)} | env.drv.agents.calls)
    assert killed == clean, (killed, clean)
    results = lines(env.out_dir(outcome.result["run_id"]) / "results.jsonl")
    assert [r["id"] for r in results] == list(IDS)


def test_ft11_06_summary_written_before_the_kill(eval_env: EvalEnv, tmp_path: Path) -> None:
    """FT11-06 X6 the killed run had already closed its row and written `summary.json`;
    the resume rewrites it with the same results (no question re-run, exit 0)."""
    payload = _payload(eval_env)
    job_id = enqueue("eval", payload, eval_gpu_class(payload))
    plan = faults.write_fault_plan(tmp_path, [X6_RULE])
    warehouse = eval_env.data_root / "warehouse" / eval_env.build.warehouse.name
    killed = _child(faults.fault_env(plan), "eval", str(eval_env.drv.cfg_root),
                    str(eval_env.data_root), eval_env.build.build_id, str(warehouse), job_id,
                    str(tmp_path / "calls.log"))  # fmt: skip
    assert killed.returncode == KILLED_RETURNCODE, killed.stderr[-3000:]
    [row] = read_all("SELECT run_id FROM run WHERE kind = 'eval'", ())
    out = eval_env.out_dir(row["run_id"])
    first = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert (first["mock"], first["exit_code"]) == (True, 0)
    _, outcome = eval_env.handle(_payload(eval_env, resume_run_id=str(row["run_id"])))
    second = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert outcome.result["passed"] is True
    assert second["metrics"] == first["metrics"]
    assert eval_env.chat.calls == []
