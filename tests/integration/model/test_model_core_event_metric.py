"""`260_event.sql` (U02-121) and `270_metric_daily.sql` (U02-122), impl 02 T02-17.

IT02-16: event service resolution through `stg.service_name_lookup` (alias, unique name,
ambiguous name), incident resolution on `incident_ref`, and the duration rule, against the
real `core.incident` built by file 230 (T02-16); the empty case runs the whole 000-299
build. IT02-17: one metric row per key (latest), and the unmapped count in
`stg.build_counts`.
"""

from __future__ import annotations

import datetime

import pytest
from tests.integration.model._core_build import SVC, build_core
from tests.integration.model._core_late import build_late, rerun
from tests.integration.model._stg_lake import UTC, Row, at, commit
from tests.support.build_harness import BuildHarness

pytestmark = pytest.mark.integration

MON = "monitoring"
EV = "monitoring:event:"
INC = "servicenow:incident:"
MAPPINGS = {
    "enums": {"monitoring.severity": {"crit": "critical", "warn": "warning"}},
    "service_overrides": [{"service_id": SVC + "s4", "aliases": ["Pay-Alias"]}],
}


def _services(harness: BuildHarness) -> None:
    services = [("s1", "Checkout"), ("s2", "Dup"), ("s3", "DUP"), ("s4", "Payments")]
    commit(
        harness.layout.raw,
        "servicenow",
        "cmdb_ci_service",
        [Row(k, at(0), {"name": name}) for k, name in services],
    )


def _incidents(harness: BuildHarness) -> None:
    numbers = [("i1", "INC0001"), ("i2", "INC0002"), ("i3", "INC0002")]
    commit(
        harness.layout.raw,
        "servicenow",
        "incident",
        [Row(k, at(0), {"number": number}) for k, number in numbers],
    )


def _event(key: str, service: str, ts: str, end_ts: str | None, ref: str | None) -> Row:
    fields = {
        "source_tool": "datadog",
        "event_key": key,
        "ts": ts,
        "service": service,
        "host": f"h-{key}",
        "severity_raw": "CRIT",
        "title": f"alert {key}",
        "status": "resolved",
        "dedup_key": f"dd-{key}",
        "end_ts": end_ts,
        "incident_ref": ref,
    }
    return Row(f"datadog:{key}", at(0), fields)


def _t(hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2024, 3, 1, hour, minute, tzinfo=UTC)


def _core_event(
    key: str,
    ts: datetime.datetime | None,
    service_id: str | None,
    duration_s: int | None,
    incident_id: str | None,
) -> tuple[object, ...]:
    return (
        f"{EV}datadog:{key}",
        "datadog",
        ts,
        service_id,
        f"h-{key}",
        "critical",
        f"alert {key}",
        "resolved",
        f"dd-{key}",
        duration_s,
        incident_id,
    )


def test_it02_16_event_service_incident_duration(build_harness: BuildHarness) -> None:
    """IT02-16 events with an alias, a unique name, an ambiguous name, an unknown name and
    `incident_ref`s matching one, two and no incidents: service and incident per U02-121;
    duration only when both timestamps are set and `end_ts >= ts`."""
    _services(build_harness)
    events = [
        _event("e1", "CHECKOUT", "2024-03-01 10:00:00", "2024-03-01 10:05:00", "INC0001"),
        _event("e2", "pay-alias", "2024-03-01 11:00:00", "2024-03-01 10:59:00", "INC0002"),
        _event("e3", "Dup", "2024-03-01 12:00:00", None, "INC9999"),
        _event("e4", "unknown", "2024-03-01 13:00:00", "2024-03-01 13:00:00", None),
        _event("e5", "Payments", "bad ts", "2024-03-01 14:00:00", "inc0001"),
    ]
    commit(build_harness.layout.raw, MON, "event", events)
    _incidents(build_harness)
    build_late(build_harness, [230, 260], mappings=MAPPINGS)
    sql = (
        "SELECT event_id, source_tool, ts, service_id, host, severity, alert_name, status,"
        " dedup_key, duration_s, incident_id FROM core.event ORDER BY event_id"
    )
    rows = build_harness.query(sql)
    assert rows == [
        _core_event("e1", _t(10), SVC + "s1", 300, INC + "i1"),  # unique name, one incident
        _core_event("e2", _t(11), SVC + "s4", None, None),  # alias; end < ts; two incidents
        _core_event("e3", _t(12), None, None, None),  # ambiguous name; no end; no incident
        _core_event("e4", _t(13), None, 0, None),  # unknown name; end == ts
        _core_event("e5", None, SVC + "s4", None, None),  # bad ts; ref case differs
    ]
    rerun(build_harness, 260)  # idempotent
    assert build_harness.query(sql) == rows


def test_it02_16_event_empty_typed(build_harness: BuildHarness) -> None:
    """IT02-16 no event files: the whole 000-299 build runs and `core.event` exists,
    empty, with the U02-121 types."""
    build_core(build_harness, hi=299)
    assert build_harness.query("SELECT count(*) FROM core.event") == [(0,)]
    rows = build_harness.query(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'core' AND table_name = 'event'"
    )
    types = {str(name): str(kind) for name, kind in rows}
    assert types == {
        "event_id": "VARCHAR",
        "source_tool": "VARCHAR",
        "ts": "TIMESTAMP WITH TIME ZONE",
        "service_id": "VARCHAR",
        "host": "VARCHAR",
        "severity": "VARCHAR",
        "alert_name": "VARCHAR",
        "status": "VARCHAR",
        "dedup_key": "VARCHAR",
        "duration_s": "BIGINT",
        "incident_id": "VARCHAR",
    }


def _metric(key: str, hours: int, **fields: str | None) -> Row:
    base: dict[str, str | None] = {
        "source_tool": "datadog",
        "metric_name": "cpu",
        "unit": "percent",
        "service": "Checkout",
        "date": "2024-03-01",
        "value": "1",
    }
    base.update(fields)
    return Row(key, at(hours), base)


METRICS = [
    _metric("m1", 0, value="1"),
    _metric("m2", 1, value="2"),  # same key as m1, later: wins
    _metric("m3", 2, metric_name="mem", value="3"),
    _metric("m4", 2, metric_name="mem", value="4"),  # same time as m3, higher record_id: wins
    _metric("m5", 0, service="nope"),  # unmapped service
    _metric("m6", 0, date="not a date"),  # NULL date
    _metric("m7", 0, metric_name=None),  # NULL metric name: dropped, not "unmapped"
    _metric("m8", 0, source_tool="prometheus", value="8"),  # separate key
    _metric("m9", 0, service="pay-alias", unit="ms", value="9.5"),
]


def test_it02_17_metric_daily_latest_per_key(build_harness: BuildHarness) -> None:
    """IT02-17 duplicate keys and an unmapped service: one row per (date, service_id,
    metric_name, source_tool), the latest `source_updated_at` then highest `record_id`;
    `metric_daily_raw` and `metric_daily_unmapped` in `stg.build_counts`."""
    _services(build_harness)
    commit(build_harness.layout.raw, MON, "metric_daily", METRICS)
    build_late(build_harness, [270], mappings=MAPPINGS)
    sql = (
        "SELECT date, service_id, metric_name, value, unit, source_tool FROM core.metric_daily"
        " ORDER BY ALL"
    )
    day = datetime.date(2024, 3, 1)
    expected = [
        (day, SVC + "s1", "cpu", 2.0, "percent", "datadog"),
        (day, SVC + "s1", "cpu", 8.0, "percent", "prometheus"),
        (day, SVC + "s1", "mem", 4.0, "percent", "datadog"),
        (day, SVC + "s4", "cpu", 9.5, "ms", "datadog"),
    ]
    assert build_harness.query(sql) == expected
    counts_sql = "SELECT name, value FROM stg.build_counts ORDER BY name"
    counts = [("metric_daily_raw", 9), ("metric_daily_unmapped", 2)]
    assert build_harness.query(counts_sql) == counts
    rerun(build_harness, 270)  # delete-then-insert: no duplicate count rows
    assert build_harness.query(counts_sql) == counts
    assert build_harness.query(sql) == expected


def test_it02_17_metric_daily_empty(build_harness: BuildHarness) -> None:
    """IT02-17 no metric files: `core.metric_daily` is empty with the U02-122 types and
    both counts are 0."""
    build_late(build_harness, [270])
    assert build_harness.query("SELECT count(*) FROM core.metric_daily") == [(0,)]
    types = build_harness.query(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'core' AND table_name = 'metric_daily' ORDER BY ordinal_position"
    )
    assert types == [
        ("date", "DATE"),
        ("service_id", "VARCHAR"),
        ("metric_name", "VARCHAR"),
        ("value", "DOUBLE"),
        ("unit", "VARCHAR"),
        ("source_tool", "VARCHAR"),
    ]
    assert build_harness.query("SELECT name, value FROM stg.build_counts ORDER BY name") == [
        ("metric_daily_raw", 0),
        ("metric_daily_unmapped", 0),
    ]
