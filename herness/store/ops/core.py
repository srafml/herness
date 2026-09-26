"""Ops store core (impl 02 U02-36 … U02-43, R-10): per-thread connections, retried writes.

Module state (ENG §2.3 exception, impl 02 §13): ``_registry`` (path override, generation, open
connections under a lock) and a thread-local (pid, path, generation, connection) entry.
"""

from __future__ import annotations

import contextlib
import datetime
import decimal
import enum
import functools
import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePath
from typing import Final

from pydantic import BaseModel

from herness.core import time as clock
from herness.core.errors import ConfigError, HernessError, SchemaViolation, StoreBusy
from herness.core.logging import get_logger
from herness.store.layout import data_layout

from . import _shims

OPS_JSON_MAX_BYTES: Final[int] = 65_536

_MIN_SQLITE: Final = (3, 38, 0)
_OP_RE: Final = re.compile(r"[a-z_]{1,64}")
_JSON_BYTES_RANGE: Final = (1024, 16 * 1024 * 1024)
_MAX_ROWS_LIMIT: Final = 1_000_000
_PRAGMAS: Final = (
    "PRAGMA foreign_keys = ON; PRAGMA synchronous = NORMAL; PRAGMA trusted_schema = OFF;"
    " PRAGMA temp_store = MEMORY; PRAGMA cache_size = -65536;"
)
# Not Final: tests override them (UT02-28 uses busy_timeout 100 ms).
_BUSY_TIMEOUT_MS = 10_000
_SLOW_WRITE_S = 1.0

type Params = Sequence[object] | Mapping[str, object]
type _Entry = tuple[int, Path, int, sqlite3.Connection]  # pid, path, generation, connection

_log = get_logger("store.ops")


class _Registry:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.connections: list[sqlite3.Connection] = []
        self.path_override: Path | None = None
        self.generation = 0


_registry: Final = _Registry()
_local: Final = threading.local()


def _map_error(exc: sqlite3.Error, busy: str, other: str, op: str = "") -> HernessError:
    text = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text):
        return StoreBusy(busy, op=op)
    return SchemaViolation(other, op=op)


@functools.cache
def _check_sqlite() -> None:
    probe = sqlite3.connect(":memory:")
    try:
        options = {str(row[0]) for row in probe.execute("PRAGMA compile_options")}
    finally:
        probe.close()
    if sqlite3.sqlite_version_info < _MIN_SQLITE or "ENABLE_FTS5" not in options:
        msg = "SQLite 3.38+ with FTS5 required"
        raise ConfigError(msg)


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=True
        )
        conn.execute(f"PRAGMA busy_timeout = {int(_BUSY_TIMEOUT_MS)}")
        mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        conn.executescript(_PRAGMAS)
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        raise _map_error(exc, "ops store busy while opening", "cannot open ops store") from exc
    if mode != "wal":
        conn.close()
        msg = f"ops store must use WAL journal mode, got {mode}"
        raise ConfigError(msg)
    conn.row_factory = sqlite3.Row
    return conn


def connection() -> sqlite3.Connection:
    """This thread's ops connection (U02-37); ConfigError, StoreBusy or SchemaViolation."""
    path = _registry.path_override or data_layout().ops_db
    key = (os.getpid(), path, _registry.generation)
    cached: _Entry | None = getattr(_local, "entry", None)
    if cached is not None:
        if cached[:3] == key:
            return cached[3]
        _local.entry = None
        with _registry.lock, contextlib.suppress(ValueError):
            _registry.connections.remove(cached[3])
        if cached[0] == key[0]:  # never close a handle inherited across fork
            cached[3].close()
    _check_sqlite()
    conn = _open(path)
    with _registry.lock:
        _registry.connections.append(conn)
    _local.entry = (*key, conn)
    _log.debug("store.ops.connected", thread=threading.current_thread().name)
    return conn


def run_write[T](fn: Callable[[sqlite3.Connection], T], *, op: str) -> T:
    """Run SQL-only ``fn(conn)`` in one ``BEGIN IMMEDIATE`` transaction, retried (U02-38).

    Raises StoreBusy, SchemaViolation, ConfigError (bad ``op``, nesting) or ``fn``'s error."""
    if _OP_RE.fullmatch(op) is None:
        msg = "run_write op must match ^[a-z_]{1,64}$"
        raise ConfigError(msg)
    if connection().in_transaction:
        msg = f"nested run_write in {op}"
        raise ConfigError(msg)

    def attempt() -> T:
        _shims.fault_point("sqlite.write", kind=op)
        conn = connection()
        started = clock.monotonic()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn)
            conn.execute("COMMIT")
        except BaseException as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.IntegrityError):  # names table and column, never values
                msg = f"ops constraint failed in {op}: {exc}"
                raise SchemaViolation(msg, op=op) from exc
            if isinstance(exc, sqlite3.Error):
                other = f"ops write failed in {op}: {type(exc).__name__}"
                raise _map_error(exc, f"ops store busy in {op}", other, op) from exc
            raise
        duration = clock.monotonic() - started
        if duration > _SLOW_WRITE_S:
            _log.warning("store.ops.slow_write", op=op, duration_ms=int(duration * 1000))
        return result

    return _shims.retry_call("sqlite_write", attempt)


def _read_error(exc: sqlite3.Error) -> HernessError:
    return _map_error(exc, "ops store busy during read", f"ops read failed: {type(exc).__name__}")


def read_one(sql: str, params: Params = ()) -> sqlite3.Row | None:
    """Return the first row of a parameterised query, or None (U02-39); no retry here."""
    try:
        row: sqlite3.Row | None = connection().execute(sql, params).fetchone()
    except sqlite3.Error as exc:
        raise _read_error(exc) from exc
    return row


def read_all(sql: str, params: Params = (), *, max_rows: int = 100_000) -> list[sqlite3.Row]:
    """Return at most ``max_rows`` rows; more raise SchemaViolation (U02-40, TH02-07)."""
    if not 1 <= max_rows <= _MAX_ROWS_LIMIT:
        msg = f"max_rows must be 1..{_MAX_ROWS_LIMIT}"
        raise ConfigError(msg)
    try:
        rows: list[sqlite3.Row] = connection().execute(sql, params).fetchmany(max_rows + 1)
    except sqlite3.Error as exc:
        raise _read_error(exc) from exc
    if len(rows) > max_rows:
        msg = f"read exceeded {max_rows} rows"
        raise SchemaViolation(msg)
    return rows


def _encode(value: object) -> object:  # TypeError messages carry type names only
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError(type(value).__name__)
        return clock.format_utc(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(type(value).__name__)


_dumps: Final = functools.partial(
    json.dumps,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
    default=_encode,
)


def dump_json(value: object, *, field: str, max_bytes: int = OPS_JSON_MAX_BYTES) -> str:
    """Compact sorted JSON TEXT of at most ``max_bytes`` bytes, else SchemaViolation (U02-41)."""
    if not _JSON_BYTES_RANGE[0] <= max_bytes <= _JSON_BYTES_RANGE[1]:
        msg = "dump_json max_bytes must be 1 KiB..16 MiB"
        raise ConfigError(msg)
    try:
        text: str = _dumps(value)
    except TypeError as exc:
        msg = f"JSON for {field} not serialisable: {exc}"
        raise SchemaViolation(msg) from exc
    except ValueError as exc:  # NaN/Infinity, or a circular reference
        nan = "Out of range float" in str(exc)
        msg = f"JSON for {field} " + ("contains NaN or Infinity" if nan else "is circular")
        raise SchemaViolation(msg) from exc
    if len(text.encode("utf-8")) > max_bytes:
        msg = f"JSON for {field} exceeds {max_bytes} bytes"
        raise SchemaViolation(msg)
    return text


def load_json(text: str | None, *, field: str) -> object:
    """Parse a JSON TEXT column; None stays None; Decimal strings stay strings (U02-42)."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"invalid JSON in {field}"
        raise SchemaViolation(msg) from exc


def reset_connections(*, path: Path | None = None) -> None:
    """Close registered connections, set the override, invalidate other threads' (U02-43)."""
    with _registry.lock:
        for conn in _registry.connections:
            with contextlib.suppress(sqlite3.ProgrammingError):
                conn.close()
        _registry.connections.clear()
        _registry.path_override = path
        _registry.generation += 1
