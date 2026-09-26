"""Tests for herness.connectors.reconcile and SyncRunner.run_reconcile (U01-42, U01-44,
U01-45; T01-08, TH01-13). Lake files are real Parquet under the ops store's data root;
tombstones go to the recording fake lake writer."""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from structlog.testing import capture_logs
from tests.support.fake_lake import FakeLake
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors._runner_data import (
    NOW,
    FakeConnector,
    FakeGuard,
    T,
    batch,
    make_runner,
    metrics,
    monitoring_cfg,
    servicenow_cfg,
)
from tests.unit.connectors._settings_data import files

import herness.connectors.reconcile as reconcile_module
from herness.connectors.base import KEY_SCHEMA
from herness.connectors.deletion import DeletionFilter
from herness.connectors.reconcile import find_missing_keys, reconcile_entity
from herness.connectors.runner import SyncResult, SyncRunner
from herness.connectors.settings import FilesSettings
from herness.core import time as clock
from herness.core.errors import CircuitOpen, ConfigError, SchemaViolation
from herness.store.ops import get_watermark, set_watermark
from herness.store.ops.privacy import create_deletion_request, set_deletion_status

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("guard")]

_SRC, _ENT = "servicenow", "incident"
_EMPTY = pa.array([], pa.string())


@dataclass
class KeyConnector(FakeConnector):
    """`FakeConnector` that also lists keys (`SupportsKeyListing`)."""

    keys: list[pa.RecordBatch | BaseException] = field(default_factory=list)
    listed: list[str] = field(default_factory=list)

    def list_keys(self, entity: str) -> Iterator[pa.RecordBatch]:
        self.listed.append(entity)
        for item in self.keys:
            if isinstance(item, BaseException):
                raise item
            yield item


def _keys(values: list[str]) -> pa.RecordBatch:
    return pa.RecordBatch.from_arrays([pa.array(values, pa.string())], schema=KEY_SCHEMA)


def _lake_dir(root: Path, source: str = _SRC, entity: str = _ENT) -> Path:
    return root / "raw" / source / entity


def _write_lake(root: Path, name: str, rows: pa.RecordBatch, *, day: str = "2026-02-28") -> None:
    directory = _lake_dir(root, str(rows["_source"][0]), str(rows["_entity"][0])) / f"dt={day}"
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_batches([rows]), directory / name)


def _scratch(root: Path) -> Path:
    return root / "tmp" / "reconcile"


def _runner(conn: object, root: Path, lake: FakeLake, **cfg: object) -> SyncRunner:
    return make_runner(conn, servicenow_cfg(**cfg), lake, root)


def _written(lake: FakeLake) -> list[pa.RecordBatch]:
    return [b for w in lake.writers for b in w.batches]


def _seed_abc(root: Path) -> None:
    _write_lake(root, "part-1.parquet", batch(_SRC, _ENT, ["a", "b", "c"], T))


def _pct(value: float) -> object:
    return servicenow_cfg().reconcile.model_copy(update={"max_delete_pct": value})


# --- UT01-40: one tombstone for the missing key -----------------------------------------


def test_ut01_40_one_tombstone_for_missing_key(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-40 lake a,b,c and source a,b: one tombstone for c (detection time), watermark
    unchanged, scratch folder empty, completion logged."""
    root = ops_store.data_root
    _seed_abc(root)
    set_watermark(_SRC, _ENT, "sys_updated_on", T, now=NOW)
    conn = KeyConnector(keys=[_keys(["a"]), _keys(["b"])])
    runner = _runner(conn, root, lake, reconcile=_pct(50.0))

    with capture_logs() as logs:
        result = runner.run_reconcile(_ENT)

    assert conn.listed == [_ENT]
    rows = _written(lake)
    assert len(rows) == 1
    assert rows[0]["_record_id"].to_pylist() == ["servicenow:incident:c"]
    assert rows[0]["_deleted"].to_pylist() == [True]
    assert rows[0]["_source_updated_at"].to_pylist() == [NOW]
    assert lake.kinds() == ["open", "write", "commit"]
    text = clock.format_utc(T)
    files = (root / "raw" / _SRC / _ENT / "part-0000.parquet",)
    assert result == SyncResult(_SRC, _ENT, "reconcile", 1, 1, 0, files, text, text)
    wm = get_watermark(_SRC, _ENT)
    assert wm is not None
    assert wm.value == T
    assert list(_scratch(root).iterdir()) == []
    done = next(e for e in logs if e["event"] == "connectors.reconcile.completed")
    assert done["log_level"] == "info"
    fields = ("source", "entity", "live_keys", "source_keys", "tombstones")
    assert {k: done[k] for k in fields} == {
        "source": _SRC,
        "entity": _ENT,
        "live_keys": 3,
        "source_keys": 2,
        "tombstones": 1,
    }


def test_ut01_40_find_missing_keys_latest_row_rule(ops_store: OpsStoreHandle) -> None:
    """UT01-40 find_missing_keys uses the latest row per record over hive partitions, skips
    dot-prefixed temp files and deleted ids, and returns sorted missing keys."""
    root = ops_store.data_root
    _write_lake(root, "part-1.parquet", batch(_SRC, _ENT, ["d", "a", "b", "c"], T))
    later = T + datetime.timedelta(hours=1)
    _write_lake(
        root, "part-2.parquet", batch(_SRC, _ENT, ["b"], later, deleted=True), day="2026-03-01"
    )
    _write_lake(root, ".part-3.parquet.tmp-x", batch(_SRC, _ENT, ["z"], later))
    scratch = _scratch(root)
    scratch.mkdir(parents=True)
    keys_file = scratch / "keys.parquet"
    pq.write_table(pa.Table.from_batches([_keys(["a"])]), keys_file)
    deleted = pa.array(["servicenow:incident:c"], pa.string())

    missing, live = find_missing_keys(_lake_dir(root), keys_file, deleted=deleted, temp_dir=scratch)

    assert live == 2
    assert missing.type == pa.string()
    assert missing.to_pylist() == ["d"]
    keys_file.unlink()


def test_ut01_40_keys_argument_deleted_at_and_chunks(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-40 explicit `keys` and `deleted_at`: 1,001 missing keys go out in `batch_rows`
    chunks into one writer, stamped with `deleted_at`; `list_keys` is not called."""
    root = ops_store.data_root
    names = [f"k{i:04d}" for i in range(1001)]
    _write_lake(root, "part-1.parquet", batch(_SRC, _ENT, names, T))
    conn = KeyConnector()
    runner = _runner(conn, root, lake, batch_rows=1000, reconcile=_pct(100.0))
    when = T - datetime.timedelta(days=1)

    result = reconcile_entity(runner, _ENT, keys=iter([]), deleted_at=when)

    assert conn.listed == []
    assert [e for e in lake.events if e[0] == "write"] == [("write", 0, 1000), ("write", 0, 1)]
    assert result.tombstones == result.rows == 1001
    assert {v for b in _written(lake) for v in b["_source_updated_at"].to_pylist()} == {when}
    assert list(_scratch(root).iterdir()) == []


def test_ut01_40_files_skips_guard(
    ops_store: OpsStoreHandle, lake: FakeLake, guard: FakeGuard
) -> None:
    """UT01-40 the files source reconciles without the breaker guard; others call it."""
    root = ops_store.data_root
    _write_lake(root, "part-1.parquet", batch("files", "roster", ["a", "b"], T))
    conn = KeyConnector(name="files", entities=("roster",))
    runner = make_runner(conn, FilesSettings.model_validate(files()), lake, root)

    result = reconcile_entity(runner, "roster", keys=iter([_keys(["a", "b"])]))

    assert guard.calls == []
    assert result.rows == 0
    assert (result.watermark_before, result.watermark_after) == (None, None)
    _seed_abc(root)
    _runner(KeyConnector(keys=[_keys(["a", "b", "c"])]), root, lake).run_reconcile(_ENT)
    assert guard.calls == [_SRC]


def test_ut01_40_open_circuit_raises(
    ops_store: OpsStoreHandle, lake: FakeLake, guard: FakeGuard
) -> None:
    """UT01-40 an open breaker raises `CircuitOpen` before any key is listed."""
    guard.open_keys.add(_SRC)
    conn = KeyConnector(keys=[_keys(["a"])])
    with pytest.raises(CircuitOpen):
        _runner(conn, ops_store.data_root, lake).run_reconcile(_ENT)
    assert conn.listed == []
    assert lake.events == []


@pytest.mark.parametrize(
    "bad",
    [
        pa.RecordBatch.from_arrays([pa.array(["a", None], pa.string())], names=["_source_key"]),
        pa.RecordBatch.from_arrays([pa.array([1, 2], pa.int64())], names=["_source_key"]),
        pa.RecordBatch.from_arrays([pa.array(["a"], pa.string())], names=["key"]),
    ],
    ids=["null", "int", "name"],
)
def test_ut01_40_bad_key_batch(
    ops_store: OpsStoreHandle, lake: FakeLake, bad: pa.RecordBatch
) -> None:
    """UT01-40 a key batch with a null key, another type or another column raises
    `SchemaViolation`; nothing is written and the scratch folder is empty."""
    root = ops_store.data_root
    _seed_abc(root)
    runner = _runner(KeyConnector(keys=[_keys(["a"]), bad]), root, lake)
    with pytest.raises(SchemaViolation, match="invalid key batch"):
        runner.run_reconcile(_ENT)
    assert lake.events == []
    assert list(_scratch(root).iterdir()) == []


def test_ut01_40_large_string_keys_accepted(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-40 a `large_string` key column is cast to `KEY_SCHEMA`."""
    root = ops_store.data_root
    _seed_abc(root)
    wide = pa.RecordBatch.from_arrays(
        [pa.array(["a", "b"], pa.large_string())], names=["_source_key"]
    )
    result = _runner(KeyConnector(keys=[wide]), root, lake, reconcile=_pct(50.0)).run_reconcile(
        _ENT
    )
    assert result.tombstones == 1


def test_ut01_40_listing_error_removes_scratch(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-40 a key listing that raises propagates; nothing written; scratch removed."""
    root = ops_store.data_root
    _seed_abc(root)
    failure = SchemaViolation("listing failed", source=_SRC)
    runner = _runner(KeyConnector(keys=[_keys(["a"]), failure]), root, lake)
    with pytest.raises(SchemaViolation, match="listing failed"):
        runner.run_reconcile(_ENT)
    assert lake.events == []
    assert list(_scratch(root).iterdir()) == []


def test_ut01_40_query_failure(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-40 a DuckDB error (corrupt lake file) becomes `SchemaViolation("reconcile
    query failed")` chained from the `duckdb.Error`; scratch removed."""
    root = ops_store.data_root
    directory = _lake_dir(root) / "dt=2026-02-28"
    directory.mkdir(parents=True)
    (directory / "part-1.parquet").write_bytes(b"not parquet")
    runner = _runner(KeyConnector(keys=[_keys(["a"])]), root, lake)
    with pytest.raises(SchemaViolation, match="reconcile query failed") as info:
        runner.run_reconcile(_ENT)
    assert isinstance(info.value.__cause__, duckdb.Error)
    assert dict(info.value.context) == {"entity": _ENT}
    assert list(_scratch(root).iterdir()) == []


@pytest.mark.parametrize("abort_fails", [False, True], ids=["abort", "abort_fails"])
def test_ut01_40_commit_failure_aborts(
    ops_store: OpsStoreHandle, lake: FakeLake, abort_fails: bool
) -> None:
    """UT01-40 a failing commit aborts the writer (a failing abort is logged, never raised
    over the original error) and propagates."""
    root = ops_store.data_root
    _seed_abc(root)
    lake.fail_on_commit = OSError("disk full")
    if abort_fails:
        lake.abort_error = RuntimeError("abort broke")
    runner = _runner(KeyConnector(keys=[_keys(["a", "b"])]), root, lake, reconcile=_pct(50.0))
    with capture_logs() as logs, pytest.raises(OSError, match="disk full"):
        runner.run_reconcile(_ENT)
    assert lake.kinds() == ["open", "write", "abort"]
    failed = [e for e in logs if e["event"] == "connectors.lake.abort_failed"]
    assert len(failed) == int(abort_fails)
    assert list(_scratch(root).iterdir()) == []


# --- UT01-41 / ST01-13: the safety valve ------------------------------------------------


def _assert_valve(root: Path, lake: FakeLake, runner: SyncRunner, live: int, missing: int) -> None:
    with capture_logs() as logs, pytest.raises(SchemaViolation) as info:
        runner.run_reconcile(_ENT)
    assert info.value.message == "reconcile safety valve"
    assert dict(info.value.context) == {"source": _SRC, "entity": _ENT}
    assert lake.events == []
    aborted = [e for e in logs if e["event"] == "connectors.reconcile.aborted"]
    assert len(aborted) == 1
    assert aborted[0]["log_level"] == "error"
    fields = ("source", "entity", "live_keys", "missing_keys", "max_delete_pct")
    assert {k: aborted[0][k] for k in fields} == {
        "source": _SRC,
        "entity": _ENT,
        "live_keys": live,
        "missing_keys": missing,
        "max_delete_pct": 2.0,
    }
    samples = [m for m in metrics() if m[0] == "herness_connectors_reconcile_aborted_total"]
    assert samples == [
        ("herness_connectors_reconcile_aborted_total", {"source": _SRC, "entity": _ENT}, 1.0)
    ]
    assert list(_scratch(root).iterdir()) == []


def test_ut01_41_valve_trips(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-41 100 live keys, 90 source keys, valve 2 %: nothing written, SchemaViolation,
    `connectors.reconcile.aborted` and the aborted counter."""
    root = ops_store.data_root
    names = [f"k{i:03d}" for i in range(100)]
    _write_lake(root, "part-1.parquet", batch(_SRC, _ENT, names, T))
    runner = _runner(KeyConnector(keys=[_keys(names[:90])]), root, lake)
    _assert_valve(root, lake, runner, 100, 10)


def test_ut01_41_valve_boundary_passes(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-41 exactly 2 % missing (2 of 100) does not trip the valve."""
    root = ops_store.data_root
    names = [f"k{i:03d}" for i in range(100)]
    _write_lake(root, "part-1.parquet", batch(_SRC, _ENT, names, T))
    result = _runner(KeyConnector(keys=[_keys(names[2:])]), root, lake).run_reconcile(_ENT)
    assert result.tombstones == 2


def test_st01_13_empty_listing_trips_valve(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """ST01-13 a key listing with zero keys trips the valve; nothing is written."""
    root = ops_store.data_root
    _seed_abc(root)
    _assert_valve(root, lake, _runner(KeyConnector(keys=[]), root, lake), 3, 3)


# --- UT01-42: records under a deletion request ------------------------------------------


def _running_deletion(record_id: str) -> None:
    request = create_deletion_request(
        record_id=record_id, requested_by="a" * 32, reason_ref="ticket-1", now=NOW
    )
    set_deletion_status(request.request_id, "running")


def test_ut01_42_no_tombstone_under_deletion(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-42 c under a `running` deletion request gets no tombstone (and does not count
    as live for the valve)."""
    root = ops_store.data_root
    _seed_abc(root)
    _running_deletion("servicenow:incident:c")
    deletion = DeletionFilter(_SRC, _ENT)
    assert deletion.ids.to_pylist() == []  # before reload: empty, never None
    deletion.reload()
    assert deletion.ids.to_pylist() == ["servicenow:incident:c"]
    result = _runner(KeyConnector(keys=[_keys(["a", "b"])]), root, lake).run_reconcile(_ENT)
    assert result.tombstones == 0
    assert _written(lake) == []


def test_ut01_42_deletion_filter_on_tombstones(
    ops_store: OpsStoreHandle, lake: FakeLake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-42 tombstones pass through the deletion filter: a deleted id the query still
    reported is dropped and counted as skipped."""
    root = ops_store.data_root
    _running_deletion("servicenow:incident:c")
    found = (pa.array(["c"], pa.string()), 100)
    monkeypatch.setattr(reconcile_module, "find_missing_keys", lambda *_a, **_k: found)
    result = _runner(KeyConnector(keys=[_keys(["a"])]), root, lake).run_reconcile(_ENT)
    assert (result.rows, result.tombstones, result.skipped_deleted) == (0, 0, 1)
    assert _written(lake) == []


# --- UT01-43: run_reconcile preconditions -----------------------------------------------


def test_ut01_43_monitoring_not_reconciled(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-43 the monitoring connector (even with a `list_keys`) raises ConfigError."""
    conn = KeyConnector(name="monitoring", entities=("event",))
    runner = make_runner(conn, monitoring_cfg(), lake, ops_store.data_root)
    with pytest.raises(ConfigError, match="monitoring is not reconciled"):
        runner.run_reconcile("event")
    assert conn.listed == []


def test_ut01_43_connector_without_list_keys(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-43 a connector without `list_keys` raises ConfigError; so does an unknown
    entity of a key-listing connector."""
    runner = _runner(FakeConnector(), ops_store.data_root, lake)
    with pytest.raises(ConfigError, match="servicenow is not reconciled"):
        runner.run_reconcile(_ENT)
    runner = _runner(KeyConnector(), ops_store.data_root, lake)
    with pytest.raises(ConfigError):
        runner.run_reconcile("nope")
    assert lake.events == []


# --- UT01-44: nothing to tombstone ------------------------------------------------------


def test_ut01_44_already_tombstoned(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-44 c already tombstoned in the lake: 0 tombstones."""
    root = ops_store.data_root
    _seed_abc(root)
    later = T + datetime.timedelta(minutes=5)
    _write_lake(root, "part-2.parquet", batch(_SRC, _ENT, ["c"], later, deleted=True))
    result = _runner(KeyConnector(keys=[_keys(["a", "b"])]), root, lake).run_reconcile(_ENT)
    assert (result.rows, result.tombstones) == (0, 0)
    assert lake.events == []
    assert list(_scratch(root).iterdir()) == []


def test_ut01_44_empty_lake(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-44 an empty lake (no committed file) gives 0 tombstones, even with an empty
    key listing (live 0 never trips the valve)."""
    root = ops_store.data_root
    scratch = _scratch(root)
    scratch.mkdir(parents=True)
    missing, live = find_missing_keys(
        _lake_dir(root), scratch / "none.parquet", deleted=_EMPTY, temp_dir=scratch
    )
    assert (missing.to_pylist(), missing.type, live) == ([], pa.string(), 0)
    result = _runner(KeyConnector(keys=[]), root, lake).run_reconcile(_ENT)
    assert (result.rows, result.tombstones, result.files) == (0, 0, ())
    assert lake.events == []
    assert list(scratch.iterdir()) == []
