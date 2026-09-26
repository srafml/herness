"""Unit tests for herness.store.ops.core (impl 02 U02-36 … U02-43; UT02-25 … UT02-31)."""

from __future__ import annotations

import datetime
import decimal
import enum
import math
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from herness.core.errors import ConfigError, NotFound, SchemaViolation, StoreBusy
from herness.core.resilience import ProcessState
from herness.store.ops import _shims, core

pytestmark = pytest.mark.unit

_TABLE = "CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)"


def _create_table(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE)


@pytest.fixture
def item_table(ops_store: Path) -> Path:
    core.run_write(_create_table, op="create_item")
    return ops_store


def _count() -> int:
    row = core.read_one("SELECT count(*) AS n FROM item")
    assert row is not None
    return int(row["n"])


# --- UT02-25 connection PRAGMAs --------------------------------------------------------


def test_ut02_25_connection_pragmas(ops_store: Path) -> None:
    """UT02-25 the connection carries every PRAGMA of U02-37, Row factory and autocommit."""
    conn = core.connection()
    pragma = {
        name: conn.execute(f"PRAGMA {name}").fetchone()[0]
        for name in (
            "journal_mode",
            "foreign_keys",
            "busy_timeout",
            "synchronous",
            "trusted_schema",
            "temp_store",
            "cache_size",
        )
    }
    assert pragma == {
        "journal_mode": "wal",
        "foreign_keys": 1,
        "busy_timeout": 10000,
        "synchronous": 1,
        "trusted_schema": 0,
        "temp_store": 2,
        "cache_size": -65536,
    }
    assert conn.row_factory is sqlite3.Row
    assert conn.isolation_level is None
    assert ops_store.is_file()
    assert core.OPS_JSON_MAX_BYTES == 65_536


def test_ut02_25_old_sqlite_rejected(ops_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-25 SQLite older than 3.38 is a ConfigError naming the requirement."""
    core._check_sqlite.cache_clear()
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 37, 2))
    with pytest.raises(ConfigError, match=r"SQLite 3\.38\+ with FTS5 required"):
        core.connection()
    core._check_sqlite.cache_clear()


def test_ut02_25_non_wal_rejected(tmp_path: Path) -> None:
    """UT02-25 a database that cannot switch to WAL (in-memory) is a ConfigError."""
    core.reset_connections(path=Path(":memory:"))
    try:
        with pytest.raises(ConfigError, match="WAL"):
            core.connection()
    finally:
        core.reset_connections()


def test_ut02_25_locked_while_opening_is_store_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-25 PRAGMAs that hit `database is locked` raise StoreBusy."""
    db_path = tmp_path / "ops.sqlite"
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("CREATE TABLE t (x INTEGER)")
    holder.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(core, "_BUSY_TIMEOUT_MS", 50)
    core.reset_connections(path=db_path)
    try:
        with pytest.raises(StoreBusy):
            core.connection()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        core.reset_connections()


def test_ut02_25_parent_not_creatable_is_schema_violation(tmp_path: Path) -> None:
    """UT02-25 an OSError creating the parent directory is SchemaViolation("cannot open ...")."""
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    core.reset_connections(path=blocker / "sub" / "ops.sqlite")
    try:
        with pytest.raises(SchemaViolation, match="cannot open ops store"):
            core.connection()
    finally:
        core.reset_connections()


def test_ut02_25_unopenable_path_is_schema_violation(tmp_path: Path) -> None:
    """UT02-25 a path that is a directory is a SchemaViolation, not a raw sqlite3 error."""
    core.reset_connections(path=tmp_path)
    try:
        with pytest.raises(SchemaViolation, match="cannot open ops store"):
            core.connection()
    finally:
        core.reset_connections()


# --- UT02-26 one connection per thread -------------------------------------------------


def test_ut02_26_connection_per_thread(ops_store: Path) -> None:
    """UT02-26 different objects in two threads; the same object on repeat in one thread."""
    seen: dict[str, sqlite3.Connection] = {}

    def worker() -> None:
        first = core.connection()
        assert core.connection() is first
        seen["worker"] = first

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    main = core.connection()
    assert core.connection() is main
    assert seen["worker"] is not main


def test_ut02_26_foreign_pid_entry_not_reused(
    ops_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-26 a cached connection from another pid (fork) is replaced, not reused."""
    first = core.connection()
    monkeypatch.setattr(core.os, "getpid", lambda: -1)
    second = core.connection()
    assert second is not first


# --- UT02-27 transactions and bounded reads -------------------------------------------


def test_ut02_27_callback_raising_commits_nothing(item_table: Path) -> None:
    """UT02-27 a callback that inserts then raises leaves nothing committed."""

    def insert_then_fail(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO item (name) VALUES ('a')")
        msg = "boom"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        core.run_write(insert_then_fail, op="insert_item")
    assert _count() == 0
    assert not core.connection().in_transaction


def test_ut02_27_run_write_returns_value_and_commits(item_table: Path) -> None:
    """UT02-27 run_write returns the callback value and commits its writes."""

    def insert(conn: sqlite3.Connection) -> int:
        cur = conn.execute("INSERT INTO item (name) VALUES ('a')")
        assert cur.lastrowid is not None
        return cur.lastrowid

    assert core.run_write(insert, op="insert_item") == 1
    row = core.read_one("SELECT name FROM item WHERE id = ?", (1,))
    assert row is not None
    assert row["name"] == "a"
    assert core.read_one("SELECT name FROM item WHERE id = :id", {"id": 9}) is None


def test_ut02_27_read_all_cap(item_table: Path) -> None:
    """UT02-27 read_all returns up to max_rows and raises beyond it."""

    def insert_five(conn: sqlite3.Connection) -> None:
        conn.executemany("INSERT INTO item (name) VALUES (?)", [(f"n{i}",) for i in range(5)])

    core.run_write(insert_five, op="insert_item")
    rows = core.read_all("SELECT name FROM item ORDER BY id", max_rows=5)
    assert [r["name"] for r in rows] == ["n0", "n1", "n2", "n3", "n4"]
    with pytest.raises(SchemaViolation, match="read exceeded 3 rows"):
        core.read_all("SELECT name FROM item", max_rows=3)
    assert len(core.read_all("SELECT name FROM item")) == 5


@pytest.mark.parametrize("max_rows", [0, 1_000_001])
def test_ut02_27_read_all_max_rows_range(item_table: Path, max_rows: int) -> None:
    """UT02-27 max_rows outside 1..1,000,000 is a ConfigError."""
    with pytest.raises(ConfigError, match="max_rows"):
        core.read_all("SELECT name FROM item", max_rows=max_rows)


def test_ut02_27_read_errors_mapped(item_table: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-27 read errors: bad SQL → SchemaViolation; locked → StoreBusy (no retry)."""
    with pytest.raises(SchemaViolation, match="ops read failed: OperationalError"):
        core.read_one("SELECT nope FROM item")
    with pytest.raises(SchemaViolation):
        core.read_all("SELECT nope FROM item")

    class _Locked:
        def execute(self, *_args: object) -> None:
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(core, "connection", _Locked)
    with pytest.raises(StoreBusy):
        core.read_one("SELECT 1")
    with pytest.raises(StoreBusy):
        core.read_all("SELECT 1")


def test_ut02_27_write_errors_mapped(item_table: Path) -> None:
    """UT02-27 constraint and other sqlite errors → SchemaViolation; a HernessError is kept."""

    def dup(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO item (name) VALUES ('x')")
        conn.execute("INSERT INTO item (name) VALUES ('x')")

    with pytest.raises(SchemaViolation, match=r"ops constraint failed in insert_item: UNIQUE"):
        core.run_write(dup, op="insert_item")

    def bad_sql(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO missing VALUES (1)")

    with pytest.raises(SchemaViolation, match="ops write failed in insert_item: OperationalError"):
        core.run_write(bad_sql, op="insert_item")

    def not_found(_conn: sqlite3.Connection) -> None:
        msg = "gone"
        raise NotFound(msg)

    with pytest.raises(NotFound, match="gone"):
        core.run_write(not_found, op="insert_item")
    assert _count() == 0


@pytest.mark.parametrize("op", ["", "Bad", "has-dash", "x" * 65])
def test_ut02_27_invalid_op_rejected(item_table: Path, op: str) -> None:
    """UT02-27 op must match ^[a-z_]{1,64}$."""
    with pytest.raises(ConfigError, match="op"):
        core.run_write(_create_table, op=op)


def test_ut02_27_slow_write_logged(item_table: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-27 a transaction above the slow-write threshold logs store.ops.slow_write."""
    monkeypatch.setattr(core, "_SLOW_WRITE_S", -1.0)
    with capture_logs() as logs:
        core.run_write(lambda conn: conn.execute("DELETE FROM item"), op="clear_item")
    slow = [e for e in logs if e["event"] == "store.ops.slow_write"]
    assert len(slow) == 1
    assert slow[0]["op"] == "clear_item"
    assert slow[0]["log_level"] == "warning"
    assert isinstance(slow[0]["duration_ms"], int)


# --- UT02-28 busy retry and nesting ----------------------------------------------------


def test_ut02_28_busy_writer_retried(item_table: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-28 another connection holds BEGIN IMMEDIATE for 0.5 s: run_write retries, succeeds."""
    monkeypatch.setattr(core, "_BUSY_TIMEOUT_MS", 100)
    core.reset_connections(path=item_table)
    attempts: list[int] = []
    real_retry = _shims.retry_call

    def counting_retry(name: str, fn: Callable[[], object]) -> object:
        def counted() -> object:
            attempts.append(1)
            return fn()

        return real_retry(name, counted)

    monkeypatch.setattr(_shims, "retry_call", counting_retry)
    locked = threading.Event()

    def hold() -> None:
        other = sqlite3.connect(item_table, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        locked.set()
        time.sleep(0.5)
        other.execute("COMMIT")
        other.close()

    thread = threading.Thread(target=hold)
    thread.start()
    assert locked.wait(5)
    core.run_write(lambda conn: conn.execute("INSERT INTO item (name) VALUES ('a')"), op="insert")
    thread.join()
    assert len(attempts) >= 2
    assert _count() == 1


def test_ut02_28_nested_run_write_is_config_error(item_table: Path) -> None:
    """UT02-28 nested run_write → ConfigError; the outer transaction still commits."""

    def outer(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO item (name) VALUES ('outer')")
        with pytest.raises(ConfigError, match="nested run_write in inner"):
            core.run_write(_create_table, op="inner")

    core.run_write(outer, op="outer")
    assert _count() == 1


def test_ut02_28_nested_run_write_not_retried(
    item_table: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-28 the nested ConfigError is raised on the inner call's single attempt."""
    calls: list[str] = []
    monkeypatch.setattr(_shims, "fault_point", lambda _name, **labels: calls.append(labels["kind"]))

    def outer(conn: sqlite3.Connection) -> None:
        with pytest.raises(ConfigError, match="nested run_write in inner"):
            core.run_write(_create_table, op="inner")

    core.run_write(outer, op="outer")
    assert calls == ["outer", "inner"]


def test_ut02_28_busy_while_opening_is_retried(
    ops_store: Path, reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-28 StoreBusy while a thread first opens its connection is retried by run_write."""
    real_open = core._open
    opens: list[Path] = []

    def busy_once(path: Path) -> sqlite3.Connection:
        opens.append(path)
        if len(opens) == 1:
            msg = "ops store busy while opening"
            raise StoreBusy(msg)
        return real_open(path)

    monkeypatch.setattr(core, "_open", busy_once)
    core.run_write(_create_table, op="create_item")
    assert len(opens) == 2
    assert _count() == 0


class _RollbackFails:
    """Connection proxy whose ROLLBACK raises (the real connection stays usable)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        if sql == "ROLLBACK":
            msg = "disk I/O error"
            raise sqlite3.OperationalError(msg)
        return self._conn.execute(sql, *args)


def test_ut02_27_failed_rollback_keeps_original_error(
    item_table: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-27 a failing ROLLBACK is logged; the original error is kept and mapped."""
    real = core.connection()
    monkeypatch.setattr(core, "connection", lambda: _RollbackFails(real))

    def dup(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO item (name) VALUES ('x')")
        conn.execute("INSERT INTO item (name) VALUES ('x')")

    def fail(conn: sqlite3.Connection) -> None:
        msg = "boom"
        raise RuntimeError(msg)

    with capture_logs() as logs:
        with pytest.raises(SchemaViolation, match="ops constraint failed in insert_item"):
            core.run_write(dup, op="insert_item")
        real.execute("ROLLBACK")
        with pytest.raises(RuntimeError, match="boom"):
            core.run_write(fail, op="insert_item")
        real.execute("ROLLBACK")
    failed = [e for e in logs if e["event"] == "store.ops.rollback_failed"]
    assert [(e["op"], e["error"]) for e in failed] == [("insert_item", "OperationalError")] * 2


# --- UT02-29 dump_json ----------------------------------------------------------------


class _Colour(enum.Enum):
    RED = "red"


class _Model(BaseModel):
    when: datetime.date
    amount: decimal.Decimal


def test_ut02_29_dump_json_encodings() -> None:
    """UT02-29 Decimal, aware datetime, Path, enum and model are encoded as specified."""
    aware = datetime.datetime(
        2026, 9, 25, 12, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=2))
    )
    value = {
        "b_decimal": decimal.Decimal("12.50"),
        "a_ts": aware,
        "c_path": PurePosixPath("a/b.txt"),
        "d_enum": _Colour.RED,
        "e_model": _Model(when=datetime.date(2026, 1, 2), amount=decimal.Decimal("1.5")),
        "f_date": datetime.date(2026, 1, 2),
        "g_text": "é",
    }
    text = core.dump_json(value, field="payload")
    assert text == (
        '{"a_ts":"2026-09-25T10:30:00.000000Z","b_decimal":"12.50","c_path":"a/b.txt",'
        '"d_enum":"red","e_model":{"amount":"1.5","when":"2026-01-02"},'
        '"f_date":"2026-01-02","g_text":"é"}'
    )


def test_ut02_29_windows_path_is_posix() -> None:
    """UT02-29 a Path is written as a POSIX string."""
    assert core.dump_json([Path("a") / "b"], field="files") == '["a/b"]'


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ({1, 2}, "JSON for payload not serialisable: set"),
        (datetime.datetime(2026, 1, 1), "JSON for payload not serialisable: datetime"),  # noqa: DTZ001 - naive on purpose
        (math.nan, "JSON for payload contains NaN or Infinity"),
        (math.inf, "JSON for payload contains NaN or Infinity"),
        ("x" * (70 * 1024), "JSON for payload exceeds 65536 bytes"),
    ],
    ids=["set", "naive", "nan", "inf", "oversize"],
)
def test_ut02_29_dump_json_rejects(value: object, pattern: str) -> None:
    """UT02-29 set, naive datetime, NaN, Infinity and a 70 KiB string raise SchemaViolation."""
    with pytest.raises(SchemaViolation, match=pattern):
        core.dump_json(value, field="payload")


def test_ut02_29_max_bytes_explicit() -> None:
    """UT02-29 a caller with a larger contract passes max_bytes; the range is 1 KiB to 16 MiB."""
    big = "x" * (70 * 1024)
    assert core.dump_json(big, field="checkpoint", max_bytes=4 * 1024 * 1024) == f'"{big}"'
    for bad in (1023, 16 * 1024 * 1024 + 1):
        with pytest.raises(ConfigError, match="max_bytes"):
            core.dump_json(1, field="payload", max_bytes=bad)


def test_ut02_29_circular_reference_rejected() -> None:
    """UT02-29 a circular structure is a SchemaViolation, not a raw ValueError."""
    loop: list[object] = []
    loop.append(loop)
    with pytest.raises(SchemaViolation, match="payload"):
        core.dump_json(loop, field="payload")


# --- UT02-30 load_json ----------------------------------------------------------------


def test_ut02_30_load_json_invalid_names_field() -> None:
    """UT02-30 `"{bad"` raises SchemaViolation naming the field."""
    with pytest.raises(SchemaViolation, match="invalid JSON in labels"):
        core.load_json("{bad", field="labels")


def _deep_list(depth: int) -> list[object]:
    value: list[object] = []
    for _ in range(depth):
        value = [value]
    return value


def test_ut02_29_deep_nesting_rejected() -> None:
    """UT02-29 JSON nested beyond the recursion limit is a SchemaViolation naming the field."""
    with pytest.raises(SchemaViolation, match="JSON for payload is nested too deeply"):
        core.dump_json(_deep_list(100_000), field="payload", max_bytes=16 * 1024 * 1024)


def test_ut02_30_load_json_deep_nesting_rejected() -> None:
    """UT02-30 deeply nested JSON text is a SchemaViolation naming the field."""
    with pytest.raises(SchemaViolation, match="invalid JSON in labels"):
        core.load_json("[" * 100_000 + "]" * 100_000, field="labels")


def test_ut02_30_load_json_values() -> None:
    """UT02-30 None loads as None; Decimal strings stay strings."""
    assert core.load_json(None, field="labels") is None
    assert core.load_json('{"a":"1.50","b":[1,2]}', field="labels") == {"a": "1.50", "b": [1, 2]}


# --- UT02-31 reset_connections --------------------------------------------------------


def test_ut02_31_reset_closes_and_switches_path(tmp_path: Path) -> None:
    """UT02-31 reset closes the old connections; the next connection uses the new path."""
    first_path, second_path = tmp_path / "one" / "ops.sqlite", tmp_path / "two" / "ops.sqlite"
    core.reset_connections(path=first_path)
    try:
        old = core.connection()
        other: dict[str, sqlite3.Connection] = {}
        opened, release = threading.Event(), threading.Event()

        def worker() -> None:
            other["conn"] = core.connection()
            opened.set()
            release.wait(5)

        thread = threading.Thread(target=worker)
        thread.start()
        assert opened.wait(5)
        core.reset_connections(path=second_path)
        release.set()
        thread.join()
        with pytest.raises(sqlite3.ProgrammingError):
            old.execute("SELECT 1")
        new = core.connection()
        assert new is not old
        files = [row["file"] for row in new.execute("PRAGMA database_list")]
        assert Path(files[0]) == second_path.resolve()
        assert second_path.is_file()
    finally:
        core.reset_connections()


def test_ut02_31_reset_same_path_invalidates_other_threads(tmp_path: Path) -> None:
    """UT02-31 after reset to the same path, another thread's entry is not reused."""
    db_path = tmp_path / "ops.sqlite"
    core.reset_connections(path=db_path)
    try:
        seen: list[sqlite3.Connection] = []
        step, go = threading.Event(), threading.Event()

        def worker() -> None:
            seen.append(core.connection())
            step.set()
            go.wait(5)
            seen.append(core.connection())

        thread = threading.Thread(target=worker)
        thread.start()
        assert step.wait(5)
        core.reset_connections(path=db_path)
        go.set()
        thread.join()
        assert seen[0] is not seen[1]
    finally:
        core.reset_connections()
