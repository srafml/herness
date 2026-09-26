"""Execution internals of `herness.harness.tools.execute_recorded` (impl 05 U05-35); private.

Split out so `tools.py` keeps room for the tool registry and dispatch within its 400-line
budget. Holds the guard step (2), the streaming execution (4-5) and the JSON-safe evidence
sample (6). `result_hash` and `iter_batch_rows` come from `herness.metrics.evidence` and are
never reimplemented (R-15, UT05-124). `json_safe` is the U05-35 step 6 conversion that the
Verifier's private stand-in (`_verifier_rerun.json_safe`) is to be replaced with.
"""

from __future__ import annotations

import functools
import json
import math
import re
import threading
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Final, cast

import duckdb
import pyarrow as pa
from pydantic import JsonValue

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError, QueryError, SchemaViolation, ToolInputError
from herness.core.ids import query_id as compute_query_id
from herness.core.redact import redact_text
from herness.core.resilience import fault_point
from herness.core.types import ToolContext
from herness.harness.sql_guard import GuardedQuery, SqlGuard
from herness.metrics.evidence import iter_batch_rows, result_hash

BATCH_ROWS: Final = 10_000
SAMPLE_ROWS: Final = 50
ERROR_CHARS: Final = 500
_PARAM_KEY_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
_QUOTED_RE: Final = re.compile(r"'(?:[^']|'')*'")
_TIMEOUT_HINT: Final = "filter by period or use get_metric"
_DUCKDB_HINT: Final = "check table and column names with describe_table"
_SIZE_HINT: Final = "aggregate first or add filters"

type Row = tuple[object, ...]
type _SchemaMap = Mapping[str, Mapping[str, Mapping[str, str]]]


def _blocked_columns() -> Sequence[str]:
    """`harness.sql.blocked_columns` of the process config (U05-72)."""
    return get_config().models.harness.sql.blocked_columns


def query_id_for(sql: str, params: Mapping[str, JsonValue], build_id: str) -> str:
    """Step 1 with the U05-35 preconditions on `params` names; `ToolInputError` otherwise."""
    for key in params:
        if _PARAM_KEY_RE.fullmatch(key) is None:
            msg = "query param names must match ^[a-z][a-z0-9_]{0,31}$"
            raise ToolInputError(msg)
        if re.search(rf"\${key}\b", sql) is None:
            msg = f"query param {key} is not referenced as ${key} in the SQL"
            raise ToolInputError(msg)
    try:
        return compute_query_id(sql, params, build_id)
    except SchemaViolation as exc:
        msg = "query params not JSON-compatible"
        raise ToolInputError(msg) from exc


@dataclass(frozen=True, slots=True)
class _GuardInput:
    """Schema and blocked columns for the cached internal-SQL guard; not part of its key."""

    schema: _SchemaMap = field(compare=False)
    blocked: tuple[str, ...] = field(compare=False)


@functools.lru_cache(maxsize=64)
def _internal_guard(build_id: str, sql: str, given: _GuardInput) -> GuardedQuery:
    """Guard result of an internal SQL constant, cached per `(build_id, sql)` (step 2)."""
    del build_id  # part of the cache key only
    return SqlGuard(given.schema, given.blocked).check(sql, allow_catalog=True)


def clear_guard_cache() -> None:
    """Forget cached internal-SQL guard results (tests; a rebuilt build of the same id)."""
    _internal_guard.cache_clear()


def check_sql(ctx: ToolContext, sql: str, *, guard: bool) -> GuardedQuery:
    """Step 2: agent SQL under the full guard; internal SQL only for `ordered` and lineage."""
    # A schema without tables allows nothing; sqlglot rejects empty mappings (T05-14).
    schema = {name: tables for name, tables in ctx.warehouse.schema().items() if tables}
    blocked = tuple(_blocked_columns())
    if guard:
        return SqlGuard(schema, blocked).check(sql)
    try:
        return _internal_guard(ctx.build_id, sql, _GuardInput(schema, blocked))
    except QueryError as exc:
        msg = "internal tool SQL failed the SQL guard"
        raise ConfigError(msg, rule=exc.message) from exc


def output_names(columns: Sequence[str], guarded: frozenset[str]) -> frozenset[str]:
    """Result columns named in `guarded`; unaliased outputs appear there as `_col_<i>`."""
    wanted = {_norm(name) for name in guarded}
    return frozenset(
        name for i, name in enumerate(columns) if _norm(name) in wanted or f"_col_{i}" in wanted
    )


def _norm(name: str) -> str:
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold())


def timeout_error(timeout_s: float) -> QueryError:
    msg = f"timeout after {timeout_s}s"
    return QueryError(msg, hint=_TIMEOUT_HINT)


class _Scan:
    """One pass over the rows: counts them, keeps the first `keep`, stops past `limit`."""

    def __init__(self, keep: int, limit: int) -> None:
        self.count = 0
        self.rows: list[Row] = []
        self._keep = keep
        self._limit = limit

    def feed(self, rows: Iterable[Row]) -> Iterator[Row]:
        for row in rows:
            self.count += 1
            if self.count > self._limit:
                msg = "result too large"
                raise QueryError(msg, hint=_SIZE_HINT)
            if len(self.rows) < self._keep:
                self.rows.append(row)
            yield row


@dataclass(frozen=True, slots=True)
class Executed:
    """Steps 4-5 output: columns, DuckDB type names (VI-11), kept rows, count and hash."""

    columns: list[str]
    types: list[str]
    rows: list[Row]
    row_count: int
    result_hash: str
    duration_ms: int


def run_query(ctx: ToolContext, sql: str, params: Mapping[str, JsonValue]) -> Executed:
    """Steps 4-5: execute under the interrupt timer, hashing while streaming (R-15)."""
    limits = ctx.sql_limits
    try:
        fault_point("sql.query", role=ctx.role)
    except QueryError as exc:
        if exc.context.get("timeout"):  # an injected timeout reads like a real one
            raise timeout_error(limits.timeout_s) from exc
        raise
    cur = cast("duckdb.DuckDBPyConnection", ctx.warehouse.cursor())
    fired = threading.Event()

    def interrupt() -> None:
        fired.set()
        cur.interrupt()

    # `cancel()` cannot stop a callback that already started, so a late `interrupt()` may hit
    # this thread's cached cursor after the query ended; DuckDB clears the interrupt flag when
    # the next query starts, so the window is harmless (review M2).
    timer = threading.Timer(limits.timeout_s, interrupt)
    timer.daemon = True
    timer.start()
    started = clock.monotonic()
    scan = _Scan(limits.return_rows, limits.scan_rows)
    try:
        cur.execute(sql, dict(params))
        columns = [str(d[0]) for d in cur.description or ()]
        types = [str(d[1]) for d in cur.description or ()]
        rows = scan.feed(iter_batch_rows(cur.to_arrow_reader(BATCH_ROWS)))
        digest = result_hash(list(zip(columns, types, strict=True)), rows)
    except (duckdb.Error, OSError, pa.ArrowException) as exc:
        raise _engine_error(exc, fired=fired.is_set(), timeout_s=limits.timeout_s) from exc
    except SchemaViolation as exc:  # a cell `result_hash` cannot encode
        msg = "result has a value that cannot be recorded"
        raise QueryError(msg, hint="cast the column to text or a number") from exc
    finally:
        timer.cancel()
    duration_ms = round((clock.monotonic() - started) * 1000)
    return Executed(columns, types, scan.rows, scan.count, digest, duration_ms)


def _engine_error(exc: Exception, *, fired: bool, timeout_s: float) -> QueryError:
    """Step 5 mapping; an interrupt may surface from DuckDB or, mid-stream, from pyarrow."""
    text = str(exc)
    if fired or isinstance(exc, duckdb.InterruptException) or "INTERRUPT" in text:
        return timeout_error(timeout_s)
    return QueryError(safe_error_text(text), hint=_DUCKDB_HINT)


def safe_error_text(text: str) -> str:
    """DuckDB error text without quoted values, redacted, first 500 chars (ENG §3.4 ruling).

    Conversion errors quote the offending cell, which may be redact-on-read ticket text.
    """
    redacted = redact_text(_QUOTED_RE.sub("'<value>'", text))
    return (redacted if redacted is not None else "query failed")[:ERROR_CHARS]


def json_safe(value: object) -> JsonValue:
    """U05-35 step 6 JSON-safe cell: Decimal -> str, date -> ISO, datetime -> ISO UTC with Z.

    Non-finite floats become "NaN", "Infinity", "-Infinity" (ops JSON rejects them).
    Naive datetimes (DuckDB `TIMESTAMP`) are read as UTC. Bytes become `None` inside lists
    and dicts; `sample_rows` omits top-level bytes cells.
    """
    if isinstance(value, float) and not math.isfinite(value):
        safe: JsonValue = str(value).replace("inf", "Infinity").replace("nan", "NaN")
    elif value is None or isinstance(value, bool | int | float | str):
        safe = value
    elif isinstance(value, datetime):
        safe = clock.format_utc(value if value.tzinfo is not None else value.replace(tzinfo=UTC))
    elif isinstance(value, date | time):
        safe = value.isoformat()
    elif isinstance(value, bytes | bytearray | memoryview):
        safe = None
    elif isinstance(value, list | tuple):
        safe = [json_safe(member) for member in value]
    elif isinstance(value, dict):
        safe = {str(key): json_safe(member) for key, member in value.items()}
    else:  # Decimal -> str, and any other scalar (UUID, timedelta) as its text
        safe = str(value)
    return safe


def _redacted(value: JsonValue) -> JsonValue:
    if value is None:
        return None
    return redact_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))


def sample_rows(
    columns: Sequence[str], rows: Iterable[Row], redact: frozenset[str] = frozenset()
) -> list[dict[str, JsonValue]]:
    """The first `SAMPLE_ROWS` rows as JSON-safe dicts; bytes omitted, `redact` cells redacted."""
    sample: list[dict[str, JsonValue]] = []
    for row in rows:
        if len(sample) == SAMPLE_ROWS:
            break
        cells = {
            name: json_safe(cell)
            for name, cell in zip(columns, row, strict=True)
            if not isinstance(cell, bytes | bytearray | memoryview)
        }
        sample.append({k: _redacted(v) if k in redact else v for k, v in cells.items()})
    return sample
