"""Tests for herness._cli.cmd_data: sync, build, enrich, score, metrics list, pipeline
(T09-23, U09-95).

Commands run through ``herness.cli.main`` as the OS user ``alice`` (``cli_env``). Job
submission is replaced by a spy on ``cmd_data.submit_job`` and the ``jobs`` module reference
``_job_cmds`` reads the queued status from; spec 01 and spec 04 calls are faked on their
modules, which the command bodies import lazily.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support.cli_env import CliEnv

from herness import cli
from herness._cli import _job_cmds, cmd_data, wait
from herness.connectors import jobs as connector_jobs
from herness.core.jobs import JobRow
from herness.metrics import portfolio
from herness.store import warehouse

pytestmark = pytest.mark.unit

JOB = "job_01JA0000000000000000000001"
NEW_COMMANDS = (
    "sync", "build", "enrich", "score", "metrics list", "pipeline", "report funding",
    "report org", "report render", "review", "resume", "decide",
)  # fmt: skip


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    code = exc.value.code
    assert isinstance(code, int)
    return code


class Submitted:
    """Spy for ``submit_job`` plus the queued status read of ``--no-wait``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def submit(self, opts: object, **kw: Any) -> str:
        del opts
        self.calls.append(kw)
        return JOB

    def get(self, job_id: str) -> SimpleNamespace:
        assert job_id == JOB
        return SimpleNamespace(status="queued")


@pytest.fixture
def admin(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> Submitted:
    """``alice`` is an admin; job submission and the queued read are spied."""
    cli_env.write_config(admins=["alice"])
    spy = Submitted()
    monkeypatch.setattr(cmd_data, "submit_job", spy.submit)
    monkeypatch.setattr(_job_cmds, "jobs", spy)
    return spy


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(capsys.readouterr().out)
    return loaded


# --- UT09-87 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["sync", "--backfill"],
        ["sync", "--entity", "incident"],
        ["sync", "jira", "--full", "--backfill", "--from", "2026-01-01"],
        ["sync", "--reconcile"],
        ["sync", "jira", "--reconcile", "--full"],
        ["sync", "jira", "--to", "2026-01-01"],
        ["sync", "--check-mapping"],
        ["sync", "servicenow", "--discover-fields"],
        ["build", "--inline", "--no-wait"],
        ["pipeline", "--from-stage", "score"],
        ["pipeline", "--from-stage", "nope", "--build-id", "b"],
        ["score", "--scenario", "base", "--step", "org"],
        ["score", "--scenario", "12x"],
    ],
)
def test_ut09_87_bad_options_exit_2_no_job(
    admin: Submitted, args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-87 bad option combinations are usage errors (exit 2); nothing is enqueued."""
    assert _main(["--json", *args]) == 2
    envelope = _json(capsys)
    assert envelope["error"]["type"] == "UserInputError"
    assert admin.calls == []


def test_ut09_87_sync_backfill_enqueues_impl01_payload_unchanged(
    admin: Submitted, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-87 `sync jira --backfill --from 2026-01-01`: impl 01's kind, payload and idem_key
    reach submit_job unchanged; --no-wait prints the job id."""
    seen: list[tuple[Any, ...]] = []
    payload = {"source": "jira", "mode": "backfill", "start": "2026-01-01"}

    def fake(source: str | None, **kw: Any) -> tuple[str, dict[str, Any], str]:
        seen.append((source, kw))
        return "sync", payload, "sync:jira:backfill:2026-01-01:2026-10-10"

    monkeypatch.setattr(connector_jobs, "build_sync_payload", fake)
    args = ["--json", "sync", "jira", "--backfill", "--from", "2026-01-01", "--no-wait"]
    assert _main(args) == 0
    ((source, kw),) = seen
    assert source == "jira"
    assert kw["backfill"] is True
    assert kw["from_"] == date(2026, 1, 1)
    assert kw["to"] is None
    assert isinstance(kw["today"], date)
    assert admin.calls == [{"kind": "sync", "payload": payload, "gpu_class": "none",
                            "idem_key": "sync:jira:backfill:2026-01-01:2026-10-10",
                            "inline": False}]  # fmt: skip
    assert _json(capsys)["data"] == {"schema": "cli/1", "job_id": JOB, "status": "queued"}


def test_ut09_87_score_scenario_calls_optimizer_with_persist_false(
    admin: Submitted, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-87 `score --scenario 2000000`: the optimizer runs with persist=False; no job."""
    calls: list[tuple[Any, dict[str, Any]]] = []
    result = SimpleNamespace(model_dump=lambda mode: {
        "scenario": "custom_2000000", "solver_status": "OPTIMAL", "selected": ["epic-1"],
        "total_effort_usd": "10", "total_expected_impact_usd": "20"})  # fmt: skip

    def optimize(scenario: object, **kw: Any) -> object:
        calls.append((scenario, kw))
        return result

    monkeypatch.setattr(portfolio, "optimize_portfolio", optimize)
    monkeypatch.setattr(warehouse, "read_current", lambda: "20260926-100000-000001")
    assert _main(["--json", "score", "--scenario", "2,000,000"]) == 0
    ((scenario, kw),) = calls
    assert kw == {"persist": False}
    assert isinstance(scenario, portfolio.Scenario)
    assert (scenario.name, scenario.budget_usd) == ("custom_2000000", Decimal(2_000_000))
    assert _json(capsys)["data"]["selected"] == ["epic-1"]
    assert admin.calls == []
    assert _main(["score", "--scenario", "base"]) == 0
    assert calls[-1] == ("base", {"persist": False})


def test_ut09_87_score_scenario_needs_current_build(
    admin: Submitted, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-87 `score --scenario` before any promotion: NotFound `no_current` (exit 7)."""
    monkeypatch.setattr(warehouse, "read_current", lambda: None)
    monkeypatch.setattr(portfolio, "optimize_portfolio", pytest.fail)
    assert _main(["--json", "score", "--scenario", "base"]) == 7
    error = _json(capsys)["error"]
    assert (error["type"], error["details"]["code"]) == ("NotFound", "no_current")


@pytest.mark.parametrize(
    ("args", "payload"),
    [
        (["build"], {"stages": ["build"], "build_id": None}),
        (["enrich", "--build-id", "b1", "--stage", "classify", "--depth", "deep"],
         {"stages": ["enrich"], "build_id": "b1", "enrich_stage": "classify", "depth": "deep"}),
        (["score", "--step", "org"], {"stages": ["score"], "build_id": None,
                                      "score_steps": ["org"]}),
        (["score"], {"stages": ["score"], "build_id": None, "score_steps": None}),
        (["pipeline"], {"stages": ["build", "enrich", "score", "dq", "promote"],
                        "build_id": None}),
        (["pipeline", "--from-stage", "dq", "--build-id", "b1"],
         {"stages": ["dq", "promote"], "build_id": "b1"}),
    ],
)  # fmt: skip
def test_ut09_87_pipeline_commands_enqueue_build_pipeline(
    admin: Submitted, args: list[str], payload: dict[str, Any]
) -> None:
    """UT09-87 build/enrich/score/pipeline enqueue `build_pipeline`, gpu_class none (R-43)."""
    assert _main([*args, "--no-wait"]) == 0
    assert admin.calls == [{"kind": "build_pipeline", "payload": payload, "gpu_class": "none",
                            "inline": False}]  # fmt: skip


def _row(status: str, **kw: Any) -> JobRow:
    values: dict[str, Any] = {"job_id": JOB, "kind": "build_pipeline", "gpu_class": "none",
                              "status": status, "priority": 80, "attempts": 1,
                              "max_attempts": 3, "payload": {}}  # fmt: skip
    return JobRow(**(values | kw))


def test_ut09_87_wait_follows_and_reports_failure(
    admin: Submitted, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-87 --wait follows the job; a failed job's JSON error is its `last_error` class."""
    polls: list[float] = []
    failed = _row("failed", last_error={"class": "SchemaViolation", "message": "dq blocked"})

    def follow(opts: object, job_id: str, *, poll_s: float) -> wait.FollowOutcome:
        del opts
        polls.append(poll_s)
        return wait.FollowOutcome(failed, 5, None, False, False, ())

    monkeypatch.setattr(_job_cmds, "follow_job", follow)
    assert _main(["--json", "build"]) == 5
    assert polls == [2.0]  # cfg.app.cli.poll_interval_s
    envelope = _json(capsys)
    assert envelope["ok"] is False
    assert envelope["error"]["type"] == "SchemaViolation"
    assert envelope["error"]["exit_code"] == 5
    assert envelope["error"]["details"] == {"job_id": JOB}
    done = _row("done", result={"run_id": "run_x"})
    outcome = wait.FollowOutcome(done, 0, "run_x", False, False, ())
    monkeypatch.setattr(_job_cmds, "follow_job", lambda *a, **k: outcome)
    assert _main(["--json", "build"]) == 0
    assert _json(capsys)["data"] == {"schema": "cli/1", "job_id": JOB, "status": "done",
                                     "run_id": "run_x", "partial": False}  # fmt: skip


def test_ut09_87_sync_check_mapping_and_discover_fields_enqueue_nothing(
    admin: Submitted, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-87 --check-mapping and --discover-fields run in-process; no job."""
    from herness.connectors import factory, mapping_check  # noqa: PLC0415
    from herness.connectors.jira import FieldCandidate  # noqa: PLC0415

    issue = mapping_check.MappingIssue("incident", "u_x", "stg_incident.sql")
    monkeypatch.setattr(mapping_check, "check_mapping", lambda source, cfg: [issue])
    assert _main(["--json", "sync", "servicenow", "--check-mapping"]) == 0
    assert _json(capsys)["data"]["issues"] == [
        {"entity": "incident", "column": "u_x", "file": "stg_incident.sql"}]  # fmt: skip
    field = FieldCandidate("customfield_1", "Story Points", "number", "story_points")
    connector = SimpleNamespace(discover_fields=lambda: [field])
    monkeypatch.setattr(factory, "build_connector", lambda name, cfg: connector)
    assert _main(["--json", "sync", "jira", "--discover-fields"]) == 0
    assert _json(capsys)["data"]["fields"][0]["id"] == "customfield_1"
    assert admin.calls == []


def test_ut09_87_metrics_list_is_a_viewer_command(
    cli_env: CliEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-87 `metrics list` (viewer) prints the catalog's describe() rows; build is admin."""
    cli_env.write_config()
    assert _main(["--json", "metrics", "list"]) == 0
    rows = _json(capsys)["data"]["metrics"]
    assert rows
    assert {"name", "unit", "enabled"} <= set(rows[0])
    assert _main(["--json", "build", "--no-wait"]) == 11
    assert _json(capsys)["error"]["type"] == "PermissionDenied"


@pytest.mark.parametrize("path", NEW_COMMANDS)
def test_ut09_87_help_of_every_new_command_exits_0(
    cli_env: CliEnv, path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-87 `--help` of every T09-23 command exits 0 and shows its usage line."""
    del cli_env
    assert _main([*path.split(), "--help"]) == 0
    assert f" {path} [OPTIONS]" in capsys.readouterr().out
