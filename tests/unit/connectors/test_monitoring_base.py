"""Tests for the monitoring row builders and day helpers (impl 01 U01-80, U01-81; T01-19)."""

from __future__ import annotations

import datetime
import json
from typing import Any

import pyarrow as pa
import pytest
from tests.unit.connectors._monitoring_data import DAY, FETCHED, TS, event_row, metric_row

from herness.connectors.base import DEFAULT_BATCH_ROWS, METADATA_FIELDS, METADATA_SCHEMA
from herness.connectors.monitoring.base import (
    EVENT_COLUMNS,
    METRIC_COLUMNS,
    TOOLS,
    complete_days,
    event_batch,
    floor_day,
    metric_batch,
)
from herness.core.errors import ConfigError, SchemaViolation

pytestmark = pytest.mark.unit

UTC = datetime.UTC
_NAIVE = datetime.datetime(2026, 3, 1, 9, 30)  # noqa: DTZ001 - deliberately naive


def _row(batch: pa.RecordBatch, i: int = 0) -> dict[str, Any]:
    return {name: batch.column(name)[i].as_py() for name in batch.schema.names}


# --- constants ---------------------------------------------------------------------------


def test_ut01_79_column_tuples() -> None:
    """UT01-79 EVENT_COLUMNS, METRIC_COLUMNS and the tool list are the spec values."""
    assert EVENT_COLUMNS == (
        "source_tool",
        "event_key",
        "ts",
        "service",
        "host",
        "severity_raw",
        "title",
        "status",
        "dedup_key",
        "end_ts",
        "incident_ref",
    )
    assert METRIC_COLUMNS == ("source_tool", "date", "service", "metric_name", "value", "unit")
    assert TOOLS == ("prometheus", "datadog", "splunk", "dynatrace")


# --- event_batch -------------------------------------------------------------------------


def test_ut01_79_event_batch_columns_and_keys() -> None:
    """UT01-79 event rows: metadata columns then EVENT_COLUMNS, all field columns string;
    `_source_key` `<tool>:<id>`, `_source_updated_at` = ts, ts/end_ts ISO with Z."""
    end = datetime.datetime(
        2026, 3, 1, 11, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=1))
    )
    batch = event_batch(
        "datadog",
        [event_row("e1", end_ts=end, incident_ref="INC1"), event_row("e2", service=None)],
        fetched_at=FETCHED,
    )
    assert tuple(batch.schema.names) == (*METADATA_FIELDS, *EVENT_COLUMNS)
    assert (
        batch.schema.field("_source_updated_at").type
        == METADATA_SCHEMA.field("_source_updated_at").type
    )
    assert all(batch.schema.field(c).type == pa.string() for c in EVENT_COLUMNS)
    first = _row(batch)
    assert first["_record_id"] == "monitoring:event:datadog:e1"
    assert first["_source"] == "monitoring"
    assert first["_entity"] == "event"
    assert first["_source_key"] == "datadog:e1"
    assert first["_source_updated_at"] == TS
    assert first["_fetched_at"] == FETCHED
    assert first["_deleted"] is False
    assert first["_payload"] == '{"id":"e1"}'
    assert first["source_tool"] == "datadog"
    assert first["ts"] == "2026-03-01T09:30:00Z"
    assert first["end_ts"] == "2026-03-01T10:00:00Z"
    assert first["incident_ref"] == "INC1"
    assert first["severity_raw"] == "P2"
    second = _row(batch, 1)
    assert second["service"] is None
    assert second["end_ts"] is None


def test_ut01_79_source_tool_comes_from_the_tool_argument() -> None:
    """UT01-79 a row field named `source_tool` cannot override the validated tool."""
    batch = event_batch("splunk", [event_row(source_tool="../../etc")], fetched_at=FETCHED)
    assert _row(batch)["source_tool"] == "splunk"


def test_ut01_79_event_batch_accepts_batch_rows_rows() -> None:
    """UT01-79 exactly `batch_rows` rows form one batch."""
    rows = [event_row(f"e{i}") for i in range(3)]
    assert event_batch("prometheus", rows, fetched_at=FETCHED, batch_rows=3).num_rows == 3
    full = [event_row(f"e{i}") for i in range(DEFAULT_BATCH_ROWS)]
    assert event_batch("prometheus", full, fetched_at=FETCHED).num_rows == DEFAULT_BATCH_ROWS


@pytest.mark.parametrize(
    ("tool", "rows", "extra"),
    [
        ("datadog", [], {}),
        ("datadog", [event_row("a"), event_row("b")], {"batch_rows": 1}),
        ("grafana", [event_row()], {}),
        ("datadog", [event_row("")], {}),
        ("datadog", [event_row(None)], {}),
        ("datadog", [event_row("bad\nkey")], {}),
        ("datadog", [event_row("k" * 600)], {}),
        ("datadog", [event_row(ts=_NAIVE)], {}),
        ("datadog", [event_row(ts="2026-03-01T09:30:00Z")], {}),
        ("datadog", [event_row(end_ts=_NAIVE)], {}),
        ("datadog", [event_row(host=7)], {}),
        ("datadog", [event_row(payload=None)], {}),
        ("datadog", [{"event_key": "x"}], {}),
        ("datadog", [event_row()], {"fetched_at": _NAIVE}),
    ],
)
def test_ut01_79_event_batch_violations(tool: str, rows: list[Any], extra: dict[str, Any]) -> None:
    """UT01-79 violated preconditions raise SchemaViolation("monitoring row") naming the tool
    only when it is a known tool; row values are never echoed."""
    kwargs: dict[str, Any] = {"fetched_at": FETCHED} | extra
    with pytest.raises(SchemaViolation) as info:
        event_batch(tool, rows, **kwargs)
    assert info.value.message == "monitoring row"
    assert info.value.context["source"] == "monitoring"
    assert info.value.context["tool"] == (tool if tool in TOOLS else "unknown")


# --- metric_batch ------------------------------------------------------------------------


def test_ut01_79_metric_batch_columns_and_keys() -> None:
    """UT01-79 metric rows: `<tool>|m|svc|date` key, `_source_updated_at` = day end (date + 1
    day 00:00 UTC), ISO date, value as json.dumps(float)."""
    batch = metric_batch(
        "prometheus",
        [metric_row(value=3), metric_row(datetime.date(2026, 2, 27), 0.1)],
        fetched_at=FETCHED,
    )
    assert tuple(batch.schema.names) == (*METADATA_FIELDS, *METRIC_COLUMNS)
    assert all(batch.schema.field(c).type == pa.string() for c in METRIC_COLUMNS)
    first = _row(batch)
    assert first["_source_key"] == "prometheus|m|checkout|2026-02-28"
    assert first["_record_id"] == "monitoring:metric_daily:prometheus|m|checkout|2026-02-28"
    assert first["_entity"] == "metric_daily"
    assert first["_source_updated_at"] == datetime.datetime(2026, 3, 1, tzinfo=UTC)
    assert first["date"] == "2026-02-28"
    assert first["value"] == "3.0"
    assert first["unit"] == "count"
    assert first["source_tool"] == "prometheus"
    assert first["_payload"] == "{}"
    assert _row(batch, 1)["value"] == json.dumps(0.1)


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), float("-inf")])
def test_ut01_79_metric_value_none_for_missing_or_non_finite(value: float | None) -> None:
    """UT01-79 `value` is None for None, NaN and infinite values."""
    batch = metric_batch("dynatrace", [metric_row(value=value)], fetched_at=FETCHED)
    assert _row(batch)["value"] is None


def test_ut01_79_metric_month_and_year_end() -> None:
    """UT01-79 the day end crosses month and year boundaries."""
    batch = metric_batch("splunk", [metric_row(datetime.date(2025, 12, 31))], fetched_at=FETCHED)
    assert _row(batch)["_source_updated_at"] == datetime.datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("tool", "rows"),
    [
        ("datadog", []),
        ("nagios", [metric_row()]),
        ("datadog", [metric_row(service="")]),
        ("datadog", [metric_row(service=None)]),
        ("datadog", [metric_row(metric_name="")]),
        ("datadog", [metric_row(unit=None)]),
        ("datadog", [metric_row(datetime.datetime(2026, 2, 28, tzinfo=UTC))]),
        ("datadog", [metric_row("2026-02-28")]),
        ("datadog", [metric_row(value=True)]),
        ("datadog", [metric_row(value="1.0")]),
        ("datadog", [metric_row(service="a\x00b")]),
        ("datadog", [metric_row(payload=1)]),
    ],
)
def test_ut01_79_metric_batch_violations(tool: str, rows: list[Any]) -> None:
    """UT01-79 violated metric preconditions raise SchemaViolation("monitoring row")."""
    with pytest.raises(SchemaViolation, match=r"^monitoring row$"):
        metric_batch(tool, rows, fetched_at=FETCHED)


def test_ut01_79_metric_batch_too_many_rows() -> None:
    """UT01-79 more than `batch_rows` metric rows raise SchemaViolation."""
    rows = [metric_row(DAY - datetime.timedelta(days=i)) for i in range(3)]
    with pytest.raises(SchemaViolation, match=r"^monitoring row$"):
        metric_batch("datadog", rows, fetched_at=FETCHED, batch_rows=2)


# --- floor_day / complete_days -----------------------------------------------------------


def test_ut01_79_floor_day_uses_the_utc_date() -> None:
    """UT01-79 floor_day returns 00:00 UTC of the UTC date (an offset can change the day)."""
    plus2 = datetime.timezone(datetime.timedelta(hours=2))
    assert floor_day(datetime.datetime(2026, 3, 1, 1, 0, tzinfo=plus2)) == datetime.datetime(
        2026, 2, 28, tzinfo=UTC
    )
    assert floor_day(datetime.datetime(2026, 3, 1, tzinfo=UTC)) == datetime.datetime(
        2026, 3, 1, tzinfo=UTC
    )


def test_ut01_79_complete_days() -> None:
    """UT01-79 complete_days is (floor_day(since), floor_day(until)); empty when first >=
    second."""
    since = datetime.datetime(2026, 2, 20, 6, tzinfo=UTC)
    until = datetime.datetime(2026, 3, 1, 12, tzinfo=UTC)
    assert complete_days(since, until) == (
        datetime.datetime(2026, 2, 20, tzinfo=UTC),
        datetime.datetime(2026, 3, 1, tzinfo=UTC),
    )
    first, second = complete_days(until - datetime.timedelta(hours=1), until)
    assert first == second


@pytest.mark.parametrize("bad", [_NAIVE, "2026-03-01", None])
def test_ut01_79_day_helpers_reject_naive(bad: Any) -> None:
    """UT01-79 naive (or non-datetime) input raises ConfigError."""
    with pytest.raises(ConfigError):
        floor_day(bad)
    with pytest.raises(ConfigError):
        complete_days(bad, FETCHED)
    with pytest.raises(ConfigError):
        complete_days(FETCHED, bad)
