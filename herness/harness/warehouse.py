"""Read-only DuckDB handles for agent- and Verifier-facing warehouse access (U05-38).

Design 05 §5.4.1. The only place that opens `wh-<build_id>.duckdb`: connects read-only,
then locks down `enable_external_access` and `lock_configuration` so agent SQL cannot
read files, reach the network, `ATTACH`, or change settings back (TH05-04, TH05-06). A
self-check re-reads the setting after locking and fails loudly (`ConfigError`) if the
pinned DuckDB build does not honour it. `build_id` and the resolved path are checked
against traversal and symlinks before the file is ever opened (TH05-17).

VI-4 (open-questions (b) 4): verified against the pinned DuckDB (1.5.5, see report) —
`SET enable_external_access = false` then `SET lock_configuration = true` on the
just-opened read-only connection behaves exactly as design §5.4.1 specifies; no
deviation was needed.

The result cache stores `(result, Evidence)` pairs for `execute_recorded`; `_CachedRow`
is a private stand-in for `RecordedResult` (U05-35), not built yet, and callers should
retype `cache_get`/`cache_put` to it once that unit lands.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import duckdb

from herness.core.errors import ConfigError, QueryError
from herness.core.types import Evidence
from herness.harness.llm.settings import SqlSettings

BUILD_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9A-HJKMNP-TV-Z]{6}$")
RESULT_CACHE_ENTRIES = 512
_SCHEMAS = ("core", "enrich", "metrics", "score", "meta")
_SchemaMap = Mapping[str, Mapping[str, Mapping[str, str]]]


class _CachedRow(Protocol):
    """Minimal shape a cached result needs; retyped to `RecordedResult` when U05-35 lands."""

    @property
    def row_count(self) -> int: ...


type _CacheValue = tuple[_CachedRow, Evidence]


def _connect_config(sql: SqlSettings) -> dict[str, str | bool | int | float | list[str]]:
    return {
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
        "python_enable_replacements": False,
        "threads": sql.threads,
        "memory_limit": sql.memory_limit,
    }


def _validated_path(build_id: str, warehouse_dir: Path) -> Path:
    if BUILD_ID_RE.fullmatch(build_id) is None:
        msg = "invalid build_id"
        raise ConfigError(msg, build_id=build_id)
    base = warehouse_dir.resolve()
    path = base / f"wh-{build_id}.duckdb"
    if path.is_symlink():
        msg = "warehouse path must not be a symlink"
        raise ConfigError(msg, build_id=build_id)
    if path.resolve().parent != base:
        msg = "warehouse path escapes warehouse_dir"
        raise ConfigError(msg, build_id=build_id)
    return path


def _load_schema(con: duckdb.DuckDBPyConnection) -> _SchemaMap:
    rows = con.execute(
        "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns"
        " WHERE lower(table_schema) IN (?, ?, ?, ?, ?)",
        list(_SCHEMAS),
    ).fetchall()
    schema: dict[str, dict[str, dict[str, str]]] = {name: {} for name in _SCHEMAS}
    for schema_name, table_name, column_name, data_type in rows:
        tables = schema[str(schema_name).lower()]
        columns = tables.setdefault(str(table_name).lower(), {})
        columns[str(column_name).lower()] = str(data_type).lower()
    return schema


class DuckWarehouse:
    """Read-only view of one warehouse build; implements `WarehouseHandle` (U05-08)."""

    def __init__(
        self, build_id: str, path: Path, con: duckdb.DuckDBPyConnection, sql: SqlSettings
    ) -> None:
        self._build_id = build_id
        self._path = path
        self._con = con
        self._sql = sql
        self._local: threading.local = threading.local()
        self._schema: _SchemaMap | None = None
        self._schema_lock = threading.Lock()
        self._cache: OrderedDict[str, _CacheValue] = OrderedDict()
        self._cache_lock = threading.Lock()

    @property
    def build_id(self) -> str:
        return self._build_id

    @property
    def path(self) -> Path:
        return self._path

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """A DuckDB cursor cached for the calling thread (thread-local, R-invariant)."""
        cur: duckdb.DuckDBPyConnection | None = getattr(self._local, "cursor", None)
        if cur is None:
            cur = self._con.cursor()
            self._local.cursor = cur
        return cur

    def schema(self) -> _SchemaMap:
        """Schema → table → column → DuckDB type (lower-case), loaded once and cached.

        Loaded once under a lock on its own cursor: `dispatch` runs tools in threads, and a
        cold load on the shared connection raced into a partial schema (T05-16 finding).
        """
        with self._schema_lock:
            if self._schema is None:
                with self._con.cursor() as cur:
                    self._schema = _load_schema(cur)
            return self._schema

    def table_comment(self, qualified: str) -> str:
        """The table's comment from `duckdb_tables()`, or `""` when there is none."""
        schema_name, _, table_name = qualified.partition(".")
        with self._con.cursor() as cur:  # own cursor: tools call this from worker threads
            row = cur.execute(
                "SELECT comment FROM duckdb_tables() WHERE schema_name = ? AND table_name = ?",
                [schema_name, table_name],
            ).fetchone()
        return "" if row is None or row[0] is None else str(row[0])

    def cache_get(self, query_id: str) -> _CacheValue | None:
        """Return the cached `(result, Evidence)` for `query_id`, marking it recently used."""
        with self._cache_lock:
            value = self._cache.get(query_id)
            if value is not None:
                self._cache.move_to_end(query_id)
            return value

    def cache_put(self, query_id: str, value: _CacheValue) -> None:
        """Cache `value` (LRU, ≤ `RESULT_CACHE_ENTRIES`) unless it exceeds `sql.return_rows`."""
        result, _evidence = value
        if result.row_count > self._sql.return_rows:
            return
        with self._cache_lock:
            self._cache[query_id] = value
            self._cache.move_to_end(query_id)
            while len(self._cache) > RESULT_CACHE_ENTRIES:
                self._cache.popitem(last=False)

    def close(self) -> None:
        self._con.close()


def open_warehouse(build_id: str, *, warehouse_dir: Path, sql: SqlSettings) -> DuckWarehouse:
    """Open `wh-<build_id>.duckdb` read-only, locked down per design §5.4.1 (VI-4)."""
    path = _validated_path(build_id, warehouse_dir)
    if not path.exists():
        msg = f"warehouse build {build_id} not found"
        raise QueryError(msg, hint=None, build_id=build_id)
    try:
        con = duckdb.connect(str(path), read_only=True, config=_connect_config(sql))
    except duckdb.IOException as exc:
        msg = f"warehouse build {build_id} unreadable"
        raise QueryError(msg, build_id=build_id) from exc
    try:  # any failure after connect closes the connection (no leaked handle, T05-17)
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        check = con.execute("SELECT current_setting('enable_external_access')").fetchone()
        if check is None or check[0] is not False:
            msg = "warehouse self-check failed: enable_external_access is not locked to false"
            raise ConfigError(msg, build_id=build_id)  # noqa: TRY301 - handler closes con
    except BaseException:
        con.close()
        raise
    return DuckWarehouse(build_id, path, con, sql)


class WarehousePool:
    """Process-wide pool of open `DuckWarehouse` handles, one per `build_id`, LRU-bounded."""

    def __init__(self, warehouse_dir: Path, sql: SqlSettings, *, max_open: int = 3) -> None:
        self._warehouse_dir = warehouse_dir
        self._sql = sql
        self._max_open = max_open
        self._lock = threading.Lock()
        self._open: OrderedDict[str, DuckWarehouse] = OrderedDict()

    def get(self, build_id: str) -> DuckWarehouse:
        """Return the open handle for `build_id`, opening and evicting the LRU if needed."""
        with self._lock:
            handle = self._open.get(build_id)
            if handle is not None:
                self._open.move_to_end(build_id)
                return handle
            handle = open_warehouse(build_id, warehouse_dir=self._warehouse_dir, sql=self._sql)
            self._open[build_id] = handle
            while len(self._open) > self._max_open:
                _, lru = self._open.popitem(last=False)
                lru.close()
            return handle

    def close_all(self) -> None:
        """Close every open handle; the pool holds none afterward."""
        with self._lock:
            for handle in self._open.values():
                handle.close()
            self._open.clear()
