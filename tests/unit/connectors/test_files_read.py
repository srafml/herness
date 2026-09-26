"""Files connector DuckDB reader tests (T01-09, U01-49)."""

from __future__ import annotations

import datetime
import decimal
import json
import os
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.unit.connectors import _files_data as d

from herness.connectors import files
from herness.connectors.base import METADATA_FIELDS
from herness.connectors.files import FilesConnector, InboxFile, InboxFileChanged
from herness.core.errors import ConfigError, SchemaViolation

pytestmark = pytest.mark.unit

_UTC = datetime.UTC
_EXCEL = d.excel_available()


def _read(conn: FilesConnector, entity: str) -> list[pa.RecordBatch]:
    (f,) = conn.candidates(entity)
    return list(conn.read_file(entity, f))


def _one(conn: FilesConnector, entity: str) -> pa.Table:
    return pa.Table.from_batches(_read(conn, entity))


def test_ut01_47_composite_key_joined_with_pipe(tmp_path: Path) -> None:
    """UT01-47 a composite key becomes ``a|b``; metadata columns come first."""
    d.drop(tmp_path, "teams/a.csv", "Region,Team Id,Name\neu,7,Ops\nus,8,Dev\n")
    entities = {"teams": {"pattern": "*.csv", "key_field": ["Region", "Team Id"]}}
    table = _one(d.connector(tmp_path, entities), "teams")
    assert table.column_names == [*METADATA_FIELDS, "region", "team_id", "name"]
    assert table["_source_key"].to_pylist() == ["eu|7", "us|8"]
    assert table["_record_id"].to_pylist() == ["files:teams:eu|7", "files:teams:us|8"]
    assert table["_source"].to_pylist() == ["files", "files"]
    assert table["_entity"].to_pylist() == ["teams", "teams"]
    assert table["_deleted"].to_pylist() == [False, False]
    assert table["_fetched_at"].to_pylist() == [d.NOW, d.NOW]
    payload = json.loads(table["_payload"][0].as_py())
    assert payload == {"Region": "eu", "Team Id": "7", "Name": "Ops"}


def test_ut01_47_null_key_reports_row(tmp_path: Path) -> None:
    """UT01-47 a null key part raises SchemaViolation naming the file and data row."""
    rows = "".join(f"eu,{i}\n" for i in range(1, 1500)) + "eu,\n"
    d.drop(tmp_path, "teams/a.csv", "region,id\n" + rows)
    entities = {"teams": {"pattern": "*.csv", "key_field": ["region", "id"]}}
    conn = d.connector(tmp_path, entities, batch_rows=1000)
    with pytest.raises(SchemaViolation, match=r"null key in teams/a\.csv at row 1500$"):
        _read(conn, "teams")


def test_ut01_47_missing_key_and_collision(tmp_path: Path) -> None:
    """UT01-47 a missing key column or two columns with one snake name fail."""
    d.drop(tmp_path, "teams/a.csv", "name\nx\n")
    d.drop(tmp_path, "sites/a.csv", "id,Site Id,site_id\n1,2,3\n")
    entities = {
        "teams": {"pattern": "*.csv", "key_field": ["id"]},
        "sites": {"pattern": "*.csv", "key_field": ["id"]},
    }
    conn = d.connector(tmp_path, entities)
    with pytest.raises(SchemaViolation, match="key field missing"):
        _read(conn, "teams")
    with pytest.raises(SchemaViolation, match="column collision"):
        _read(conn, "sites")


def test_ut01_48_updated_field_parsed(tmp_path: Path) -> None:
    """UT01-48 ``last_modified`` values become ``_source_updated_at``."""
    csv = "id,Last Modified\n1,2026-08-01 10:00:00\n2,2026-08-02T11:30:00Z\n"
    d.drop(tmp_path, "teams/a.csv", csv)
    entities = {
        "teams": {"pattern": "*.csv", "key_field": ["id"], "updated_field": "Last Modified"}
    }
    table = _one(d.connector(tmp_path, entities), "teams")
    assert table["_source_updated_at"].to_pylist() == [
        datetime.datetime(2026, 8, 1, 10, tzinfo=_UTC),
        datetime.datetime(2026, 8, 2, 11, 30, tzinfo=_UTC),
    ]


def test_ut01_48_mtime_without_updated_field(tmp_path: Path) -> None:
    """UT01-48 without an updated_field every row carries the file mtime."""
    path = d.drop(tmp_path, "teams/a.csv", "id\n1\n2\n")
    table = _one(d.connector(tmp_path), "teams")
    mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime_ns // 10**9, _UTC)
    assert table["_source_updated_at"].to_pylist() == [mtime, mtime]


def test_ut01_48_updated_field_missing_or_bad(tmp_path: Path) -> None:
    """UT01-48 an absent or unparseable updated column raises SchemaViolation."""
    d.drop(tmp_path, "teams/a.csv", "id\n1\n")
    d.drop(tmp_path, "sites/a.csv", "id,ts\n1,yesterday\n")
    entities = {
        "teams": {"pattern": "*.csv", "key_field": ["id"], "updated_field": "ts"},
        "sites": {"pattern": "*.csv", "key_field": ["id"], "updated_field": "ts"},
    }
    conn = d.connector(tmp_path, entities)
    with pytest.raises(SchemaViolation, match="updated field missing"):
        _read(conn, "teams")
    with pytest.raises(SchemaViolation, match="unparseable timestamp in ts"):
        _read(conn, "sites")


@pytest.mark.skipif(not _EXCEL, reason=d.EXCEL_SKIP)
def test_ut01_49_xlsx_sheet_read_as_strings(tmp_path: Path) -> None:
    """UT01-49 the configured XLSX sheet is read with every value as a string."""
    (tmp_path / "sheets").mkdir()
    path = d.write_xlsx(
        tmp_path / "sheets" / "t.xlsx",
        {"Other": [["id"], ["99"]], "Teams": [["Id", "Size"], ["1", "12"], ["2", "3"]]},
    )
    os.utime(path, ns=(d.mtime_ns(3600), d.mtime_ns(3600)))
    entities = {"sheets": {"pattern": "*.xlsx", "key_field": ["Id"], "sheet": "Teams"}}
    table = _one(d.connector(tmp_path, entities), "sheets")
    assert table["_source_key"].to_pylist() == ["1", "2"]
    assert table["size"].to_pylist() == ["12", "3"]
    assert pa.types.is_string(table.schema.field("size").type)


def test_ut01_49_parquet_keeps_arrow_types(tmp_path: Path) -> None:
    """UT01-49 Parquet int and decimal columns keep their Arrow types."""
    (tmp_path / "sites").mkdir()
    path = tmp_path / "sites" / "s.parquet"
    amounts = [decimal.Decimal("1.25"), decimal.Decimal("20.50")]
    data = pa.table(
        {"SiteId": pa.array([10, 11], pa.int64()), "Amount": pa.array(amounts, pa.decimal128(9, 2))}
    )
    pq.write_table(data, path)
    os.utime(path, ns=(d.mtime_ns(3600), d.mtime_ns(3600)))
    entities = {"sites": {"pattern": "*.parquet", "key_field": ["SiteId"]}}
    table = _one(d.connector(tmp_path, entities), "sites")
    assert table.schema.field("site_id").type == pa.int64()
    assert table.schema.field("amount").type == pa.decimal128(9, 2)
    assert table["_source_key"].to_pylist() == ["10", "11"]
    assert json.loads(table["_payload"][0].as_py()) == {"SiteId": 10, "Amount": "1.25"}


def test_ut01_49_query_builder(tmp_path: Path) -> None:
    """UT01-49 suffixes select the reader; the path and sheet are bound parameters."""
    p = tmp_path / "x'y"
    csv = files._query(p.with_suffix(".CSV"), None)
    assert csv == (
        False,
        "SELECT * FROM read_csv($path, all_varchar = true, header = true)",
        {"path": str(p.with_suffix(".CSV"))},
    )
    assert files._query(p.with_suffix(".parquet"), None)[1] == "SELECT * FROM read_parquet($path)"
    load, sql, params = files._query(p.with_suffix(".xlsx"), "Teams")
    assert load is True
    assert sql == "SELECT * FROM read_xlsx($path, all_varchar = true, sheet = $sheet)"
    assert params == {"path": str(p.with_suffix(".xlsx")), "sheet": "Teams"}
    assert files._query(p.with_suffix(".xlsx"), None)[1] == (
        "SELECT * FROM read_xlsx($path, all_varchar = true)"
    )
    with pytest.raises(SchemaViolation, match="unsupported inbox file type"):
        files._query(p.with_suffix(".txt"), None)


def test_ut01_49_duckdb_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-49 each read opens its own capped DuckDB with extension autoloading off."""
    d.drop(tmp_path, "teams/a.csv", "id\n1\n")
    seen: list[dict[str, Any]] = []
    real = duckdb.connect

    def spy(database: str, *, config: dict[str, Any]) -> duckdb.DuckDBPyConnection:
        seen.append({"database": database, **config})
        return real(database, config=config)

    monkeypatch.setattr(files.duckdb, "connect", spy)
    _read(d.connector(tmp_path), "teams")
    assert seen == [
        {
            "database": ":memory:",
            "memory_limit": "1GB",
            "threads": 2,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
        }
    ]


def test_ut01_50_corrupt_xlsx_is_schema_violation(tmp_path: Path) -> None:
    """UT01-50 a corrupt XLSX (or a missing excel extension) raises SchemaViolation."""
    d.drop(tmp_path, "sheets/bad.xlsx", b"PK\x03\x04 not a workbook")
    entities = {"sheets": {"pattern": "*.xlsx", "key_field": ["id"]}}
    with pytest.raises(SchemaViolation, match=r"unreadable inbox file sheets/bad\.xlsx") as info:
        _read(d.connector(tmp_path, entities), "sheets")
    assert isinstance(info.value.__cause__, duckdb.Error)


def test_ut01_50_corrupt_parquet_and_bad_suffix(tmp_path: Path) -> None:
    """UT01-50 a corrupt Parquet file and an unsupported suffix raise SchemaViolation."""
    d.drop(tmp_path, "sites/bad.parquet", b"PAR1 broken")
    entities = {"sites": {"pattern": "*.parquet", "key_field": ["id"]}}
    conn = d.connector(tmp_path, entities)
    with pytest.raises(SchemaViolation, match="unreadable inbox file"):
        _read(conn, "sites")
    path = d.drop(tmp_path, "sites/x.txt", "id\n1\n")
    st = path.stat()
    odd = InboxFile(path, "sites/x.txt", st.st_size, d.NOW, st.st_mtime_ns)
    with pytest.raises(SchemaViolation, match="unsupported inbox file type"):
        list(conn.read_file("sites", odd))


def test_st01_09_xlsx_zip_bomb_fails_as_schema_violation(tmp_path: Path) -> None:
    """ST01-09 a small XLSX that inflates past the memory cap fails as SchemaViolation."""
    (tmp_path / "sheets").mkdir()
    # With the extension the cell inflates to 1.2 GiB (> the 1 GB DuckDB cap); without it
    # the read fails at LOAD excel, so a small bomb keeps the test fast.
    size = int(1.2 * 1024**3) if _EXCEL else 8 * 1_048_576
    path = d.write_xlsx_bomb(tmp_path / "sheets" / "bomb.xlsx", size)
    assert path.stat().st_size < 4 * 1_048_576
    os.utime(path, ns=(d.mtime_ns(3600), d.mtime_ns(3600)))
    entities = {"sheets": {"pattern": "*.xlsx", "key_field": ["id"], "sheet": "Teams"}}
    with pytest.raises(SchemaViolation, match="unreadable inbox file"):
        _read(d.connector(tmp_path, entities), "sheets")


def test_ut01_49_file_changed_during_read(tmp_path: Path) -> None:
    """UT01-49 a file rewritten while its batches are read raises InboxFileChanged."""
    path = d.drop(tmp_path, "teams/a.csv", "id\n" + "".join(f"{i}\n" for i in range(3000)))
    conn = d.connector(tmp_path)
    (f,) = conn.candidates("teams")
    it = conn.read_file("teams", f)
    next(it)
    path.write_text("id\n1\n")
    with pytest.raises(InboxFileChanged):
        list(it)


def test_ut01_49_file_removed_during_read(tmp_path: Path) -> None:
    """UT01-49 a file deleted after its rows were read raises InboxFileChanged."""
    path = d.drop(tmp_path, "teams/a.csv", "id\n1\n")
    conn = d.connector(tmp_path)
    (f,) = conn.candidates("teams")
    it = conn.read_file("teams", f)
    next(it)
    path.unlink()
    with pytest.raises(InboxFileChanged):
        next(it)


def test_ut01_45_sync_filters_by_mtime_window(tmp_path: Path) -> None:
    """UT01-45 sync reads files with ``since <= mtime < until`` in mtime order."""
    d.drop(tmp_path, "teams/old.csv", "id\n1\n", age_s=9000)
    d.drop(tmp_path, "teams/mid.csv", "id\n2\n", age_s=5000)
    d.drop(tmp_path, "teams/new.csv", "id\n3\n", age_s=1000)
    conn = d.connector(tmp_path)

    def keys(since: datetime.datetime | None, until: datetime.datetime | None) -> list[str]:
        return [k for b in conn.sync("teams", since, until) for k in b["_source_key"].to_pylist()]

    at = datetime.datetime.fromtimestamp(d.mtime_ns(5000) // 10**9, _UTC)
    assert keys(None, None) == ["1", "2", "3"]
    assert keys(at, None) == ["2", "3"]
    assert keys(None, at) == ["1"]


def test_ut01_45_list_keys_rules(tmp_path: Path) -> None:
    """UT01-45 list_keys reads the newest file and needs snapshot mode and a file."""
    entities = {
        "teams": {"pattern": "*.csv", "key_field": ["id"], "mode": "snapshot"},
        "sites": {"pattern": "*.csv", "key_field": ["id"]},
    }
    conn = d.connector(tmp_path, entities)
    with pytest.raises(ConfigError, match="no snapshot file"):
        list(conn.list_keys("teams"))
    with pytest.raises(ConfigError, match="files entity sites is not in snapshot mode"):
        list(conn.list_keys("sites"))
    d.drop(tmp_path, "teams/old.csv", "id\n1\n2\n", age_s=9000)
    d.drop(tmp_path, "teams/new.csv", "id\n2\n3\n", age_s=5000)
    assert [k for b in conn.list_keys("teams") for k in b.column(0).to_pylist()] == ["2", "3"]


def test_ut01_47_invalid_key_rejected(tmp_path: Path) -> None:
    """UT01-47 a key with a control character fails like ``record_id``, without its value."""
    (tmp_path / "sites").mkdir()
    path = tmp_path / "sites" / "s.parquet"
    pq.write_table(pa.table({"id": ["ok", "bad" + chr(1)]}), path)
    os.utime(path, ns=(d.mtime_ns(3600), d.mtime_ns(3600)))
    entities = {"sites": {"pattern": "*.parquet", "key_field": ["id"]}}
    with pytest.raises(SchemaViolation, match="invalid record key") as info:
        _read(d.connector(tmp_path, entities), "sites")
    assert "bad" not in str(info.value)
