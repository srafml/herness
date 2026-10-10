"""IT06-03, IT06-15, IT06-17 … IT06-21, IT06-32, ST06-13: the run driver over real swarm steps.

Every swarm step runs for real over the migrated ops store, the Blackboard writer and the spec
08 task helpers; only `run_agent` is scripted per role, and the Verifier, memory, renderer and
pipeline are fakes (`_swarm_run_env`). A "kill" at a step boundary is a preemption seen by the
loop at that status (the job's `should_yield`), or a task left `running` as a dead process
leaves it; resume then runs on a fresh `Swarm`.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import ALL_STATUSES, Drv, FakeJob

from herness.core import config as c
from herness.core import redact as r
from herness.core.errors import ConfigError
from herness.core.jobs import claim_task, enqueue, get
from herness.core.types import ReportDraft
from herness.harness import tools as tools_mod
from herness.harness.swarm import _run_env, _run_steps, handler
from herness.harness.swarm.lifecycle import RunResult
from herness.harness.swarm.planner import build_plan_context
from herness.metrics.portfolio import PortfolioResult
from herness.store.ops import find_run_by_job, get_run, query_findings, select_tasks

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
test_redactor = e.env_mod.test_redactor
BOUNDARIES = ("planning", "running", "challenging", "verifying", "writing", "recording")


def _changes(logs: list[dict[str, Any]], run_id: str) -> list[str]:
    seq = [ev for ev in logs if ev["event"] == "harness.run.status_changed"]
    seq = [ev for ev in seq if ev["run_id"] == run_id]
    return [seq[0]["from"], *(ev["to"] for ev in seq)] if seq else []


def _draft(res: RunResult) -> ReportDraft:
    assert res.draft_path is not None
    return ReportDraft.model_validate_json(Path(res.draft_path).read_bytes())


def _status(run_id: str) -> str:
    run = get_run(run_id)
    assert run is not None
    return run.status


def _job_status(job: FakeJob) -> str | None:
    run = find_run_by_job(job.job_id)
    return None if run is None else run.status


# --- IT06-32 / IT06-01 / IT06-02 (driver level)


def test_it06_32_standard_run_status_sequence(drv: Drv) -> None:
    """IT06-32 full standard run: `harness.run.status_changed` events walk design 06 §5.1;
    usage, render, `rec_id`s, metrics and the tracer close at the end."""
    swarm = drv.swarm()
    with structlog.testing.capture_logs() as logs:
        res = asyncio.run(swarm.start(drv.request(depth="standard")))
    assert res.status == "done"
    assert _changes(logs, res.run_id) == list(ALL_STATUSES)
    run = get_run(res.run_id)
    assert run is not None
    assert run.finished_at is not None
    assert set(run.token_usage) == {"analysis", "writer", "by_role"}
    assert run.token_usage["by_role"]["analyst"]["input"] > 0  # type: ignore[index]
    assert drv.renders == [(res.run_id, list(drv.hcfg.app.reports.formats))]
    draft = _draft(res)
    assert draft.coverage.publishable is True
    assert [r.rec_id for r in draft.recommendations] == drv.memory.recs[res.run_id]
    assert all(t.status == "done" for t in select_tasks(res.run_id))
    assert {"planner", "analyst", "skeptic", "writer"} <= set(drv.agents.calls)
    finished = [ev for ev in logs if ev["event"] == "harness.run.finished"]
    assert finished[0]["status"] == "done"
    labels = {"kind": "funding_review", "status": "done"}
    assert ("herness_harness_runs_total", labels) in drv.metrics.counters
    assert drv.metrics.histograms[-1][0] == "herness_harness_run_duration_seconds"


@pytest.mark.parametrize("kind", ["funding_review", "org_review"])
def test_it06_01_it06_02_scripted_run_done_and_recorded(drv: Drv, kind: str) -> None:
    """IT06-01 / IT06-02 (driver level): a scripted fast run ends `done`; the plan equals the
    deterministic plan; recommendations cite verified findings; `rec_id`s are filled."""
    res = asyncio.run(drv.swarm().start(drv.request(kind=kind, depth="fast")))
    assert res.status == "done"
    analysts = [t for t in select_tasks(res.run_id) if t.role == "analyst"]
    assert sorted(t.spec.dedup_key for t in analysts) == sorted(
        s.dedup_key for s in e.StubPipeline(kind, 2, 1, t2_must=False).deterministic_tasks(
            e.plan_ctx_for(get_run(res.run_id))
        )
    )  # fmt: skip
    draft = _draft(res)
    verified = {f.finding_id for f in query_findings(res.run_id, statuses={"verified"}, limit=50)}
    assert draft.recommendations
    assert all(set(r.finding_ids) <= verified for r in draft.recommendations)
    assert all(r.rec_id for r in draft.recommendations)
    assert draft.verification.passed is True


# --- kills at every step boundary


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_it06_32_resume_after_kill_at_each_boundary(drv: Drv, boundary: str) -> None:
    """IT06-32 a drive stopped at each step boundary resumes on a fresh Swarm to `done`:
    no task reruns, no finding is posted twice, the status sequence stays §5.1."""
    job = FakeJob()
    job.yield_when = lambda: _job_status(job) == boundary
    with structlog.testing.capture_logs() as logs:
        first = asyncio.run(drv.swarm().start(drv.request(depth="standard"), job))
        assert first.status == boundary
        calls = dict(drv.agents.calls)
        findings = len(query_findings(first.run_id, limit=500))
        res = asyncio.run(drv.swarm().resume(first.run_id))
    assert res.status == "done"
    assert _changes(logs, res.run_id) == list(ALL_STATUSES)
    assert drv.agents.calls["planner"] == 1
    assert drv.agents.calls["analyst"] == 3  # two t1 tasks and one t2 task, once each
    if boundary in {"writing", "recording"}:
        assert len(query_findings(res.run_id, limit=500)) == findings
        assert calls == {k: v for k, v in drv.agents.calls.items() if k != "writer"} | (
            {"writer": calls["writer"]} if "writer" in calls else {}
        )
    assert all(t.attempts == 1 for t in select_tasks(res.run_id))


def test_it06_32_resume_resets_a_task_left_running(drv: Drv) -> None:
    """IT06-32 a process killed mid fan-out leaves a task `running`; resume recovers it
    (`recover_run_tasks`) and finishes the run."""
    job = FakeJob()
    job.yield_when = lambda: _job_status(job) == "running"
    first = asyncio.run(drv.swarm().start(drv.request(), job))
    pending = [t for t in select_tasks(first.run_id) if t.status == "pending"]
    assert claim_task(pending[0].task_id)  # what a killed worker leaves behind
    with structlog.testing.capture_logs() as logs:
        res = asyncio.run(drv.swarm().resume(first.run_id))
    assert res.status == "done"
    resumed = next(ev for ev in logs if ev["event"] == "harness.run.resumed")
    assert resumed["recovered"]["running"] == 1


# --- IT06-19 cancel and preemption


def test_it06_19_cancel_mid_fan_out(drv: Drv) -> None:
    """IT06-19 cancel during fan-out: the run is `canceled`, its review job canceled, no task
    left running, the stopped task released with no attempt charged; cancel is idempotent."""
    drv.agents.analyst = {"t2": "block"}
    swarm = drv.swarm()

    async def scenario() -> tuple[RunResult, str]:
        drv.agents.running = asyncio.Event()
        start = asyncio.create_task(swarm.start(drv.request()))
        await drv.agents.running.wait()
        run_id = select_tasks_any_run()
        job_id = enqueue("review", {"run_id": run_id, "resume": True}, "reasoning")
        await swarm.cancel(run_id)
        await swarm.cancel(run_id)  # idempotent
        return await start, job_id

    with structlog.testing.capture_logs() as logs:
        res, job_id = asyncio.run(scenario())
    assert res.status == "canceled"
    tasks = select_tasks(res.run_id)
    assert not [t for t in tasks if t.status == "running"]
    blocked = next(t for t in tasks if t.spec.scope.entity_ids == ["t2"])
    assert (blocked.status, blocked.attempts) == ("pending", 0)
    assert get(job_id).status == "canceled"
    to_canceled = [ev for ev in logs if ev["event"] == "harness.run.status_changed"
                   and ev["to"] == "canceled"]  # fmt: skip
    assert len(to_canceled) == 1
    asyncio.run(drv.swarm().cancel(res.run_id))  # a terminal run: nothing changes
    assert _status(res.run_id) == "canceled"


def select_tasks_any_run() -> str:
    """The one run of the test store."""
    from herness.store.ops import select_runs  # noqa: PLC0415

    runs = select_runs(statuses={"running"})
    assert len(runs) == 1
    return runs[0].run_id


def test_it06_19_preempt_yields_then_resume_finishes(
    drv: Drv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-19 preemption: the handler answers `yield` with the run still open; the next job
    start resumes it to `done` and the preempted task is charged no attempt."""
    drv.agents.analyst = {"t2": "block"}
    monkeypatch.setattr(handler, "build_swarm_from_config", lambda cfg: drv.swarm())
    request = drv.request().model_dump(mode="json")
    job = FakeJob({"request": request})
    job.yield_when = lambda: drv.agents.running is not None and drv.agents.running.is_set()

    def first_run() -> Any:
        drv.agents.running = asyncio.Event()
        return handler.review_job_handler(job)  # type: ignore[arg-type]

    out = first_run()
    assert out.status == "yield"
    run_id = str(out.result["run_id"])
    assert out.result["status"] == "running"
    assert not [t for t in select_tasks(run_id) if t.status == "running"]
    drv.agents.analyst = {}
    job.yield_when = lambda: False
    out = handler.review_job_handler(job)  # type: ignore[arg-type]
    assert out.status == "done"
    assert out.result == {"run_id": run_id, "status": "done", "partial": False}
    t2 = next(t for t in select_tasks(run_id) if t.spec.scope.entity_ids == ["t2"])
    assert (t2.status, t2.attempts) == ("done", 1)


def test_it06_19_force_resume_of_a_canceled_run(drv: Drv) -> None:
    """IT06-19 `resume --force` restarts a canceled run at `planning` and finishes it."""
    job = FakeJob()
    job.yield_when = lambda: _job_status(job) == "running"
    first = asyncio.run(drv.swarm().start(drv.request(), job))
    asyncio.run(drv.swarm().cancel(first.run_id))
    assert asyncio.run(drv.swarm().resume(first.run_id)).status == "canceled"
    res = asyncio.run(drv.swarm().resume(first.run_id, force=True))
    assert res.status == "done"


# --- IT06-18, ST06-13 resume guards


def _changed_config(drv: Drv) -> c.HernessConfig:
    """The test config with `pipelines.yaml` edited (one swarm knob)."""
    path = drv.cfg_root / "config" / "pipelines.yaml"
    path.write_text("version: 1\nswarm:\n  verifier_batch: 7\n", encoding="utf-8")
    redactor = r._State.redactor  # the config reset drops the test redactor
    c.reset_config()
    data = drv.hcfg.paths.data
    changed = c.init_config(
        "local", (f"paths.data={Path(data).as_posix()}",), config_dir=path.parent, env={}
    )
    r._State.redactor = redactor
    return changed


def _stopped_run(drv: Drv) -> RunResult:
    job = FakeJob()
    job.yield_when = lambda: _job_status(job) == "running"
    return asyncio.run(drv.swarm().start(drv.request(), job))


def test_st06_13_changed_pipelines_yaml_refuses_resume(drv: Drv) -> None:
    """ST06-13 TH06-13: after `pipelines.yaml` changes, resume is refused and the run row and
    its tasks are unchanged; `force` overrides the guard."""
    first = _stopped_run(drv)
    before, tasks = get_run(first.run_id), select_tasks(first.run_id)
    changed = _changed_config(drv)
    with structlog.testing.capture_logs() as logs, pytest.raises(ConfigError) as err:
        asyncio.run(drv.swarm(changed).resume(first.run_id))
    assert str(err.value) == f"config changed since run start: run_id={first.run_id}"
    assert get_run(first.run_id) == before
    assert select_tasks(first.run_id) == tasks
    refused = [ev for ev in logs if ev["event"] == "harness.run.resume_refused"]
    assert refused[0]["reason"] == "config_changed"
    assert asyncio.run(drv.swarm(changed).resume(first.run_id, force=True)).status == "done"


def test_it06_18_resume_guards(drv: Drv) -> None:
    """IT06-18 changed config → `ConfigError`, run unchanged; retired build → `failed` with
    `meta.blocked_reason = "build_retired"` and `ConfigError`."""
    first = _stopped_run(drv)
    with pytest.raises(ConfigError, match="config changed since run start"):
        asyncio.run(drv.swarm(_changed_config(drv)).resume(first.run_id))
    assert _status(first.run_id) == "running"
    retired = drv.swarm(build_exists=lambda build_id: False)
    with pytest.raises(ConfigError) as err:
        asyncio.run(retired.resume(first.run_id, force=True))
    assert str(err.value) == f"build retired; start a new run: run_id={first.run_id}"
    run = get_run(first.run_id)
    assert run is not None
    assert (run.status, run.meta["blocked_reason"]) == ("failed", "build_retired")


# --- IT06-20 planner dead


def test_it06_20_planner_dead_then_forced_resume(drv: Drv) -> None:
    """IT06-20 the planner fails three times: run `failed`; `resume --force` plans the
    deterministic tasks only (no model call, `H_wildcards = 0`) and finishes the run."""
    drv.agents.planner = "fail"
    res = asyncio.run(drv.swarm().start(drv.request(depth="standard")))
    assert res.status == "failed"
    planner_row = next(t for t in select_tasks(res.run_id) if t.role == "planner")
    assert (planner_row.status, planner_row.attempts) == ("dead", 3)
    assert drv.agents.calls["planner"] == 3
    drv.agents.planner = "ok"
    resumed = asyncio.run(drv.swarm().resume(res.run_id, force=True))
    assert resumed.status == "done"
    assert drv.agents.calls["planner"] == 3  # the forced plan made no model call
    analysts = [t for t in select_tasks(res.run_id) if t.role == "analyst" and not t.spec.round]
    assert all(t.spec.priority >= 1 for t in analysts)
    assert len([t for t in analysts if t.spec.specialty != "crosscheck"]) == 3


# --- IT06-21 writer dead, IT06-15 coverage, IT06-03 budget


def test_it06_21_writer_dead_findings_only_draft(drv: Drv) -> None:
    """IT06-21 the Writer fails: run `partial`; `draft.json` is `findings_only` with the
    verified findings and no Writer paragraphs (R-49); the partial run is rendered."""
    drv.agents.writer = "fail"
    res = asyncio.run(drv.swarm().start(drv.request()))
    assert res.status == "partial"
    draft = _draft(res)
    assert draft.mode == "findings_only"
    verified = query_findings(res.run_id, statuses={"verified"}, limit=50)
    claims = [p.text for s in draft.sections for p in s.paragraphs]
    assert claims
    assert set(claims) <= {f.claim for f in verified}
    assert draft.recommendations == []
    assert drv.renders == [(res.run_id, list(drv.hcfg.app.reports.formats))]
    assert drv.memory.writes == 0


def test_it06_15_dead_must_cover_task_partial(drv: Drv) -> None:
    """IT06-15 a dead must-cover task with 95 % of the planned tasks done: `partial`, and the
    entity is listed in `flags.partial_coverage`."""
    drv.set_pipeline(t1=19, t2=1, t2_must=True)
    drv.agents.analyst = {"t2": "fail"}
    res = asyncio.run(drv.swarm().start(drv.request()))
    assert res.status == "partial"
    assert res.coverage.done_tasks / res.coverage.planned_tasks == pytest.approx(0.95)
    assert "team:t2" in _draft(res).flags["partial_coverage"]
    assert drv.memory.writes == 0


def test_it06_03_budget_exhausted_partial(drv: Drv) -> None:
    """IT06-03 a run budget well below the need: `partial`, banner `budget_exhausted`, draft
    written, pending tasks dead with `run_budget`, no recommendations recorded."""
    drv.set_pipeline(t1=10, t2=10)
    drv.agents.charge = 30_000
    req = drv.request(budget_override={"run_tokens": 100_000})
    with structlog.testing.capture_logs() as logs:
        res = asyncio.run(drv.swarm().start(req))
    assert res.status == "partial"
    draft = _draft(res)
    assert "budget_exhausted" in draft.banners
    dead = [t for t in select_tasks(res.run_id) if t.status == "dead"]
    assert {str((t.last_error or {}).get("class")) for t in dead} == {"BudgetExceeded"}
    assert any("run_budget" in str((t.last_error or {}).get("message")) for t in dead)
    assert drv.memory.writes == 0
    assert any(ev["event"] == "harness.budget.exhausted" for ev in logs)
    run = get_run(res.run_id)
    assert run is not None
    assert run.token_usage["analysis"]["exhausted"] is True  # type: ignore[index]


# --- IT06-17 custom scenario


class _Reader:
    """A recorded reader answering the U06-87 DQ and stored-scenario reads."""

    def __call__(self, sql: str, params: dict[str, Any]) -> tools_mod.RecordedResult:
        del params
        cols = ["scenario"] if "DISTINCT scenario" in sql else ["check_name"]
        rows: list[tuple[object, ...]] = [("base",)] if cols == ["scenario"] else []
        return tools_mod.RecordedResult(
            query_id="q_" + "b" * 16, columns=cols, types=["VARCHAR"], rows=rows,
            row_count=len(rows), truncated=False, ordered=True,
        )  # fmt: skip


def test_it06_17_custom_scenario_budget(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-17 `--budget 1500000`: the driver's plan context solves `custom_1500000` with
    `optimize_portfolio(persist=False)`; the draft's `portfolio_custom[0]` is that result and the
    recommendation's expected USD cites its query id."""
    calls: list[tuple[Any, dict[str, Any]]] = []
    custom_qid = e.PORTFOLIO_QID

    def portfolio(scenario: Any, **kw: Any) -> PortfolioResult:
        calls.append((scenario, kw))
        return PortfolioResult.model_validate(
            {"scenario": scenario, "budget_usd": Decimal(1_500_000), "solver_status": "OPTIMAL",
             "rows": [], "selected": [], "total_effort_usd": Decimal(0),
             "total_expected_impact_usd": Decimal(0), "binding_constraints": [],
             "query_ids": [custom_qid], "flags": []}
        )  # fmt: skip

    monkeypatch.setattr(_run_steps, "build_plan_context", build_plan_context)
    monkeypatch.setattr(_run_env, "planner_reader", lambda env: _Reader())
    req = drv.request(scenarios=["custom_1500000"])
    res = asyncio.run(drv.swarm(portfolio_fn=portfolio).start(req))
    assert res.status == "done"
    assert len(calls) == 1
    scenario, kw = calls[0]
    assert scenario == "custom_1500000"
    assert kw["persist"] is False
    assert kw["run_id"] == res.run_id
    draft = _draft(res)
    assert draft.portfolio_custom[0]["scenario"] == "custom_1500000"
    usd = draft.recommendations[0].numbers[0]
    assert usd.query_id == custom_qid
    assert json.loads(json.dumps(draft.portfolio_custom[0]["query_ids"])) == [custom_qid]


# --- U06-82 step 8 and the record carry-overs


def test_rf_t06_22_fatal_step_error_fails_run(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> None:
    """U06-82 step 8: a FatalError escaping a step sets `failed` (CAS) and is re-raised."""

    async def boom(env: Any) -> int:
        del env
        msg = "dedup broke"
        raise ConfigError(msg)

    monkeypatch.setattr(_run_steps, "run_dedup", boom)
    with structlog.testing.capture_logs() as logs, pytest.raises(ConfigError):
        asyncio.run(drv.swarm().start(drv.request()))
    run_id = select_tasks_last_run()
    assert _status(run_id) == "failed"
    failed = [ev for ev in logs if ev["event"] == "harness.run.failed"]
    assert failed[0]["error_type"] == "ConfigError"


def select_tasks_last_run() -> str:
    from herness.store.ops import select_runs  # noqa: PLC0415

    return select_runs(statuses={"failed", "running", "done", "partial"})[-1].run_id


def test_rf_t06_22_retryable_error_keeps_status(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> None:
    """U06-82 step 8: a RetryableError (StoreBusy) propagates with no status change."""
    from herness.core.errors import StoreBusy  # noqa: PLC0415

    async def busy(env: Any) -> int:
        del env
        msg = "busy"
        raise StoreBusy(msg)

    monkeypatch.setattr(_run_steps, "run_dedup", busy)
    with pytest.raises(StoreBusy):
        asyncio.run(drv.swarm().start(drv.request()))
    assert _status(select_tasks_last_run()) == "running"


def test_rf_t06_22_render_crash_after_done_keeps_done(drv: Drv) -> None:
    """T06-20 M2: a non-HernessError from the renderer after `done` propagates (a bug is not
    swallowed) but the run stays `done`; a later re-render clears `meta.render_error` (M3)."""

    def crash(run_id: str, formats: Any) -> None:
        del run_id, formats
        msg = "renderer bug"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="renderer bug"):
        asyncio.run(drv.swarm(render=crash).start(drv.request()))
    assert _status(select_tasks_last_run()) == "done"


def test_rf_t06_22_stale_render_error_cleared(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> None:
    """T06-20 M3: a partial run re-rendered by the driver clears the stale `render_error`."""
    from herness.core.errors import QueryError  # noqa: PLC0415

    drv.agents.writer = "fail"
    attempts: list[int] = []

    def flaky(run_id: str, formats: Any) -> None:
        del run_id, formats
        attempts.append(1)
        if len(attempts) == 1:
            msg = "render failed"
            raise QueryError(msg)

    swarm = drv.swarm(render=flaky)
    res = asyncio.run(swarm.start(drv.request()))
    run = get_run(res.run_id)
    assert run is not None
    assert run.meta["render_error"] == "QueryError: render failed"
    steps = _render_steps(drv, res.run_id, flaky, monkeypatch)
    asyncio.run(steps())
    run = get_run(res.run_id)
    assert run is not None
    assert run.meta["render_error"] is None
    assert len(attempts) == 2


def _render_steps(drv: Drv, run_id: str, render: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """`Steps.render_only` of a fresh env over the stored run (the re-render path)."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    run = get_run(run_id)
    pool = ThreadPoolExecutor(1)
    parts = _run_env.EnvParts(
        cfg=drv.hcfg.pipelines, llms=drv.llms, memory=drv.memory,  # type: ignore[arg-type]
        deps=drv.deps, writer=pool,
    )  # fmt: skip
    stop = _run_env.Stopper(run_id, __import__("threading").Event(), None)

    async def go() -> None:
        env, reader = _run_env.build_env(parts, run, None, force=False, stop=stop,  # type: ignore[arg-type]
                                         no_wildcards=False)  # fmt: skip
        steps = _run_steps.Steps(env, None, render, None, reader)  # type: ignore[arg-type]
        await steps.render_only()
        pool.shutdown()

    del monkeypatch
    return go


def test_rf_t06_22_escalated_run_posts_summary(drv: Drv) -> None:
    """U06-82 step 7: an escalated run posts its summary through the bound poster once; with
    no binding the driver logs `harness.escalation.summary_unbound` (T06-24 carry-over)."""
    from herness.store.ops import run_write, update_run_fields  # noqa: PLC0415

    posted: list[tuple[str, str | None]] = []
    first = _stopped_run(drv)
    patch = {"escalated_from": {"session_id": "ses_x", "message_id": "msg_x"}}
    run_write(lambda conn: update_run_fields(conn, first.run_id, meta_patch=patch), op="t")

    def poster(run: Any, draft: ReportDraft | None) -> str:
        posted.append((run.run_id, None if draft is None else draft.mode))
        return "msg_1"

    res = asyncio.run(drv.swarm(post_summary=poster).resume(first.run_id))
    assert res.status == "done"
    assert posted == [(res.run_id, "full")]
    with structlog.testing.capture_logs() as logs:
        again = _stopped_run(drv)
        run_write(lambda conn: update_run_fields(conn, again.run_id, meta_patch=patch), op="t")
        asyncio.run(drv.swarm().resume(again.run_id))
    assert any(ev["event"] == "harness.escalation.summary_unbound" for ev in logs)


def test_rf_t06_22_large_stage_reentered_on_resume(drv: Drv) -> None:
    """F06-14: a deep local run killed in the large stage re-enters `gpu_scope("large")`
    before continuing, and the driver exits it after the Writer."""
    from herness.store.ops import run_write, update_run_fields  # noqa: PLC0415

    job = FakeJob()
    job.yield_when = lambda: _job_status(job) == "verifying"
    first = asyncio.run(drv.swarm().start(drv.request(depth="deep"), job))
    patch = {"stage": "large"}
    run_write(lambda conn: update_run_fields(conn, first.run_id, meta_patch=patch), op="t")
    job2 = FakeJob()
    res = asyncio.run(drv.swarm().resume(first.run_id, job2))  # type: ignore[arg-type]
    assert res.status == "done"
    assert job2.scopes == ["enter:large", "exit:large"]
    assert Decimal(0) <= res.cost_usd
