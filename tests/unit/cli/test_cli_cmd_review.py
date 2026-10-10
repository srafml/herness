"""Tests for herness._cli.cmd_review: report, review, resume, decide (T09-23, U09-96).

Commands run through ``herness.cli.main`` as the OS user ``alice`` (``cli_env``). Job
submission is a spy on ``cmd_review.submit_job``; following goes through fakes of
``_job_cmds.follow_job`` and the ``jobs`` module reference; the shared user actions are
faked on ``herness.reports.actions``, which the command bodies import lazily.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support.cli_env import CliEnv
from tests.support.fake_memory import FakeMemoryStore

from herness import cli
from herness._cli import _job_cmds, cmd_review, wait
from herness.core.jobs import JobRow
from herness.harness import memory
from herness.reports import actions

pytestmark = pytest.mark.unit

JOB = "job_01JA0000000000000000000001"
RUN = "run_01JA0000000000000000000002"
REC = "rec_01JA0000000000000000000003"
RENDER_ONLY = "Render options apply only with --wait; use `herness report render <run_id>`."


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    code = exc.value.code
    assert isinstance(code, int)
    return code


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(capsys.readouterr().out)
    return loaded


def _row(status: str = "done", **kw: Any) -> JobRow:
    values: dict[str, Any] = {"job_id": JOB, "kind": "review", "gpu_class": "reasoning",
                              "status": status, "priority": 80, "attempts": 1,
                              "max_attempts": 3, "payload": {}}  # fmt: skip
    return JobRow(**(values | kw))


class Jobs:
    """Spy for ``submit_job``, the queued read and ``follow_job`` (outcomes in order)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outcomes: list[wait.FollowOutcome] = []

    def submit(self, opts: object, **kw: Any) -> str:
        del opts
        self.calls.append(kw)
        return f"{JOB[:-1]}{len(self.calls)}"

    def get(self, job_id: str) -> SimpleNamespace:
        return SimpleNamespace(status="queued")

    def follow(self, opts: object, job_id: str, *, poll_s: float) -> wait.FollowOutcome:
        del opts, poll_s
        outcome = self.outcomes.pop(0)
        return dataclasses.replace(outcome, job=outcome.job.model_copy(update={"job_id": job_id}))


@pytest.fixture
def jobs(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> Jobs:
    """``alice`` is an admin; submission, the queued read and following are faked."""
    cli_env.write_config(admins=["alice"])
    spy = Jobs()
    monkeypatch.setattr(cmd_review, "submit_job", spy.submit)
    monkeypatch.setattr(_job_cmds, "jobs", spy)
    monkeypatch.setattr(_job_cmds, "follow_job", spy.follow)
    return spy


# --- UT09-88 report funding|org --------------------------------------------------------------


def test_ut09_88_no_wait_with_render_option_warns(
    jobs: Jobs, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-88 `report funding --no-wait --top-n 5`: review job enqueued; render warning."""
    assert _main(["report", "funding", "--no-wait", "--top-n", "5"]) == 0
    ((call),) = jobs.calls
    assert (call["kind"], call["gpu_class"], call["inline"]) == ("review", "reasoning", False)
    request = call["payload"]["request"]
    assert (request["kind"], request["depth"], request["scenarios"]) == (
        "funding_review", "standard", [])  # fmt: skip
    out = capsys.readouterr()
    assert f"Warning: {RENDER_ONLY}" in out.err
    assert JOB[:-1] in out.out
    assert _main(["--json", "report", "org", "--no-wait", "--format", "md"]) == 0
    envelope = _json(capsys)
    assert envelope["warnings"] == [RENDER_ONLY]
    assert jobs.calls[-1]["payload"]["request"]["kind"] == "org_review"


def test_ut09_88_budget_no_wait_json_prints_job_id_and_status(
    jobs: Jobs, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-88 acceptance: `report funding --budget 2000000 --no-wait --json` prints
    {"job_id", "status"}; the budget becomes scenario custom_2000000; no render warning."""
    assert _main(["--json", "report", "funding", "--budget", "2000000", "--no-wait"]) == 0
    envelope = _json(capsys)
    assert set(envelope["data"]) == {"schema", "job_id", "status"}
    assert envelope["data"]["status"] == "queued"
    assert envelope["warnings"] == []
    (scenario,) = jobs.calls[0]["payload"]["request"]["scenarios"]
    assert (scenario["name"], scenario["budget_usd"]) == ("custom_2000000", "2000000")


@pytest.mark.parametrize(
    "args",
    [
        ["report", "funding", "--inline", "--no-wait"],
        ["report", "funding", "--budget", "1.5"],
        ["report", "funding", "--budget", "0"],
        ["report", "org", "--format", "html,doc"],
        ["report", "org", "--format", "md,md"],
        ["report", "funding", "--depth", "bottomless"],
        ["review", "--kind", "all"],
        ["review", "--at", "2026-10-11T06:00:00"],
        ["review", "--at", "tomorrow"],
        ["review", "--at", "2026-10-11T06:00:00+00:00", "--inline"],
        ["review", "--inline", "--no-wait"],
        ["resume", "../../etc"],
        ["resume", RUN, "--inline", "--no-wait"],
        ["report", "render", "..\\x"],
    ],
)
def test_ut09_88_bad_options_exit_2_no_job(
    jobs: Jobs, args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-88 bad options and ids are usage errors (exit 2) before any job or path use."""
    assert _main(["--json", *args]) == 2
    assert _json(capsys)["error"]["type"] == "UserInputError"
    assert jobs.calls == []


def test_ut09_88_wait_reads_stored_manifest_or_rerenders(
    jobs: Jobs, cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """UT09-88 --wait: default options read <data>/reports/<run>/manifest.json; an option
    that leaves the config default re-renders through rerender_report."""
    manifest = {"run_id": RUN, "kind": "funding_review", "build_id": "b", "template_version":
                "1", "rendered_at": "2026-10-10T00:00:00Z", "publishable": True,
                "files": {"report.html": "0" * 64}, "numbers_total": 0, "numbers_linked": 0,
                "uncited": [], "evidence_entries": 0, "warnings": []}  # fmt: skip
    folder = cli_env.root / "data" / "reports" / RUN
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    done = wait.FollowOutcome(_row(result={"run_id": RUN}), 0, RUN, False, False, ())
    jobs.outcomes = [done]
    opened: list[str] = []
    monkeypatch.setattr(cmd_review.webbrowser, "open", opened.append)
    assert _main(["--json", "report", "funding", "--open"]) == 0
    data = _json(capsys)["data"]
    assert data == {"schema": "cli/1", **manifest, "job_id": JOB[:-1] + "1", "partial": False}
    assert opened == [(folder / "report.html").resolve().as_uri()]
    seen: list[dict[str, Any]] = []

    def rerender(actor: object, **kw: Any) -> object:
        seen.append(kw)
        from herness.core.types import ReportManifest  # noqa: PLC0415

        return ReportManifest.model_validate(manifest)

    monkeypatch.setattr(actions, "rerender_report", rerender)
    partial = wait.FollowOutcome(_row(result={"run_id": RUN}), 6, RUN, False, True, ("p",))
    jobs.outcomes = [partial]
    assert _main(["--json", "report", "funding", "--top-n", "5", "--no-strict"]) == 6
    envelope = _json(capsys)
    assert (envelope["data"]["partial"], envelope["warnings"]) == (True, ["p"])
    assert seen == [{"run_id": RUN, "formats": None, "out_dir": None, "strict": False,
                     "top_n": 5}]  # fmt: skip


def test_ut09_88_wait_failed_or_missing_manifest(
    jobs: Jobs, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-88 a failed review job keeps its exit code; a done run without a report is 7."""
    failed = _row("failed", last_error={"class": "BudgetExceeded", "message": "cap"})
    jobs.outcomes = [wait.FollowOutcome(failed, 10, None, False, False, ())]
    assert _main(["--json", "report", "org"]) == 10
    assert _json(capsys)["error"]["type"] == "BudgetExceeded"
    jobs.outcomes = [wait.FollowOutcome(_row(result={"run_id": RUN}), 0, RUN, False, False, ())]
    assert _main(["--json", "report", "org"]) == 7
    assert _json(capsys)["error"]["details"]["code"] == "report_not_found"


# --- UT09-88 review, resume, decide -------------------------------------------------------------


def test_ut09_88_review_both_in_order_first_failure_code(
    jobs: Jobs, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-88 `review --kind both`: funding then org, each with scheduled_for; the exit code
    is the first job that did not end plain done."""
    at = "2026-10-11T06:00:00+02:00"
    assert _main(["--json", "review", "--at", at, "--no-wait"]) == 0
    kinds = [c["payload"]["request"]["kind"] for c in jobs.calls]
    assert kinds == ["funding_review", "org_review"]
    assert {c["scheduled_for"] for c in jobs.calls} == {datetime(2026, 10, 11, 4, tzinfo=UTC)}
    assert [r["status"] for r in _json(capsys)["data"]["jobs"]] == ["queued", "queued"]
    jobs.outcomes = [
        wait.FollowOutcome(_row(result={"run_id": RUN}), 0, RUN, False, False, ()),
        wait.FollowOutcome(_row("failed", last_error={"class": "SchemaViolation",
                                                      "message": "dq blocked"}), 5, None,
                           False, False, ()),
    ]  # fmt: skip
    assert _main(["--json", "review"]) == 5
    envelope = _json(capsys)
    assert [r["status"] for r in envelope["data"]["jobs"]] == ["done", "failed"]
    error = envelope["error"]
    assert (error["type"], error["exit_code"], error["message"]) == (
        "SchemaViolation", 5, "dq blocked")  # fmt: skip
    assert error["details"] == {"job_id": JOB[:-1] + "4"}
    partial = wait.FollowOutcome(_row(result={"run_id": RUN}), 6, RUN, False, True, ("p",))
    jobs.outcomes = [partial, wait.FollowOutcome(_row("canceled"), 1, None, False, False, ())]
    assert _main(["--json", "review"]) == 6
    envelope = _json(capsys)
    assert (envelope["error"], envelope["warnings"]) == (None, ["p"])


def test_ut09_88_resume_goes_through_resume_run(
    jobs: Jobs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-88 `resume RUN --retry-dead --force --no-wait` calls actions.resume_run."""
    seen: list[tuple[Any, dict[str, Any]]] = []

    def resume_run(actor: Any, **kw: Any) -> str:
        seen.append((actor, kw))
        return JOB

    monkeypatch.setattr(actions, "resume_run", resume_run)
    assert _main(["--json", "resume", RUN, "--retry-dead", "--force", "--no-wait"]) == 0
    ((actor, kw),) = seen
    assert (actor.role, actor.channel) == ("admin", "cli")
    assert kw == {"run_id": RUN, "retry_dead": True, "force": True}
    assert _json(capsys)["data"] == {"schema": "cli/1", "job_id": JOB, "status": "queued"}
    assert jobs.calls == []


def test_ut09_88_decide_calls_decide_recommendation(
    cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-88 `decide`: ids and options checked first; then decide_recommendation with the
    CLI actor, the parsed date and the memory store."""
    cli_env.write_config(reviewers=["alice"])
    seen: list[tuple[Any, dict[str, Any]]] = []
    store = FakeMemoryStore()
    monkeypatch.setattr(memory, "get_memory_store", lambda: store)
    monkeypatch.setattr(actions, "decide_recommendation", lambda a, **kw: seen.append((a, kw)))
    reason = ["--reason", "Funded in the Q4 plan."]
    for bad in (["rec_x", "accepted"], [REC, "maybe"], [REC, "accepted", "--effective-at",
                                                         "10/01/2026"]):  # fmt: skip
        assert _main(["--json", "decide", *bad, *reason]) == 2
        assert _json(capsys)["error"]["type"] == "UserInputError"
    assert seen == []
    args = ["--json", "decide", REC, "deferred", *reason, "--effective-at", "2026-11-01"]
    assert _main(args) == 0
    ((actor, kw),) = seen
    assert (actor.role, len(actor.user_ref)) == ("reviewer", 32)
    assert kw == {"rec_id": REC, "decision": "deferred", "reason": "Funded in the Q4 plan.",
                  "effective_at": date(2026, 11, 1), "memory": store}  # fmt: skip
    assert _json(capsys)["data"]["effective_at"] == "2026-11-01"
