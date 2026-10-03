"""Fake monitoring adapters and row builders shared by the monitoring base tests (T01-19)."""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from herness.connectors.monitoring.base import (
    EventRow,
    MetricRow,
    complete_days,
    event_batch,
    metric_batch,
)

UTC = datetime.UTC
FETCHED = datetime.datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
TS = datetime.datetime(2026, 3, 1, 9, 30, tzinfo=UTC)
DAY = datetime.date(2026, 2, 28)


def event_row(key: str = "e1", ts: datetime.datetime = TS, **extra: Any) -> EventRow:
    row: dict[str, Any] = {
        "event_key": key,
        "ts": ts,
        "service": "checkout",
        "host": "web-1",
        "severity_raw": "P2",
        "title": "Latency high",
        "status": "closed",
        "dedup_key": "dk-1",
        "end_ts": None,
        "incident_ref": None,
        "payload": '{"id":"e1"}',
    }
    return EventRow(**(row | extra))  # type: ignore[typeddict-item]


def metric_row(day: datetime.date = DAY, value: Any = 1.5, **extra: Any) -> MetricRow:
    row: dict[str, Any] = {
        "date": day,
        "service": "checkout",
        "metric_name": "m",
        "value": value,
        "unit": "count",
        "payload": "{}",
    }
    return MetricRow(**(row | extra))  # type: ignore[typeddict-item]


@dataclass
class FakeAdapter:
    """Adapter replaying fixed rows: events whose ``ts`` is in ``[since, until)`` and one
    metric row per complete UTC day of ``complete_days(since, until)``."""

    tool: str
    event_times: list[datetime.datetime] = field(default_factory=list)
    calls: list[tuple[str, datetime.datetime, datetime.datetime]] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    error: Exception | None = None

    def check(self) -> None:
        self.checks.append(self.tool)
        if self.error is not None:
            raise self.error

    def events(
        self, since: datetime.datetime, until: datetime.datetime
    ) -> Iterator[pa.RecordBatch]:
        self.calls.append(("events", since, until))
        rows = [
            event_row(f"{self.tool[:2]}{i}", ts)
            for i, ts in enumerate(self.event_times)
            if since <= ts < until
        ]
        if rows:
            yield event_batch(self.tool, rows, fetched_at=FETCHED)

    def daily_metrics(
        self, since: datetime.datetime, until: datetime.datetime
    ) -> Iterator[pa.RecordBatch]:
        self.calls.append(("daily_metrics", since, until))
        first, end = complete_days(since, until)
        days: list[MetricRow] = []
        while first < end:
            days.append(metric_row(first.date(), 2.0))
            first += datetime.timedelta(days=1)
        if days:
            yield metric_batch(self.tool, days, fetched_at=FETCHED)
