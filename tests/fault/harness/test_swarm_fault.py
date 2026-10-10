"""Swarm-level fault rows FT06-01 ... FT06-05 (impl 06 §11, T06-26).

Every row drives the real run driver (`Swarm.start` / `Swarm.resume`, or the spec 08 `review`
job handler through `run_inline`) over the T06-22 `drv` set-up with every role on the real
spec 05 loop and spec 11 `FakeLLMClient` scripts (`tests.support._f06_swarm`). Faults come
from JSON plans that `fault_point` loads under `HERNESS_ENV=test` (`fault_env`, R-40). A
"kill" is the plan action `kill`, raised in-process as `SimulatedKill` (a `BaseException`
that no product handler catches); the resume runs on a fresh `Swarm` as `herness resume`
does. Retry waits are the no-op sleeps of `reset_process_state`; nothing sleeps.

The warehouse is the `drv` fake with planted evidence, not `tiny_build`: the synthetic builds
have no `score.*` layer (T02-22 fake hook), which the funding plan context and the Writer's
recommendation read (T06-26 report, deviation 1).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.support import _f06_swarm as f6
from tests.support.fake_gpu import FakeGpu
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fault_env import FaultEnv

from herness._cli.output import exit_code_for
from herness.core import time as clock
from herness.core.errors import StoreBusy
from herness.core.jobs import enqueue, gpu, run_inline
from herness.core.jobs import get as get_job
from herness.core.jobs.inline import inline_owner
from herness.core.jobs.ports import require_jobs_backend
from herness.core.resilience import policy
from herness.harness.gates import TaskSlots
from herness.harness.memory import MemoryStore
from herness.harness.swarm import handler
from herness.harness.swarm.run import Swarm
from herness.store.ops import (
    core,
    find_run_by_job,
    get_run,
    get_task,
    query_findings,
    run_recommendations,
    select_tasks,
)

pytestmark = pytest.mark.fault

drv = e.drv  # fixtures
test_redactor = e.env_mod.test_redactor  # type: ignore[attr-defined]
__all__ = ["drv", "test_redactor"]

# The point a parametrized FT06-02 kill uses for each phase (counted from a clean run).
PHASE_POINTS: dict[str, tuple[str, dict[str, str]]] = {
    "planning": ("swarm.after_task_claim", {"role": "planner"}),
    "challenging": ("swarm.after_task_claim", {"role": "skeptic"}),
    "writing": ("sqlite.write", {"kind": "task_complete"}),  # the Writer's completion
    "recording": ("sqlite.write", {"kind": "swarm_record_done"}),  # recommendations written
}


def _other_run(*known: str) -> str:
    """The id of the one run not in `known` (the run a kill left behind)."""
    rows = core.read_all("SELECT run_id FROM run ORDER BY run_id", ())
    (run_id,) = [r["run_id"] for r in rows if r["run_id"] not in known]
    return str(run_id)


def _status(run_id: str) -> str:
    run = get_run(run_id)
    assert run is not None
    return run.status


def _one_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """One task at a time, so "after k of n analysts" is exact."""
    monkeypatch.setattr(TaskSlots, "for_run", classmethod(lambda cls, *_a: cls(1)))


def _swarm(drv: Drv, memory: MemoryStore) -> Swarm:
    swarm = Swarm(drv.hcfg.pipelines, drv.llms, memory, deps=drv.deps)
    drv.swarms.append(swarm)
    return swarm


def _as_review_job(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> None:
    """The spec 08 `review` handler builds the `drv` swarm (its composition root, U06-85)."""
    monkeypatch.setattr(handler, "build_swarm_from_config", lambda cfg: drv.swarm())
    handler.register_handlers()


# --- FT06-01 -----------------------------------------------------------------------------------


def test_ft06_01_kill_after_finding_write_then_resume_no_rerun_no_duplicates(
    drv: Drv, fault_env: FaultEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT06-01 kill at `swarm.after_finding_write` after k = 2 of n = 4 analysts (plan
    `{point: swarm.after_finding_write, action: kill, role: analyst, nth: 2}`), resume: no
    `done` task is executed again, no finding is duplicated (the re-executed killed analyst
    posts its finding again and the Blackboard keeps one), and the draft equals a clean run's
    except timestamps (ids mapped to their findings)."""
    scripts = f6.use_real_agents(drv, monkeypatch)
    f6.install_kill(monkeypatch)
    _one_slot(monkeypatch)
    drv.set_pipeline(t1=2, t2=2)
    clean = asyncio.run(drv.swarm().start(drv.request(depth="standard")))
    assert clean.status == "done"

    fault_env([{"point": "swarm.after_finding_write", "action": "kill", "role": "analyst",
                "nth": 2}])  # fmt: skip
    f6.run_killed(lambda: drv.swarm().start(drv.request(depth="standard")))
    run_id = _other_run(clean.run_id)
    analysts = select_tasks(run_id, roles={"analyst"})
    done = {t.task_id for t in analysts if t.status == "done"}
    (killed,) = [t for t in analysts if t.status == "running"]
    assert (_status(run_id), len(analysts), len(done)) == ("running", 4, 1)  # k - 1 done
    assert len(query_findings(run_id, limit=500)) == 2  # the k-th wrote before the kill

    fault_env([])
    res = asyncio.run(drv.swarm().resume(run_id))
    assert res.status == "done"
    assert {t: scripts.executions[t] for t in done} == dict.fromkeys(done, 1)
    assert scripts.executions[killed.task_id] == 2  # re-executed: it posted again
    assert f6.task_view(run_id) == f6.task_view(clean.run_id)
    assert f6.finding_view(run_id) == f6.finding_view(clean.run_id)
    by_task = Counter(f.task_id for f in query_findings(run_id, limit=500))
    assert max(by_task.values()) == 1
    reports = f6.reports(drv)
    assert f6.draft_view(run_id, reports) == f6.draft_view(clean.run_id, reports)


# --- FT06-02 -----------------------------------------------------------------------------------


@pytest.mark.parametrize("phase", list(PHASE_POINTS))
def test_ft06_02_kill_in_phase_resume_reaches_done_without_duplicates(
    drv: Drv, fault_env: FaultEnv, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    """FT06-02 kill in `planning` (planner claim), `challenging` (first Skeptic claim),
    `writing` (the Writer's `task_complete` write, after `draft.json`) and `recording`
    (`swarm_record_done`, after the recommendations are written); the rule's `nth` is counted
    on a clean run of the same request. Resume reaches `done` with no duplicate task, finding
    or recommendation (real `MemoryStore`), compared with the clean run."""
    f6.use_real_agents(drv, monkeypatch)
    f6.install_kill(monkeypatch)
    memory = f6.real_memory(drv, tmp_path)
    fault_env([])
    log = f6.record_points(monkeypatch)
    clean = asyncio.run(_swarm(drv, memory).start(drv.request(depth="standard")))
    assert clean.status == "done"
    point, labels = PHASE_POINTS[phase]
    nth = log.nth(point, phase, **labels)

    fault_env([{"point": point, "action": "kill", "nth": nth, **labels}])
    f6.run_killed(lambda: _swarm(drv, memory).start(drv.request(depth="standard")))
    run_id = _other_run(clean.run_id)
    assert _status(run_id) == phase
    recs_before = len(run_recommendations(run_id))
    assert recs_before == (1 if phase == "recording" else 0)

    fault_env([])
    res = asyncio.run(_swarm(drv, memory).resume(run_id))
    assert res.status == "done"
    assert f6.task_view(run_id) == f6.task_view(clean.run_id)
    assert f6.finding_view(run_id) == f6.finding_view(clean.run_id)
    recs = run_recommendations(run_id)
    assert [(r["kind"], r["target_type"], r["target_id"]) for r in recs] == [
        (r["kind"], r["target_type"], r["target_id"]) for r in run_recommendations(clean.run_id)
    ]
    reports = f6.reports(drv)
    assert f6.draft_view(run_id, reports) == f6.draft_view(clean.run_id, reports)


# --- FT06-03 -----------------------------------------------------------------------------------


def _classes(fake: FakeGpu) -> list[str]:
    """The loaded class after each compose command, without empty states and repeats."""
    seq: list[str] = []
    for classes in fake.running_classes:
        for cls in sorted(classes):
            if not seq or seq[-1] != cls:
                seq.append(cls)
    return seq


def test_ft06_03_deep_kill_inside_large_scope_resume_reenters_and_finishes(
    drv: Drv,
    fault_env: FaultEnv,
    monkeypatch: pytest.MonkeyPatch,
    fake_gpu: FakeGpu,
    fake_keyring: MemoryKeyring,
) -> None:
    """FT06-03 a deep funding `review` job (gpu_class reasoning) runs inline on the `fake_gpu`
    compose host and is killed inside the large scope (gate 1 runs in the large stage: plan
    `{point: swarm.after_task_claim, action: kill, role: verifier, nth: 1}`). The class went
    reasoning → large before the kill; the dead owner's job is requeued (start-up recovery),
    the rerun resumes the run, re-enters the scope and finishes: reasoning → large → reasoning
    → large → reasoning, one class at a time. Not run on real containers (open-questions
    (b)9/(b)15)."""
    f6.use_real_agents(drv, monkeypatch)
    at_kill: list[set[str]] = []
    f6.install_kill(monkeypatch, on_kill=lambda: at_kill.append(fake_gpu.running()))
    monkeypatch.setattr(gpu, "_gpu", lambda: fake_gpu.gpu)
    fake_keyring.store[("herness", "vllm.api_key")] = "ft06-03-" + "warmup"  # warm-up bearer
    _as_review_job(drv, monkeypatch)
    request: dict[str, Any] = {"request": {"kind": "funding_review", "depth": "deep"}}
    job_id = enqueue("review", request, "reasoning")

    fault_env([{"point": "swarm.after_task_claim", "action": "kill", "role": "verifier",
                "nth": 1}])  # fmt: skip
    f6.expect_kill(lambda: run_inline(job_id))
    run = find_run_by_job(job_id)
    assert run is not None
    assert (run.status, run.meta.get("stage")) == ("verifying", "large")
    assert [{fake_gpu.class_of(s) for s in running} for running in at_kill] == [{"large"}]
    assert _classes(fake_gpu) == ["reasoning", "large", "reasoning"]

    fault_env([])
    requeued = require_jobs_backend().requeue_owned([inline_owner()], clock.now())
    assert [row[0] for row in requeued] == [job_id]
    out = run_inline(job_id)
    assert (out.status, out.result["run_id"], out.result["status"]) == ("done", run.run_id, "done")
    assert _classes(fake_gpu) == ["reasoning", "large", "reasoning", "large", "reasoning"]
    fake_gpu.assert_single_class()
    assert all(t.status == "done" for t in select_tasks(run.run_id))


# --- FT06-04 -----------------------------------------------------------------------------------


def test_ft06_04_store_busy_beyond_retries_exits_non_zero_then_resume_continues(
    drv: Drv, fault_env: FaultEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT06-04 `sqlite.write` StoreBusy beyond the `sqlite_write` retries (6 attempts) on the
    usage write after the analyst fan-out (plan: one `delay:0.001` pass-through, then
    `error:StoreBusy` for `attempts` calls, both `kind: swarm_run_usage`) while a `review` job
    runs inline: StoreBusy leaves the handler, the inline run raises it (R-46 exit code 8,
    non-zero), the run keeps status `running` and the job is requeued. Running the job again
    resumes the run to `done` without executing a finished analyst again."""
    scripts = f6.use_real_agents(drv, monkeypatch)
    _as_review_job(drv, monkeypatch)
    attempts = policy("sqlite_write").attempts
    fault_env([
        {"point": "sqlite.write", "action": "delay:0.001", "kind": "swarm_run_usage", "count": 1},
        {"point": "sqlite.write", "action": "error:StoreBusy", "kind": "swarm_run_usage",
         "count": attempts},
    ])  # fmt: skip
    job_id = enqueue("review", {"request": {"kind": "funding_review", "depth": "standard"}}, "none")
    with pytest.raises(StoreBusy) as caught:
        run_inline(job_id)
    assert exit_code_for(caught.value) == 8
    run = find_run_by_job(job_id)
    assert run is not None
    assert run.status == "running"
    job = get_job(job_id)
    assert (job.status, (job.last_error or {}).get("class")) == ("queued", "StoreBusy")
    analysts = select_tasks(run.run_id, roles={"analyst"})
    assert analysts
    assert all(t.status == "done" for t in analysts)

    fault_env([])
    later = clock.now() + timedelta(hours=2)  # past the job back-off
    monkeypatch.setattr(clock, "now", lambda: later)
    with structlog.testing.capture_logs() as logs:
        out = run_inline(job_id)
    assert (out.status, out.result["status"]) == ("done", "done")
    assert any(ev["event"] == "harness.run.resumed" for ev in logs)
    assert {scripts.executions[t.task_id] for t in analysts} == {1}
    assert _status(run.run_id) == "done"


# --- FT06-05 -----------------------------------------------------------------------------------


def test_ft06_05_kill_after_task_claim_task_recovered_attempts_counted_once(
    drv: Drv, fault_env: FaultEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT06-05 kill at `swarm.after_task_claim` on the first analyst claim (plan
    `{point: swarm.after_task_claim, action: kill, role: analyst, nth: 1}`): the claimed task
    is left `running` with attempts 1 and its agent never ran; resume recovers it (`running`
    → `pending`, attempts kept), runs it once and it ends `done` with attempts 2 (the crashed
    claim counted once, not reset and not charged twice); every other task has attempts 1."""
    scripts = f6.use_real_agents(drv, monkeypatch)
    f6.install_kill(monkeypatch)
    fault_env([{"point": "swarm.after_task_claim", "action": "kill", "role": "analyst",
                "nth": 1}])  # fmt: skip
    f6.run_killed(lambda: drv.swarm().start(drv.request(depth="standard")))
    run_id = _other_run()
    (claimed,) = select_tasks(run_id, statuses={"running"})
    assert (claimed.role, claimed.attempts, scripts.executions[claimed.task_id]) == (
        "analyst", 1, 0
    )  # fmt: skip

    fault_env([])
    with structlog.testing.capture_logs() as logs:
        res = asyncio.run(drv.swarm().resume(run_id))
    assert res.status == "done"
    resumed = next(ev for ev in logs if ev["event"] == "harness.run.resumed")
    assert resumed["recovered"]["running"] == 1
    row = get_task(claimed.task_id)
    assert row is not None
    assert (row.status, row.attempts, scripts.executions[claimed.task_id]) == ("done", 2, 1)
    others = [t for t in select_tasks(run_id) if t.task_id != claimed.task_id]
    assert {t.attempts for t in others} == {1}
