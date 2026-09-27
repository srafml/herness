"""Tests for herness.core.jobs.outcomes: the failure policy table and outcome application
(impl 08 U08-49, U08-50; T08-15; TH08-08).
"""

from __future__ import annotations

import inspect
import json
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from pydantic import JsonValue
from tests.unit.core.jobs._queue_env import jobs_db  # noqa: F401 - fixture

from herness.core import errors
from herness.core import time as clock
from herness.core.errors import (
    CircuitOpen,
    ConfigError,
    EgressBlocked,
    HernessError,
    ModelUnavailable,
    RateLimited,
    RetryableError,
    SchemaViolation,
    SourceUnavailable,
)
from herness.core.jobs import outcomes, queue
from herness.core.jobs.outcomes import FailureAction, decide_failure, finish_job
from herness.core.jobs.ports import JobRow, require_jobs_backend
from herness.core.redact import redact_text
from herness.core.resilience import process_state
from herness.core.resilience.policies import job_backoff_delay
from herness.core.types import JobKind, JobOutcome
from herness.store.ops.core import read_all

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
OWNER = "h:1:cli"
MAX = 3


# --- UT08-58: decide_failure table ---------------------------------------------------------


def _leaves() -> set[type[HernessError]]:
    """Every leaf class of the taxonomy in herness.core.errors."""
    classes = {
        obj
        for _, obj in inspect.getmembers(errors, inspect.isclass)
        if issubclass(obj, HernessError) and obj.__module__ == errors.__name__
    }
    return {cls for cls in classes if not any(o is not cls and issubclass(o, cls) for o in classes)}


def _build(cls: type[HernessError], variant: str) -> HernessError:
    if cls is CircuitOpen:
        at = NOW - timedelta(minutes=5) if variant == "past" else NOW + timedelta(minutes=7)
        return CircuitOpen("open", key="source:jira", retry_at=at)
    if cls is RateLimited:
        return RateLimited("429", retry_after=None if variant == "none" else 30.0)
    if cls is EgressBlocked:
        return EgressBlocked("blocked")
    return cls("failure")


_VARIANTS: dict[type[HernessError], tuple[str, ...]] = {
    CircuitOpen: ("future", "past"),
    RateLimited: ("after", "none"),
}
_CASES = [
    (cls, variant)
    for cls in sorted(_leaves(), key=lambda c: c.__name__)
    for variant in _VARIANTS.get(cls, ("plain",))
]


def _row(kind: JobKind, attempts: int, *, max_attempts: int = MAX) -> JobRow:
    return JobRow(
        job_id="job_01J0000000000000000000000A",
        kind=kind,
        gpu_class="none",
        status="running",
        priority=60,
        attempts=attempts,
        max_attempts=max_attempts,
        payload={},
    )


def _expected(err: HernessError, kind: str, attempts: int) -> FailureAction:
    """U08-49 written out independently of the implementation."""
    below = attempts < MAX
    if isinstance(err, CircuitOpen):
        if kind in {"sync", "reconcile"}:
            result: dict[str, JsonValue] = {
                "partial": True,
                "outcome": "skipped_open_circuit",
                "skipped_open_circuit": [err.key],
            }
            return FailureAction("done", None, result)
        at = max(err.retry_at, NOW)
    elif isinstance(err, RateLimited) and err.retry_after is not None:
        at = NOW + timedelta(seconds=err.retry_after)
    elif isinstance(err, RetryableError):
        at = NOW + timedelta(seconds=job_backoff_delay(attempts, rng=random.Random(0)))
    else:
        return FailureAction("failed", None, None)
    return FailureAction("requeue", at, None) if below else FailureAction("failed", None, None)


def test_ut08_58_table_covers_every_taxonomy_leaf() -> None:
    """UT08-58 acceptance check: the table holds every leaf class of herness.core.errors."""
    covered = {cls for cls, _ in _CASES}
    assert covered == _leaves()
    assert {SourceUnavailable, ModelUnavailable, CircuitOpen, SchemaViolation} <= covered
    assert len(covered) >= 19


@pytest.mark.usefixtures("jobs_db")
@pytest.mark.parametrize("kind", ["sync", "review"])
@pytest.mark.parametrize("attempts", [MAX - 1, MAX])
@pytest.mark.parametrize(("cls", "variant"), _CASES, ids=[f"{c.__name__}-{v}" for c, v in _CASES])
def test_ut08_58_decide_failure_table(
    cls: type[HernessError], variant: str, attempts: int, kind: JobKind
) -> None:
    """UT08-58 each error class x attempts below and at max x kinds sync and review."""
    err = _build(cls, variant)
    action = decide_failure(err, _row(kind, attempts), NOW, rng=random.Random(0))
    assert action == _expected(err, kind, attempts)


@pytest.mark.usefixtures("jobs_db")
def test_ut08_58_reconcile_skips_open_circuit_even_at_max() -> None:
    """UT08-58 reconcile, like sync, finishes `done` partial on an open circuit (R-39)."""
    err = CircuitOpen("open", key="source:jira", retry_at=NOW)
    action = decide_failure(err, _row("reconcile", MAX), NOW, rng=random.Random(0))
    assert action.action == "done"
    assert action.result == {
        "partial": True,
        "outcome": "skipped_open_circuit",
        "skipped_open_circuit": ["source:jira"],
    }


@pytest.mark.usefixtures("jobs_db")
def test_ut08_58_backoff_comes_from_job_backoff_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-58 the backoff wait is `job_backoff_delay(row.attempts, rng=rng)`."""
    seen: list[tuple[int, random.Random]] = []

    def fake(attempts: int, *, rng: random.Random) -> float:
        seen.append((attempts, rng))
        return 12.5

    monkeypatch.setattr(outcomes, "job_backoff_delay", fake)
    rng = random.Random(3)
    action = decide_failure(errors.StoreBusy("busy"), _row("eval", 2), NOW, rng=rng)
    assert action == FailureAction("requeue", NOW + timedelta(seconds=12.5), None)
    assert seen == [(2, rng)]


# --- UT08-59: finish_job -------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.current = NOW


@pytest.fixture
def fake_now(jobs_db: Any, monkeypatch: pytest.MonkeyPatch) -> _Clock:  # noqa: F811
    """`jobs_db` with a settable clock and a seeded process rng."""
    del jobs_db
    fake = _Clock()
    monkeypatch.setattr(clock, "now", lambda: fake.current)
    process_state().rng = random.Random(0)
    return fake


def _running(
    kind: JobKind = "review", *, payload: dict[str, Any] | None = None, max_attempts: int = MAX
) -> JobRow:
    job_id = queue.enqueue(kind, payload or {"n": 1}, "none", max_attempts=max_attempts)
    row = queue.claim(owner=OWNER, allowed_classes=["none"], job_id=job_id)
    assert row is not None
    return row


def _get(job_id: str) -> JobRow:
    row = require_jobs_backend().get_job(job_id)
    assert row is not None
    return row


def _events(kind: str | None = None) -> list[dict[str, Any]]:
    rows = [dict(r) for r in read_all("SELECT kind, target, job_id, detail FROM resilience_event")]
    for row in rows:
        row["detail"] = json.loads(row["detail"]) if row["detail"] else {}
    return [r for r in rows if kind is None or r["kind"] == kind]


def _finished(kind: str, status: str) -> float:
    key = ("herness_jobs_finished_total", (("kind", kind), ("status", status)), "jobs")
    return process_state().metric_buffer.counters.get(key, 0.0)


def _run_seconds(kind: str) -> list[float]:
    name = "herness_jobs_run_seconds"
    hist = process_state().metric_buffer.histograms
    return [v for k, v in hist if k[0] == name and k[1] == (("kind", kind),)]


def _finish(row: JobRow, outcome: JobOutcome | HernessError, **kw: Any) -> str:
    started = kw.pop("started", NOW - timedelta(seconds=4))
    return finish_job(
        row, kw.pop("owner", OWNER), outcome, attempt_started_at=started, stop_reason=kw.get("sr")
    )


def test_ut08_59_done_drops_state_and_records(fake_now: _Clock) -> None:
    """UT08-59 `done` with a `state` key: stored without it; event, metrics recorded."""
    row = _running()
    outcome = JobOutcome(status="done", result={"state": {"k": 1}, "count": 2, "partial": True})
    assert _finish(row, outcome) == "done"
    after = _get(row.job_id)
    assert after.status == "done"
    assert after.result == {"count": 2, "partial": True}
    assert after.lease_owner is None
    (event,) = _events("job_done")
    assert event["job_id"] == row.job_id
    assert event["detail"] == {"kind": "review", "attempt": 1, "duration_s": 4.0, "partial": True}
    assert _finished("review", "done") == 1.0
    assert _run_seconds("review") == [4.0]


def test_ut08_59_yield_gives_back_the_attempt(fake_now: _Clock) -> None:
    """UT08-59 `yield`: attempts - 1, queued again at now when not preempted."""
    row = _running()
    assert row.attempts == 1
    assert _finish(row, JobOutcome(status="yield"), sr="shutdown") == "queued"
    after = _get(row.job_id)
    assert (after.status, after.attempts, after.scheduled_for) == ("queued", 0, NOW)
    (event,) = _events("job_yield")
    assert event["detail"] == {"kind": "review", "attempt": 1, "stop_reason": "shutdown"}
    assert _finished("review", "yield") == 1.0


def test_ut08_59_preempt_resumes_at_next_window(
    fake_now: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-59 a preempted yield resumes at `next_window_allowing(row.gpu_class, now)`."""
    later = NOW + timedelta(hours=9)
    calls: list[tuple[str, datetime]] = []

    def window(cls: str, now: datetime) -> datetime:
        calls.append((cls, now))
        return later

    monkeypatch.setattr(outcomes, "next_window_allowing", window)
    row = _running()
    assert _finish(row, JobOutcome(status="yield"), sr="preempt") == "queued"
    assert calls == [("none", NOW)]
    assert _get(row.job_id).scheduled_for == later


def test_ut08_59_retryable_error_requeues(fake_now: _Clock) -> None:
    """UT08-59 a retryable error below max: requeued with backoff, `retry` event, WARNING."""
    row = _running()
    wait = job_backoff_delay(1, rng=random.Random(0))
    with structlog.testing.capture_logs() as logs:
        assert _finish(row, ModelUnavailable("child_crash")) == "queued"
    after = _get(row.job_id)
    assert after.status == "queued"
    assert after.scheduled_for == NOW + timedelta(seconds=wait)
    assert after.last_error == {
        "class": "ModelUnavailable",
        "message": "child_crash",
        "at": clock.format_utc(NOW),
        "attempt": 1,
    }
    (event,) = _events("retry")
    assert event["detail"]["target"] == "job"
    assert event["detail"]["policy"] == "job_backoff"
    assert event["detail"]["error_type"] == "ModelUnavailable"
    assert event["detail"]["attempt"] == 1
    assert event["detail"]["wait_s"] == pytest.approx(wait)
    (line,) = [entry for entry in logs if entry["event"] == "jobs.job.rescheduled"]
    assert line["log_level"] == "warning"
    assert line["error_type"] == "ModelUnavailable"
    assert line["scheduled_for"] == clock.format_utc(NOW + timedelta(seconds=wait))
    assert _finished("review", "requeued") == 1.0


def test_ut08_59_error_message_redacted_and_cut(fake_now: _Clock) -> None:
    """UT08-59 `last_error.message` is `redact_text(str(err))[:2048]`; raw text never logged."""
    raw = "x a@b.co" * 120  # 960 chars; each address grows when redacted
    row = _running()
    with structlog.testing.capture_logs() as logs:
        assert _finish(row, SchemaViolation(raw)) == "failed"
    message = (_get(row.job_id).last_error or {})["message"]
    assert message == (redact_text(raw) or "")[:2048]
    assert len(message) == 2048
    assert "a@b.co" not in message
    assert all("a@b.co" not in repr(entry) for entry in logs)


def test_ut08_59_unredactable_message_is_withheld(
    fake_now: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-59 a failed redaction stores a placeholder, never the raw message (fail closed)."""
    monkeypatch.setattr(outcomes, "redact_text", lambda _text: None)
    row = _running()
    assert _finish(row, SchemaViolation("alice@example.com")) == "failed"
    message = (_get(row.job_id).last_error or {})["message"]
    assert isinstance(message, str)
    assert "alice" not in message
    assert message == outcomes.MESSAGE_WITHHELD


def test_ut08_59_fatal_error_fails(fake_now: _Clock) -> None:
    """UT08-59 a fatal error: `failed`, `job_failed` event, finished metric."""
    row = _running()
    assert _finish(row, ConfigError("bad")) == "failed"
    after = _get(row.job_id)
    assert after.status == "failed"
    assert (after.last_error or {})["class"] == "ConfigError"
    (event,) = _events("job_failed")
    assert event["detail"] == {"kind": "review", "attempt": 1, "error_type": "ConfigError"}
    assert _finished("review", "failed") == 1.0
    assert _run_seconds("review") == [4.0]


def test_ut08_59_retryable_at_max_fails(fake_now: _Clock) -> None:
    """UT08-59 a retryable error on the last attempt dead-letters the job."""
    row = _running(max_attempts=1)
    assert _finish(row, SourceUnavailable("down")) == "failed"
    assert _get(row.job_id).status == "failed"
    assert _events("retry") == []


def test_ut08_59_open_circuit_sync_is_done_partial(fake_now: _Clock) -> None:
    """UT08-59 an open circuit on a sync job finishes it `done` partial (R-39)."""
    row = _running("sync", payload={"source": "jira"})
    err = CircuitOpen("open", key="source:jira", retry_at=NOW + timedelta(minutes=5))
    assert _finish(row, err) == "done"
    after = _get(row.job_id)
    assert after.status == "done"
    assert after.result == {
        "partial": True,
        "outcome": "skipped_open_circuit",
        "skipped_open_circuit": ["source:jira"],
    }
    (event,) = _events("job_done")
    assert event["detail"]["partial"] is True


_OUTCOMES: list[Callable[[], JobOutcome | HernessError]] = [
    lambda: JobOutcome(status="done", result={"state": {"a": 1}}),
    lambda: JobOutcome(status="yield"),
    lambda: ModelUnavailable("stalled"),
    lambda: ConfigError("bad"),
]


@pytest.mark.parametrize("make", _OUTCOMES, ids=["done", "yield", "requeue", "failed"])
def test_ut08_59_wrong_owner_is_lease_lost(
    fake_now: _Clock, make: Callable[[], JobOutcome | HernessError]
) -> None:
    """UT08-59 wrong owner → `lease_lost`, row unchanged, WARNING only (TH08-08)."""
    row = _running()
    before = _get(row.job_id)
    counters = dict(process_state().metric_buffer.counters)
    with structlog.testing.capture_logs() as logs:
        assert _finish(row, make(), owner="h:2:cli") == "lease_lost"
    assert _get(row.job_id) == before
    assert _events() == []
    assert dict(process_state().metric_buffer.counters) == counters
    assert _run_seconds("review") == []
    (line,) = [entry for entry in logs if entry["event"] == "jobs.job.lease_lost"]
    assert line["log_level"] == "warning"
    assert (line["job_id"], line["owner"]) == (row.job_id, "h:2:cli")


def test_ut08_59_lease_lost_skips_chain_advance(
    fake_now: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-59 a lost lease never advances a chain."""
    seen: list[JobRow] = []
    monkeypatch.setattr(outcomes, "advance_chain", seen.append)
    row = _running(payload={"schedule": "nightly", "fire_at": clock.format_utc(NOW)})
    assert _finish(row, JobOutcome(status="done"), owner="h:2:cli") == "lease_lost"
    assert _finish(row, ConfigError("bad"), owner="h:2:cli") == "lease_lost"
    assert seen == []


def test_ut08_59_missing_job_is_lease_lost(fake_now: _Clock) -> None:
    """UT08-59 a job gone on re-read: `lease_lost`, no write, event or metric."""
    row = _running().model_copy(update={"job_id": "job_01J0000000000000000000000Z"})
    counters = dict(process_state().metric_buffer.counters)
    with structlog.testing.capture_logs() as logs:
        assert _finish(row, JobOutcome(status="done")) == "lease_lost"
    assert _events() == []
    assert dict(process_state().metric_buffer.counters) == counters
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == ["jobs.job.lease_lost"]


def test_ut08_59_canceled_job_is_finalized(fake_now: _Clock) -> None:
    """UT08-59 a job canceled while running: finalized, `canceled`, lease cleared."""
    row = _running()
    assert queue.cancel(row.job_id) == "cancel_requested"
    fake_now.current = NOW + timedelta(seconds=9)
    with structlog.testing.capture_logs() as logs:
        assert _finish(row, JobOutcome(status="done", result={"a": 1})) == "canceled"
    after = _get(row.job_id)
    assert (after.status, after.lease_owner) == ("canceled", None)
    assert after.finished_at == NOW + timedelta(seconds=9)
    assert after.result is None
    assert _events() == []
    (line,) = [entry for entry in logs if entry["event"] == "jobs.job.canceled"]
    assert (line["log_level"], line["job_id"], line["owner"]) == ("info", row.job_id, OWNER)


def test_ut08_59_canceled_with_wrong_owner_is_lease_lost(fake_now: _Clock) -> None:
    """UT08-59 finalizing a canceled job needs the lease owner too."""
    row = _running()
    queue.cancel(row.job_id)
    assert _finish(row, JobOutcome(status="done"), owner="h:2:cli") == "lease_lost"
    assert _get(row.job_id).finished_at is None


def test_ut08_59_done_advances_chain_with_row_after(
    fake_now: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-59 a scheduled `done` job advances its chain with the re-read row."""
    seen: list[JobRow] = []
    monkeypatch.setattr(outcomes, "advance_chain", seen.append)
    payload = {"schedule": "nightly", "fire_at": clock.format_utc(NOW)}
    row = _running(payload=payload)
    assert _finish(row, JobOutcome(status="done", result={"ok": True})) == "done"
    (after,) = seen
    assert (after.job_id, after.status, after.result) == (row.job_id, "done", {"ok": True})


def test_ut08_59_failed_scheduled_job_reports_chain_break(
    fake_now: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-59 a scheduled job that fails hands its `failed` row to `advance_chain`."""
    seen: list[JobRow] = []
    monkeypatch.setattr(outcomes, "advance_chain", seen.append)
    row = _running(payload={"schedule": "nightly", "fire_at": clock.format_utc(NOW)})
    assert _finish(row, ConfigError("bad")) == "failed"
    assert [r.status for r in seen] == ["failed"]


def test_ut08_59_unscheduled_job_does_not_advance(
    fake_now: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-59 a job without `schedule` in its payload never calls `advance_chain`."""
    seen: list[JobRow] = []
    monkeypatch.setattr(outcomes, "advance_chain", seen.append)
    assert _finish(_running(), JobOutcome(status="done")) == "done"
    assert _finish(_running(payload={"n": 2}), ConfigError("bad")) == "failed"
    assert _finish(_running(payload={"n": 3}), JobOutcome(status="yield")) == "queued"
    assert seen == []


def test_ut08_59_row_gone_after_write_skips_chain(
    fake_now: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-59 a row gone when re-read after the write: no chain advance, no error."""
    seen: list[JobRow] = []
    monkeypatch.setattr(outcomes, "advance_chain", seen.append)
    row = _running(payload={"schedule": "nightly", "fire_at": clock.format_utc(NOW)})
    backend = require_jobs_backend()
    reads = iter([backend.get_job(row.job_id), None])
    monkeypatch.setattr(backend, "get_job", lambda _job_id: next(reads))
    assert _finish(row, JobOutcome(status="done")) == "done"
    assert seen == []


def test_ut08_59_chain_error_is_logged_not_raised(
    fake_now: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-59 a chain advance error is logged `jobs.schedule.error`; the job stays done."""

    def broken(_row: JobRow) -> None:
        msg = "broken entry"
        raise ConfigError(msg)

    monkeypatch.setattr(outcomes, "advance_chain", broken)
    row = _running(payload={"schedule": "nightly", "fire_at": clock.format_utc(NOW)})
    with structlog.testing.capture_logs() as logs:
        assert _finish(row, JobOutcome(status="done")) == "done"
    (line,) = [entry for entry in logs if entry["event"] == "jobs.schedule.error"]
    assert (line["schedule"], line["error_type"]) == ("nightly", "ConfigError")
    assert _get(row.job_id).status == "done"


def test_ut08_59_fault_point_before_complete(
    fake_now: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-59 `fault_point("job.before_complete", kind=...)` runs first."""
    calls: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(outcomes, "fault_point", lambda name, **kw: calls.append((name, kw)))
    row = _running()
    _finish(row, JobOutcome(status="done"))
    assert calls == [("job.before_complete", {"kind": "review"})]
