"""Resilience ops-store area (impl 08 U08-94, R-08): SQL for `source_health` and
`resilience_event`, and the `purge_events` retention purge.

The tables are impl 02 migrations 001 and 002 (§4.3.1, §4.3.2); this area adds no migration.
Reads use `read_one` / `read_all`; every write runs in `run_write` (`BEGIN IMMEDIATE`, R-10).
Metric rows are delegated to the single `metric_sample` writer of `herness.store.ops.metrics`
(U08-100, R-12): the one area-to-area import, recorded in the `ops-areas-acyclic` contract.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Final, Literal, cast

from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.core.resilience.ports import EventRow, HealthRow, JsonScalar
from herness.core.types import BreakerState, MetricSample

from .core import dump_json, load_json, read_all, read_one, run_write
from .metrics import purge_metric_samples, record_metric_samples

LAST_ERROR_MAX_CHARS: Final = 500  # §4.1.3: redacted, at most 500 chars
TARGET_MAX_CHARS: Final = 200  # §4.1.4

_UPSERT_HEALTH: Final = (
    "INSERT INTO source_health"
    " (source, state, failures, trips, opened_at, last_error, updated_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(source) DO UPDATE SET state = excluded.state,"
    " failures = excluded.failures, trips = excluded.trips, opened_at = excluded.opened_at,"
    " last_error = excluded.last_error, updated_at = excluded.updated_at"
)
_CLAIM_PROBE: Final = (
    "UPDATE source_health SET state = 'half_open', updated_at = :now WHERE source = :key"
    " AND ((state = 'open' AND :now >= :probe_due)"
    " OR (state = 'half_open' AND updated_at < :stale_before)) RETURNING source"
)
_INSERT_EVENT: Final = (
    "INSERT INTO resilience_event (event_id, ts, kind, component, target, run_id, job_id,"
    " task_id, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_LATEST_EVENT: Final = (
    "SELECT * FROM resilience_event WHERE kind = ? ORDER BY ts DESC, event_id DESC LIMIT 1"
)


def _placeholders(count: int) -> str:
    """`?, ?, …` for an `IN (...)` list: built from the length only, never from values."""
    return ", ".join("?" * count)


def _health(row: sqlite3.Row) -> HealthRow:
    opened = row["opened_at"]
    return HealthRow(
        source=row["source"],
        state=cast("BreakerState", row["state"]),
        failures=row["failures"],
        trips=row["trips"],
        opened_at=None if opened is None else clock.parse_utc(opened),
        last_error=row["last_error"],
        updated_at=clock.parse_utc(row["updated_at"]),
    )


def _event(row: sqlite3.Row) -> EventRow:
    detail = load_json(row["detail"], field="resilience_event.detail")
    if not isinstance(detail, dict):
        msg = "resilience_event.detail is not a JSON object"
        raise SchemaViolation(msg)
    return EventRow(
        event_id=row["event_id"],
        ts=clock.parse_utc(row["ts"]),
        kind=row["kind"],
        component=cast("Literal['resilience', 'jobs']", row["component"]),
        target=row["target"],
        run_id=row["run_id"],
        job_id=row["job_id"],
        task_id=row["task_id"],
        detail=cast("dict[str, JsonScalar | list[JsonScalar]]", detail),
    )


def purge_events(before: datetime) -> int:
    """Delete `resilience_event` rows older than ``before``; return the count (U08-94).

    Called with ``now - 90 days`` by the `herness.admin` retention purge (R-07).
    """
    cutoff = clock.format_utc(before)

    def delete(conn: sqlite3.Connection) -> int:
        return conn.execute("DELETE FROM resilience_event WHERE ts < ?", (cutoff,)).rowcount

    return run_write(delete, op="resilience_event_purge")


class SqliteResilienceBackend:
    """`ResilienceBackend` (U08-08) over the ops store (U08-94); stateless, thread-safe."""

    def health_get(self, key: str) -> HealthRow | None:
        """The `source_health` row of ``key``, or None."""
        row = read_one("SELECT * FROM source_health WHERE source = ?", (key,))
        return None if row is None else _health(row)

    def health_list(self, states: Sequence[BreakerState]) -> list[HealthRow]:
        """Every row whose state is in ``states``, ordered by source."""
        if not states:
            return []
        marks = _placeholders(len(states))
        sql = f"SELECT * FROM source_health WHERE state IN ({marks}) ORDER BY source"  # noqa: S608 - placeholders only
        return [_health(row) for row in read_all(sql, tuple(states))]

    def health_apply(
        self,
        key: str,
        fn: Callable[[HealthRow | None], HealthRow | None],
        now: datetime,
    ) -> tuple[HealthRow | None, HealthRow | None]:
        """Read-modify-write of one row in one `BEGIN IMMEDIATE`; ``fn`` returning None
        writes nothing. The written row carries ``source = key``, ``updated_at = now`` and
        ``last_error`` cut to 500 chars, and is returned as written."""

        def apply(conn: sqlite3.Connection) -> tuple[HealthRow | None, HealthRow | None]:
            row = conn.execute("SELECT * FROM source_health WHERE source = ?", (key,)).fetchone()
            before = None if row is None else _health(row)
            after = fn(before)
            if after is None:
                return before, None
            error = None if after.last_error is None else after.last_error[:LAST_ERROR_MAX_CHARS]
            after = dataclasses.replace(after, source=key, last_error=error, updated_at=now)
            opened = None if after.opened_at is None else clock.format_utc(after.opened_at)
            stamp = clock.format_utc(now)
            conn.execute(
                _UPSERT_HEALTH,
                (key, after.state, after.failures, after.trips, opened, error, stamp),
            )
            return before, after

        return run_write(apply, op="source_health")

    def health_claim_probe(
        self, key: str, now: datetime, probe_due: datetime, stale_before: datetime
    ) -> bool:
        """Claim the half-open probe of ``key`` (design 08 §5.3 plus the stale extension)."""
        params: Mapping[str, object] = {
            "key": key,
            "now": clock.format_utc(now),
            "probe_due": clock.format_utc(probe_due),
            "stale_before": clock.format_utc(stale_before),
        }

        def claim(conn: sqlite3.Connection) -> bool:
            return conn.execute(_CLAIM_PROBE, params).fetchone() is not None

        return run_write(claim, op="source_health_probe")

    def health_reset(self, keys: Sequence[str], now: datetime) -> list[str]:
        """Close each listed breaker unless already closed with no failures; return the
        changed keys, sorted."""
        if not keys:
            return []
        sql = (
            "UPDATE source_health SET state = 'closed', failures = 0, trips = 0,"  # noqa: S608 - placeholders only
            f" opened_at = NULL, updated_at = ? WHERE source IN ({_placeholders(len(keys))})"
            " AND (state != 'closed' OR failures != 0) RETURNING source"
        )
        params = (clock.format_utc(now), *keys)

        def reset(conn: sqlite3.Connection) -> list[str]:
            return sorted(str(row[0]) for row in conn.execute(sql, params).fetchall())

        return run_write(reset, op="source_health_reset")

    def insert_event(self, row: EventRow) -> None:
        """Append one `resilience_event` row (no idempotency key, §4.1.4)."""
        target = None if row.target is None else row.target[:TARGET_MAX_CHARS]
        detail = dump_json(row.detail, field="resilience_event.detail")
        ts = clock.format_utc(row.ts)
        values = (row.event_id, ts, row.kind, row.component, target, row.run_id, row.job_id)

        def insert(conn: sqlite3.Connection) -> None:
            conn.execute(_INSERT_EVENT, (*values, row.task_id, detail))

        run_write(insert, op="resilience_event")

    def count_events(self, kind: str, *, target: str | None = None, since: datetime) -> int:
        """Rows of ``kind`` (and ``target`` when given) with ``ts >= since``."""
        sql = "SELECT COUNT(*) FROM resilience_event WHERE kind = ? AND ts >= ?"
        params: list[object] = [kind, clock.format_utc(since)]
        if target is not None:
            sql += " AND target = ?"
            params.append(target)
        row = read_one(sql, params)
        return 0 if row is None else int(row[0])

    def event_counts(self, since: datetime, kinds: Sequence[str]) -> dict[str, int]:
        """Count per kind of ``kinds`` since ``since``; kinds with no rows count 0."""
        counts = dict.fromkeys(kinds, 0)
        if not kinds:
            return counts
        marks = _placeholders(len(kinds))
        sql = (
            f"SELECT kind, COUNT(*) FROM resilience_event WHERE kind IN ({marks})"  # noqa: S608 - placeholders only
            " AND ts >= ? GROUP BY kind"
        )
        for row in read_all(sql, (*kinds, clock.format_utc(since))):
            counts[str(row[0])] = int(row[1])
        return counts

    def latest_event(self, kind: str) -> EventRow | None:
        """The newest row of ``kind`` (ties broken by `event_id`), or None."""
        row = read_one(_LATEST_EVENT, (kind,))
        return None if row is None else _event(row)

    def insert_metric_samples(self, rows: Sequence[MetricSample]) -> int:
        """Delegate to the single `metric_sample` writer (U08-100, R-12)."""
        return record_metric_samples(rows)

    def purge_events(self, before: datetime) -> int:
        """Port form of the module function `purge_events`."""
        return purge_events(before)

    def purge_metric_samples(self, before: datetime) -> int:
        """Port form of `herness.store.ops.metrics.purge_metric_samples`."""
        return purge_metric_samples(before)
