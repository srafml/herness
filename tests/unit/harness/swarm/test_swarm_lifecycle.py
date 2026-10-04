"""UT06-54, UT06-55, UT06-57, UT06-91: run lifecycle helpers (U06-76, U06-77, U06-83, U06-84,
U06-86, U06-137)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import structlog
from pydantic import ValidationError
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness import _blackboard_env as env_mod
from tests.unit.harness._blackboard_env import BUILD_ID, FakeWarehouse

from herness.core import time as clock
from herness.core.config import HernessConfig, get_config
from herness.core.errors import ConfigError, FatalError, NotFound, SchemaViolation
from herness.core.ids import IdKind, new_id
from herness.core.jobs.ports import JobRow, bind_jobs_backend
from herness.core.jobs.tasks import claim_task
from herness.core.types import Coverage, EntityScope, TaskSpec
from herness.harness.blackboard import Blackboard
from herness.harness.findings import EntityCatalog, compute_dedup_key
from herness.harness.pipelines.settings import resolve_knobs
from herness.harness.swarm import lifecycle
from herness.harness.swarm.lifecycle import (
    RunRequest,
    RunResult,
    create_run_record,
    handle_budget_exhausted,
    request_config_hash,
    swarm_health,
)
from herness.harness.swarm.routing import default_tools, role_budget
from herness.metrics.portfolio import Scenario
from herness.store.ops import (
    RunRow,
    find_run_by_job,
    get_run,
    get_task,
    insert_run,
    insert_tasks,
    run_write,
    select_runs,
    select_tasks,
)
from herness.store.ops.jobs import SqliteJobsBackend

pytestmark = pytest.mark.unit

test_redactor = env_mod.test_redactor  # fixture
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
_OPEN = ("created", "planning", "running", "challenging", "verifying", "writing", "recording")
_ALL = (*_OPEN, "done", "partial", "failed", "canceled")


@pytest.fixture
def cfg(ops_store: OpsStoreHandle) -> HernessConfig:
    del ops_store
    return get_config()


@pytest.fixture
def writer_bb() -> Iterator[Blackboard]:
    bb = Blackboard(
        "run_" + "0" * 26,
        build_id=BUILD_ID,
        catalog=EntityCatalog(FakeWarehouse()),
        allowed_numerals=(),
    )
    try:
        yield bb
    finally:
        bb.close()


def _no_build() -> str | None:
    return None


def _create(bb: Blackboard, cfg: HernessConfig, req: RunRequest, **kw: Any) -> RunRow:
    values: dict[str, Any] = {
        "job_id": None,
        "escalated_from": None,
        "now": NOW,
        "current_build": lambda: BUILD_ID,
    }
    values.update(kw)
    return create_run_record(bb, cfg, req, **values)


# --- UT06-54 RunRequest, RunResult, create_run_record ----------------------------------------


def test_ut06_54_chat_without_question_rejected() -> None:
    """UT06-54 a chat request needs a question; review requests do not."""
    with pytest.raises(ValidationError):
        RunRequest(kind="chat")
    with pytest.raises(ValidationError):
        RunRequest(kind="chat", question="")
    assert RunRequest(kind="chat", question="why?").question == "why?"
    req = RunRequest(kind="org_review")
    assert (req.depth, req.profile, req.scenarios, req.budget_override) == (
        "standard",
        None,
        [],
        None,
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"unknown": 1},
        {"question": "q" * 2_001},
        {"scenarios": ["a"] * 6},
        {"scenarios": [""]},
        {"scenarios": ["s" * 65]},
        {"build_id": "not-a-build"},
        {"session_id": "s" * 65},
        {"profile": "cloud"},
        {"depth": "huge"},
    ],
)
def test_ut06_54_request_field_bounds(bad: dict[str, object]) -> None:
    """UT06-54 extra fields and out-of-bound values are rejected."""
    with pytest.raises(ValidationError):
        RunRequest.model_validate({"kind": "funding_review", **bad})


def test_ut06_54_request_accepts_json_input() -> None:
    """UT06-54 strict=False: a JSON payload validates (dates, focus, scenarios)."""
    req = RunRequest.model_validate_json(
        '{"kind": "funding_review", "depth": "deep", "profile": "hybrid",'
        ' "focus": {"entity_type": "team", "entity_ids": ["t1"], "period_start": "2026-01-01"},'
        ' "scenarios": ["base", "tight"], "build_id": "20260925-101500-ABCDEF",'
        ' "budget_override": {"max_tasks_per_run": 10}}'
    )
    assert req.focus is not None
    assert req.scenarios == ["base", "tight"]


def test_ut06_54_run_result_model() -> None:
    """UT06-54 RunResult carries the U06-77 fields and forbids extras."""
    cov = Coverage(
        planned_tasks=1,
        done_tasks=1,
        dead_tasks=0,
        must_cover_total=0,
        must_cover_done=0,
        verified_findings=0,
        rejected_findings=0,
        publishable=False,
    )
    res = RunResult(
        run_id="run_" + "1" * 26,
        status="partial",
        draft_path=None,
        coverage=cov,
        dead_task_ids=[],
        token_usage={"analysis": {}},
        cost_usd=Decimal("0.10"),
    )
    assert res.coverage.publishable is False
    with pytest.raises(ValidationError):
        RunResult.model_validate({**res.model_dump(), "extra": 1})


def test_ut06_54_create_writes_run_and_planner(writer_bb: Blackboard, cfg: HernessConfig) -> None:
    """UT06-54 create_run_record: run `created` with meta, and one pending planner task."""
    req = RunRequest(kind="org_review", depth="standard")
    escalated = {"session_id": "s1", "message_id": "m1"}
    run = _create(writer_bb, cfg, req, job_id="job_x", escalated_from=escalated)
    assert run.run_id.startswith("run_")
    assert len(run.run_id) == 30
    stored = get_run(run.run_id)
    assert stored == run
    assert (run.status, run.kind, run.depth, run.profile) == (
        "created",
        "org_review",
        "standard",
        cfg.profile,
    )
    assert (run.build_id, run.started_at, run.finished_at) == (BUILD_ID, NOW, None)
    assert run.config_hash == request_config_hash(cfg, req, cfg.profile)
    assert run.meta == {
        "request": req.model_dump(mode="json"),
        "stage": "main",
        "escalated_from": escalated,
        "render_error": None,
        "blocked_reason": None,
        "job_id": "job_x",
    }
    found = find_run_by_job("job_x")
    assert found is not None
    assert found.run_id == run.run_id
    tasks = select_tasks(run.run_id)
    assert len(tasks) == 1
    task = tasks[0]
    knobs = resolve_knobs(cfg.pipelines, "org_review", "standard")
    objective = "Plan the org_review review."
    assert (task.role, task.status, task.spec.objective) == ("planner", "pending", objective)
    assert task.spec.scope.entity_type == "run"
    assert task.spec.scope.entity_ids == [run.run_id]
    assert task.spec.model_role == "planner"
    assert task.spec.tools == default_tools(
        "planner", "general", "standard", child_depth=0, knobs=knobs
    )
    assert task.spec.budget == role_budget("planner", knobs, writer_tokens=0)
    key = compute_dedup_key("planner", "general", task.spec.scope, objective)
    assert task.spec.dedup_key == key


def test_ut06_54_request_profile_and_build_win(writer_bb: Blackboard, cfg: HernessConfig) -> None:
    """UT06-54 req.profile and req.build_id override cfg.profile and the promoted build."""
    req = RunRequest(kind="funding_review", profile="synth", build_id="20260101-000000-ABCDEF")

    def boom() -> str | None:
        raise AssertionError

    run = _create(writer_bb, cfg, req, current_build=boom)
    assert (run.profile, run.build_id) == ("synth", "20260101-000000-ABCDEF")
    assert run.config_hash == request_config_hash(cfg, req, "synth")
    assert run.meta["job_id"] is None


def _nothing_written() -> None:
    assert select_runs(statuses=_ALL) == []
    row = run_write(lambda c: c.execute("SELECT count(*) FROM task").fetchone(), op="t")
    assert row[0] == 0


def test_ut06_54_bad_override_writes_nothing(writer_bb: Blackboard, cfg: HernessConfig) -> None:
    """UT06-54 a budget_override outside the allowlist → ConfigError, no row written."""
    req = RunRequest(kind="funding_review", budget_override={"not_a_knob": 1})
    with pytest.raises(ConfigError):
        _create(writer_bb, cfg, req)
    _nothing_written()


def test_ut06_54_no_promoted_build_writes_nothing(
    writer_bb: Blackboard, cfg: HernessConfig
) -> None:
    """UT06-54 no promoted build → NotFound before any write (R-19)."""
    with pytest.raises(NotFound, match="no promoted warehouse build"):
        _create(writer_bb, cfg, RunRequest(kind="org_review"), current_build=_no_build)
    _nothing_written()


def test_ut06_54_chat_kind_rejected(writer_bb: Blackboard, cfg: HernessConfig) -> None:
    """UT06-54 create_run_record requires a review kind (chat → ConfigError, no rows)."""
    with pytest.raises(ConfigError):
        _create(writer_bb, cfg, RunRequest(kind="chat", question="q"))
    _nothing_written()


class _TaskInsertError(Exception):
    """Raised by the patched planner insert after the run row is in the transaction."""


def test_ut06_54_run_and_planner_in_one_transaction(
    writer_bb: Blackboard, cfg: HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT06-54 the planner insert fails after insert_run → neither row is kept (one transaction)."""
    seen: list[int] = []

    def failing_insert_tasks(conn: Any, specs: Any, *, now: datetime) -> None:
        insert_tasks(conn, specs, now=now)
        seen.append(conn.execute("SELECT count(*) FROM run").fetchone()[0])
        raise _TaskInsertError

    monkeypatch.setattr(lifecycle, "insert_tasks", failing_insert_tasks)
    with pytest.raises(FatalError, match="_TaskInsertError"):  # run_write maps it
        _create(writer_bb, cfg, RunRequest(kind="org_review"))
    assert seen == [1]  # the run row was written before the planner insert failed
    _nothing_written()


# --- UT06-55 request_config_hash ---------------------------------------------------------------


def test_ut06_55_hash_stable_and_depth_sensitive(cfg: HernessConfig) -> None:
    """UT06-55 same request (with focus) twice → equal hash; changed depth/focus → different."""
    focus = EntityScope(entity_type="team", entity_ids=["t1", "t2"], period_start=date(2026, 1, 1))
    req = RunRequest(
        kind="funding_review",
        focus=focus,
        scenarios=["base"],
        budget_override={"k_samples": 1},
    )
    same = RunRequest.model_validate_json(req.model_dump_json())
    assert same.focus == focus
    first = request_config_hash(cfg, req, "local")
    assert first == request_config_hash(cfg, same, "local")
    assert first.startswith("cfg_")
    assert len(first) == 20
    deep = req.model_copy(update={"depth": "deep"})
    assert request_config_hash(cfg, deep, "local") != first
    assert request_config_hash(cfg, req, "hybrid") != first
    other_focus = req.model_copy(update={"focus": focus.model_copy(update={"entity_ids": ["t1"]})})
    assert request_config_hash(cfg, other_focus, "local") != first


def test_ut06_55_hash_ignores_non_behavioural_fields(cfg: HernessConfig) -> None:
    """UT06-55 build_id and session_id are not part of the hash; question is."""
    req = RunRequest(kind="org_review")
    base = request_config_hash(cfg, req, "local")
    other = req.model_copy(update={"build_id": "20260101-000000-ABCDEF", "session_id": "s"})
    assert request_config_hash(cfg, other, "local") == base
    asked = req.model_copy(update={"question": "q"})
    assert request_config_hash(cfg, asked, "local") != base


def test_ut06_55_scenario_entry_round_trips_and_hashes(cfg: HernessConfig) -> None:
    """UT06-55 a `Scenario` entry round-trips through JSON and changes the config hash."""
    scenario = Scenario(name="custom_2000000", budget_usd=Decimal(2_000_000))
    req = RunRequest(kind="funding_review", scenarios=[scenario, "base"])
    dumped = req.model_dump(mode="json")
    assert dumped["scenarios"][0]["name"] == "custom_2000000"
    assert dumped["scenarios"][0]["budget_usd"] == "2000000"
    assert dumped["scenarios"][1] == "base"
    back = RunRequest.model_validate(dumped)
    assert back.scenarios == [scenario, "base"]
    assert RunRequest.model_validate_json(req.model_dump_json()).scenarios == [scenario, "base"]
    base = request_config_hash(cfg, req.model_copy(update={"scenarios": ["base"]}), "local")
    first = request_config_hash(cfg, req, "local")
    assert first != base
    assert request_config_hash(cfg, back, "local") == first
    other = Scenario(name="custom_2000000", budget_usd=Decimal(1_000_000))
    changed = req.model_copy(update={"scenarios": [other, "base"]})
    assert request_config_hash(cfg, changed, "local") != first


# --- UT06-57 swarm_health ----------------------------------------------------------------------


def _run(status: str, *, age_h: float, kind: str = "funding_review", job: str | None = None) -> str:
    run_id = new_id(IdKind.RUN)
    row = RunRow(
        run_id=run_id,
        kind=kind,
        depth="standard",
        profile="local",
        build_id=BUILD_ID,
        status=status,
        started_at=NOW - timedelta(hours=age_h),
        finished_at=None,
        token_usage={},
        cost_usd=Decimal(0),
        config_hash="cfg_" + "0" * 16,
        meta={"job_id": job},
    )
    assert run_write(lambda conn: insert_run(conn, row), op="test_run")
    return run_id


def _job(status: str, payload: dict[str, Any], job_id: str | None = None) -> JobRow:
    return JobRow(
        job_id=job_id or new_id(IdKind.JOB),
        kind="review",
        gpu_class="reasoning",
        status=status,  # type: ignore[arg-type]
        priority=50,
        attempts=0,
        max_attempts=3,
        payload=payload,
    )


class _Jobs:
    """Fake `list_jobs` recording its calls."""

    def __init__(self, *jobs: JobRow) -> None:
        self.jobs = jobs
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kw: Any) -> list[JobRow]:
        self.calls.append(kw)
        return [j for j in self.jobs if j.status == kw.get("status")]


def _alive() -> bool:
    return True


def _dead() -> bool:
    return False


def test_ut06_57_healthy(ops_store: OpsStoreHandle) -> None:
    """UT06-57 no open runs, or only recent/terminal/chat ones with a live worker → ok."""
    del ops_store
    assert swarm_health(list_jobs=_Jobs(), worker_alive=_dead, now=NOW)["status"] == "ok"
    _run("running", age_h=1)
    _run("done", age_h=100)
    _run("running", age_h=100, kind="chat")
    jobs = _Jobs()
    assert swarm_health(list_jobs=jobs, worker_alive=_alive, now=NOW) == {
        "status": "ok",
        "reason": "",
    }


def test_ut06_57_stalled_run_degraded(ops_store: OpsStoreHandle) -> None:
    """UT06-57 an open run older than 24 h with no queued/running review job → degraded."""
    del ops_store
    run_id = _run("challenging", age_h=25)
    unrelated = _job("queued", {"run_id": "run_" + "9" * 26})
    jobs = _Jobs(unrelated, _job("done", {"run_id": run_id}))
    health = swarm_health(list_jobs=jobs, worker_alive=_alive, now=NOW)
    assert health == {"status": "degraded", "reason": f"stalled run {run_id}"}
    assert sorted(c["status"] for c in jobs.calls) == ["queued", "running"]
    assert all(c["kind"] == "review" and c["limit"] == 50 for c in jobs.calls)


def test_ut06_57_exactly_24h_is_not_stalled(ops_store: OpsStoreHandle) -> None:
    """UT06-57 a run started exactly 24 h ago is not "more than 24 h" old → ok."""
    del ops_store
    _run("running", age_h=24)
    _run("planning", age_h=24 - 1 / 3600)
    assert swarm_health(list_jobs=_Jobs(), worker_alive=_alive, now=NOW) == {
        "status": "ok",
        "reason": "",
    }


def test_ut06_57_just_over_24h_is_stalled(ops_store: OpsStoreHandle) -> None:
    """UT06-57 a run started 24 h and one second ago with no job → degraded, stalled."""
    del ops_store
    run_id = _run("running", age_h=24 + 1 / 3600)
    health = swarm_health(list_jobs=_Jobs(), worker_alive=_alive, now=NOW)
    assert health == {"status": "degraded", "reason": f"stalled run {run_id}"}


def test_ut06_57_old_run_with_job_is_ok(ops_store: OpsStoreHandle) -> None:
    """UT06-57 an old open run referenced by payload.run_id or meta.job_id is not stalled."""
    del ops_store
    by_payload = _run("running", age_h=30)
    job_id = new_id(IdKind.JOB)
    _run("planning", age_h=30, job=job_id)
    jobs = _Jobs(
        _job("running", {"run_id": by_payload}),
        _job("queued", {"request": {"kind": "org_review"}}, job_id=job_id),
    )
    assert swarm_health(list_jobs=jobs, worker_alive=_alive, now=NOW)["status"] == "ok"


def test_ut06_57_no_live_worker_degraded(ops_store: OpsStoreHandle) -> None:
    """UT06-57 open review runs while worker_alive() is false → degraded, no live worker."""
    del ops_store
    _run("running", age_h=1)
    _run("created", age_h=2, kind="org_review")
    jobs = _Jobs()
    health = swarm_health(list_jobs=jobs, worker_alive=_dead, now=NOW)
    assert health == {"status": "degraded", "reason": "no live worker for 2 open runs"}
    assert jobs.calls == []


def test_ut06_57_unreadable_store_down(
    ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT06-57 the ops store cannot be read → down with the error class name."""
    del ops_store

    def unreadable(**kw: object) -> list[RunRow]:
        msg = "bad row"
        raise SchemaViolation(msg)

    monkeypatch.setattr(lifecycle, "select_runs", unreadable)
    health = swarm_health(list_jobs=_Jobs(), worker_alive=_alive, now=NOW)
    assert health == {"status": "down", "reason": "SchemaViolation"}


def test_ut06_57_unreadable_jobs_down(ops_store: OpsStoreHandle) -> None:
    """UT06-57 a HernessError reading review jobs → down with the error class name."""
    del ops_store
    _run("running", age_h=48)

    def broken(**kw: object) -> list[JobRow]:
        msg = "bad filter"
        raise ConfigError(msg)

    health = swarm_health(list_jobs=broken, worker_alive=_alive, now=NOW)
    assert health == {"status": "down", "reason": "ConfigError"}


def test_ut06_57_real_store_unreadable_down(ops_store: OpsStoreHandle) -> None:
    """UT06-57 a dropped run table (real store read error) → down."""
    del ops_store
    run_write(lambda conn: conn.execute("ALTER TABLE run RENAME TO run_gone"), op="test_drop")
    health = swarm_health(list_jobs=_Jobs(), worker_alive=_alive, now=NOW)
    assert health["status"] == "down"
    assert health["reason"]


# --- UT06-91 handle_budget_exhausted -----------------------------------------------------------


def _task(run_id: str, role: str, n: int) -> TaskSpec:
    values: dict[str, Any] = {
        "task_id": new_id(IdKind.TASK),
        "run_id": run_id,
        "role": role,
        "objective": f"objective {role} {n}",
        "scope": {"entity_type": "team", "entity_ids": ["t1"]},
        "tools": ["post_finding"],
        "budget": {"max_steps": 5, "max_tokens": 5_000, "wall_clock_s": 60},
        "model_role": role,
        "dedup_key": f"{n:016x}",
    }
    if role == "skeptic":
        values.update(round=1, inputs={"finding_ids": [new_id(IdKind.FINDING)]})
    return TaskSpec.model_validate(values)


def test_ut06_91_pending_tasks_dead_and_run_verifying(
    ops_store: OpsStoreHandle, test_redactor: object, writer_bb: Blackboard
) -> None:
    """UT06-91 pending analyst and skeptic tasks → dead BudgetExceeded run_budget; run verifying."""
    del ops_store, test_redactor
    bind_jobs_backend(SqliteJobsBackend())
    run_id = _run("running", age_h=1)
    analyst, skeptic = _task(run_id, "analyst", 1), _task(run_id, "skeptic", 2)
    busy, writer = _task(run_id, "analyst", 3), _task(run_id, "writer", 4)
    specs = [analyst, skeptic, busy, writer]
    run_write(lambda conn: insert_tasks(conn, specs, now=clock.now()), op="test_tasks")
    assert claim_task(busy.task_id)
    with structlog.testing.capture_logs() as logs:
        dead = asyncio.run(handle_budget_exhausted(writer_bb, run_id, max_task_attempts=3, now=NOW))
    assert dead == 2
    for spec in (analyst, skeptic):
        row = get_task(spec.task_id)
        assert row is not None
        assert row.status == "dead"
        assert row.last_error is not None
        assert (row.last_error["class"], row.last_error["message"]) == (
            "BudgetExceeded",
            "run_budget",
        )
    statuses = {s.task_id: getattr(get_task(s.task_id), "status", None) for s in (busy, writer)}
    assert statuses == {busy.task_id: "running", writer.task_id: "pending"}
    run = get_run(run_id)
    assert run is not None
    assert run.status == "verifying"
    events = [e for e in logs if e["event"] == "harness.budget.exhausted"]
    assert len(events) == 1
    event = events[0]
    assert event["log_level"] == "warning"
    assert {k: event[k] for k in ("run_id", "phase", "tasks_dead")} == {
        "run_id": run_id,
        "phase": "analysis",
        "tasks_dead": 2,
    }
    assert set(event) - {"event", "log_level", "run_id", "phase", "tasks_dead"} <= {"component"}


def test_ut06_91_no_pending_and_other_status(
    ops_store: OpsStoreHandle, test_redactor: object, writer_bb: Blackboard
) -> None:
    """UT06-91 nothing pending → 0 dead; a run outside running/challenging keeps its status."""
    del ops_store, test_redactor
    bind_jobs_backend(SqliteJobsBackend())
    run_id = _run("writing", age_h=1)
    dead = asyncio.run(handle_budget_exhausted(writer_bb, run_id, max_task_attempts=3, now=NOW))
    assert dead == 0
    run = get_run(run_id)
    assert run is not None
    assert run.status == "writing"


def test_ut06_91_lost_claim_is_skipped(
    ops_store: OpsStoreHandle,
    test_redactor: object,
    writer_bb: Blackboard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT06-91 a task claimed elsewhere between read and claim is left alone."""
    del ops_store, test_redactor
    bind_jobs_backend(SqliteJobsBackend())
    run_id = _run("challenging", age_h=1)
    spec = _task(run_id, "analyst", 7)
    run_write(lambda conn: insert_tasks(conn, [spec], now=clock.now()), op="test_tasks")
    monkeypatch.setattr(lifecycle, "claim_task", lambda task_id: False)
    dead = asyncio.run(handle_budget_exhausted(writer_bb, run_id, max_task_attempts=3, now=NOW))
    assert dead == 0
    row = get_task(spec.task_id)
    assert row is not None
    assert row.status == "pending"
    run = get_run(run_id)
    assert run is not None
    assert run.status == "verifying"
