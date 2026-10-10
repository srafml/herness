"""Integration tests of the T09-23 commands through ``herness.cli.main`` (impl 09 U09-95,
U09-96; flows F09-01, F09-02, F09-07, F09-08).

Each test combines ``cli_env`` (config tree with roles, OS user, keyring) with ``report_env``
(migrated ops store, the T11-17 ``tiny_build`` warehouse copy, seeded runs and drafts) in one
temporary root, so the CLI loads that config and its ``paths.data`` is the seeded store. Jobs
are real ops-store rows; no worker runs. ``--inline`` goes through impl 08's real
``run_inline`` (spied) with fake handlers registered for ``build_pipeline`` and ``review``;
for the reasoning-class review job the GPU step is stubbed (no model server in tests).
"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.support.cli_env import CliEnv
from tests.support.fake_memory import FakeMemoryStore
from tests.support.report_env import (
    BUILD_ID,
    FUND_RUN,
    OPS_EVIDENCE,
    ORG_RUN,
    REC1,
    ReportEnv,
    fixture_draft,
)  # fmt: skip

import herness.core.jobs
from herness import cli
from herness.core.jobs import inline as jobs_inline
from herness.core.jobs.handlers import register_handler
from herness.core.types import JobOutcome, ReportManifest
from herness.harness import memory
from herness.harness.swarm import RunRequest
from herness.metrics import portfolio
from herness.reports.render import render_run

pytestmark = pytest.mark.integration

E2 = OPS_EVIDENCE[1].qid
NO_WORKER = "Warning: No worker is running; the job is queued."
FIX = "Fix: Start `herness worker` or the `herness-worker` task, or rerun with `--inline` (admin)."


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    code = exc.value.code
    assert isinstance(code, int)
    return code


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(capsys.readouterr().out)
    return loaded


class Env:
    """The CLI environment, the report environment and the inline-run spy."""

    def __init__(self, cli_env: CliEnv, report_env: ReportEnv) -> None:
        self.cli, self.report = cli_env, report_env
        self.inline_runs: list[str] = []
        self.requests: list[RunRequest] = []

    def as_user(self, user: str) -> None:
        self.cli.user = user

    def jobs(self) -> list[tuple[str, str]]:
        with sqlite3.connect(self.report.data_root / "ops.sqlite") as con:
            return list(con.execute("SELECT kind, status FROM job ORDER BY created_at"))

    def review_handler(self, run_id: str, *, partial: bool = False) -> None:
        """A fake spec 06 review handler: records the request, renders ``run_id``'s draft."""

        def handle(ctx: Any) -> JobOutcome:
            self.requests.append(RunRequest.model_validate(ctx.job.payload["request"]))
            render_run(run_id, ["html", "md"])  # the record step renders the report
            return JobOutcome(status="done", result={"run_id": run_id, "partial": partial})

        register_handler("review", handle)


@pytest.fixture
def env(cli_env: CliEnv, report_env: ReportEnv, monkeypatch: pytest.MonkeyPatch) -> Env:
    """Admin `root`, reviewer `rev`, viewer `alice`; run_inline spied; GPU step stubbed."""
    cli_env.write_config(admins=["root"], reviewers=["rev"])
    state = Env(cli_env, report_env)
    real = herness.core.jobs.run_inline

    def spy(job_id: str) -> JobOutcome:
        state.inline_runs.append(job_id)
        return real(job_id)

    monkeypatch.setattr(herness.core.jobs, "run_inline", spy)
    monkeypatch.setattr(jobs_inline, "_gpu_controller", lambda *args: None)
    return state


# --- IT09-24, IT09-04, FT09-06: report render ----------------------------------------------


def test_it09_24_report_render_json_equals_manifest(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-24 `herness --json report render RUN`: files written; JSON data equals manifest."""
    assert _main(["--json", "report", "render", FUND_RUN]) == 0
    envelope = _json(capsys)
    folder = env.report.reports_root / FUND_RUN
    stored = json.loads((folder / "manifest.json").read_text("utf-8"))
    data = dict(envelope["data"])
    assert data.pop("schema") == "cli/1"
    assert data == stored
    assert set(stored["files"]) == {"report.html", "report.md"}
    assert all((folder / name).is_file() for name in stored["files"])
    assert (envelope["ok"], envelope["warnings"]) == (True, [])
    assert env.jobs() == []  # rendering never enqueues


def test_it09_04_uncited_number_exit_12_then_no_strict(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-04 draft with an uncited "42": exit 12 with error.type ReportContractError;
    `--no-strict` renders <mark class="uncited"> and lists it in the manifest."""
    draft = fixture_draft("funding")
    draft["sections"][1]["paragraphs"][0]["text"] = "Incidents rose by 42% this quarter."
    env.report.write_draft(draft)
    assert _main(["--json", "report", "render", FUND_RUN]) == 12
    error = _json(capsys)["error"]
    assert (error["type"], error["exit_code"]) == ("ReportContractError", 12)
    assert error["details"]["code"] == "uncited"
    assert _main(["--json", "report", "render", FUND_RUN, "--no-strict"]) == 0
    data = _json(capsys)["data"]
    assert data["uncited"] == [{"where": "sections[1].paragraphs[0]", "text": "42%"}]
    html = env.report.out(FUND_RUN, "report.html").decode()
    assert '<mark class="uncited">42%</mark>' in html


def test_ft09_06_deleted_warehouse_exit_12_end_to_end(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """FT09-06 the run's warehouse file deleted: `herness report render` exits 12 with the
    build-retired message (R-46), end to end through the CLI."""
    env.report.warehouse.unlink()
    assert _main(["--json", "report", "render", FUND_RUN]) == 12
    error = _json(capsys)["error"]
    assert error["type"] == "ReportContractError"
    assert error["details"]["code"] == "build_retired"
    assert error["message"] == f"Build {BUILD_ID} used by this run was deleted by retention."
    assert _main(["report", "render", FUND_RUN]) == 12
    assert "Error: Build" in capsys.readouterr().err


# --- IT09-07 (CLI part), IT09-09: report funding|org ------------------------------------------


def test_it09_07_partial_run_exit_6_with_warning(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-07 (CLI part) partial run with two dead tasks: `report org --inline` and
    `report render` exit 6 with the partial warning; `partial: true` (R-46)."""
    dead = [{"task_id": f"tsk_{n}", "role": "analyst", "objective": "dig", "last_error": "boom"}
            for n in (8, 9)]  # fmt: skip
    env.report.write_draft(fixture_draft("org") | {"dead_tasks": dead})
    env.as_user("root")
    env.review_handler(ORG_RUN, partial=True)
    assert _main(["--json", "report", "org", "--inline"]) == 6
    envelope = _json(capsys)
    assert envelope["ok"] is False
    assert envelope["data"]["partial"] is True
    assert envelope["data"]["publishable"] is False
    assert envelope["warnings"] == [f"Run `{ORG_RUN}` finished partial: some tasks did not finish."]
    assert len(env.inline_runs) == 1
    assert "banner-partial_run" in env.report.out(ORG_RUN, "report.html").decode()
    env.as_user("alice")
    assert _main(["--json", "report", "render", ORG_RUN]) == 6
    assert _json(capsys)["warnings"] == [
        f"Run `{ORG_RUN}` is partial; the report carries the partial banner."]  # fmt: skip
    assert _main(["report", "render", ORG_RUN]) == 6
    assert "Warning: Run `" in capsys.readouterr().err


def test_it09_09_custom_budget_reaches_run_request(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-09 `report funding --budget 2000000`: RunRequest.scenarios holds custom_2000000;
    the renderer shows it from portfolio_custom without calling the optimizer; its numbers
    link ops `evidence` rows."""
    optimizer: list[object] = []
    monkeypatch.setattr(portfolio, "optimize_portfolio", lambda *a, **k: optimizer.append(a))
    custom = {"scenario": {"name": "custom_2000000"}, "budget_usd": "2000000",
              "solver_status": "OPTIMAL", "rows": [
                  {"candidate_id": "epic-2", "selected": True, "order_rank": 1,
                   "expected_impact_usd": "700", "query_ids": [E2]}]}  # fmt: skip
    env.report.write_draft(fixture_draft("funding") | {"portfolio_custom": [custom]})
    env.as_user("root")
    env.review_handler(FUND_RUN)
    assert _main(["--json", "report", "funding", "--budget", "2000000", "--inline"]) == 0
    (request,) = env.requests
    (scenario,) = request.scenarios
    assert isinstance(scenario, portfolio.Scenario)
    assert (scenario.name, int(scenario.budget_usd)) == ("custom_2000000", 2_000_000)
    assert request.kind == "funding_review"
    data = _json(capsys)["data"]
    assert (data["run_id"], data["partial"]) == (FUND_RUN, False)
    html = env.report.out(FUND_RUN, "report.html").decode()
    section = html.split('<section id="portfolio">', 1)[1].split("</section>", 1)[0]
    assert '<span class="tag">custom</span>' in section
    assert f'href="#ev-{E2}"' in section
    assert optimizer == []
    with sqlite3.connect(env.report.data_root / "ops.sqlite") as con:
        rows = con.execute("SELECT run_id FROM evidence WHERE query_id = ?", (E2,)).fetchall()
    assert rows == [(FUND_RUN,)]
    assert not [w for w in data["warnings"] if w.startswith("evidence not found")]
    assert env.jobs() == [("review", "done")]


def test_it09_09_render_options_rerender_into_out_dir(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-09 `report funding --format md --out DIR --inline`: the finished run is
    re-rendered through rerender_report into DIR; the paths are printed."""
    env.as_user("root")
    env.review_handler(FUND_RUN)
    out = tmp_path / "custom-out"
    assert _main(["report", "funding", "--format", "md", "--out", str(out), "--inline"]) == 0
    assert (out / "report.md").is_file()
    assert not (out / "report.html").exists()
    manifest = ReportManifest.model_validate_json((out / "manifest.json").read_bytes())
    assert set(manifest.files) == {"report.md"}
    assert str(out / "report.md") in capsys.readouterr().out


# --- IT09-28: --inline and --no-wait without a worker ----------------------------------------


def _fake_build(seen: list[dict[str, Any]]) -> Callable[[Any], JobOutcome]:
    def handle(ctx: Any) -> JobOutcome:
        seen.append(copy.deepcopy(dict(ctx.job.payload)))
        return JobOutcome(status="done", result={})

    return handle


def test_it09_28_inline_admin_only_and_no_worker_warning(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-28 no live worker: admin `build --inline` enqueues then runs inline (exit 0);
    reviewer gets exit 11 PermissionDenied and no job; `build --no-wait` prints the
    no-worker warning with the fix; `report render` needs no worker (R-45)."""
    seen: list[dict[str, Any]] = []
    register_handler("build_pipeline", _fake_build(seen))
    env.as_user("rev")
    assert _main(["--json", "build", "--inline"]) == 11
    assert _json(capsys)["error"]["type"] == "PermissionDenied"
    assert env.jobs() == []
    assert env.inline_runs == []
    env.as_user("root")
    assert _main(["--json", "build", "--inline"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["data"]["status"] == "done"
    assert NO_WORKER not in out.err
    assert seen == [{"stages": ["build"], "build_id": None}]
    assert len(env.inline_runs) == 1
    assert env.jobs() == [("build_pipeline", "done")]
    assert _main(["build", "--no-wait"]) == 0
    out = capsys.readouterr()
    assert NO_WORKER in out.err
    assert FIX in out.err
    assert env.jobs()[-1] == ("build_pipeline", "queued")
    env.as_user("rev")
    assert _main(["report", "render", FUND_RUN]) == 0
    assert len(env.jobs()) == 2


# --- IT09-22 (CLI half) ----------------------------------------------------------------------


def test_it09_22_decide_via_cli_calls_memory_decide_and_audits(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-22 (CLI half) `herness decide REC accepted --reason ...` as a reviewer:
    MemoryStore.decide receives the user_ref; one recommendation_decision audit line."""
    store = FakeMemoryStore()
    monkeypatch.setattr(memory, "get_memory_store", lambda: store)
    env.as_user("rev")
    reason = "Approved in the funding review."
    assert _main(["--json", "decide", REC1, "accepted", "--reason", reason]) == 0
    assert _json(capsys)["data"]["rec_id"] == REC1
    ((name, args, _),) = store.calls
    assert name == "decide"
    rec_id, decision, why, user_ref, effective = args
    assert (rec_id, decision, why, effective) == (REC1, "accepted", reason, None)
    assert len(user_ref) == 32
    logs = sorted(env.cli.root.rglob("audit-*.jsonl"))
    lines = [json.loads(x) for p in logs for x in p.read_text("utf-8").splitlines()]
    decisions = [x for x in lines if x.get("event") == "recommendation_decision"]
    assert len(decisions) == 1
    assert decisions[0]["actor"] == user_ref
    assert decisions[0]["fields"] == {"rec_id": REC1, "decision": "accepted",
                                      "decided_by": user_ref}  # fmt: skip
    env.as_user("alice")
    assert _main(["--json", "decide", REC1, "accepted", "--reason", reason]) == 11
    assert len(store.calls) == 1
