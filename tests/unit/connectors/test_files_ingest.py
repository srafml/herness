"""Tests for herness.connectors.files_ingest and the files dispatch of `SyncRunner`
(U01-50; T01-10, TH01-10, TH01-12). Inbox files are real CSV drops under `tmp_path`; lake
writes go to the recording fake lake, which also writes real Parquet for the snapshot
reconcile tests."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from structlog.testing import capture_logs
from tests.support.fake_lake import FakeLake, FakeLakeWriter
from tests.support.fault_env import FaultEnv
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors._files_data import NOW, connector, drop, settings
from tests.unit.connectors._runner_data import metrics

import herness.connectors.files_ingest as ingest_module
from herness.connectors.files import FilesConnector, InboxFile, fingerprint_file
from herness.connectors.files_ingest import _KeyCollector, ingest_files
from herness.connectors.runner import SyncResult, SyncRunner
from herness.core.errors import SchemaViolation, SourceUnavailable
from herness.store.ops import FileIngestRow, get_file_ingest, read_all, record_file_ingest

pytestmark = pytest.mark.unit

_ENT = "teams"
_DELTA = {_ENT: {"pattern": "*.csv", "key_field": ["id"]}}
_SNAPSHOT = {_ENT: {"pattern": "*.csv", "key_field": ["id"], "mode": "snapshot"}}


class ParquetWriter(FakeLakeWriter):
    """Recording writer that also writes its committed rows as a real Parquet file."""

    def commit(self) -> Any:
        fs = super().commit()
        for path in fs.files:
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.Table.from_batches(self.batches), path)
        return fs


class ParquetLake(FakeLake):
    """`FakeLake` whose committed files exist on disk, so reconcile can read them."""

    def __call__(self, source: str, entity: str) -> ParquetWriter:
        with self.lock:
            writer = ParquetWriter(self, len(self.writers), source, entity)
            self.writers.append(writer)
            self.events.append(("open", writer.number))
        return writer


def _csv(keys: list[str], tag: str = "x") -> str:
    return "id,name\n" + "".join(f"{k},{tag}{k}\n" for k in keys)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inbox(ops_store: OpsStoreHandle) -> Path:
    root = ops_store.data_root / "inbox"
    root.mkdir(exist_ok=True)
    return root


def _runner(
    conn: FilesConnector, entities: dict[str, Any], lake: FakeLake, root: Path
) -> SyncRunner:
    return SyncRunner(
        conn,
        settings(entities),
        clock=lambda: NOW,
        writer_factory=lake.factory(),
        data_root=root,
    )


def _setup(
    ops_store: OpsStoreHandle, lake: FakeLake, entities: dict[str, Any] = _DELTA
) -> tuple[Path, FilesConnector, SyncRunner]:
    inbox = _inbox(ops_store)
    conn = connector(inbox, entities)
    return inbox, conn, _runner(conn, entities, lake, ops_store.data_root)


def _ingest_rows() -> list[dict[str, Any]]:
    rows = read_all("SELECT fingerprint, path, rows, files FROM file_ingest ORDER BY path")
    return [dict(r) for r in rows]


def _record_spy(monkeypatch: pytest.MonkeyPatch, lake: FakeLake) -> None:
    """Record each `record_file_ingest` call in the lake's ordered event list."""
    real = record_file_ingest

    def spy(row: FileIngestRow) -> bool:
        lake.events.append(("record", row.path))
        return real(row)

    monkeypatch.setattr(ingest_module, "record_file_ingest", spy)


def _events(logs: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    return [e for e in logs if e["event"] == name]


# --- UT01-51: fingerprint dedupe, modified file, record after commit ---------------------


def test_ut01_51_copy_skipped_modified_ingested_record_after_commit(
    ops_store: OpsStoreHandle, lake: FakeLake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-51 two files, then a renamed copy and a modified file: the copy is skipped, the
    modified file ingested; every `file_ingest` record follows its writer's commit."""
    inbox, _, runner = _setup(ops_store, lake)
    _record_spy(monkeypatch, lake)
    a = drop(inbox, f"{_ENT}/a.csv", _csv(["1", "2"]), age_s=7200)
    b = drop(inbox, f"{_ENT}/b.csv", _csv(["3"]), age_s=7000)

    first = runner.run_incremental(_ENT)

    assert (first.rows, first.mode, first.watermark_before, first.watermark_after) == (
        3,
        "incremental",
        None,
        None,
    )
    assert [r["path"] for r in _ingest_rows()] == [f"{_ENT}/a.csv", f"{_ENT}/b.csv"]
    drop(inbox, f"{_ENT}/c.csv", a.read_bytes(), age_s=6000)  # renamed copy of a
    drop(inbox, f"{_ENT}/b.csv", _csv(["3", "4"], tag="y"), age_s=5000)  # modified b

    with capture_logs() as logs:
        second = runner.run_incremental(_ENT)

    assert second.rows == 2
    skipped = _events(logs, "connectors.files.skipped_known")
    assert sorted(e["fingerprint"] for e in skipped) == sorted([_sha(a)[:12]] * 2)
    assert all(e["log_level"] == "info" and e["entity"] == _ENT for e in skipped)
    rows = _ingest_rows()
    assert len(rows) == 3
    assert _sha(b) in {r["fingerprint"] for r in rows}
    records = [i for i, e in enumerate(lake.events) if e[0] == "record"]
    assert len(records) == 3
    for i in records:  # the event before each record is its writer's non-empty commit
        assert lake.events[i - 1][0] == "commit"
        assert int(str(lake.events[i - 1][2])) > 0


def test_ut01_51_fault_after_commit_reingests_once(
    ops_store: OpsStoreHandle, lake: FakeLake, fault_env: FaultEnv
) -> None:
    """UT01-51 (sub-controller ruling) `connector.before_watermark` fails after the commit
    and before `record_file_ingest`: nothing recorded; the rerun ingests the file once and
    `file_ingest` holds one row per fingerprint."""
    inbox, _, runner = _setup(ops_store, lake)
    path = drop(inbox, f"{_ENT}/a.csv", _csv(["1", "2"]))
    # Call 1 is the write loop's own point after its commit; call 2 is ingest_files' step e.
    rule = {"point": "connector.before_watermark", "action": "error:SourceUnavailable"}
    fault_env([rule | {"source": "files", "nth": 2}])

    with pytest.raises(SourceUnavailable):
        runner.run_incremental(_ENT)

    assert lake.kinds() == ["open", "write", "commit"]
    assert _ingest_rows() == []
    result = runner.run_incremental(_ENT)
    assert result.rows == 2
    assert [k for k in lake.kinds() if k == "commit"] == ["commit", "commit"]
    assert [r["fingerprint"] for r in _ingest_rows()] == [_sha(path)]


def test_ut01_51_ingest_files_direct_empty_inbox(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-51 no entity folder: an empty `incremental` result, no writer opened."""
    _, _, runner = _setup(ops_store, lake)
    assert ingest_files(runner, _ENT) == SyncResult(
        "files", _ENT, "incremental", 0, 0, 0, (), None, None
    )
    assert lake.events == []


def test_ut01_51_unreadable_file_not_recorded(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-51 a file without the key column raises `SchemaViolation`, is logged
    `unreadable`, aborted and not recorded (retried next run)."""
    inbox, _, runner = _setup(ops_store, lake)
    drop(inbox, f"{_ENT}/bad.csv", "other,name\n1,x\n")

    with capture_logs() as logs, pytest.raises(SchemaViolation):
        runner.run_incremental(_ENT)

    [rejected] = _events(logs, "connectors.files.rejected")
    assert (rejected["reason"], rejected["file"]) == ("unreadable", f"{_ENT}/bad.csv")
    assert "abort" in lake.kinds()
    assert "commit" not in lake.kinds()
    assert _ingest_rows() == []


# --- UT01-52: snapshot reconcile ---------------------------------------------------------


def test_ut01_52_snapshot_missing_key_one_tombstone(ops_store: OpsStoreHandle) -> None:
    """UT01-52 snapshot entity: file1 has 100 keys, file2 misses one: the second run writes
    one tombstone with `_source_updated_at` = file2 mtime."""
    lake = ParquetLake(ops_store.data_root / "raw")
    inbox, conn, runner = _setup(ops_store, lake, _SNAPSHOT)
    keys = [f"k{i:03d}" for i in range(100)]
    drop(inbox, f"{_ENT}/day1.csv", _csv(keys), age_s=7200)

    first = runner.run_incremental(_ENT)

    assert (first.rows, first.tombstones) == (100, 0)
    drop(inbox, f"{_ENT}/day2.csv", _csv(keys[:57] + keys[58:]), age_s=3600)
    file2 = next(f for f in conn.candidates(_ENT) if f.rel_path.endswith("day2.csv"))

    second = runner.run_incremental(_ENT)

    assert (second.rows, second.tombstones) == (99 + 1, 1)
    tombstones = [
        row for w in lake.writers for b in w.batches for row in b.to_pylist() if row["_deleted"]
    ]
    assert len(tombstones) == 1
    assert tombstones[0]["_source_key"] == "k057"
    assert tombstones[0]["_source_updated_at"] == file2.mtime
    assert len(second.files) == 2  # the data file and the tombstone file
    assert len(_ingest_rows()) == 2


def test_ut01_52_snapshot_too_large_raises_before_write(
    ops_store: OpsStoreHandle, lake: FakeLake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-52 a snapshot over the key cap raises `SchemaViolation("snapshot too large")`
    while the keys accumulate: the batch is never written and the file not recorded."""
    monkeypatch.setattr(ingest_module, "MAX_SNAPSHOT_KEYS", 3)
    inbox, _, runner = _setup(ops_store, lake, _SNAPSHOT)
    drop(inbox, f"{_ENT}/big.csv", _csv(["1", "2", "3", "4"]))

    with capture_logs() as logs, pytest.raises(SchemaViolation, match="snapshot too large"):
        runner.run_incremental(_ENT)

    assert lake.kinds() == ["open", "abort"]
    assert [e["reason"] for e in _events(logs, "connectors.files.rejected")] == ["too_large"]
    assert _ingest_rows() == []


def test_ut01_52_snapshot_empty_file_reconciles_nothing(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-52 a header-only snapshot file on an empty lake: recorded with 0 rows, no
    tombstones."""
    inbox, _, runner = _setup(ops_store, lake, _SNAPSHOT)
    drop(inbox, f"{_ENT}/empty.csv", "id,name\n")

    result = runner.run_incremental(_ENT)

    assert (result.rows, result.tombstones, result.files) == (0, 0, ())
    assert [(r["rows"], r["files"]) for r in _ingest_rows()] == [(0, "[]")]


def test_ut01_52_key_collector_dedupes_and_batches() -> None:
    """UT01-52 the snapshot key collector skips empty batches, dedupes keys and yields
    `KEY_SCHEMA` batches of the requested size; delta mode keeps nothing."""
    schema = pa.schema([pa.field("_source_key", pa.string())])

    def rows(values: list[str]) -> pa.RecordBatch:
        return pa.RecordBatch.from_arrays([pa.array(values, pa.string())], schema=schema)

    source = [rows([]), rows(["a", "b"]), rows(["b", "c"])]
    snap = _KeyCollector(_ENT, snapshot=True)
    assert [b.num_rows for b in snap.wrap(iter(source))] == [0, 2, 2]
    assert [b.column(0).to_pylist() for b in snap.batches(2)] == [["a", "b"], ["c"]]
    delta = _KeyCollector(_ENT, snapshot=False)
    assert len(list(delta.wrap(iter(source)))) == 3
    assert list(delta.batches(2)) == []


# --- UT01-53: file changes during read ---------------------------------------------------


def _changing_read(
    conn: FilesConnector, inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], None]:
    """Make `read_file` rewrite the file after its first batch; returns an undo."""
    real = conn.read_file

    def read(entity: str, f: InboxFile) -> Iterator[pa.RecordBatch]:
        it = real(entity, f)
        yield next(it)
        drop(inbox, f.rel_path, _csv(["1", "2", "3"], tag="z"), age_s=1800)
        yield from it

    monkeypatch.setattr(conn, "read_file", read)
    return lambda: monkeypatch.setattr(conn, "read_file", real)


def test_ut01_53_change_during_read_aborts_then_next_run_ingests(
    ops_store: OpsStoreHandle, lake: FakeLake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-53 the file changes during `read_file`: writer aborted, no `file_ingest` row,
    `rejected` logged with reason `changed`; the next run ingests the new content."""
    inbox, conn, runner = _setup(ops_store, lake)
    drop(inbox, f"{_ENT}/a.csv", _csv(["1", "2"]))
    undo = _changing_read(conn, inbox, monkeypatch)

    with capture_logs() as logs:
        result = runner.run_incremental(_ENT)

    assert result.rows == 0
    assert lake.kinds() == ["open", "write", "abort"]
    assert _ingest_rows() == []
    [rejected] = _events(logs, "connectors.files.rejected")
    assert (rejected["reason"], rejected["log_level"]) == ("changed", "warning")
    undo()
    again = runner.run_incremental(_ENT)
    assert again.rows == 3
    assert [r["rows"] for r in _ingest_rows()] == [3]


# --- ST01-10: content swapped between fingerprint and read --------------------------------


def test_st01_10_swap_after_fingerprint_skipped(
    ops_store: OpsStoreHandle, lake: FakeLake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST01-10 the content is swapped after the fingerprint and before the read: the file
    is skipped (writer aborted) and no `file_ingest` row exists for either content."""
    inbox, _, runner = _setup(ops_store, lake)
    path = drop(inbox, f"{_ENT}/a.csv", _csv(["1", "2"]))
    real = fingerprint_file
    seen: list[str] = []

    def swap(f: InboxFile) -> str:
        seen.append(real(f))
        drop(inbox, f.rel_path, _csv(["6", "6"], tag="q"), age_s=1800)
        return seen[-1]

    monkeypatch.setattr(ingest_module, "fingerprint_file", swap)

    with capture_logs() as logs:
        result = runner.run_incremental(_ENT)

    assert result.rows == 0
    assert "commit" not in lake.kinds()
    assert [e["reason"] for e in _events(logs, "connectors.files.rejected")] == ["changed"]
    assert get_file_ingest(seen[0]) is None
    assert get_file_ingest(_sha(path)) is None
    assert _ingest_rows() == []


def test_st01_10_change_while_fingerprinted_skipped(
    ops_store: OpsStoreHandle, lake: FakeLake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST01-10 the file changes after listing, before hashing ends: rejected `changed`, no
    writer opened, nothing recorded."""
    inbox, conn, runner = _setup(ops_store, lake)
    drop(inbox, f"{_ENT}/a.csv", _csv(["1"]))
    real = conn.candidates

    def listed(entity: str) -> list[InboxFile]:
        found = real(entity)
        drop(inbox, f"{_ENT}/a.csv", _csv(["1", "2"]), age_s=1800)
        return found

    monkeypatch.setattr(conn, "candidates", listed)

    with capture_logs() as logs:
        result = runner.run_incremental(_ENT)

    assert result.rows == 0
    assert lake.events == []
    [rejected] = _events(logs, "connectors.files.rejected")
    assert (rejected["reason"], rejected["file"]) == ("changed", f"{_ENT}/a.csv")
    assert _ingest_rows() == []


# --- ST01-12: ingest records and INFO logs -----------------------------------------------


def test_st01_12_file_ingest_rows_and_info_logs(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """ST01-12 two files: `file_ingest` rows carry fingerprint, path, size, mtime, rows and
    lake files; INFO `connectors.files.ingested` logs carry counts and a 12-hex fingerprint
    prefix only; the ingested-files counter is written per file."""
    inbox, conn, runner = _setup(ops_store, lake)
    a = drop(inbox, f"{_ENT}/a.csv", _csv(["1", "2"]), age_s=7200)
    b = drop(inbox, f"{_ENT}/b.csv", _csv(["3"]), age_s=3600)
    found = {f.rel_path: f for f in conn.candidates(_ENT)}

    with capture_logs() as logs:
        result = runner.run_incremental(_ENT)

    assert result.rows == 3
    for path, rows, part in ((a, 2, "part-0000"), (b, 1, "part-0001")):
        rel = f"{_ENT}/{path.name}"
        row = get_file_ingest(_sha(path))
        assert row is not None
        assert (row.source, row.entity, row.path, row.rows) == ("files", _ENT, rel, rows)
        assert (row.size_bytes, row.mtime) == (found[rel].size_bytes, found[rel].mtime)
        assert row.files == (f"raw/files/{_ENT}/{part}.parquet",)
        assert row.ingested_at == NOW
    ingested = _events(logs, "connectors.files.ingested")
    assert [(e["rows"], e["files"], e["log_level"]) for e in ingested] == [
        (2, 1, "info"),
        (1, 1, "info"),
    ]
    assert [e["fingerprint"] for e in ingested] == [_sha(a)[:12], _sha(b)[:12]]
    assert all(e["entity"] == _ENT and set(e) <= _LOG_KEYS for e in ingested)
    counted = [m for m in metrics() if m[0] == "herness_connectors_files_ingested_total"]
    assert counted == [("herness_connectors_files_ingested_total", {"entity": _ENT}, 1.0)] * 2


_LOG_KEYS = {"event", "log_level", "component", "entity", "fingerprint", "rows", "files"}
