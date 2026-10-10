"""T06-22 review round 1 (`test_rf_t06_22_*`): driver error rules, no-wildcards persistence,
usage persistence and restore, plan-context retries, setup failures, the TH06-13 config tie."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv, FakeJob

from herness.core.errors import ConfigError, StoreBusy
from herness.harness.swarm import _run_env, _run_steps, record
from herness.harness.swarm.run import Swarm
from herness.store.ops import get_run, select_runs, select_tasks

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


def _usage(run_id: str) -> dict[str, Any]:
    run = get_run(run_id)
    assert run is not None
    return run.token_usage


def test_rf_t06_22_non_herness_step_error_fails_run(
    drv: Drv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U06-82 step 8 (review I1): a non-HernessError escaping a step (a ValueError from the
    record step) fails the run (CAS, `harness.run.failed`) and propagates; a retry then
    returns the terminal result instead of crashing again."""

    async def broken(env: Any, render: Any) -> None:
        del env, render
        msg = "record step bug"
        raise ValueError(msg)

    monkeypatch.setattr(record, "run_record_step", broken)
    with structlog.testing.capture_logs() as logs, pytest.raises(ValueError, match="bug"):
        asyncio.run(drv.swarm().start(drv.request()))
    run_id = _only_run()
    assert _status(run_id) == "failed"
    failed = [ev for ev in logs if ev["event"] == "harness.run.failed"]
    assert failed[0]["error_type"] == "ValueError"
    assert asyncio.run(drv.swarm().resume(run_id)).status == "failed"


def test_rf_t06_22_no_wildcards_survives_a_preempted_forced_restart(drv: Drv) -> None:
    """Review M2: a forced dead-planner restart preempted before planning commits keeps
    `meta.no_wildcards`; the next unforced resume plans without a Planner model call."""
    drv.agents.planner = "fail"
    res = asyncio.run(drv.swarm().start(drv.request(depth="standard")))
    assert res.status == "failed"
    drv.agents.planner = "ok"
    job = FakeJob()
    job.yield_when = lambda: _status(res.run_id) == "planning"
    stopped = asyncio.run(drv.swarm().resume(res.run_id, job, force=True))  # type: ignore[arg-type]
    assert stopped.status == "planning"
    run = get_run(res.run_id)
    assert run is not None
    assert run.meta["no_wildcards"] is True
    done = asyncio.run(drv.swarm().resume(res.run_id))
    assert done.status == "done"
    assert drv.agents.calls["planner"] == 3  # only the three failed attempts
    run = get_run(res.run_id)
    assert run is not None
    assert run.meta["no_wildcards"] is False


def test_rf_t06_22_usage_persisted_and_restored(drv: Drv) -> None:
    """Review M3: usage is on the run after planning, and the ledgers resume from
    `run.token_usage` (analysis tokens charged before a stop are kept, not reset)."""
    drv.agents.charge = 1_000
    job = FakeJob()
    job.yield_when = lambda: _status(_only_run()) == "running"
    first = asyncio.run(drv.swarm().start(drv.request(depth="standard"), job))  # type: ignore[arg-type]
    assert set(_usage(first.run_id)) == {"analysis", "writer", "by_role"}
    job.yield_when = lambda: _status(first.run_id) == "challenging"
    asyncio.run(drv.swarm().resume(first.run_id, job))  # type: ignore[arg-type]
    before = _usage(first.run_id)["analysis"]["tokens_in"]
    assert before == 3_000  # three analyst tasks, 1,000 each
    res = asyncio.run(drv.swarm().resume(first.run_id))
    assert res.status == "done"
    usage = _usage(first.run_id)
    assert usage["analysis"]["tokens_in"] == before
    assert usage["writer"]["name"] == "writer"


def test_rf_t06_22_retryable_plan_context_error_charges_nothing(
    drv: Drv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M4: a RetryableError building the plan context propagates with no status change
    and no planner attempt charged."""

    async def busy(*args: Any, **kw: Any) -> Any:
        del args, kw
        msg = "busy"
        raise StoreBusy(msg)

    monkeypatch.setattr(_run_steps, "build_plan_context", busy)
    with pytest.raises(StoreBusy):
        asyncio.run(drv.swarm().start(drv.request()))
    run_id = _only_run()
    assert _status(run_id) == "planning"
    planner = next(t for t in select_tasks(run_id) if t.role == "planner")
    assert (planner.status, planner.attempts) == ("pending", 0)


def test_rf_t06_22_setup_failure_fails_run(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review M5: a setup error in `build_env` fails the run per U06-82 step 8; a
    RetryableError leaves it unchanged."""
    job = FakeJob()
    job.yield_when = lambda: True
    first = asyncio.run(drv.swarm().start(drv.request(), job))  # type: ignore[arg-type]
    status = _status(first.run_id)

    def busy(*args: Any, **kw: Any) -> Any:
        del args, kw
        msg = "busy"
        raise StoreBusy(msg)

    monkeypatch.setattr(_run_env, "get_pipeline", busy)
    with pytest.raises(StoreBusy):
        asyncio.run(drv.swarm().resume(first.run_id))
    assert _status(first.run_id) == status

    def broken(*args: Any, **kw: Any) -> Any:
        del args, kw
        msg = "no pipeline"
        raise ConfigError(msg)

    monkeypatch.setattr(_run_env, "get_pipeline", broken)
    with structlog.testing.capture_logs() as logs, pytest.raises(ConfigError, match="no pipe"):
        asyncio.run(drv.swarm().resume(first.run_id))
    assert _status(first.run_id) == "failed"
    errors = [ev["error_type"] for ev in logs if ev["event"] == "harness.run.failed"]
    assert errors == ["ConfigError"]


def test_rf_t06_22_cfg_must_match_hashed_config(drv: Drv) -> None:
    """Review M7c / TH06-13: the knobs config must be the hashed `deps.hcfg.pipelines`."""
    swarm_cfg = drv.hcfg.pipelines.swarm.model_copy(update={"verifier_batch": 3})
    other = drv.hcfg.pipelines.model_copy(update={"swarm": swarm_cfg})
    with pytest.raises(ConfigError, match=r"deps\.hcfg\.pipelines"):
        Swarm(other, drv.llms, drv.memory, deps=drv.deps)  # type: ignore[arg-type]
