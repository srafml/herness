"""Tests for herness.core.resilience.events: record_event and EVENT_KINDS (impl 08 U08-18;
T08-05). Secret-looking fixtures are built at runtime so detect-secrets stays quiet."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog
from tests.support.fake_clock import FakeClock
from tests.support.ops_store import OpsStoreHandle

from herness.core import redact as r
from herness.core.errors import ConfigError, StoreBusy
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.resilience.events import DETAIL_FIELDS, EVENT_KINDS, record_event
from herness.core.resilience.ports import EventRow
from herness.store.ops.core import read_all
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.unit

KEY_TEXT = "api" + "_key=" + "synthetic" + "_key_abc"
EMAIL = "ops.person@example.com"


class _Tracer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    @property
    def run_id(self) -> str | None:
        return None

    @property
    def task_id(self) -> str | None:
        return None

    def emit(self, type: str, **fields: object) -> None:  # noqa: A002 - port name
        self.calls.append((type, fields))


class _BusyBackend(SqliteResilienceBackend):
    def insert_event(self, row: EventRow) -> None:
        msg = "ops store busy in resilience_event"
        raise StoreBusy(msg, op="resilience_event")


def _rows() -> list[dict[str, Any]]:
    return [dict(row) for row in read_all("SELECT * FROM resilience_event ORDER BY ts")]


def test_ut08_29_event_kinds_are_the_21_of_design_08() -> None:
    """UT08-29 EVENT_KINDS holds the 21 kinds; each has an allowlist of at most 20 keys."""
    assert len(EVENT_KINDS) == 21
    assert set(DETAIL_FIELDS) == EVENT_KINDS
    assert DETAIL_FIELDS["rekey_planned"] == {"fire_at", "key_id"}
    assert all(len(keys) <= 20 for keys in DETAIL_FIELDS.values())


def test_ut08_29_record_retry_filters_and_redacts(
    ops_db: OpsStoreHandle, test_redactor: r.Redactor, fake_clock: FakeClock
) -> None:
    """UT08-29 extra key and nested dict dropped; secret string redacted and ≤ 200 chars."""
    del ops_db, test_redactor
    detail = {
        "attempt": 2,
        "wait_s": 1.5,
        "policy": "source_http",
        "error_type": f"SourceUnavailable {KEY_TEXT} {EMAIL} " + "x" * 400,
        "breaker_key": {"nested": "dict"},
        "retry_after_s": None,
        "prompt": "extra key, never stored",
        "target": ["jira", 3, True],
    }
    with structlog.testing.capture_logs() as logs:
        record_event(
            "retry", component="resilience", target="jira" * 60, run_id="run_1", detail=detail
        )
    (row,) = _rows()
    assert row["event_id"].startswith("evt_")
    assert len(row["event_id"]) == 30
    assert row["ts"] == "2026-09-01T00:00:00.000000Z"
    assert (row["kind"], row["component"], row["run_id"]) == ("retry", "resilience", "run_1")
    assert row["target"] == ("jira" * 60)[:200]
    stored = json.loads(row["detail"])
    assert set(stored) == {"attempt", "wait_s", "policy", "error_type", "retry_after_s", "target"}
    assert stored["target"] == ["jira", 3, True]
    assert len(stored["error_type"]) == 200
    assert "synthetic_key_abc" not in row["detail"]
    assert EMAIL not in row["detail"]
    events = [e["event"] for e in logs]
    assert "resilience.event.detail_dropped" in events
    line = next(e for e in logs if e["event"] == "resilience.call.retry_scheduled")
    assert line["log_level"] == "warning"
    assert (line["kind"], line["run_id"]) == ("retry", "run_1")
    assert line["detail"] == stored
    dropped = next(e for e in logs if e["event"] == "resilience.event.detail_dropped")
    assert dropped["dropped"] == 2
    assert "prompt" not in str(dropped)


def test_ut08_29_value_split_at_redaction_window_does_not_leak(
    ops_db: OpsStoreHandle, test_redactor: r.Redactor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-29 a long redactable token first, then an e-mail straddling char 4 000: the
    redacted window shrinks below 200 chars, yet the undetected fragment is not stored.

    The known-value scrub runs first and today also masks credential spans over the whole
    text; it is made a pass-through here so the window edge of `redact_text` is what is
    tested (a token only `redact_text` detects reaches the same state)."""
    del ops_db, test_redactor
    from herness.core.resilience import events  # noqa: PLC0415 - module under test

    monkeypatch.setattr(events, "scrub_secrets", lambda _l, _m, event: dict(event))
    token = "api" + "_key=" + "A" * 3975  # redacts to a short placeholder
    text = token + " x " + EMAIL + " tail"
    assert text.index(EMAIL) < 4000 < text.index(EMAIL) + len(EMAIL)
    record_event("fallback", component="resilience", detail={"reason": text})
    (row,) = _rows()
    reason = json.loads(row["detail"]).get("reason", "")
    assert "ops.person" not in reason
    assert "AAAA" not in reason


def test_ut08_29_scalar_conversions(
    ops_db: OpsStoreHandle, test_redactor: r.Redactor, fake_clock: FakeClock
) -> None:
    """UT08-29 datetimes become ts text; NaN, objects and nested lists are dropped; lists
    are cut to 20 items."""
    del ops_db, test_redactor, fake_clock
    when = datetime(2026, 9, 2, 3, 4, 5, tzinfo=UTC)
    record_event(
        "schedule_fired",
        component="jobs",
        target="nightly",
        detail={"fire_at": when, "step": float("nan"), "schedule": object()},
    )
    record_event("repair", component="resilience", detail={"error_paths": list(range(30))})
    record_event("repair", component="resilience", detail={"error_paths": [["nested"]]})
    first, second, third = (json.loads(row["detail"]) for row in _rows())
    assert first == {"fire_at": "2026-09-02T03:04:05.000000Z"}
    assert second == {"error_paths": list(range(20))}
    assert third == {}


def test_ut08_29_unknown_kind_and_unbound_raise(
    reset_process_state: ProcessState, ops_store: OpsStoreHandle
) -> None:
    """UT08-29 unknown kind or component → ConfigError; unbound backend → ConfigError."""
    del ops_store
    with pytest.raises(ConfigError, match=r"not bound"):
        record_event("retry", component="resilience")
    bind_ops_backend(SqliteResilienceBackend())
    with pytest.raises(ConfigError, match="unknown resilience event kind"):
        record_event("exploded", component="resilience")
    with pytest.raises(ConfigError, match="unknown resilience event kind"):
        record_event("retry", component="ops")  # type: ignore[arg-type]
    assert _rows() == []


@pytest.mark.parametrize(
    ("kind", "level", "log_event"),
    [
        ("breaker_open", "warning", "resilience.breaker.opened"),
        ("breaker_half_open", "info", "resilience.breaker.half_opened"),
        ("job_failed", "error", "jobs.job.failed"),
        ("job_yield", "info", "jobs.job.yielded"),
        ("service_restart", "warning", "jobs.service.restarted"),
        ("chain_skipped", "info", "jobs.chain.skipped"),
    ],
)
def test_ut08_29_log_level_and_event_name(
    ops_db: OpsStoreHandle, kind: str, level: str, log_event: str
) -> None:
    """UT08-29 each kind logs its §8.1 event name at its table level with field `kind`."""
    del ops_db
    component = "resilience" if log_event.startswith("resilience.") else "jobs"
    with structlog.testing.capture_logs() as logs:
        record_event(kind, component=component, job_id="job_1", detail={"attempt": 1})  # type: ignore[arg-type]
    (line,) = [e for e in logs if e["event"] == log_event]
    assert (line["event"], line["log_level"], line["kind"]) == (log_event, level, kind)
    assert line["job_id"] == "job_1"


def test_ut08_29_trace_fan_out_only_for_trace_kinds(
    ops_db: OpsStoreHandle, test_redactor: r.Redactor
) -> None:
    """UT08-29 the tracer gets retry/fallback/repair/guard_stop only, with the filtered detail."""
    del ops_db, test_redactor
    tracer = _Tracer()
    record_event(
        "guard_stop", component="resilience", detail={"cause": "loop", "x": 1}, tracer=tracer
    )
    record_event("fallback", component="resilience", detail={"reason": "timeout"}, tracer=tracer)
    record_event("breaker_open", component="resilience", detail={"key": "jira"}, tracer=tracer)
    record_event("retry", component="resilience", detail={"attempt": 1})
    assert tracer.calls == [("guard_stop", {"cause": "loop"}), ("fallback", {"reason": "timeout"})]
    assert len(_rows()) == 4


def test_ut08_29_failed_redaction_drops_the_value(
    ops_db: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-29 a string whose redaction fails (None or ConfigError) is dropped, fail closed."""
    del ops_db
    from herness.core.resilience import events  # noqa: PLC0415 - module under test

    results: Sequence[object] = [None, ConfigError("no redaction key")]
    it = iter(results)

    def fake(_text: str) -> str | None:
        value = next(it)
        if isinstance(value, Exception):
            raise value
        return None

    monkeypatch.setattr(events, "redact_text", fake)
    record_event("fallback", component="resilience", detail={"reason": "a", "to_profile": "b"})
    (row,) = _rows()
    assert json.loads(row["detail"]) == {}


def test_ut08_30_store_failure_is_dropped_not_raised(
    reset_process_state: ProcessState, test_redactor: r.Redactor
) -> None:
    """UT08-30 insert_event raising StoreBusy: no exception; resilience.event.dropped logged."""
    del reset_process_state, test_redactor
    bind_ops_backend(_BusyBackend())
    with structlog.testing.capture_logs() as logs:
        record_event("job_done", component="jobs", job_id="job_1", detail={"kind": "sync"})
    dropped = next(e for e in logs if e["event"] == "resilience.event.dropped")
    assert dropped["log_level"] == "warning"
    assert (dropped["kind"], dropped["error_type"]) == ("job_done", "StoreBusy")
    assert any(e["event"] == "jobs.job.done" for e in logs)
