"""Tests for tool-stream fan-out in `SyncRunner.run_incremental` (U01-38 step 3, U01-79;
T01-06): per-tool keys, error isolation, open-circuit skips and aggregation."""

from __future__ import annotations

import datetime

import pytest
from structlog.testing import capture_logs
from tests.support.fake_lake import FakeLake
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors._runner_data import (
    NOW,
    SINCE,
    UNTIL,
    FakeGuard,
    FakeToolConnector,
    T,
    batch,
    make_runner,
    monitoring_cfg,
)

from herness.core import time as clock
from herness.core.errors import (
    AuthError,
    CircuitOpen,
    NotFound,
    SourceUnavailable,
)
from herness.store.ops import get_watermark, set_watermark

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("guard")]

_ENT = "event"
_PROM, _DD = "monitoring:prometheus", "monitoring:datadog"
_SEC = datetime.timedelta(seconds=1)


def _seed(key: str, value: datetime.datetime = T) -> None:
    set_watermark(key, _ENT, "ts", value, now=NOW)


def _wm(key: str) -> datetime.datetime | None:
    wm = get_watermark(key, _ENT)
    return None if wm is None else wm.value


def _rows(prefix: str, start: datetime.datetime = T + _SEC) -> object:
    return batch("monitoring", _ENT, [f"{prefix}1", f"{prefix}2"], start)


def test_ut01_92_failed_tool_does_not_stop_others(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-92 datadog raising SourceUnavailable: prometheus rows are committed and
    monitoring:prometheus advanced; the datadog watermark is unchanged; the error is
    re-raised after all tools and connectors.sync.failed is logged for datadog."""
    _seed(_PROM)
    _seed(_DD)
    err = SourceUnavailable("datadog down")
    conn = FakeToolConnector({"datadog": [err], "prometheus": [_rows("p")]})
    runner = make_runner(conn, monitoring_cfg(), lake, ops_store.data_root)

    with capture_logs() as logs, pytest.raises(SourceUnavailable) as info:
        runner.run_incremental(_ENT)

    assert info.value is err
    assert [c[0] for c in conn.calls] == ["datadog", "prometheus"]
    assert conn.calls[1][2:] == (SINCE, UNTIL)
    assert _wm(_PROM) == T + 2 * _SEC
    assert _wm(_DD) == T
    assert lake.writers[0].abort_calls == 1
    assert lake.writers[1].state == "committed"
    failed = [e for e in logs if e["event"] == "connectors.sync.failed"]
    assert len(failed) == 1
    assert failed[0]["log_level"] == "error"
    assert {k: failed[0][k] for k in ("source", "entity", "stream", "mode", "error_class")} == {
        "source": "monitoring",
        "entity": _ENT,
        "stream": _DD,
        "mode": "incremental",
        "error_class": "SourceUnavailable",
    }


def test_ut01_92_aggregate_result(
    ops_store: OpsStoreHandle, lake: FakeLake, guard: FakeGuard
) -> None:
    """UT01-92 all tools succeed: sums, concatenated files and the minimum watermarks; the
    per-tool started/completed events name their stream key `monitoring:<tool>`."""
    _seed(_PROM, T)
    _seed(_DD, T - datetime.timedelta(minutes=10))
    conn = FakeToolConnector({"prometheus": [_rows("p")], "datadog": [_rows("d", T + 5 * _SEC)]})

    runner = make_runner(conn, monitoring_cfg(), lake, ops_store.data_root)
    with capture_logs() as logs:
        result = runner.run_incremental(_ENT)

    assert guard.calls == [_PROM, _DD]
    started = [e for e in logs if e["event"] == "connectors.sync.started"]
    assert [(e["source"], e["stream"]) for e in started] == [
        ("monitoring", _PROM),
        ("monitoring", _DD),
    ]
    done = [e for e in logs if e["event"] == "connectors.sync.completed"]
    assert [e["source"] for e in done] == [_PROM, _DD]
    assert result.source == "monitoring"
    assert result.mode == "incremental"
    assert result.rows == 4
    assert len(result.files) == 2
    assert result.watermark_before == clock.format_utc(T - datetime.timedelta(minutes=10))
    assert result.watermark_after == clock.format_utc(T + 2 * _SEC)


def test_ut01_92_open_circuit_tool_skipped(
    ops_store: OpsStoreHandle, lake: FakeLake, guard: FakeGuard
) -> None:
    """UT01-92 a tool whose breaker is open is skipped and recorded in skipped_open with a
    connectors.sync.skipped_open_circuit warning; the other tool's result is returned."""
    _seed(_PROM)
    guard.open_keys.add(_DD)
    conn = FakeToolConnector({"datadog": [_rows("d")], "prometheus": [_rows("p")]})
    runner = make_runner(conn, monitoring_cfg(), lake, ops_store.data_root)

    with capture_logs() as logs:
        result = runner.run_incremental(_ENT)

    assert runner.skipped_open == (_DD,)
    assert [c[0] for c in conn.calls] == ["prometheus"]
    assert result.rows == 2
    skipped = [e for e in logs if e["event"] == "connectors.sync.skipped_open_circuit"]
    assert len(skipped) == 1
    assert skipped[0]["log_level"] == "warning"
    assert (skipped[0]["source"], skipped[0]["stream"]) == ("monitoring", _DD)
    assert skipped[0]["retry_at"] == clock.format_utc(NOW + datetime.timedelta(minutes=5))


def test_ut01_92_all_tools_open_raises_first_circuit_open(
    ops_store: OpsStoreHandle, lake: FakeLake, guard: FakeGuard
) -> None:
    """UT01-92 every tool skipped: the first CircuitOpen is raised."""
    guard.open_keys.update({_PROM, _DD})
    conn = FakeToolConnector({"datadog": [], "prometheus": []})
    runner = make_runner(conn, monitoring_cfg(), lake, ops_store.data_root)

    with pytest.raises(CircuitOpen) as info:
        runner.run_incremental(_ENT)

    assert info.value.key == _DD
    assert runner.skipped_open == (_DD, _PROM)


def test_ut01_92_fatal_error_wins_over_retryable(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-92 a FatalError from a later tool is raised in preference to an earlier
    retryable error."""
    _seed(_PROM)
    _seed(_DD)
    fatal = AuthError("bad key")
    conn = FakeToolConnector({"datadog": [SourceUnavailable("down")], "prometheus": [fatal]})
    with pytest.raises(AuthError) as info:
        make_runner(conn, monitoring_cfg(), lake, ops_store.data_root).run_incremental(_ENT)
    assert info.value is fatal


def test_ut01_92_other_error_is_not_swallowed(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-92 a kept error that is neither fatal nor retryable is still raised."""
    _seed(_PROM)
    _seed(_DD)
    other = NotFound("gone")
    conn = FakeToolConnector({"datadog": [other], "prometheus": [_rows("p")]})
    with pytest.raises(NotFound) as info:
        make_runner(conn, monitoring_cfg(), lake, ops_store.data_root).run_incremental(_ENT)
    assert info.value is other


def test_ut01_92_tool_without_watermark_backfills(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-92 a tool with no watermark takes the backfill path: aggregate mode backfill and
    watermark_before None."""
    _seed(_PROM)
    conn = FakeToolConnector({"prometheus": [_rows("p")], "datadog": [_rows("d")]})
    result = make_runner(conn, monitoring_cfg(), lake, ops_store.data_root).run_incremental(_ENT)

    assert result.mode == "backfill"
    assert result.watermark_before is None
    assert result.watermark_after == clock.format_utc(T + 2 * _SEC)
    assert _wm(_DD) == T + 2 * _SEC
