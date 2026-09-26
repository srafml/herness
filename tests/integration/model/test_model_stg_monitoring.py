"""Monitoring staging `130_stg_monitoring.sql` (impl 02 U02-111, T02-14).

The spec's own test rows for this file (IT02-16, IT02-17) were reassigned to the core
`260`/`270` staging of a later card (T02-16); this card's Tests row names only IT02-33
(`docs/impl/02-data-model.impl.md` line ~3903), so every function below is filed under
IT02-33 with this note, per the T02-14 brief.

Lake files are written with the real `LakeWriter`; each test builds files 000-160 on a
temp DuckDB build through `build_harness` with real reference tables (U02-87).
"""

import datetime

import pytest
from tests.integration.model._stg_lake import UTC, Row, at, build, cast_stats, columns, commit
from tests.support.build_harness import BuildHarness

pytestmark = pytest.mark.integration

MON = "monitoring"
SEVERITY_ENUM = {"monitoring.severity": {"crit": "critical", "warn": "warning"}}


def test_it02_33_mon_event_typed_columns_and_cast_stats(build_harness: BuildHarness) -> None:
    """IT02-33 (reassigned from IT02-16/17, see module docstring): `stg.mon_event` types
    `ts`/`end_ts`/`severity` with cast flags and leaves the rest raw; a malformed `end_ts`
    and an unmapped `severity_raw` count as cast failures in `stg.cast_stats`."""
    raw = build_harness.layout.raw
    good = {
        "source_tool": "datadog",
        "event_key": "ev1",
        "ts": "2024-03-01 10:00:00",
        "service": "svc-1",
        "host": "host-1",
        "severity_raw": "CRIT",
        "title": "CPU high",
        "status": "firing",
        "dedup_key": "dd1",
        "end_ts": "2024-03-01 10:05:00",
        "incident_ref": "INC0001",
    }
    bad = {
        "source_tool": "datadog",
        "event_key": "ev2",
        "ts": "2024-03-01 11:00:00",
        "service": "svc-2",
        "host": "host-2",
        "severity_raw": "unmapped-sev",
        "title": "disk full",
        "status": "firing",
        "dedup_key": "dd2",
        "end_ts": "not-a-timestamp",
        "incident_ref": "",
    }
    commit(raw, MON, "event", [Row("a1", at(0), good)])
    commit(raw, MON, "event", [Row("a2", at(0), bad)])

    build(build_harness, enums=SEVERITY_ENUM, lo=0, hi=160)

    rows = build_harness.query(
        "SELECT record_id, source_key, source_tool, event_key, host, status, dedup_key,"
        " incident_ref, service_name, ts, end_ts, severity, alert_name FROM stg.mon_event"
        " ORDER BY source_key"
    )
    assert rows == [
        (
            "monitoring:event:a1",
            "a1",
            "datadog",
            "ev1",
            "host-1",
            "firing",
            "dd1",
            "INC0001",
            "svc-1",
            datetime.datetime(2024, 3, 1, 10, 0, tzinfo=UTC),
            datetime.datetime(2024, 3, 1, 10, 5, tzinfo=UTC),
            "critical",
            "CPU high",
        ),
        (
            "monitoring:event:a2",
            "a2",
            "datadog",
            "ev2",
            "host-2",
            "firing",
            "dd2",
            None,
            "svc-2",
            datetime.datetime(2024, 3, 1, 11, 0, tzinfo=UTC),
            None,
            None,
            "disk full",
        ),
    ]
    stats = cast_stats(build_harness, "mon_event")
    assert stats["ts"] == (2, 0)
    assert stats["end_ts"] == (2, 1)  # a2's end_ts is non-null text that fails to parse
    assert stats["severity"] == (2, 1)  # a2's severity_raw does not map to a canonical value


def test_it02_33_mon_metric_daily_typed_columns_and_cast_stats(
    build_harness: BuildHarness,
) -> None:
    """IT02-33 (reassigned from IT02-16/17, see module docstring): `stg.mon_metric_daily`
    types `date`/`value` with cast flags; a malformed date or value counts as a cast
    failure while the row still lands (with the offending column NULL)."""
    raw = build_harness.layout.raw
    good = {
        "source_tool": "datadog",
        "metric_name": "cpu.usage",
        "unit": "percent",
        "service": "svc-1",
        "date": "2024-03-01",
        "value": "42.5",
    }
    bad_date = {
        "source_tool": "datadog",
        "metric_name": "cpu.usage",
        "unit": "percent",
        "service": "svc-2",
        "date": "not-a-date",
        "value": "10",
    }
    bad_value = {
        "source_tool": "datadog",
        "metric_name": "cpu.usage",
        "unit": "percent",
        "service": "svc-3",
        "date": "2024-03-02",
        "value": "not-a-number",
    }
    commit(raw, MON, "metric_daily", [Row("m1", at(0), good)])
    commit(raw, MON, "metric_daily", [Row("m2", at(0), bad_date)])
    commit(raw, MON, "metric_daily", [Row("m3", at(0), bad_value)])

    build(build_harness, lo=0, hi=160)

    rows = build_harness.query(
        "SELECT source_key, source_tool, metric_name, unit, service_name, date, value"
        " FROM stg.mon_metric_daily ORDER BY source_key"
    )
    assert rows == [
        ("m1", "datadog", "cpu.usage", "percent", "svc-1", datetime.date(2024, 3, 1), 42.5),
        ("m2", "datadog", "cpu.usage", "percent", "svc-2", None, 10.0),
        ("m3", "datadog", "cpu.usage", "percent", "svc-3", datetime.date(2024, 3, 2), None),
    ]
    stats = cast_stats(build_harness, "mon_metric_daily")
    assert stats["date"] == (3, 1)
    assert stats["value"] == (3, 1)


def test_it02_33_monitoring_absent_entities_give_empty_typed_tables(
    build_harness: BuildHarness,
) -> None:
    """IT02-33: no `monitoring/event` or `monitoring/metric_daily` lake files: the build
    succeeds and both staging tables exist, empty, with the same typed columns."""
    build(build_harness, lo=0, hi=160)

    assert build_harness.query("SELECT count(*) FROM stg.mon_event") == [(0,)]
    assert build_harness.query("SELECT count(*) FROM stg.mon_metric_daily") == [(0,)]
    event_cols = dict(columns(build_harness, "mon_event"))
    assert event_cols["severity"] == "VARCHAR"
    assert event_cols["ts"] == "TIMESTAMP WITH TIME ZONE"
    metric_cols = dict(columns(build_harness, "mon_metric_daily"))
    assert metric_cols["date"] == "DATE"
    assert metric_cols["value"] == "DOUBLE"
