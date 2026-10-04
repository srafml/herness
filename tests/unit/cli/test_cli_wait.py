"""Tests for herness._cli.wait: submit, follow and inline-run jobs (T09-21, U09-90, U09-91,
U09-103).

``FakeJobs`` replaces the ``herness.core.jobs`` module reference of ``wait`` (enqueue, get,
worker_alive, run_inline); each job id maps to a script of ``JobRow`` states that ``get``
walks through, repeating the last one. Run and task reads of ``herness.store.ops`` are faked
the same way, and the poll sleep runs on ``fake_clock``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs
from tests.support.fake_clock import FakeClock

import herness.enrich
from herness._cli import wait
from herness._cli.output import exit_code_for
from herness.cli import GlobalOptions
from herness.core import errors as e
from herness.core.jobs import JobRow
from herness.core.types import JobOutcome
from herness.reports import rules
from herness.reports.rules import Actor, UserInputError
from herness.store import ops

pytestmark = pytest.mark.unit

# freezegun's start() reads every attribute of every loaded module. herness.enrich (loaded by
# herness.cli) is a lazy PEP 562 facade, so resolve its names before `fake_clock` freezes time:
# resolving them while frozen imports LanceDB and pandas under freezegun, which stalls.
for _name in dir(herness.enrich):
    getattr(herness.enrich, _name)

JOB = "job_01"
RUN = "run_01"
ADMIN = Actor(user_ref="a" * 32, role="admin", channel="cli", display="root")
REVIEWER = Actor(user_ref="b" * 32, role="reviewer", channel="cli", display="rev")
PARTIAL = f"Run `{RUN}` finished partial: some tasks did not finish."
CIRCUIT = "The source circuit is open; this sync was skipped. The next scheduled run retries."
DETACHED = f"Detached; job `{JOB}` keeps running."


def _row(status: str = "queued", **kw: Any) -> JobRow:
    values: dict[str, Any] = {
        "job_id": JOB,
        "kind": "review",
        "gpu_class": "none",
        "status": status,
        "priority": 80,
        "attempts": 0,
        "max_attempts": 3,
        "payload": {},
    }
    values.update(kw)
    return JobRow(**values)


class FakeJobs:
    """In-test stand-in for the four ``herness.core.jobs`` functions ``wait`` calls."""

    MANUAL_PRIORITY = 80

    def __init__(self, *states: JobRow | BaseException, alive: bool = True) -> None:
        self.states: list[JobRow | BaseException] = list(states)
        self.alive = alive
        self.calls: list[str] = []
        self.enqueued: list[tuple[Any, ...]] = []
        self.inline: JobOutcome | BaseException = JobOutcome(status="done")

    def enqueue(self, *args: Any, **kw: Any) -> str:
        self.calls.append("enqueue")
        self.enqueued.append((*args, kw))
        return JOB

    def get(self, job_id: str) -> JobRow:
        self.calls.append("get")
        assert job_id == JOB
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(state, BaseException):
            raise state
        return state

    def worker_alive(self) -> bool:
        self.calls.append("worker_alive")
        return self.alive

    def run_inline(self, job_id: str) -> JobOutcome:
        self.calls.append("run_inline")
        assert job_id == JOB
        if isinstance(self.inline, BaseException):
            raise self.inline
        return self.inline


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake ``get_run`` / ``ui_task_status_counts``; the dict sets status and task counts."""
    state: dict[str, Any] = {"status": "done", "counts": [], "reads": 0}

    def get_run(run_id: str) -> object:
        assert run_id == RUN
        return SimpleNamespace(status=state["status"])

    def counts(run_id: str) -> list[tuple[str, str, int]]:
        assert run_id == RUN
        state["reads"] += 1
        return list(state["counts"].pop(0) if len(state["counts"]) > 1 else state["counts"][0])

    state["counts"] = [[]]
    monkeypatch.setattr(ops, "get_run", get_run)
    monkeypatch.setattr(ops, "ui_task_status_counts", counts)
    return state


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch) -> Callable[[FakeJobs], FakeJobs]:
    """Install a FakeJobs as the ``jobs`` module reference of ``wait``."""

    def _install(fake: FakeJobs) -> FakeJobs:
        monkeypatch.setattr(wait, "jobs", fake)
        return fake

    return _install


def _follow(opts: GlobalOptions | None = None) -> wait.FollowOutcome:
    return wait.follow_job(opts or GlobalOptions(), JOB, poll_s=2.0)


# --- UT09-67 terminal statuses -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("final", "run_status", "expected"),
    [
        (_row("done", result={"run_id": RUN}), "done", (0, False, ())),
        (_row("done", result={"run_id": RUN, "partial": True}), "done", (6, True, (PARTIAL,))),
        (_row("done", result={"run_id": RUN}), "partial", (6, True, (PARTIAL,))),
        (_row("done", kind="sync", result={"outcome": "skipped_open_circuit"}), "",
         (4, False, (CIRCUIT,))),
        (_row("done", kind="sync", result={}), "", (0, False, ())),
        (_row("done", kind="sync"), "", (0, False, ())),
        (_row("failed", last_error={"class": "ModelUnavailable", "message": "m"}), "",
         (9, False, ())),
        (_row("failed", kind="build_pipeline", last_error={"class": "NoSuchError"}), "",
         (1, False, ())),
        (_row("canceled"), "", (1, False, ())),
    ],
)  # fmt: skip
def test_ut09_67_terminal_exit_codes(
    final: JobRow,
    run_status: str,
    expected: tuple[int, bool, tuple[str, ...]],
    install: Callable[[FakeJobs], FakeJobs],
    runs: dict[str, Any],
    fake_clock: FakeClock,
) -> None:
    """UT09-67 done 0; partial (result or run status) 6; open circuit 4; failed by class 9/1;
    canceled 1."""
    runs["status"] = run_status
    install(FakeJobs(_row(), _row("running", attempts=1), final))
    start = fake_clock.now()
    outcome = _follow(GlobalOptions(quiet=True))
    assert (outcome.exit_code, outcome.partial, outcome.warnings) == expected
    assert outcome.job == final
    assert outcome.detached is False
    assert outcome.run_id == (RUN if final.result and "run_id" in final.result else None)
    assert (fake_clock.now() - start).total_seconds() == 4.0  # two polls slept poll_s each


def test_ut09_67_failed_and_canceled_messages(
    install: Callable[[FakeJobs], FakeJobs], capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-67 failed prints class and cleaned `last_error.message`; canceled its line."""
    bad = {"class": "ModelUnavailable", "message": "model \x1b[31mdown\x07", "key": "x"}
    install(FakeJobs(_row("failed", last_error=bad)))
    assert _follow(GlobalOptions(quiet=True)).exit_code == 9
    err = capsys.readouterr().err
    assert err == f"Error: Job `{JOB}` failed (ModelUnavailable): model down\n"
    install(FakeJobs(_row("failed", last_error=None)))
    assert _follow(GlobalOptions(quiet=True)).exit_code == 1
    assert "failed (UnknownError)" in capsys.readouterr().err
    install(FakeJobs(_row("canceled")))
    assert _follow(GlobalOptions(quiet=True)).exit_code == 1
    assert capsys.readouterr().err == f"Error: Job `{JOB}` was canceled.\n"
    install(FakeJobs(_row("canceled")))
    assert _follow(GlobalOptions(json=True, quiet=True)).exit_code == 1
    assert capsys.readouterr() == ("", "")  # JSON callers build the envelope from the job


def test_ut09_67_circuit_key_and_run_id_sources(
    install: Callable[[FakeJobs], FakeJobs], runs: dict[str, Any]
) -> None:
    """UT09-67 CircuitOpen uses `last_error.key`; run_id from result, request or payload."""
    model = {"class": "CircuitOpen", "message": "m", "key": "model:reasoning"}
    install(FakeJobs(_row("failed", last_error=model)))
    assert _follow().exit_code == 9
    source = {"class": "CircuitOpen", "message": "m", "key": "jira"}
    install(FakeJobs(_row("failed", last_error=source)))
    assert _follow().exit_code == 4
    install(FakeJobs(_row("done", payload={"request": {"run_id": RUN}})))
    assert _follow().run_id == RUN
    install(FakeJobs(_row("done", payload={"run_id": RUN})))
    assert _follow().run_id == RUN
    install(FakeJobs(_row("done", payload={"request": {"kind": "org_review"}, "run_id": 5})))
    assert _follow().run_id is None


def test_ut09_67_progress_lines(
    install: Callable[[FakeJobs], FakeJobs],
    runs: dict[str, Any],
    fake_clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT09-67 a progress line on stderr when status, attempts or task counts change."""
    runs["counts"] = [[], [("analyst", "running", 2)], [("analyst", "running", 2)],
                      [("analyst", "done", 2)]]  # fmt: skip
    running = _row("running", attempts=1, result={"run_id": RUN})
    install(FakeJobs(_row(), _row(), *[running] * 4, _row("done", result={})))
    outcome = _follow()
    assert outcome.exit_code == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert err.splitlines() == [
        f"Job `{JOB}`: queued (attempt 0/3)",
        f"Job `{JOB}`: running (attempt 1/3)",
        f"Job `{JOB}`: running (attempt 1/3); tasks: analyst running 2",
        f"Job `{JOB}`: running (attempt 1/3); tasks: analyst done 2",
        f"Job `{JOB}`: done (attempt 0/3)",
    ]
    assert runs["reads"] == 4
    install(FakeJobs(_row(), _row("running"), _row("done")))
    assert _follow(GlobalOptions(quiet=True)).exit_code == 0
    assert capsys.readouterr().err == ""


def test_ut09_67_store_busy_retried_then_raised(
    install: Callable[[FakeJobs], FakeJobs], fake_clock: FakeClock
) -> None:
    """UT09-67 StoreBusy during a poll is retried (WARNING); ten in a row raise it."""
    busy = e.StoreBusy("busy")
    install(FakeJobs(busy, busy, _row(), busy, _row("done")))
    with capture_logs() as logs:
        assert _follow(GlobalOptions(quiet=True)).exit_code == 0
    warned = [x for x in logs if x["event"] == "cli.job.poll_busy"]
    assert [x["failures"] for x in warned] == [1, 2, 1]
    assert {x["log_level"] for x in warned} == {"warning"}
    fake = install(FakeJobs(*[busy] * 10, _row("done")))
    with pytest.raises(e.StoreBusy):
        _follow(GlobalOptions(quiet=True))
    assert fake.calls.count("get") == 10


def test_ut09_67_detaches_after_max_follow(
    install: Callable[[FakeJobs], FakeJobs],
    fake_clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT09-67 after MAX_FOLLOW_S (48 h) follow detaches with exit 0 and never cancels."""
    assert wait.MAX_FOLLOW_S == 172_800
    start = fake_clock.now()
    fake = install(FakeJobs(_row("running", attempts=1)))
    with capture_logs() as logs:
        outcome = wait.follow_job(GlobalOptions(quiet=True), JOB, poll_s=3600.0)
    assert (outcome.exit_code, outcome.detached, outcome.partial) == (0, True, False)
    assert outcome.job.status == "running"
    assert (fake_clock.now() - start).total_seconds() == wait.MAX_FOLLOW_S
    assert fake.calls.count("get") == 49
    assert set(fake.calls) == {"get", "worker_alive"}
    assert capsys.readouterr().err == "Still running; detached. Follow with `herness jobs list`.\n"
    assert [x for x in logs if x["event"] == "cli.job.detached"] == [
        {"event": "cli.job.detached", "log_level": "info", "component": "cli", "job_id": JOB,
         "reason": "max_follow"}
    ]  # fmt: skip


# --- UT09-68 worker absent ---------------------------------------------------------------------


def test_ut09_68_worker_absent_warned_once(
    install: Callable[[FakeJobs], FakeJobs],
    fake_clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT09-68 one warning naming `herness worker` and `--inline`; follow keeps polling."""
    fake = install(FakeJobs(_row(), _row(), _row(), _row("done"), alive=False))
    opts = GlobalOptions(quiet=True)
    with capture_logs() as logs:
        job_id = wait.submit_job(opts, kind="sync", payload={"source": "jira"}, gpu_class="none")
        outcome = wait.follow_job(opts, job_id, poll_s=2.0)
    assert outcome.exit_code == 0
    assert opts.worker_warned is True
    err = capsys.readouterr().err
    assert err == (
        "Warning: No worker is running; the job is queued.\n"
        "Fix: Start `herness worker` or the `herness-worker` task, or rerun with `--inline`"
        " (admin).\n"
    )
    assert fake.calls.count("get") == 4  # kept polling while no worker runs
    absent = [x for x in logs if x["event"] == "cli.worker.absent"]
    assert absent == [
        {"event": "cli.worker.absent", "log_level": "warning", "component": "cli", "job_id": JOB}
    ]


def test_ut09_68_follow_warns_when_submit_did_not(
    install: Callable[[FakeJobs], FakeJobs],
    fake_clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT09-68 follow warns once when the worker is absent and no warning was printed yet."""
    fake = install(FakeJobs(_row(), _row(), _row("done"), alive=False))
    opts = GlobalOptions(quiet=True)
    assert wait.follow_job(opts, JOB, poll_s=2.0).exit_code == 0
    assert capsys.readouterr().err.count("No worker is running") == 1
    assert opts.worker_warned is True
    assert fake.calls.count("worker_alive") == 1  # not asked again once warned


def test_ut09_68_inline_submit_skips_worker_check(
    install: Callable[[FakeJobs], FakeJobs], capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-68 a job the caller runs inline is not checked for a worker."""
    fake = install(FakeJobs(_row(), alive=False))
    opts = GlobalOptions()
    assert wait.submit_job(opts, kind="sync", payload={}, gpu_class="none", inline=True) == JOB
    assert fake.calls == ["enqueue"]
    assert (opts.worker_warned, capsys.readouterr().err) == (False, "")


# --- UT09-69 Ctrl+C ----------------------------------------------------------------------------


def test_ut09_69_interrupt_in_sleep_detaches(
    install: Callable[[FakeJobs], FakeJobs],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT09-69 KeyboardInterrupt during the poll sleep -> exit 130, detached, job untouched."""
    fake = install(FakeJobs(_row("running", attempts=1)))

    def interrupted(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(wait.clock, "sleep", interrupted)
    with capture_logs() as logs:
        outcome = _follow(GlobalOptions(quiet=True))
    assert (outcome.exit_code, outcome.detached, outcome.job.status) == (130, True, "running")
    assert set(fake.calls) == {"get", "worker_alive"}  # nothing cancels or changes the job
    assert capsys.readouterr().err == DETACHED + "\n"
    assert [x["reason"] for x in logs if x["event"] == "cli.job.detached"] == ["interrupt"]


def test_ut09_69_interrupt_in_poll_detaches(
    install: Callable[[FakeJobs], FakeJobs],
    fake_clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT09-69 KeyboardInterrupt from a later `jobs.get` -> exit 130 with the last job seen."""
    install(FakeJobs(_row(), KeyboardInterrupt()))
    outcome = _follow(GlobalOptions(quiet=True))
    assert (outcome.exit_code, outcome.detached, outcome.job.status) == (130, True, "queued")
    assert capsys.readouterr().err == DETACHED + "\n"


def test_ut09_69_interrupt_before_first_poll_reaches_main(
    install: Callable[[FakeJobs], FakeJobs],
) -> None:
    """UT09-69 an interrupt before any job row is read propagates; U09-84 maps it to 130."""
    install(FakeJobs(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt) as exc:
        _follow()
    assert exit_code_for(exc.value) == 130


# --- UT09-82 payload size ----------------------------------------------------------------------


def test_ut09_82_payload_too_large(install: Callable[[FakeJobs], FakeJobs]) -> None:
    """UT09-82 a 70 KB payload -> UserInputError before enqueue is called."""
    fake = install(FakeJobs(_row()))
    with pytest.raises(UserInputError, match="job payload too large"):
        wait.submit_job(GlobalOptions(), kind="sync", payload={"x": "y" * 70_000}, gpu_class="none")
    assert fake.calls == []


def test_ut09_82_payload_enqueued_as_json(install: Callable[[FakeJobs], FakeJobs]) -> None:
    """UT09-82 a payload within 64 KB is enqueued in its JSON form with the manual priority."""
    from decimal import Decimal  # noqa: PLC0415

    fake = install(FakeJobs(_row()))
    with capture_logs() as logs:
        job_id = wait.submit_job(
            GlobalOptions(), kind="review", payload={"b": Decimal(5), "p": Path("a/b")},
            gpu_class="reasoning", idem_key="k1",
        )  # fmt: skip
    assert job_id == JOB
    assert fake.enqueued == [
        ("review", {"b": "5", "p": "a/b"}, "reasoning", 80, None, {"idem_key": "k1"})
    ]
    enq = [x for x in logs if x["event"] == "cli.job.enqueued"]
    assert enq == [
        {"event": "cli.job.enqueued", "log_level": "info", "component": "cli", "job_id": JOB,
         "kind": "review"}
    ]  # fmt: skip
    wait.submit_job(GlobalOptions(), kind="eval", payload={}, gpu_class="none", priority=None)
    assert fake.enqueued[-1][3] is None
    edge = {"x": "y" * (65_536 - len('{"x":""}'))}
    wait.submit_job(GlobalOptions(), kind="sync", payload=edge, gpu_class="none")  # exactly 64 KB


def test_ut09_82_unserialisable_payload(install: Callable[[FakeJobs], FakeJobs]) -> None:
    """UT09-82 a payload json_default cannot encode -> SchemaViolation, nothing enqueued."""
    fake = install(FakeJobs(_row()))
    with pytest.raises(e.SchemaViolation, match="not JSON serialisable"):
        wait.submit_job(GlobalOptions(), kind="sync", payload={"x": object()}, gpu_class="none")
    assert fake.calls == []


# --- UT09-100 run_job_inline -------------------------------------------------------------------


def test_ut09_100_reviewer_refused_before_any_call(
    install: Callable[[FakeJobs], FakeJobs],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """UT09-100 a reviewer is refused by require_role before run_inline or get is called."""
    monkeypatch.chdir(tmp_path)
    audited: list[tuple[Any, ...]] = []
    monkeypatch.setattr(rules, "audit", lambda *a, **k: audited.append((*a, k)))
    fake = install(FakeJobs(_row("done")))
    with pytest.raises(e.PermissionDenied) as exc:
        wait.run_job_inline(GlobalOptions(), REVIEWER, JOB)
    assert exc.value.details["action"] == "job_inline"
    assert fake.calls == []
    assert len(audited) == 1


def test_ut09_100_admin_done_and_partial(
    install: Callable[[FakeJobs], FakeJobs], runs: dict[str, Any]
) -> None:
    """UT09-100 admin: run_inline `done` -> the job's terminal mapping (0, or 6 partial)."""
    fake = install(FakeJobs(_row("done", result={"run_id": RUN})))
    with capture_logs() as logs:
        outcome = wait.run_job_inline(GlobalOptions(), ADMIN, JOB)
    assert (outcome.exit_code, outcome.detached, outcome.run_id) == (0, False, RUN)
    assert fake.calls == ["run_inline", "get"]
    events = [x for x in logs if x["event"].startswith("cli.job.inline")]
    assert events == [
        {"event": "cli.job.inline_started", "log_level": "info", "component": "cli",
         "job_id": JOB},
        {"event": "cli.job.inline_completed", "log_level": "info", "component": "cli",
         "job_id": JOB, "kind": "review", "status": "done", "exit_code": 0},
    ]  # fmt: skip
    runs["status"] = "partial"
    install(FakeJobs(_row("done", result={"run_id": RUN})))
    outcome = wait.run_job_inline(GlobalOptions(), ADMIN, JOB)
    assert (outcome.exit_code, outcome.partial, outcome.warnings) == (6, True, (PARTIAL,))


def test_ut09_100_yield_detaches(
    install: Callable[[FakeJobs], FakeJobs], capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-100 run_inline `yield` -> exit 130, detached, job released and queued."""
    fake = install(FakeJobs(_row()))
    fake.inline = JobOutcome(status="yield")
    outcome = wait.run_job_inline(GlobalOptions(), ADMIN, JOB)
    assert (outcome.exit_code, outcome.detached, outcome.job.status) == (130, True, "queued")
    assert capsys.readouterr().err == (f"Interrupted; job `{JOB}` was released and stays queued.\n")


def test_ut09_100_job_state_error_propagates(install: Callable[[FakeJobs], FakeJobs]) -> None:
    """UT09-100 a JobStateError from run_inline propagates (U09-84 maps it to exit 1)."""
    fake = install(FakeJobs(_row()))
    fake.inline = e.JobStateError("job not claimable")
    with pytest.raises(e.JobStateError) as exc:
        wait.run_job_inline(GlobalOptions(), ADMIN, JOB)
    assert exit_code_for(exc.value) == 1
    assert fake.calls == ["run_inline"]


def test_ut09_100_inline_failed_and_not_terminal(
    install: Callable[[FakeJobs], FakeJobs], capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-100 inline `done` but the row failed -> class code; a non-terminal row -> 1."""
    install(FakeJobs(_row("failed", last_error={"class": "StoreBusy", "message": "busy"})))
    assert wait.run_job_inline(GlobalOptions(), ADMIN, JOB).exit_code == 8
    install(FakeJobs(_row("running")))
    assert wait.run_job_inline(GlobalOptions(), ADMIN, JOB).exit_code == 1
    assert "failed (StoreBusy): busy" in capsys.readouterr().err
