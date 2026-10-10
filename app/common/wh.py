"""Read-only warehouse access following ``CURRENT`` and the named queries (impl 09 U09-60 - U09-62).

Dashboard SQL lives only in ``queries.sql.yaml``: one parameterised SELECT per name, values bound
by name and never interpolated (TH09-13), no raw ticket text from ``core`` (TH09-11), row caps
and a 30 s timeout (TH09-14). Cache keys hold the build id, query name and parameters only.
The U09-61 loader and guard live in ``_wh_queries`` (line budget) and are re-exported here.
"""

from __future__ import annotations

import datetime
import functools
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import duckdb
import pyarrow as pa
import streamlit as st

from app.common._wh_queries import (
    FORBIDDEN_TEXT_COLUMNS,
    PARAM_CHECKS,
    NamedQuery,
    ParamSpec,
    check_forbidden_columns,
    load_named_queries,
)
from herness.core import time as clock
from herness.core.errors import ConfigError, QueryError, SchemaViolation
from herness.core.logging import get_logger
from herness.reports.rules import UserInputError
from herness.store.errors import NotFoundError
from herness.store.layout import DataLayout
from herness.store.warehouse import BUILD_ID_RE, open_readonly, read_current

if TYPE_CHECKING:
    from app.common.bootstrap import AppServices

__all__ = [
    "FORBIDDEN_TEXT_COLUMNS", "NamedQuery", "NoCurrentBuild", "ParamSpec", "QueryResult",
    "check_forbidden_columns", "current_build_id", "get_conn", "load_named_queries", "query",
]  # fmt: skip

STALE_CONN_GRACE_S: Final = 300
QUERY_TIMEOUT_S: float = 30  # module attribute so tests can shorten it
# U09-63: builds whose meta.evidence lacks result_sample use the `_v1` variant.
_VARIANTS: Final = {"evidence.meta": ("evidence.meta_v1", ["meta", "evidence", "result_sample"])}
_COLUMN_SQL: Final = (
    "SELECT 1 FROM duckdb_columns() WHERE schema_name = ? AND table_name = ? AND column_name = ?"
)
_HARDEN: Final = ("SET enable_external_access = false", "SET lock_configuration = true")
_SETTINGS_SQL: Final = (
    "SELECT current_setting('enable_external_access'), current_setting('lock_configuration')"
)
_log = get_logger("app.warehouse")


class NoCurrentBuild(ConfigError):  # noqa: N818 - name fixed by impl 09 U09-60
    """No promoted warehouse build is readable."""

    def __init__(self) -> None:
        super().__init__("No promoted warehouse yet.", hint="Run `herness pipeline`.")


@dataclass(frozen=True)
class QueryResult:
    table: pa.Table
    truncated: bool
    build_id: str


def _services() -> AppServices:
    from app.common.bootstrap import get_services  # noqa: PLC0415 - cycle: bootstrap imports us

    return get_services()


def current_build_id(data_dir: Path) -> str:
    """The build named in ``CURRENT``; missing or invalid -> ``NoCurrentBuild``."""
    try:
        raw = read_current(layout=DataLayout.from_root(data_dir))
    except (SchemaViolation, NotFoundError) as exc:
        raise NoCurrentBuild from exc
    build_id = (raw or "").strip()
    if BUILD_ID_RE.fullmatch(build_id) is None:  # ^\d{8}-\d{6}-<ULID6>$ (U09-60)
        raise NoCurrentBuild
    return build_id


def _open(build_id: str, layout: DataLayout) -> duckdb.DuckDBPyConnection:
    """Open read-only, then verify-then-set the hardening; fail closed (U09-60 step 3)."""
    conn = open_readonly(build_id, layout=layout)
    try:
        if conn.execute(_SETTINGS_SQL).fetchone() != (False, True):  # open_readonly may harden
            for statement in _HARDEN:
                conn.execute(statement)
        if conn.execute(_SETTINGS_SQL).fetchone() == (False, True):
            return conn
    except duckdb.Error:
        pass  # reported below as a typed error, never a raw DuckDB one
    conn.close()
    msg = f"warehouse {build_id} could not be hardened"
    raise ConfigError(msg, build_id=build_id)


class _Pool:
    """``build_id -> (connection, last_current_at)`` behind a lock (U09-60 step 3, 4)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: dict[str, tuple[duckdb.DuckDBPyConnection, datetime.datetime]] = {}

    def get(self, build_id: str, layout: DataLayout) -> duckdb.DuckDBPyConnection:
        with self._lock:
            now = clock.now()
            for other, (conn, last) in list(self._conns.items()):
                if other != build_id and (now - last).total_seconds() > STALE_CONN_GRACE_S:
                    conn.close()
                    del self._conns[other]
            entry = self._conns.get(build_id)
            conn = entry[0] if entry is not None else _open(build_id, layout)
            self._conns[build_id] = (conn, now)
            return conn

    def close_all(self) -> None:
        with self._lock:
            for conn, _last in self._conns.values():
                conn.close()
            self._conns.clear()


@st.cache_resource(show_spinner=False)
def _pool() -> _Pool:
    return _Pool()


def _recheck(data_dir: Path, recheck_s: int) -> str:
    state, now = st.session_state, clock.now()
    checked = state.get("wh_checked_at")
    old = state.get("wh_build_id")
    if old is None or checked is None or (now - checked).total_seconds() >= recheck_s:
        state["wh_checked_at"] = now
        new = current_build_id(data_dir)
        if old is not None and new != old:
            st.toast(f"Data refreshed to build {new}")
            _log.info("app.warehouse.switched", old_build_id=old, new_build_id=new)
        state["wh_build_id"] = old = new
    return cast("str", old)


def get_conn() -> tuple[str, duckdb.DuckDBPyConnection]:
    """``(build_id, connection)`` for this session's view of ``CURRENT`` (U09-60)."""
    cfg = _services().cfg
    data_dir = Path(cfg.paths.data)
    layout = DataLayout.from_root(data_dir)
    build_id = _recheck(data_dir, cfg.app.app.current_recheck_s)
    try:
        return build_id, _pool().get(build_id, layout)
    except (NotFoundError, SchemaViolation):
        fresh = _recheck(data_dir, 0)  # retired between check and open: one forced re-read
        try:
            return fresh, _pool().get(fresh, layout)
        except (NotFoundError, SchemaViolation) as exc:
            raise NoCurrentBuild from exc


_CONVERT: Final[dict[str, Callable[[Any], object]]] = {"float": float, "list_str": tuple}


def _bind(nq: NamedQuery, params: Mapping[str, object]) -> tuple[tuple[str, object], ...]:
    if set(params) - set(nq.params):
        msg = f"Unknown parameter for {nq.name}."
        raise UserInputError(msg)
    bound: dict[str, object] = {}
    for key, spec in nq.params.items():
        value = params.get(key, spec.default)
        if value is not None and not PARAM_CHECKS[spec.type](value):
            msg = f"Parameter {key} has the wrong type or size (expected {spec.type})."
            raise UserInputError(msg)
        if value is None and spec.required:
            msg = f"Missing required parameter {key}."
            raise UserInputError(msg)
        convert = _CONVERT.get(spec.type)
        bound[key] = value if value is None or convert is None else convert(value)
    return tuple(sorted(bound.items()))


def _execute(
    build_id: str,
    name: str,
    frozen: tuple[tuple[str, object], ...],
    _conn: duckdb.DuckDBPyConnection,
    _queries: Mapping[str, NamedQuery],
) -> tuple[pa.Table, bool]:
    """The cached executor ``_run`` (keyed on build id, query name and parameters)."""
    del build_id
    cur = _conn.cursor()
    timer = threading.Timer(QUERY_TIMEOUT_S, cur.interrupt)
    try:
        nq = _queries[name]
        if (variant := _VARIANTS.get(name)) and not cur.execute(_COLUMN_SQL, variant[1]).fetchone():
            nq = _queries[variant[0]]
        values = {k: list(v) if isinstance(v, tuple) else v for k, v in frozen}
        sql = f"SELECT * FROM ({nq.sql}) AS q LIMIT {nq.row_cap + 1}"  # noqa: S608 - reviewed SQL
        timer.start()
        table = cur.execute(sql, values).to_arrow_table()
    except duckdb.InterruptException as exc:
        msg = f"query {name} timed out after {QUERY_TIMEOUT_S:g} s"
        raise QueryError(msg) from exc
    except duckdb.Error as exc:
        msg = f"query {name} failed"
        raise QueryError(msg) from exc
    finally:
        timer.cancel()
        cur.close()
    truncated = table.num_rows > nq.row_cap
    return (table.slice(0, nq.row_cap) if truncated else table), truncated


@functools.cache
def _run_cached(ttl: int) -> Callable[..., tuple[pa.Table, bool]]:
    """``st.cache_data`` over ``_execute`` with the configured TTL (one wrapper per process)."""
    run: Callable[..., tuple[pa.Table, bool]]
    run = st.cache_data(ttl=ttl, max_entries=500, show_spinner=False)(_execute)
    return run


def query(name: str, /, **params: object) -> QueryResult:
    """Execute the named query with bound parameters, row cap and timeout (U09-62)."""
    services = _services()
    queries = cast("Mapping[str, NamedQuery]", services.queries)
    nq = queries.get(name)
    if nq is None:
        msg = f"unknown named query {name[:64]}"
        raise ConfigError(msg)
    frozen = _bind(nq, params)
    build_id, conn = get_conn()
    started = clock.monotonic()
    run = _run_cached(services.cfg.app.app.cache_ttl_s.warehouse)
    table, truncated = run(build_id, name, frozen, conn, queries)
    ms, rows = int((clock.monotonic() - started) * 1000), table.num_rows
    _log.debug("app.query.completed", name=name, rows=rows, duration_ms=ms, build_id=build_id)
    return QueryResult(table, truncated, build_id)
