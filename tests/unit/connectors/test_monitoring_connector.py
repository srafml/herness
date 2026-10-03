"""Tests for `MonitoringConnector` (impl 01 U01-78, U01-79; T01-19): fan-out over fake
adapters, tool and entity validation, and per-tool watermarks written by the real runner."""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest
from tests.support.fake_lake import FakeLake
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors._monitoring_data import FETCHED, FakeAdapter, event_row
from tests.unit.connectors._runner_data import make_runner
from tests.unit.connectors._settings_data import adapter, monitoring

from herness.connectors.base import Connector, SupportsKeyListing, SupportsToolStreams
from herness.connectors.monitoring.base import MonitoringConnector, event_batch
from herness.connectors.settings import MonitoringSettings
from herness.core import registry
from herness.core.errors import AuthError, ConfigError, SchemaViolation, SourceUnavailable
from herness.store.ops import get_watermark, read_all

pytestmark = pytest.mark.unit

UTC = datetime.UTC
NOW = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
SINCE = datetime.datetime(2026, 2, 26, 6, 0, tzinfo=UTC)


def _settings(**extra: Any) -> MonitoringSettings:
    data = monitoring(prometheus=adapter("prometheus"), datadog=adapter("datadog"))
    return MonitoringSettings.model_validate(data | extra)


def _conn(*adapters: Any, settings: MonitoringSettings | None = None) -> MonitoringConnector:
    return MonitoringConnector(settings or _settings(), adapters=adapters, clock=lambda: NOW)


def _two() -> tuple[FakeAdapter, FakeAdapter]:
    prom = FakeAdapter("prometheus", [NOW - datetime.timedelta(hours=2)])
    dd = FakeAdapter("datadog", [NOW - datetime.timedelta(hours=1)])
    return prom, dd


def _conforms(conn: Connector) -> Connector:
    return conn


# --- members -----------------------------------------------------------------------------


def test_ut01_79_members_and_protocols() -> None:
    """UT01-79 name, entities, watermark fields, tools in adapter order, stream keys; the
    connector satisfies Connector and SupportsToolStreams, not SupportsKeyListing."""
    conn = _conn(*_two())
    assert conn.name == "monitoring"
    assert conn.entities == ("event", "metric_daily")
    assert conn.watermark_field("event") == "ts"
    assert conn.watermark_field("metric_daily") == "date"
    assert conn.tools() == ("prometheus", "datadog")
    assert conn.stream_key("datadog") == "monitoring:datadog"
    assert isinstance(conn, SupportsToolStreams)
    assert not isinstance(conn, SupportsKeyListing)
    assert _conforms(conn) is conn


def test_ut01_79_registered_as_monitoring_connector() -> None:
    """UT01-79 the class resolves as connector `monitoring` through the built-in table."""
    assert registry.get("connector", "monitoring") is MonitoringConnector


@pytest.mark.parametrize(
    "adapters",
    [
        (),
        (FakeAdapter("grafana"),),
        (FakeAdapter("prometheus"), FakeAdapter("prometheus")),
        (object(),),
    ],
)
def test_ut01_79_constructor_rejects_bad_adapters(adapters: tuple[Any, ...]) -> None:
    """UT01-79 no adapter, an unknown tool, a duplicate tool or a missing `tool` → ConfigError."""
    with pytest.raises(ConfigError):
        _conn(*adapters)


@pytest.mark.parametrize("tool", ["datadog", "../x", "grafana"])
def test_ut01_79_unknown_tool_is_config_error(tool: str) -> None:
    """UT01-79 stream_key and sync_tool reject an unknown or not enabled tool eagerly; the
    error names the tool only when it is a known tool."""
    conn = _conn(FakeAdapter("prometheus"))
    with pytest.raises(ConfigError) as info:
        conn.stream_key(tool)
    assert info.value.context["tool"] == ("datadog" if tool == "datadog" else "unknown")
    with pytest.raises(ConfigError):
        conn.sync_tool(tool, "event", SINCE, NOW)


def test_ut01_79_unknown_entity_is_config_error() -> None:
    """UT01-79 watermark_field, sync_tool and sync reject an unknown entity eagerly."""
    conn = _conn(FakeAdapter("prometheus"))
    with pytest.raises(ConfigError):
        conn.watermark_field("incident")
    with pytest.raises(ConfigError):
        conn.sync_tool("prometheus", "incident", SINCE, NOW)
    with pytest.raises(ConfigError):
        conn.sync("../incident", SINCE, NOW)


def test_ut01_79_check_calls_every_adapter_first_error_propagates() -> None:
    """UT01-79 check() calls each adapter in order; the first error propagates."""
    prom, dd = _two()
    _conn(prom, dd).check()
    assert (prom.checks, dd.checks) == (["prometheus"], ["datadog"])
    prom.error = AuthError("bad")
    dd.error = SourceUnavailable("down")
    with pytest.raises(AuthError):
        _conn(prom, dd).check()
    assert len(dd.checks) == 1


# --- sync_tool / sync --------------------------------------------------------------------


def test_ut01_79_sync_tool_both_entities() -> None:
    """UT01-79 sync_tool routes `event` to events() and `metric_daily` to daily_metrics();
    source keys `<tool>:<id>` and `<tool>|m|svc|date`; metric `_source_updated_at` = day end."""
    prom, dd = _two()
    conn = _conn(prom, dd)
    (events,) = conn.sync_tool("datadog", "event", SINCE, NOW)
    assert events.column("_source_key").to_pylist() == ["datadog:da0"]
    assert dd.calls == [("events", SINCE, NOW)]
    metrics = list(conn.sync_tool("datadog", "metric_daily", SINCE, NOW))
    keys = [k for b in metrics for k in b.column("_source_key").to_pylist()]
    assert keys == [f"datadog|m|checkout|2026-02-{d}" for d in (26, 27, 28)]
    ends = [v for b in metrics for v in b.column("_source_updated_at").to_pylist()]
    assert ends[-1] == datetime.datetime(2026, 3, 1, tzinfo=UTC)
    assert dd.calls[-1] == ("daily_metrics", SINCE, NOW)
    assert prom.calls == []


def test_ut01_79_sync_tool_since_none_resolves_backfill_start() -> None:
    """UT01-79 since=None → settings.backfill_for(entity).resolve_start(until)."""
    prom = FakeAdapter("prometheus")
    settings = _settings(entities={"event": {"backfill": {"start": datetime.date(2026, 2, 1)}}})
    conn = _conn(prom, settings=settings)
    assert list(conn.sync_tool("prometheus", "event", None, NOW)) == []
    (metrics,) = conn.sync_tool("prometheus", "metric_daily", None, NOW)
    assert metrics.num_rows == 1096
    assert prom.calls[0][1] == datetime.datetime(2026, 2, 1, tzinfo=UTC)
    assert prom.calls[1][1] == NOW.replace(hour=0) - datetime.timedelta(days=1096)


def test_ut01_79_sync_tool_rejects_naive_and_skips_empty_window() -> None:
    """UT01-79 naive bounds raise ConfigError; `since >= until` calls no adapter."""
    prom = FakeAdapter("prometheus")
    conn = _conn(prom)
    with pytest.raises(ConfigError):
        conn.sync_tool("prometheus", "event", NOW.replace(tzinfo=None), NOW)
    with pytest.raises(ConfigError):
        conn.sync_tool("prometheus", "event", SINCE, NOW.replace(tzinfo=None))
    assert list(conn.sync_tool("prometheus", "event", NOW, NOW)) == []
    assert prom.calls == []


def test_ut01_79_sync_chains_tools_and_defaults_until_to_clock() -> None:
    """UT01-79 sync chains sync_tool over every tool in order; until=None → clock()."""
    prom, dd = _two()
    batches = list(_conn(prom, dd).sync("event", SINCE))
    tools = [t for b in batches for t in b.column("source_tool").to_pylist()]
    assert tools == ["prometheus", "datadog"]
    assert prom.calls == [("events", SINCE, NOW)]
    assert dd.calls == [("events", SINCE, NOW)]


class _Rogue(FakeAdapter):
    """Adapter returning a batch built for another tool, or a bare batch."""

    def __init__(self, tool: str, batch: pa.RecordBatch) -> None:
        super().__init__(tool)
        self.batch = batch

    def events(
        self, since: datetime.datetime, until: datetime.datetime
    ) -> Iterator[pa.RecordBatch]:
        yield self.batch


@pytest.mark.parametrize(
    "batch",
    [
        event_batch("datadog", [event_row()], fetched_at=FETCHED),  # another tool's rows
        pa.RecordBatch.from_pydict({"_source_key": ["x"]}),  # no metadata columns
    ],
)
def test_ut01_79_adapter_rows_must_carry_their_tool(batch: pa.RecordBatch) -> None:
    """UT01-79 a batch whose rows do not carry `source_tool = tool` and the `monitoring`
    metadata of the requested entity raises SchemaViolation before the runner sees it."""
    conn = _conn(_Rogue("prometheus", batch))
    with pytest.raises(SchemaViolation, match=r"^monitoring row$"):
        list(conn.sync_tool("prometheus", "event", SINCE, NOW))


def _nulled(column: str, mask: list[bool]) -> pa.RecordBatch:
    good = event_batch("prometheus", [event_row("a"), event_row("b")], fetched_at=FETCHED)
    i = good.schema.get_field_index(column)
    values = [None if m else v for m, v in zip(mask, good.column(i).to_pylist(), strict=True)]
    return good.set_column(i, pa.field(column, pa.string()), pa.array(values, pa.string()))


@pytest.mark.parametrize("column", ["_source", "_entity", "source_tool"])
@pytest.mark.parametrize("mask", [[True, True], [False, True]], ids=["all_null", "part_null"])
def test_ut01_79_null_metadata_rejected(column: str, mask: list[bool]) -> None:
    """UT01-79 an adapter batch whose `_source`, `_entity` or `source_tool` column is all or
    partly null raises SchemaViolation (a null never matches the expected value)."""
    conn = _conn(_Rogue("prometheus", _nulled(column, mask)))
    with pytest.raises(SchemaViolation, match=r"^monitoring row$"):
        list(conn.sync_tool("prometheus", "event", SINCE, NOW))


def test_ut01_79_metric_batch_for_event_entity_rejected() -> None:
    """UT01-79 a metric batch returned for the `event` entity is rejected."""
    prom = FakeAdapter("prometheus")
    metric = next(prom.daily_metrics(SINCE, NOW))
    conn = _conn(_Rogue("prometheus", metric))
    with pytest.raises(SchemaViolation):
        list(conn.sync_tool("prometheus", "event", SINCE, NOW))


# --- the real runner writes per-tool watermarks ------------------------------------------


@pytest.mark.usefixtures("guard")
def test_ut01_79_runner_writes_both_watermarks_per_tool(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-79 the real SyncRunner over MonitoringConnector with two fake adapters writes a
    watermark per (tool, entity) under `monitoring:<tool>`: `event` field `ts` = the latest
    event ts; `metric_daily` field `date` = the end of the last complete UTC day."""
    settings = _settings(backfill={"start": datetime.date(2026, 2, 20)})
    prom, dd = _two()
    conn = MonitoringConnector(settings, adapters=[prom, dd], clock=lambda: NOW)
    runner = make_runner(conn, settings, lake, ops_store.data_root, clock=lambda: NOW)

    event = runner.run_incremental("event")
    metric = runner.run_incremental("metric_daily")

    assert (event.rows, metric.rows) == (2, 2 * 9)
    expected = {
        ("prometheus", "event"): ("ts", NOW - datetime.timedelta(hours=2)),
        ("datadog", "event"): ("ts", NOW - datetime.timedelta(hours=1)),
        ("prometheus", "metric_daily"): ("date", datetime.datetime(2026, 3, 1, tzinfo=UTC)),
        ("datadog", "metric_daily"): ("date", datetime.datetime(2026, 3, 1, tzinfo=UTC)),
    }
    for (tool, entity), (field, value) in expected.items():
        wm = get_watermark(f"monitoring:{tool}", entity)
        assert wm is not None, (tool, entity)
        assert (wm.field, wm.value) == (field, value)
    assert get_watermark("monitoring", "event") is None
    slices = read_all("SELECT DISTINCT source, entity, status FROM sync_slice ORDER BY 1, 2")
    assert [tuple(r) for r in slices] == [
        ("monitoring:datadog", "event", "done"),
        ("monitoring:datadog", "metric_daily", "done"),
        ("monitoring:prometheus", "event", "done"),
        ("monitoring:prometheus", "metric_daily", "done"),
    ]
    written = [w for w in lake.writers if w.batches]
    assert {(w.source, w.entity) for w in written} == {
        ("monitoring", "event"),
        ("monitoring", "metric_daily"),
    }


@pytest.mark.usefixtures("guard")
def test_ut01_79_runner_watermark_for_tool_without_rows(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-79 a tool that returns no events (Prometheus has no alert history) still gets
    its `event` watermark after the first run, so health does not report it missing."""
    settings = _settings(backfill={"start": datetime.date(2026, 2, 20)})
    prom = FakeAdapter("prometheus")
    conn = MonitoringConnector(settings, adapters=[prom], clock=lambda: NOW)
    make_runner(conn, settings, lake, ops_store.data_root).run_incremental("event")
    wm = get_watermark("monitoring:prometheus", "event")
    assert wm is not None
    assert (wm.field, wm.value) == ("ts", NOW)
