"""Unit tests for herness.store.ops.resilience (impl 08 U08-94; T08-05).

Rows are checked against the real migrated DDL (impl 02 migrations 001 and 002; impl 08
§4.1.3, §4.1.4). The breaker behaviour on top of these methods is T08-06's.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from tests.support.ops_store import OpsStoreHandle

from herness.core.errors import SchemaViolation
from herness.core.resilience import ResilienceBackend
from herness.core.resilience.ports import EventRow, HealthRow
from herness.store import ops
from herness.store.ops import resilience as area
from herness.store.ops.core import read_all, read_one, run_write
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def backend(ops_store: OpsStoreHandle) -> SqliteResilienceBackend:
    del ops_store
    return SqliteResilienceBackend()


def _row(key: str = "jira", **changes: object) -> HealthRow:
    values: dict[str, object] = {
        "source": key,
        "state": "open",
        "failures": 5,
        "trips": 1,
        "opened_at": T0,
        "last_error": "SourceUnavailable: boom",
        "updated_at": T0,
    }
    values |= changes
    return HealthRow(**values)  # type: ignore[arg-type]


def _event(kind: str = "retry", ts: datetime = T0, **changes: object) -> EventRow:
    values: dict[str, object] = {
        "event_id": f"evt_{kind}_{ts.timestamp():.0f}",
        "ts": ts,
        "kind": kind,
        "component": "resilience",
        "target": "jira",
        "run_id": None,
        "job_id": "job_1",
        "task_id": None,
        "detail": {"attempt": 2, "error_type": "SourceUnavailable", "paths": ["a", 1]},
    }
    values |= changes
    return EventRow(**values)  # type: ignore[arg-type]


def test_ut08_29_backend_satisfies_port(backend: SqliteResilienceBackend) -> None:
    """UT08-29 SqliteResilienceBackend satisfies the ResilienceBackend port structurally."""
    port: ResilienceBackend = backend
    assert port.health_get("nothing") is None


def test_ut08_29_health_apply_upserts_source_health_row(
    backend: SqliteResilienceBackend,
) -> None:
    """UT08-29 health_apply inserts then updates one §4.1.3 row, stamping updated_at."""
    later = T0 + timedelta(seconds=30)
    before, after = backend.health_apply("jira", lambda _: _row(last_error="x" * 900), T0)
    assert before is None
    assert after is not None
    assert after.last_error == "x" * 500
    raw = read_one("SELECT * FROM source_health WHERE source = 'jira'")
    assert raw is not None
    assert dict(raw) == {
        "source": "jira",
        "state": "open",
        "failures": 5,
        "trips": 1,
        "opened_at": "2026-09-01T12:00:00.000000Z",
        "last_error": "x" * 500,
        "updated_at": "2026-09-01T12:00:00.000000Z",
    }
    closed = _row(state="closed", failures=0, trips=0, opened_at=None, last_error=None)
    before, after = backend.health_apply("jira", lambda _: closed, later)
    assert before is not None
    assert before.state == "open"
    assert after == dataclasses.replace(closed, updated_at=later)
    assert backend.health_get("jira") == after


def test_ut08_29_health_apply_none_writes_nothing(backend: SqliteResilienceBackend) -> None:
    """UT08-29 fn returning None leaves the table untouched and returns (before, None)."""
    seen: list[HealthRow | None] = []

    def fn(row: HealthRow | None) -> None:
        seen.append(row)

    assert backend.health_apply("jira", fn, T0) == (None, None)
    assert seen == [None]
    assert read_all("SELECT * FROM source_health") == []


def test_ut08_29_health_list_and_reset(backend: SqliteResilienceBackend) -> None:
    """UT08-29 health_list filters by state; health_reset closes only changed rows."""
    backend.health_apply("jira", lambda _: _row("jira"), T0)
    backend.health_apply("model:x", lambda _: _row("model:x", state="half_open"), T0)
    clean = _row("slack", state="closed", failures=0, trips=0, opened_at=None)
    backend.health_apply("slack", lambda _: clean, T0)
    assert backend.health_list([]) == []
    assert [r.source for r in backend.health_list(["open", "half_open"])] == ["jira", "model:x"]
    later = T0 + timedelta(minutes=5)
    assert backend.health_reset([], later) == []
    assert backend.health_reset(["slack", "model:x", "jira", "absent"], later) == [
        "jira",
        "model:x",
    ]
    row = backend.health_get("jira")
    assert row is not None
    assert (row.state, row.failures, row.trips, row.opened_at) == ("closed", 0, 0, None)
    assert row.updated_at == later
    slack = backend.health_get("slack")
    assert slack is not None
    assert slack.updated_at == T0


def test_ut08_19_claim_probe_due_and_stale(backend: SqliteResilienceBackend) -> None:
    """UT08-19 store part: the probe claim wins once when due and re-claims a stale
    half-open row (updated 601 s ago) but not a fresh one (599 s)."""
    backend.health_apply("jira", lambda _: _row(), T0)
    due = T0 + timedelta(seconds=60)
    early = T0 + timedelta(seconds=59)
    assert not backend.health_claim_probe("jira", early, due, early - timedelta(seconds=600))
    assert backend.health_claim_probe("jira", due, due, due - timedelta(seconds=600))
    row = backend.health_get("jira")
    assert row is not None
    assert (row.state, row.updated_at) == ("half_open", due)
    fresh = due + timedelta(seconds=599)
    assert not backend.health_claim_probe("jira", fresh, due, fresh - timedelta(seconds=600))
    stale = due + timedelta(seconds=601)
    assert backend.health_claim_probe("jira", stale, due, stale - timedelta(seconds=600))
    assert not backend.health_claim_probe("absent", stale, due, stale)


def test_ut08_29_insert_and_read_events(backend: SqliteResilienceBackend) -> None:
    """UT08-29 insert_event writes a §4.1.4 row; counts and latest read it back."""
    backend.insert_event(_event(target="t" * 300))
    backend.insert_event(_event(ts=T0 + timedelta(minutes=1), target="other"))
    backend.insert_event(_event("fallback", ts=T0 + timedelta(minutes=2)))
    raw = read_one("SELECT * FROM resilience_event ORDER BY ts LIMIT 1")
    assert raw is not None
    assert raw["ts"] == "2026-09-01T12:00:00.000000Z"
    assert raw["target"] == "t" * 200
    assert raw["detail"] == '{"attempt":2,"error_type":"SourceUnavailable","paths":["a",1]}'
    since = T0 + timedelta(seconds=30)
    assert backend.count_events("retry", since=T0) == 2
    assert backend.count_events("retry", since=since) == 1
    assert backend.count_events("retry", target="other", since=T0) == 1
    assert backend.event_counts(T0, ["retry", "fallback", "repair"]) == {
        "retry": 2,
        "fallback": 1,
        "repair": 0,
    }
    assert backend.event_counts(T0, []) == {}
    latest = backend.latest_event("retry")
    assert latest is not None
    assert (latest.ts, latest.target, latest.job_id) == (
        T0 + timedelta(minutes=1),
        "other",
        "job_1",
    )
    assert latest.detail == {"attempt": 2, "error_type": "SourceUnavailable", "paths": ["a", 1]}
    assert backend.latest_event("repair") is None


def test_ut08_29_latest_event_rejects_non_object_detail(
    backend: SqliteResilienceBackend,
) -> None:
    """UT08-29 a detail that is valid JSON but not an object is a SchemaViolation."""

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO resilience_event (event_id, ts, kind, component, detail)"
            " VALUES ('evt_x', '2026-09-01T12:00:00.000000Z', 'retry', 'resilience', '[1]')"
        )

    run_write(insert, op="test_insert")
    with pytest.raises(SchemaViolation):
        backend.latest_event("retry")


def test_ut08_29_purge_events_removes_only_older_rows(backend: SqliteResilienceBackend) -> None:
    """UT08-29 purge_events (package re-export and port method) deletes rows before the cutoff."""
    for days in (100, 91, 89, 0):
        backend.insert_event(_event(ts=T0 - timedelta(days=days)))
    assert ops.purge_events is area.purge_events
    assert ops.purge_events(T0 - timedelta(days=90)) == 2
    assert backend.purge_events(T0) == 1
    assert backend.count_events("retry", since=T0 - timedelta(days=365)) == 1


def test_ut08_32_backend_delegates_metric_rows(
    backend: SqliteResilienceBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-32 insert_metric_samples and purge_metric_samples call the single writer."""
    calls: list[object] = []
    monkeypatch.setattr(area, "record_metric_samples", lambda rows: calls.append(rows) or 7)
    monkeypatch.setattr(area, "purge_metric_samples", lambda before: calls.append(before) or 3)
    assert backend.insert_metric_samples([]) == 7
    assert backend.purge_metric_samples(T0) == 3
    assert calls == [[], T0]
