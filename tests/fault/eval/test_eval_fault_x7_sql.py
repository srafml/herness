"""Spec 11 fault case X7 (impl 11 FT11-07; F11-11; R-40; T11-33).

An org review on the T06-22 run driver (real run steps, scheduler retries, Blackboard,
Writer step and draft; scripted agents) pinned to `tiny_build` (`_eval_it_env`), in-process,
under the JSON plan `sql.query error:QueryError role=analyst` (no `nth`, `count` or `p`: it
fires on every analyst query). Each scripted analyst runs its incident query through the real
recorded-SQL path (`herness.harness.tools.execute_recorded` -> `run_query`, whose
`fault_point("sql.query", role=ctx.role)` is the injection point) before it posts; the
`QueryError` it gets ends its task attempt, as an agent left without evidence would. The
plan is written with `tests.support.faults` and enabled in this process (impl 08 honours it
under `HERNESS_ENV=test`).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from tests.integration.eval._eval_it_env import EvalEnv, eval_env
from tests.integration.harness import _swarm_run_env as e
from tests.support import faults
from tests.unit.harness._blackboard_env import test_redactor

from herness.core.resilience import process_state
from herness.core.resilience._state import NOT_LOADED
from herness.core.types import ReportDraft
from herness.harness.tools import execute_recorded
from herness.store.ops import get_run, query_findings, select_tasks

pytestmark = pytest.mark.fault

drv = e.drv  # fixtures
__all__ = ["drv", "eval_env", "test_redactor"]

X7_RULE = {"point": "sql.query", "action": "error:QueryError", "role": "analyst"}
_SCHEMA = {"core": {"team": {"team_id": "VARCHAR", "name": "VARCHAR"}}}
_DRIVER_WRITER = e.writer_output


def _querying_analysts(env: EvalEnv, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Each scripted analyst first runs its team's incident query on the real recorded-SQL
    path; the errors it got are returned (class: message)."""
    agents = env.drv.agents
    posting = agents._analyst
    # the SQL guard reads the table list from the warehouse handle (the drv fake has none)
    monkeypatch.setattr(env.drv.pool.warehouse, "schema", lambda: _SCHEMA)
    errors: list[str] = []

    async def analyst(ctx: Any, hooks: Any) -> Any:
        team = e._team_of(ctx.task_id)
        try:
            await asyncio.to_thread(execute_recorded, ctx, e.INCIDENT_SQL, {"team": team})
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            raise
        return await posting(ctx, hooks)

    monkeypatch.setattr(agents, "_analyst", analyst)
    monkeypatch.setattr(e, "writer_output", _writer_output)
    return errors


def _writer_output(run_id: str) -> dict[str, Any]:
    """The scripted Writer: the driver's draft, or, with no verified finding to cite, a
    draft that cites nothing (what a Writer can still say after every analyst died)."""
    if any(f.entity_id == "t1" for f in query_findings(run_id, statuses={"verified"}, limit=500)):
        return _DRIVER_WRITER(run_id)
    return {"title": "Org review", "recommendations": [], "caveats": [], "sections": [],
            "prior_outcomes_commentary": None}  # fmt: skip


def _enable(plan: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in faults.fault_env(plan).items():
        monkeypatch.setenv(key, value)
    state = process_state()
    state.fault_plan = NOT_LOADED
    state.faults_enabled = False


def _org_review(env: EvalEnv) -> Any:
    build_id = env.build.build_id
    swarm = env.drv.swarm(current_build=lambda: build_id, build_exists=lambda b: b == build_id)
    return asyncio.run(swarm.start(env.drv.request("org_review", depth="standard")))


def _x7_run(env: EvalEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
            ) -> tuple[Any, list[Any], list[str]]:  # fmt: skip
    errors = _querying_analysts(env, monkeypatch)
    _enable(faults.write_fault_plan(tmp_path, [X7_RULE]), monkeypatch)
    res = _org_review(env)
    return res, [t for t in select_tasks(res.run_id) if t.role == "analyst"], errors


def test_ft11_07_sql_query_error_every_call_tasks_dead_run_partial(
    eval_env: EvalEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT11-07 X7 `sql.query error:QueryError role=analyst` on every call: the analyst tasks
    are `dead`, `ReportDraft.dead_tasks` lists them, and the run is `partial` because a
    must-cover task died ("after `max_attempts`" is pinned by the strict xfail below)."""
    res, analysts, errors = _x7_run(eval_env, tmp_path, monkeypatch)
    assert analysts
    assert {t.status for t in analysts} == {"dead"}
    assert any(t.spec.must_cover for t in analysts)  # team t1 is must-cover
    assert set(errors) == {"QueryError: fault: injected"}  # the injected error, every time
    plan = process_state().fault_plan
    assert plan is not None
    assert plan is not NOT_LOADED
    assert plan.counters == [len(errors)]  # every analyst query reached the fault point
    run = get_run(res.run_id)
    assert run is not None
    assert run.status == res.status == "partial"
    assert sorted(res.dead_task_ids) == sorted(t.task_id for t in analysts)
    assert res.draft_path is not None
    draft = ReportDraft.model_validate_json(Path(res.draft_path).read_bytes())
    listed = sorted((d["task_id"], d["role"]) for d in draft.dead_tasks)
    assert listed == sorted((t.task_id, "analyst") for t in analysts)
    assert all("QueryError" in str(d["last_error"]) for d in draft.dead_tasks)
    assert draft.coverage.dead_tasks == len(analysts)
    assert draft.coverage.must_cover_done < draft.coverage.must_cover_total


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "spec conflict (controller ruling): X7 expects analyst tasks dead after max_attempts, "
        "but a task failing with QueryError (a RecoverableError) is dead on its first attempt "
        "by design 08 §5.2 'Task' (T08 U08-62 fail_task retries RetryableError only); "
        "owners impl 11 §11.4 X7 vs impl 08 U08-62"
    ),
)
def test_ft11_07_dead_only_after_max_attempts(
    eval_env: EvalEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT11-07 X7 the analyst tasks die only after `swarm.max_task_attempts` attempts, each
    attempt meeting the injected `QueryError`."""
    _, analysts, errors = _x7_run(eval_env, tmp_path, monkeypatch)
    max_attempts = eval_env.drv.hcfg.pipelines.swarm.max_task_attempts
    assert max_attempts > 1
    assert {(t.status, t.attempts) for t in analysts} == {("dead", max_attempts)}
    assert len(errors) == len(analysts) * max_attempts


def test_ft11_07_control_without_plan_queries_succeed_run_not_partial_for_dead_tasks(
    eval_env: EvalEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT11-07 control: the same analysts without the plan run their query, no task dies and
    the draft lists no dead task (the X7 outcome comes from the injected errors)."""
    errors = _querying_analysts(eval_env, monkeypatch)
    res = _org_review(eval_env)
    assert errors == []
    analysts = [t for t in select_tasks(res.run_id) if t.role == "analyst"]
    assert {t.status for t in analysts} == {"done"}
    assert res.dead_task_ids == []
