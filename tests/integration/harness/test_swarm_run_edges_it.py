"""U06-78 … U06-82 edge paths of the run driver (`test_rf_t06_22_*`) over `_swarm_run_env`.

Guards and error rules: unknown runs, chat requests, invalid stored requests, a stop that
cancels from inside the drive, a stop-induced `CancelledError` inside a step, `BudgetExceeded`
escaping a step, a plan context that cannot be built, cross-check planning and its task cap,
and the composition defaults (`SwarmDeps.from_config`, `Swarm(deps=None)`,
`build_swarm_from_config`).
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import structlog
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv, FakeJob

from herness.core import registry
from herness.core.errors import BudgetExceeded, ConfigError, NotFound, QueryError
from herness.harness.swarm import _run_env, _run_ops, _run_steps, handler
from herness.harness.swarm.run import Swarm
from herness.store.ops import get_run, run_write, select_runs, select_tasks, update_run_fields

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
test_redactor = e.env_mod.test_redactor


def _only_run() -> str:
    runs = select_runs(statuses={"created", "planning", "running", "challenging", "verifying",
                                 "writing", "recording", "done", "partial", "failed",
                                 "canceled"})  # fmt: skip
    assert len(runs) == 1
    return runs[0].run_id


def _status(run_id: str) -> str:
    run = get_run(run_id)
    assert run is not None
    return run.status


def test_rf_t06_22_guards_unknown_run_and_chat(drv: Drv) -> None:
    """U06-79/80/81: a chat request is refused before any row; unknown runs → `NotFound`."""
    swarm = drv.swarm()
    chat = drv.request(kind="chat", question="why?")
    with pytest.raises(ConfigError, match="review requests only"):
        asyncio.run(swarm.start(chat))
    assert select_runs(statuses={"created"}) == []
    missing = "run_" + "9" * 26
    with pytest.raises(NotFound, match=f"run not found: run_id={missing}"):
        asyncio.run(swarm.resume(missing))
    with pytest.raises(NotFound):
        asyncio.run(swarm.cancel(missing))


def test_rf_t06_22_terminal_resume_returns_result(drv: Drv) -> None:
    """U06-80 step 2: a terminal run resumes to its stored result without driving."""
    res = asyncio.run(drv.swarm().start(drv.request()))
    calls = dict(drv.agents.calls)
    again = asyncio.run(drv.swarm().resume(res.run_id, force=True))
    assert (again.status, again.coverage) == ("done", res.coverage)
    assert drv.agents.calls == calls


def test_rf_t06_22_invalid_stored_request(drv: Drv) -> None:
    """U06-80 step 3: a stored request that no longer validates is a `ConfigError`."""
    job = FakeJob()
    job.yield_when = lambda: True
    first = asyncio.run(drv.swarm().start(drv.request(), job))
    patch = {"request": {"kind": "funding_review", "depth": "huge"}}
    run_write(lambda conn: update_run_fields(conn, first.run_id, meta_patch=patch), op="t")
    with pytest.raises(ConfigError, match="stored run request invalid"):
        asyncio.run(drv.swarm().resume(first.run_id))


def test_rf_t06_22_job_cancel_stops_drive(drv: Drv) -> None:
    """U06-82 step 4: a job cancel seen at a step boundary CASes the run to `canceled` and
    logs one status change and `harness.run.canceled`."""
    job = FakeJob()
    job.yield_when = lambda: (get_run(_only_run()) or object()).status == "running"  # type: ignore[union-attr]
    job.stop_reason = "cancel"
    with structlog.testing.capture_logs() as logs:
        res = asyncio.run(drv.swarm().start(drv.request(), job))
    assert res.status == "canceled"
    changes = [ev for ev in logs if ev["event"] == "harness.run.status_changed"]
    assert (changes[-1]["from"], changes[-1]["to"]) == ("running", "canceled")
    assert any(ev["event"] == "harness.run.canceled" for ev in logs)


def test_rf_t06_22_stop_inside_step_is_not_an_error(
    drv: Drv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U06-82 step 4: a `CancelledError` raised by a stopped step ends the drive at the next
    boundary (preempt: no status change); without a stop it propagates."""
    job = FakeJob()
    stopped = threading.Event()
    job.yield_when = stopped.is_set

    async def interrupted(env: Any) -> int:
        del env
        stopped.set()
        raise asyncio.CancelledError

    monkeypatch.setattr(_run_steps, "run_dedup", interrupted)
    res = asyncio.run(drv.swarm().start(drv.request(), job))
    assert res.status == "running"
    stopped.clear()
    job.yield_when = lambda: False

    async def cancelled(env: Any) -> int:
        del env
        raise asyncio.CancelledError

    monkeypatch.setattr(_run_steps, "run_dedup", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(drv.swarm().resume(res.run_id))


def test_rf_t06_22_budget_exceeded_escaping_steps(
    drv: Drv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U06-82 step 3/8: `BudgetExceeded` escaping the fan-out ends the analysis (U06-84,
    `verifying`); escaping the Writer it fails the run and propagates."""

    async def exhausted(env: Any) -> int:
        del env
        msg = "run budget exhausted"
        raise BudgetExceeded(msg)

    monkeypatch.setattr(_run_steps, "run_dedup", exhausted)
    monkeypatch.setattr(_run_steps, "run_writer_step", exhausted)
    with structlog.testing.capture_logs() as logs, pytest.raises(BudgetExceeded):
        asyncio.run(drv.swarm().start(drv.request()))
    seen = [ev["to"] for ev in logs if ev["event"] == "harness.run.status_changed"]
    assert seen[-3:] == ["verifying", "writing", "failed"]
    assert _status(_only_run()) == "failed"


def test_rf_t06_22_plan_context_failure(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> None:
    """F06-03 step 1: a plan context that cannot be built fails the planner task (a
    `QueryError` is dead at once), so the run ends `failed`."""

    async def broken(*args: Any, **kw: Any) -> Any:
        del args, kw
        msg = "warehouse read failed"
        raise QueryError(msg)

    monkeypatch.setattr(_run_steps, "build_plan_context", broken)
    res = asyncio.run(drv.swarm().start(drv.request()))
    assert res.status == "failed"
    planner = next(t for t in select_tasks(res.run_id) if t.role == "planner")
    assert (planner.status, (planner.last_error or {}).get("class")) == ("dead", "QueryError")


def test_rf_t06_22_plan_context_lost_after_planning(
    drv: Drv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U06-82 step 8: after planning, a plan context failure on resume fails the run."""
    job = FakeJob()
    job.yield_when = lambda: (get_run(_only_run()) or object()).status == "running"  # type: ignore[union-attr]
    first = asyncio.run(drv.swarm().start(drv.request(), job))

    async def broken(*args: Any, **kw: Any) -> Any:
        del args, kw
        msg = "warehouse read failed"
        raise QueryError(msg)

    monkeypatch.setattr(_run_steps, "build_plan_context", broken)
    with pytest.raises(QueryError):
        asyncio.run(drv.swarm().resume(first.run_id))
    assert _status(first.run_id) == "failed"


def test_rf_t06_22_crosschecks_planned_once_and_capped(drv: Drv) -> None:
    """F06-07: standard runs plan U06-110 cross-check tasks once for must-cover usd numbers;
    a task cap below the plan logs `harness.crosscheck.skipped`."""
    drv.agents.usd = True
    res = asyncio.run(drv.swarm().start(drv.request(depth="standard")))
    assert res.status == "done"
    checks = [t for t in select_tasks(res.run_id) if t.spec.specialty == "crosscheck"]
    assert len(checks) == 1
    req = drv.request(depth="standard", budget_override={"max_tasks_per_run": 4})
    with structlog.testing.capture_logs() as logs:
        asyncio.run(drv.swarm().start(req))
    assert any(ev["event"] == "harness.crosscheck.skipped" for ev in logs)


def test_rf_t06_22_unknown_status_and_missing_plan(drv: Drv) -> None:
    """U06-82 step 2: an unknown status and a lost plan context are `ConfigError`s."""

    class _Steps:
        env = type("Env", (), {"run": type("Run", (), {"run_id": "run_x"})()})()

        async def ensure_plan_ctx(self) -> bool:
            return False

        async def running(self) -> None:  # pragma: no cover - never reached
            raise AssertionError

        created = planning = challenging = verifying = writing = recording = running

    swarm = drv.swarm()
    with pytest.raises(ConfigError, match="unknown run status bogus"):
        asyncio.run(swarm._step(_Steps(), "bogus"))  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="plan context unavailable"):
        asyncio.run(swarm._step(_Steps(), "running"))  # type: ignore[arg-type]


def test_rf_t06_22_env_gate_wait_metric_and_release(drv: Drv) -> None:
    """U06-82 step 1: the gates report waits to `herness_harness_gate_wait_seconds`; a stop
    releases tasks still `running` (R-36)."""
    from herness.core.jobs import claim_task  # noqa: PLC0415

    job = FakeJob()
    job.yield_when = lambda: (get_run(_only_run()) or object()).status == "running"  # type: ignore[union-attr]
    first = asyncio.run(drv.swarm().start(drv.request(), job))
    pool = ThreadPoolExecutor(1)
    parts = _run_env.EnvParts(
        cfg=drv.hcfg.pipelines, llms=drv.llms, memory=drv.memory,  # type: ignore[arg-type]
        deps=drv.deps, writer=pool,
    )  # fmt: skip
    run = get_run(first.run_id)
    stop = _run_env.Stopper(first.run_id, threading.Event(), None)
    task = next(t for t in select_tasks(first.run_id) if t.status == "pending")
    assert claim_task(task.task_id)

    async def go() -> int:
        env, _reader = _run_env.build_env(parts, run, None, force=False, stop=stop,  # type: ignore[arg-type]
                                          no_wildcards=False)  # fmt: skip
        gate = next(iter(env.gates.values()))
        async with gate:
            pass
        return await _run_ops.release_running(env)

    assert asyncio.run(go()) == 1
    pool.shutdown()
    assert "herness_harness_gate_wait_seconds" in {h[0] for h in drv.metrics.histograms}
    assert next(t for t in select_tasks(first.run_id) if t.task_id == task.task_id).status == (
        "pending"
    )


def test_rf_t06_22_build_env_without_planner_or_build(drv: Drv) -> None:
    """U06-82 step 1: a run without its planner task or build cannot be driven."""
    from dataclasses import replace  # noqa: PLC0415

    job = FakeJob()
    job.yield_when = lambda: True
    first = asyncio.run(drv.swarm().start(drv.request(), job))
    run = get_run(first.run_id)
    assert run is not None
    pool = ThreadPoolExecutor(1)
    parts = _run_env.EnvParts(
        cfg=drv.hcfg.pipelines, llms=drv.llms, memory=drv.memory,  # type: ignore[arg-type]
        deps=drv.deps, writer=pool,
    )  # fmt: skip
    stop = _run_env.Stopper(run.run_id, threading.Event(), None)
    with pytest.raises(ConfigError, match="has no build_id"):
        _run_env.build_env(parts, replace(run, build_id=None), None, force=False, stop=stop,
                           no_wildcards=False)  # fmt: skip
    with pytest.raises(NotFound, match="has no planner task"):
        _run_env.build_env(parts, replace(run, run_id="run_" + "8" * 26), None, force=False,
                           stop=stop, no_wildcards=False)  # fmt: skip
    pool.shutdown()


def test_rf_t06_22_composition_defaults(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> None:
    """U06-78/U06-85: `SwarmDeps.from_config`, `Swarm(deps=None)` and
    `build_swarm_from_config` compose from the loaded config; the default renderer resolves
    `renderer:report` at call time and the default tracer writes under `paths.data`."""
    deps = _run_env.SwarmDeps.from_config(drv.hcfg)
    assert deps.current_build is not None
    calls: list[tuple[str, list[str]]] = []
    registry.register("renderer", "report")(lambda run_id, fmts: calls.append((run_id, fmts)))
    deps.render("run_" + "1" * 26, ["html"])
    assert calls == [("run_" + "1" * 26, ["html"])]
    tracer = deps.tracer_factory("run_" + "1" * 26)
    tracer.close()
    swarm = Swarm(drv.hcfg.pipelines, drv.llms, drv.memory)  # type: ignore[arg-type]
    swarm.close()

    class _Memory:
        @staticmethod
        def from_config(cfg: Any) -> Any:
            del cfg
            return drv.memory

    import herness.harness.memory as memory_pkg  # noqa: PLC0415
    from herness.harness.llm import registry as llm_registry  # noqa: PLC0415

    monkeypatch.setattr(memory_pkg, "MemoryStore", _Memory)
    # The shipped `local` fallbacks name an off-network client; the test profile has no egress.
    monkeypatch.setattr(llm_registry, "LLMRegistry", lambda models, profile: drv.llms)
    built = handler.build_swarm_from_config(drv.hcfg)
    assert isinstance(built, Swarm)
    built.close()
    with pytest.raises(NotFound):
        deps.vectors.search_tickets([0.0], 1, entity=None, service_id=None)


def test_rf_t06_22_cancel_during_planning_releases_planner(drv: Drv) -> None:
    """U06-81: a cancel while the planner runs leaves no task `running`; the planner task is
    released with no attempt charged (R-36)."""
    drv.agents.planner = "block"
    swarm = drv.swarm()

    async def scenario() -> Any:
        drv.agents.running = asyncio.Event()
        start = asyncio.create_task(swarm.start(drv.request()))
        await drv.agents.running.wait()
        await swarm.cancel(_only_run())
        return await start

    res = asyncio.run(scenario())
    assert res.status == "canceled"
    planner = next(t for t in select_tasks(res.run_id) if t.role == "planner")
    assert (planner.status, planner.attempts) == ("pending", 0)
