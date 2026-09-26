"""Tests for herness.connectors.runner: SyncResult, SyncRunner and the incremental flow
(U01-36, U01-37, U01-38, U01-39; T01-06)."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs
from tests.support.fake_lake import FakeLake
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors._runner_data import (
    NOW,
    UNTIL,
    FakeConnector,
    FakeGuard,
    T,
    batch,
    make_runner,
    metrics,
    servicenow_cfg,
)

import herness.connectors.runner as runner_module
from herness.connectors.runner import SyncResult, SyncRunner
from herness.core import time as clock
from herness.core.errors import CircuitOpen, ConfigError
from herness.store.ops import get_watermark, set_watermark

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("guard")]

_SRC, _ENT = "servicenow", "incident"
_SINCE = T - servicenow_cfg().overlap_for(_ENT)  # ServiceNow default overlap: 60 min


def _seed_watermark(value: datetime.datetime, *, source: str = _SRC) -> None:
    set_watermark(source, _ENT, "sys_updated_on", value, now=NOW)


@pytest.fixture
def paths_config(ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stub the runner's `get_config()` so `paths.data` is the ops store's data root (the
    repository config does not load as a whole until T04-08 fills the metric catalog)."""
    stub = SimpleNamespace(paths=SimpleNamespace(data=ops_store.data_root))
    monkeypatch.setattr(runner_module, "get_config", lambda: stub)
    return ops_store.data_root


def _wm(source: str = _SRC) -> datetime.datetime | None:
    wm = get_watermark(source, _ENT)
    return None if wm is None else wm.value


# --- UT01-29: since/until, result and to_dict -----------------------------------------


def test_ut01_29_since_until_and_result(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-29 since = T - overlap, until = now - settle; rows committed; watermark moves to
    the max committed update time; the result carries counts, files and both watermarks."""
    _seed_watermark(T)
    rows = batch(_SRC, _ENT, ["a", "b", "c"], T + datetime.timedelta(minutes=1))
    conn = FakeConnector(steps=[rows])
    runner = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root)

    result = runner.run_incremental(_ENT)

    assert conn.calls == [(_ENT, _SINCE, UNTIL)]
    last = T + datetime.timedelta(minutes=1, seconds=2)
    assert _wm() == last
    assert result == SyncResult(
        source=_SRC,
        entity=_ENT,
        mode="incremental",
        rows=3,
        tombstones=0,
        skipped_deleted=0,
        files=(ops_store.data_root / "raw" / _SRC / _ENT / "part-0000.parquet",),
        watermark_before=clock.format_utc(T),
        watermark_after=clock.format_utc(last),
    )
    assert lake.kinds() == ["open", "write", "commit"]


def test_ut01_29_to_dict_is_json_safe(
    ops_store: OpsStoreHandle, lake: FakeLake, paths_config: Path
) -> None:
    """UT01-29 to_dict is JSON-safe with paths as POSIX strings relative to paths.data."""
    _seed_watermark(T)
    conn = FakeConnector(steps=[batch(_SRC, _ENT, ["a"], T)])
    result = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root).run_incremental(_ENT)

    data = result.to_dict()

    assert json.loads(json.dumps(data)) == data
    assert data["files"] == ["raw/servicenow/incident/part-0000.parquet"]
    assert data["mode"] == "incremental"
    assert data["watermark_before"] == clock.format_utc(T)
    explicit = result.to_dict(data_root=ops_store.data_root / "raw")
    assert explicit["files"] == ["servicenow/incident/part-0000.parquet"]


def test_ut01_29_logs_and_metrics(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-29 connectors.sync.started and .completed are logged with the §8.1 fields and
    the §8.2 metrics are written with the stream key as source label."""
    _seed_watermark(T)
    rows = batch(_SRC, _ENT, ["a", "b"], T, deleted=True)
    conn = FakeConnector(steps=[rows])
    runner = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root)

    with capture_logs() as logs:
        result = runner.run_incremental(_ENT)

    assert result.tombstones == 2
    started = next(e for e in logs if e["event"] == "connectors.sync.started")
    assert started["log_level"] == "info"
    assert {k: started[k] for k in ("source", "entity", "stream", "mode")} == {
        "source": _SRC,
        "entity": _ENT,
        "stream": _SRC,
        "mode": "incremental",
    }
    assert started["since"] == clock.format_utc(_SINCE)
    assert started["until"] == clock.format_utc(UNTIL)
    done = next(e for e in logs if e["event"] == "connectors.sync.completed")
    assert done["rows"] == 2
    assert done["tombstones"] == 2
    assert done["skipped_deleted"] == 0
    assert done["files"] == 1
    assert done["watermark_before"] == clock.format_utc(T)
    assert done["watermark_after"] == result.watermark_after
    assert done["duration_s"] == 0.0
    names = {name: (labels, value) for name, labels, value in metrics()}
    labels = {"source": _SRC, "entity": _ENT, "mode": "incremental"}
    assert names["herness_connectors_records_total"] == (labels, 2.0)
    assert names["herness_connectors_tombstones_total"] == (labels, 2.0)
    assert names["herness_connectors_skipped_deleted_total"][0] == {"source": _SRC, "entity": _ENT}
    assert names["herness_connectors_sync_duration_seconds"] == (labels, 0.0)
    lag = (NOW - T - datetime.timedelta(seconds=1)).total_seconds()
    assert names["herness_connectors_watermark_lag_seconds"][1] == lag


def test_ut01_29_unknown_entity_raises_config_error(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-29 an entity that is not configured raises ConfigError before any source call."""
    conn = FakeConnector()
    runner = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root)
    with pytest.raises(ConfigError):
        runner.run_incremental("problem")
    assert conn.calls == []


# --- UT01-30: empty stream and empty window ---------------------------------------------


def test_ut01_30_nothing_yielded(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-30 a connector yielding nothing (or only an empty batch) leaves the watermark
    unchanged and returns rows 0; the empty writer is still committed."""
    _seed_watermark(T)
    empty = batch(_SRC, _ENT, [], T)
    conn = FakeConnector(steps=[empty])
    result = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root).run_incremental(_ENT)

    assert result.rows == 0
    assert result.files == ()
    assert _wm() == T
    assert result.watermark_after == result.watermark_before == clock.format_utc(T)
    assert lake.kinds() == ["open", "commit"]


def test_ut01_30_empty_window_returns_zero_result(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-30 since >= until returns a zero result without calling the source or opening
    a writer; the watermark is unchanged."""
    ahead = NOW + datetime.timedelta(hours=1)
    _seed_watermark(ahead)
    conn = FakeConnector()
    result = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root).run_incremental(_ENT)

    assert conn.calls == []
    assert lake.events == []
    assert result.rows == 0
    assert result.mode == "incremental"
    assert result.watermark_before == result.watermark_after == clock.format_utc(ahead)


# --- UT01-32: no watermark → backfill path ------------------------------------------------


def test_ut01_32_no_watermark_calls_backfill_path(
    ops_store: OpsStoreHandle, lake: FakeLake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-32 without a watermark the backfill path is called with backfill.start
    (resolved) and now; the result mode is backfill."""
    cfg = servicenow_cfg()
    seen: list[tuple[str, str, datetime.datetime, datetime.datetime]] = []

    def spy(
        self: SyncRunner,
        entity: str,
        *,
        key: str,
        fetch: object,
        start: datetime.datetime,
        end: datetime.datetime,
    ) -> SyncResult:
        seen.append((entity, key, start, end))
        return SyncResult(_SRC, entity, "backfill", 0, 0, 0, (), None, None)

    monkeypatch.setattr(SyncRunner, "_backfill_stream", spy)
    conn = FakeConnector()
    result = make_runner(conn, cfg, lake, ops_store.data_root).run_incremental(_ENT)

    assert seen == [(_ENT, _SRC, cfg.backfill_for(_ENT).resolve_start(NOW), NOW)]
    assert result.mode == "backfill"


def test_ut01_32_interim_backfill_sets_watermark_once(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-32 the interim single-slice backfill fetches [backfill.start, now), commits
    without moving the watermark per checkpoint and sets it once to min(max, now)."""
    cfg = servicenow_cfg()
    start = cfg.backfill_for(_ENT).resolve_start(NOW)
    conn = FakeConnector(steps=[batch(_SRC, _ENT, ["a", "b"], T)])

    result = make_runner(conn, cfg, lake, ops_store.data_root).run_incremental(_ENT)

    assert conn.calls == [(_ENT, start, NOW)]
    assert result.mode == "backfill"
    assert result.rows == 2
    assert result.watermark_before is None
    assert result.watermark_after == clock.format_utc(T + datetime.timedelta(seconds=1))
    assert _wm() == T + datetime.timedelta(seconds=1)


def test_ut01_32_interim_backfill_without_rows_leaves_no_watermark(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-32 an interim backfill that commits no rows sets no watermark."""
    conn = FakeConnector()
    result = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root).run_incremental(_ENT)
    assert result.mode == "backfill"
    assert result.rows == 0
    assert result.watermark_after is None
    assert _wm() is None


# --- UT01-35: guard and constructor -------------------------------------------------------


def test_ut01_35_open_circuit_skips_source_call(
    ops_store: OpsStoreHandle, lake: FakeLake, guard: FakeGuard
) -> None:
    """UT01-35 guard raising CircuitOpen propagates and no connector call happens."""
    _seed_watermark(T)
    guard.open_keys.add(_SRC)
    conn = FakeConnector()
    runner = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root)

    with pytest.raises(CircuitOpen):
        runner.run_incremental(_ENT)

    assert guard.calls == [_SRC]
    assert conn.calls == []
    assert lake.events == []


def test_ut01_35_mismatched_connector_name_raises(ops_store: OpsStoreHandle) -> None:
    """UT01-35 constructing a runner whose connector name differs from cfg.SOURCE raises
    ConfigError."""
    with pytest.raises(ConfigError):
        SyncRunner(FakeConnector(name="jira"), servicenow_cfg())


def test_ut01_35_orphan_cleanup_once_per_instance(
    ops_store: OpsStoreHandle, lake: FakeLake, monkeypatch: pytest.MonkeyPatch, paths_config: Path
) -> None:
    """UT01-35 the orphan temp-file cleanup runs once per runner instance, on the
    configured data root's raw folder, with the runner clock."""
    calls: list[tuple[Path, str, datetime.datetime]] = []

    def fake_cleanup(raw_root: Path, source: str, *, now: datetime.datetime) -> int:
        calls.append((raw_root, source, now))
        return 0

    monkeypatch.setattr(runner_module, "cleanup_orphan_temp_files", fake_cleanup)
    _seed_watermark(NOW)
    runner = SyncRunner(
        FakeConnector(), servicenow_cfg(), clock=lambda: NOW, writer_factory=lake.factory()
    )
    runner.run_incremental(_ENT)
    runner.run_incremental(_ENT)

    assert calls == [(ops_store.data_root / "raw", _SRC, NOW)]
    assert runner.skipped_open == ()
    assert runner.stopped is False
