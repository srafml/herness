"""Tests for herness.harness.warehouse (U05-38): DuckWarehouse, WarehousePool, open_warehouse."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from herness.core.errors import ConfigError, QueryError
from herness.core.ids import query_id as compute_query_id
from herness.core.types import Evidence
from herness.harness import warehouse as wh
from herness.harness.llm.settings import SqlSettings

pytestmark = pytest.mark.unit

BUILD_ID = "20260925-101500-ABCDEF"
OTHER_BUILD_ID = "20260925-101500-BCDEFG"
THIRD_BUILD_ID = "20260925-101500-CDEFGH"
FOURTH_BUILD_ID = "20260925-101500-DEFGHJ"


def _make_tiny_build(warehouse_dir: Path, build_id: str = BUILD_ID) -> Path:
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    path = warehouse_dir / f"wh-{build_id}.duckdb"
    con = duckdb.connect(str(path))
    for schema in ("core", "enrich", "metrics", "score", "meta"):
        con.execute(f"CREATE SCHEMA {schema}")
    con.execute("CREATE TABLE core.incident (id INTEGER, short_description VARCHAR)")
    con.execute("COMMENT ON TABLE core.incident IS 'tiny incident table'")
    con.execute("CREATE TABLE meta.build_info (built_at VARCHAR)")
    con.close()
    return path


def _evidence(build_id: str = BUILD_ID, row_count: int = 1) -> Evidence:
    sql = "select count(*) as n from core.incident"
    params: dict[str, Any] = {}
    return Evidence(
        query_id=compute_query_id(sql, params, build_id),
        run_id="run_1",
        build_id=build_id,
        sql=sql,
        params=params,
        result_hash="a" * 64,
        row_count=row_count,
        result_sample=[{"n": row_count}],
        executed_at=datetime(2026, 9, 25, 12, 0, tzinfo=UTC),
        duration_ms=5,
    )


class _FakeResult:
    def __init__(self, row_count: int) -> None:
        self.row_count = row_count


# --- UT05-49: tiny_build open, settings, schema, table_comment ------------------------------


def test_ut05_49_open_warehouse_locks_settings(tmp_path: Path) -> None:
    """UT05-49 open on tiny_build: read-only, external access false, lock true."""
    _make_tiny_build(tmp_path)
    handle = wh.open_warehouse(BUILD_ID, warehouse_dir=tmp_path, sql=SqlSettings())
    try:
        assert handle.build_id == BUILD_ID
        assert handle.path == tmp_path.resolve() / f"wh-{BUILD_ID}.duckdb"
        cur = handle.cursor()
        row = cur.execute("SELECT current_setting('enable_external_access')").fetchone()
        assert row == (False,)
        row = cur.execute("SELECT current_setting('lock_configuration')").fetchone()
        assert row == (True,)
        row = cur.execute("SELECT current_setting('python_enable_replacements')").fetchone()
        assert row == (False,)  # T05-14 carry-over: no Python-variable replacement scans
        with pytest.raises(duckdb.Error):
            cur.execute("CREATE TABLE core.x (a INT)")
    finally:
        handle.close()


class _FakeCurrentSettingConn:
    """Fake `duckdb.connect` result that reports `enable_external_access` never locked."""

    def __init__(self, value: object) -> None:
        self._value = value
        self._last_sql = ""

    def execute(self, sql: str, *args: object) -> _FakeCurrentSettingConn:
        self._last_sql = sql
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return (self._value,) if "current_setting" in self._last_sql else None


def test_ut05_49_self_check_fails_loudly_when_lock_does_not_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-49 the self-check raises ConfigError if the pinned DuckDB does not honour the lock."""
    _make_tiny_build(tmp_path)
    monkeypatch.setattr(wh.duckdb, "connect", lambda *a, **k: _FakeCurrentSettingConn(True))
    with pytest.raises(ConfigError, match="self-check"):
        wh.open_warehouse(BUILD_ID, warehouse_dir=tmp_path, sql=SqlSettings())


def test_ut05_49_io_exception_on_connect_raises_query_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-49 a DuckDB IOException on open becomes QueryError('...unreadable')."""
    _make_tiny_build(tmp_path)

    def _raise(*args: object, **kwargs: object) -> Any:
        msg = "simulated I/O failure"
        raise duckdb.IOException(msg)

    monkeypatch.setattr(wh.duckdb, "connect", _raise)
    with pytest.raises(QueryError, match="unreadable"):
        wh.open_warehouse(BUILD_ID, warehouse_dir=tmp_path, sql=SqlSettings())


def test_ut05_49_schema_and_table_comment(tmp_path: Path) -> None:
    """UT05-49 schema() reports tiny_build tables/columns; table_comment reads the COMMENT."""
    _make_tiny_build(tmp_path)
    handle = wh.open_warehouse(BUILD_ID, warehouse_dir=tmp_path, sql=SqlSettings())
    try:
        schema = handle.schema()
        assert schema["core"]["incident"]["id"] == "integer"
        assert schema["meta"]["build_info"]["built_at"] == "varchar"
        assert schema is handle.schema()
        assert handle.table_comment("core.incident") == "tiny incident table"
        assert handle.table_comment("meta.build_info") == ""
    finally:
        handle.close()


def test_ut05_49_cursor_is_per_thread(tmp_path: Path) -> None:
    """UT05-49 cursor() caches one cursor per calling thread."""
    _make_tiny_build(tmp_path)
    handle = wh.open_warehouse(BUILD_ID, warehouse_dir=tmp_path, sql=SqlSettings())
    try:
        first = handle.cursor()
        assert handle.cursor() is first
        other: dict[str, object] = {}
        thread = threading.Thread(target=lambda: other.__setitem__("cur", handle.cursor()))
        thread.start()
        thread.join()
        assert other["cur"] is not first
    finally:
        handle.close()


def test_ut05_49_build_not_found_raises_query_error(tmp_path: Path) -> None:
    """UT05-49 a missing build file raises QueryError, not ConfigError."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    with pytest.raises(QueryError):
        wh.open_warehouse(BUILD_ID, warehouse_dir=tmp_path, sql=SqlSettings())


def test_ut05_49_cache_put_get_respects_return_rows_and_lru(tmp_path: Path) -> None:
    """UT05-49 cache_put stores only row_count <= return_rows; LRU evicts past the cap."""
    _make_tiny_build(tmp_path)
    sql = SqlSettings(return_rows=5)
    handle = wh.open_warehouse(BUILD_ID, warehouse_dir=tmp_path, sql=sql)
    try:
        assert handle.cache_get("q_0000000000000001") is None
        handle.cache_put("q_0000000000000001", (_FakeResult(3), _evidence(row_count=3)))
        cached = handle.cache_get("q_0000000000000001")
        assert cached is not None
        assert cached[0].row_count == 3

        handle.cache_put("q_0000000000000002", (_FakeResult(999), _evidence(row_count=999)))
        assert handle.cache_get("q_0000000000000002") is None

        for i in range(3, wh.RESULT_CACHE_ENTRIES + 3):
            qid = f"q_{i:016x}"
            handle.cache_put(qid, (_FakeResult(1), _evidence(row_count=1)))
        assert handle.cache_get("q_0000000000000001") is None
        assert len(handle._cache) == wh.RESULT_CACHE_ENTRIES
    finally:
        handle.close()


# --- UT05-50: bad ids, symlink, pool LRU -----------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    ["not-an-id", "20260925-101500-ILOU00", "../../x", "", "20260925-101500-abcdef"],
)
def test_ut05_50_bad_build_id_raises_config_error(tmp_path: Path, bad_id: str) -> None:
    """UT05-50 a build_id not matching BUILD_ID_RE raises ConfigError before any file I/O."""
    pool = wh.WarehousePool(tmp_path, SqlSettings())
    with pytest.raises(ConfigError):
        pool.get(bad_id)


def test_ut05_50_symlinked_warehouse_file_raises_config_error(tmp_path: Path) -> None:
    """UT05-50 a symlinked warehouse file raises ConfigError (TH05-17)."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    _make_tiny_build(real_dir, OTHER_BUILD_ID)
    warehouse_dir = tmp_path / "warehouse"
    warehouse_dir.mkdir()
    link = warehouse_dir / f"wh-{OTHER_BUILD_ID}.duckdb"
    try:
        link.symlink_to(real_dir / f"wh-{OTHER_BUILD_ID}.duckdb")
    except OSError as exc:
        pytest.skip(f"platform denied symlink creation: {exc}")
    with pytest.raises(ConfigError):
        wh.open_warehouse(OTHER_BUILD_ID, warehouse_dir=warehouse_dir, sql=SqlSettings())


def test_ut05_50_pool_get_caches_and_lru_closes_over_max(tmp_path: Path) -> None:
    """UT05-50 WarehousePool.get reuses handles and closes the LRU one past max_open."""
    for build_id in (BUILD_ID, OTHER_BUILD_ID, THIRD_BUILD_ID, FOURTH_BUILD_ID):
        _make_tiny_build(tmp_path, build_id)
    pool = wh.WarehousePool(tmp_path, SqlSettings(), max_open=3)
    first = pool.get(BUILD_ID)
    assert pool.get(BUILD_ID) is first

    pool.get(OTHER_BUILD_ID)
    pool.get(THIRD_BUILD_ID)
    pool.get(FOURTH_BUILD_ID)

    with pytest.raises(duckdb.Error):
        first.cursor().execute("SELECT 1")

    pool.close_all()
