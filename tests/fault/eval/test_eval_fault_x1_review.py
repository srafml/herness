"""Spec 11 fault case X1 (impl 11 FT11-01; F11-11; R-40, DD11-11; T11-33).

An org review job (spec 08 `review`, U06-85) on `tiny_build` runs with `run_inline` in a
worker subprocess (`tests.support._f11_children review`: the T06-22 driver with scripted
agents, real run steps, Blackboard, findings, drafts and run rows) under the JSON plan
`job.before_complete kill kind=review nth=1` (impl 08 has no task-commit point, R-40). The
child logs each scripted agent call by role, because the kill ends it before it reports.

`herness resume` is T09-24's CLI (herness/cli.py is frozen for this card): the test calls
what it calls, `enqueue_resume(run_id)` (U08-93), and then recovers the orphaned job as a
restarted worker would (`requeue_owned(dead_owners(host))`) and re-runs it, whose handler
finds the run by its job (U06-79). The scripts are the driver's scripted `run_agent` (no
`FakeLLMServer`: the agents of the drv set-up never call a model), so "script call counts"
are the agent calls per role. The driver's scripted Writer drafts the funding-style `fund`
recommendation, which the org-review rules remove (`rule:effort_source`,
`rule:confidence_source`), so the draft is not publishable and both the killed and the
clean run end `partial` with every task `done`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import JsonValue
from tests.integration.eval._eval_it_env import EvalEnv, eval_env
from tests.integration.harness import _swarm_run_env as e
from tests.support import faults
from tests.support._f11_children import RESULT_PREFIX
from tests.support.config_tree import REPO
from tests.support.sync_kill import KILLED_RETURNCODE
from tests.unit.harness._blackboard_env import test_redactor

from herness.core import time as clock
from herness.core.jobs import enqueue, enqueue_resume, run_inline
from herness.core.jobs._supervisor_boot import dead_owners
from herness.core.jobs.ports import require_jobs_backend
from herness.harness.swarm import handler
from herness.store.ops import find_run_by_job, get_run, query_findings, select_tasks

pytestmark = pytest.mark.fault

drv = e.drv  # fixtures
__all__ = ["drv", "eval_env", "test_redactor"]

CHILD_TIMEOUT_S: Final = 600  # F11-11 step 3
X1_RULE: Final = {"point": "job.before_complete", "action": "kill", "kind": "review", "nth": 1}
_IDS: Final = re.compile(r"\b(run|rec|task)_[0-9A-HJKMNP-TV-Z]{26}\b")
_ALL_FINDINGS: Final = {"proposed", "challenged", "verified", "rejected", "revised", "merged"}


def _child(env_extra: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONUTF8="1", HERNESS_ENV="test")
    env.pop("HERNESS_FAULTS", None)
    env.update(env_extra)
    argv = [sys.executable, "-m", "tests.support._f11_children", *args]
    return subprocess.run(  # noqa: S603 - fixed argv list, no shell
        argv, cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=CHILD_TIMEOUT_S, check=False,
    )  # fmt: skip


def _findings(run_id: str) -> Counter[tuple[str, str | None, str, str]]:
    found = query_findings(run_id, statuses=_ALL_FINDINGS, limit=500)
    return Counter((f.author_role, f.entity_id, f.claim, f.status) for f in found)


def _draft_view(data_root: Path, run_id: str) -> Any:
    """`draft.json` without `*_at` keys, finding ids mapped to (role, entity, claim) and
    run, rec and task ids masked: equal for equal reports of different runs."""
    [path] = data_root.rglob(f"{run_id}/draft.json")
    found = query_findings(run_id, statuses=_ALL_FINDINGS, limit=500)
    names = {f.finding_id: f"{f.author_role}|{f.entity_id}|{f.claim}" for f in found}

    def view(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: view(v) for k, v in node.items() if not k.endswith("_at")}
        if isinstance(node, list):
            return [view(v) for v in node]
        if isinstance(node, str):
            return _IDS.sub(lambda m: m.group(1) + "_*", names.get(node, node))
        return node

    return view(json.loads(path.read_text(encoding="utf-8")))


def _bind_handler(env: EvalEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """The `review` handler with its composition root bound to the driver's swarm."""
    build_id = env.build.build_id

    def swarm(cfg: object) -> object:
        del cfg
        return env.drv.swarm(current_build=lambda: build_id, build_exists=lambda b: b == build_id)

    monkeypatch.setattr(handler, "build_swarm_from_config", swarm)
    handler.register_handlers()


def test_ft11_01_review_killed_before_complete_resume_reexecutes_no_task(
    eval_env: EvalEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT11-01 X1 org review killed at `job.before_complete` (kind review, nth 1) in a worker
    subprocess, then `herness resume`: no `done` task re-executes (call counts equal a clean
    run's), no duplicate `finding`, `ReportDraft` equal to a clean run ignoring timestamps."""
    request: dict[str, JsonValue] = {"kind": "org_review", "depth": "standard"}
    job_id = enqueue("review", {"request": request}, "none")
    plan = faults.write_fault_plan(tmp_path, [X1_RULE])
    call_log = tmp_path / "calls.log"
    killed = _child(faults.fault_env(plan), "review", str(eval_env.drv.cfg_root),
                    str(eval_env.data_root), eval_env.build.build_id, "-", job_id,
                    str(call_log))  # fmt: skip
    logs = killed.stdout + killed.stderr
    assert killed.returncode == KILLED_RETURNCODE, (killed.returncode, logs[-3000:])
    assert RESULT_PREFIX not in killed.stdout
    assert "resilience.faults.enabled" in logs
    assert "resilience.faults.kill" in logs
    job = require_jobs_backend().get_job(job_id)
    assert job is not None
    assert (job.status, job.attempts) == ("running", 1)
    run = find_run_by_job(job_id)
    assert run is not None
    assert (run.kind, run.status) == ("org_review", "partial")  # see the module note
    killed_calls = Counter(call_log.read_text(encoding="utf-8").splitlines())
    tasks = {t.task_id: (t.status, t.attempts) for t in select_tasks(run.run_id)}
    findings = _findings(run.run_id)
    draft = _draft_view(eval_env.data_root, run.run_id)

    # `herness resume <run_id>`: the run is finished, so no resume job is enqueued
    resumed = enqueue_resume(run.run_id)
    assert (resumed.job_id, resumed.created, resumed.run_status) == (None, False, run.status)
    # the orphaned job: a restarted worker requeues it; its handler finds the finished run
    requeued = require_jobs_backend().requeue_owned(
        dead_owners(socket.gethostname().lower()), clock.now()
    )
    assert [r[0] for r in requeued] == [job_id]
    _bind_handler(eval_env, monkeypatch)
    outcome = run_inline(job_id)
    assert (outcome.status, outcome.result["run_id"]) == ("done", run.run_id)
    assert eval_env.drv.agents.calls == {}  # no task re-executed
    assert {t.task_id: (t.status, t.attempts) for t in select_tasks(run.run_id)} == tasks
    assert _findings(run.run_id) == findings
    after = get_run(run.run_id)
    assert after is not None
    assert after.status == run.status

    # the clean run of the same request
    swarm = eval_env.drv.swarm(current_build=lambda: eval_env.build.build_id,
                               build_exists=lambda b: b == eval_env.build.build_id)  # fmt: skip
    clean = asyncio.run(swarm.start(eval_env.drv.request("org_review", depth="standard")))
    assert clean.status == run.status
    assert killed_calls == Counter(eval_env.drv.agents.calls)
    assert findings == _findings(clean.run_id)  # no duplicate finding
    assert all(n == 1 for n in findings.values())
    assert draft == _draft_view(eval_env.data_root, clean.run_id)
