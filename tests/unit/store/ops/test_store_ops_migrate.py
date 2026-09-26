"""Unit tests for herness.store.ops.migrate and migrations 001-002 (impl 02 U02-44 … U02-50,
U02-129; UT02-33 … UT02-35, UT02-37, UT02-40 … UT02-42, UT02-70, UT02-71, ST02-15).

UT02-32 and the 003-006 parts of UT02-37 are in test_store_ops_migrations.py (T02-06). Tests
that edit or add migration files run on a temp copy of the package directory.
"""

from __future__ import annotations

import importlib
import itertools
import sqlite3
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from types import ModuleType

import pytest
from structlog.testing import capture_logs

from herness.core.errors import SchemaViolation
from herness.store.errors import MigrationError
from herness.store.ops import core
from herness.store.ops.migrate import (
    MIGRATION_RANGES,
    migrate,
    ops_health,
    pending_migrations,
    schema_version,
)

pytestmark = pytest.mark.unit

# The package attribute `herness.store.ops.migrate` is the function (U02-62), so reach the
# module through importlib for monkeypatching.
_MOD: ModuleType = importlib.import_module("herness.store.ops.migrate")
_TABLES_001_002 = {
    "watermark",
    "sync_slice",
    "file_ingest",
    "source_health",
    "job",
    "worker",
    "resilience_event",
}
_TS = "2026-09-26T10:00:00.000000Z"


def _package_sql() -> dict[str, bytes]:
    root = resources.files("herness.store.migrations")
    return {e.name: e.read_bytes() for e in root.iterdir() if e.name.endswith(".sql")}


@pytest.fixture
def mig_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ops_store: Path) -> Path:
    """A temp copy of the migrations package (001-006); the runner reads it."""
    target = tmp_path / "migrations"
    target.mkdir()
    for name, data in _package_sql().items():
        (target / name).write_bytes(data)
    assert len(list(target.glob("00[1-6]_*.sql"))) == 6
    monkeypatch.setattr(_MOD, "_migrations_root", lambda: target)
    return target


def _names(kind: str) -> set[str]:
    rows = core.read_all(
        "SELECT name FROM sqlite_schema WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,)
    )
    return {str(r["name"]) for r in rows}


def _recorded() -> list[tuple[int, str]]:
    rows = core.read_all("SELECT version, name FROM schema_migration ORDER BY version")
    return [(int(r["version"]), str(r["name"])) for r in rows]


def _write(sql: str, params: tuple[object, ...] = ()) -> None:
    core.run_write(lambda c: c.execute(sql, params), op="test_write")


# --- U02-129 MIGRATION_RANGES -------------------------------------------------------------


def test_ut02_70_ranges_table() -> None:
    """UT02-70 MIGRATION_RANGES holds the R-11 owner ranges, sorted and non-overlapping."""
    assert MIGRATION_RANGES == (
        (1, 9, "02"),
        (10, 19, "01"),
        (20, 29, "03"),
        (30, 39, "05"),
        (40, 49, "06"),
        (50, 59, "08"),
        (70, 79, "07"),
        (80, 89, "10"),
        (90, 99, "09"),
    )
    for (_, last, _), (first, _, _) in itertools.pairwise(MIGRATION_RANGES):
        assert last < first
    assert _MOD._owner_of(1) == "02"
    assert _MOD._owner_of(59) == "08"
    assert _MOD._owner_of(65) is None
    assert _MOD._owner_of(0) is None
    assert _MOD._owner_of(100) is None


# --- UT02-33 idempotent ---------------------------------------------------------------------


def test_ut02_33_migrate_again_applies_nothing(ops_store: Path) -> None:
    """UT02-33 a migrated store migrated again reports `applied == ()`."""
    first = migrate()
    again = migrate()
    assert again.applied == ()
    assert again.version == first.version
    assert pending_migrations() == []


def test_ut02_33_concurrent_apply_is_skipped(
    mig_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-33 a version another process applied is skipped by the in-transaction re-check."""
    migrate()
    monkeypatch.setattr(_MOD, "_read_applied", dict)  # as if the rows appeared after the read
    assert migrate().applied == ()
    assert len(_recorded()) == 6


# --- UT02-34 / ST02-15 checksums ------------------------------------------------------------


def test_ut02_34_edited_migration_is_rejected(mig_dir: Path) -> None:
    """UT02-34 an applied migration file edited afterwards raises checksum_mismatch."""
    migrate()
    path = mig_dir / "002_jobs.sql"
    path.write_bytes(path.read_bytes() + b"\n-- edited\n")
    with pytest.raises(MigrationError) as info:
        migrate()
    assert (info.value.version, info.value.name) == (2, "jobs")
    assert info.value.reason == "checksum_mismatch"
    assert str(info.value).startswith("ops migration 002_jobs: checksum_mismatch (")
    with pytest.raises(MigrationError):
        pending_migrations()


def test_ut02_34_crlf_line_endings_keep_the_checksum(mig_dir: Path) -> None:
    """UT02-34 the checksum is taken after CRLF -> LF, so a CRLF checkout is not an edit."""
    migrate()
    path = mig_dir / "001_ingestion_health.sql"
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert migrate().applied == ()


def test_ut02_34_applied_version_without_file(mig_dir: Path) -> None:
    """UT02-34 an applied version whose file is gone raises unknown_applied."""
    migrate()
    for file in mig_dir.glob("006_*.sql"):
        file.unlink()
    with pytest.raises(MigrationError) as info:
        migrate()
    assert (info.value.version, info.value.reason) == (6, "unknown_applied")


def test_st02_15_edited_applied_migration_stops_startup(mig_dir: Path) -> None:
    """ST02-15 editing an applied migration makes startup refuse with MigrationError."""
    migrate()
    path = mig_dir / "001_ingestion_health.sql"
    path.write_text(
        path.read_text(encoding="utf-8").replace("watermark", "watermarks", 1), encoding="utf-8"
    )
    (mig_dir / "007_later.sql").write_text(
        "CREATE TABLE later (id INTEGER) STRICT;\n", encoding="utf-8"
    )
    with pytest.raises(MigrationError, match="checksum_mismatch"):
        migrate()
    assert 7 not in dict(_recorded())
    assert "later" not in _names("table")


# --- UT02-35 apply failure ------------------------------------------------------------------


def test_ut02_35_failing_statement_rolls_back_the_file(mig_dir: Path) -> None:
    """UT02-35 a failing second statement raises apply_failed; the first is rolled back."""
    migrate()
    (mig_dir / "007_broken.sql").write_text(
        "-- first statement is fine\nCREATE TABLE first_ok (id INTEGER PRIMARY KEY) STRICT;\n"
        "CREATE TABLE first_ok (id INTEGER PRIMARY KEY) STRICT;\n",
        encoding="utf-8",
    )
    with pytest.raises(MigrationError) as info:
        migrate()
    assert (info.value.version, info.value.name, info.value.reason) == (7, "broken", "apply_failed")
    assert info.value.detail.startswith("statement 2: OperationalError")
    assert "first_ok" not in _names("table")
    assert schema_version() == 6


@pytest.mark.parametrize("statement", ["PRAGMA foreign_keys = OFF", "COMMIT", "vacuum"])
def test_ut02_35_forbidden_statement_kinds(mig_dir: Path, statement: str) -> None:
    """UT02-35 a migration with PRAGMA, a transaction verb or VACUUM is refused (ENG §3.5)."""
    migrate()
    (mig_dir / "007_bad.sql").write_text(
        f"CREATE TABLE fine (id INTEGER) STRICT;\n/* note */ -- x\n{statement};\n",
        encoding="utf-8",
    )
    with pytest.raises(MigrationError) as info:
        migrate()
    assert info.value.reason == "apply_failed"
    assert info.value.detail.startswith("statement 2:")
    assert "fine" not in _names("table")


def test_ut02_35_trigger_body_and_trailing_statement(mig_dir: Path) -> None:
    """UT02-35 a trigger body stays one statement; a last statement without `;` is applied."""
    (mig_dir / "007_trig.sql").write_text(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0) STRICT;\n"
        "CREATE TRIGGER t_ai AFTER INSERT ON t BEGIN\n"
        "  UPDATE t SET n = n + 1 WHERE id = new.id;\n"
        "END;\n"
        "CREATE INDEX t_n ON t (n)\n-- trailing comment\n",
        encoding="utf-8",
    )
    assert migrate().applied[-1] == "007_trig"
    assert {"t_ai"} <= _names("trigger")
    assert {"t_n"} <= _names("index")


def test_ut02_35_non_statement_failure_is_apply_failed(
    mig_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-35 a SchemaViolation outside the statements (the record insert) is apply_failed."""
    real: Callable[..., object] = core.run_write

    def failing(fn: Callable[[sqlite3.Connection], object], *, op: str) -> object:
        if op == "migrate":
            msg = "ops write failed in migrate: OperationalError"
            raise SchemaViolation(msg, op=op)
        return real(fn, op=op)

    monkeypatch.setattr(core, "run_write", failing)
    with pytest.raises(MigrationError) as info:
        migrate()
    assert (info.value.version, info.value.reason) == (1, "apply_failed")
    assert info.value.detail == "SchemaViolation"


# --- UT02-37 part: constraints of 001-002 ---------------------------------------------------


_JOB = (
    "INSERT INTO job (job_id, kind, gpu_class, status, priority, payload, idem_key,"
    " max_attempts, scheduled_for, created_at) VALUES (?, ?, 'none', ?, 50, ?, ?, 3, ?, ?)"
)


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (_JOB, ("j1", "bogus", "queued", "{}", None, _TS, _TS)),
        (_JOB, ("j1", "sync", "queued", "{bad", None, _TS, _TS)),
        (_JOB, ("j1", "sync", "queued", "{}", None, "2026-09-26 10:00:00", _TS)),
        (_JOB, ("j1", "sync", "queued", '"' + "x" * 65_536 + '"', None, _TS, _TS)),
        (
            "INSERT INTO sync_slice (source, entity, slice_start, slice_end, status, updated_at)"
            " VALUES ('jira', 'issue', ?, ?, 'lost', ?)",
            (_TS, _TS, _TS),
        ),
        (
            "INSERT INTO file_ingest (fingerprint, source, entity, path, size_bytes, rows,"
            " mtime, ingested_at, files) VALUES ('abc', 'f', 'e', 'p', 1, 1, ?, ?, '[]')",
            (_TS, _TS),
        ),
        (
            "INSERT INTO source_health (source, state, updated_at) VALUES ('jira', 'ajar', ?)",
            (_TS,),
        ),
        (
            "INSERT INTO worker (worker_id, host, pid, gpu_slot, cpu_slots, status, started_at,"
            " heartbeat_at, version, faults_enabled) VALUES ('w', 'h', 1, 0, 2, 'running', ?, ?,"
            " '1', 2)",
            (_TS, _TS),
        ),
        (
            "INSERT INTO resilience_event (event_id, ts, kind, component, detail)"
            " VALUES ('e', ?, 'k', 'c', 'not json')",
            (_TS,),
        ),
    ],
)
def test_ut02_37_001_002_reject_bad_values(
    ops_store: Path, sql: str, params: tuple[object, ...]
) -> None:
    """UT02-37 (001-002 part) bad enum, JSON, timestamp, size, boolean or length is rejected."""
    migrate()
    with pytest.raises(SchemaViolation):
        _write(sql, params)


def test_ut02_37_job_idem_key_unique_while_active(ops_store: Path) -> None:
    """UT02-37 a duplicate active `idem_key` is rejected and allowed after status `done`."""
    migrate()
    _write(_JOB, ("j1", "sync", "queued", "{}", "k", _TS, _TS))
    with pytest.raises(SchemaViolation, match="UNIQUE"):
        _write(_JOB, ("j2", "sync", "running", "{}", "k", _TS, _TS))
    _write("UPDATE job SET status = 'done' WHERE job_id = 'j1'")
    _write(_JOB, ("j2", "sync", "queued", "{}", "k", _TS, _TS))
    row = core.read_one("SELECT status, attempts FROM job WHERE job_id = 'j2'")
    assert row is not None
    assert (row["status"], row["attempts"]) == ("queued", 0)


# --- UT02-40 pending and version ------------------------------------------------------------


def test_ut02_40_pending_and_version_at_0_3_6(
    mig_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-40 at versions 0, 3 and 6 the pending lists and versions are correct."""
    names = sorted(p.stem for p in mig_dir.glob("00[1-6]_*.sql"))
    assert len(names) == 6
    assert schema_version() == 0
    assert pending_migrations() == names
    partial = tmp_path / "partial"
    partial.mkdir()
    for name in names[:3]:
        (partial / f"{name}.sql").write_bytes((mig_dir / f"{name}.sql").read_bytes())
    monkeypatch.setattr(_MOD, "_migrations_root", lambda: partial)
    assert migrate().applied == tuple(names[:3])
    monkeypatch.setattr(_MOD, "_migrations_root", lambda: mig_dir)
    assert schema_version() == 3
    assert pending_migrations() == names[3:]
    migrate()
    assert schema_version() == 6
    assert pending_migrations() == []


def test_ut02_40_non_migration_files_are_ignored(mig_dir: Path) -> None:
    """UT02-40 files that are not `NNN_<slug>.sql` are not migrations."""
    for name in ("README.md", "__init__.py", "_draft.sql", "007_Bad-Name.sql", "7_short.sql"):
        (mig_dir / name).write_text("garbage", encoding="utf-8")
    (mig_dir / "008_dir.sql").mkdir()
    assert len(pending_migrations()) == 6


# --- UT02-41 ops_health ---------------------------------------------------------------------


def test_ut02_41_health_ok_degraded_down(
    mig_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-41 health is ok when current, degraded when pending, down when unopenable."""
    migrate()
    assert ops_health() == ("ok", "ops store current")
    (mig_dir / "007_new.sql").write_text("CREATE TABLE n (id INTEGER) STRICT;\n", encoding="utf-8")
    assert ops_health() == ("degraded", "1 pending migrations")
    core.reset_connections(path=tmp_path)  # a directory cannot be opened as a database
    assert ops_health() == ("down", "SchemaViolation")


def test_ut02_41_health_degraded_without_wal(
    mig_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-41 a journal mode other than WAL is degraded."""
    migrate()
    real = core.read_one

    def fake(sql: str, params: tuple[object, ...] = ()) -> object:
        if sql.startswith("PRAGMA journal_mode"):
            return {"journal_mode": "delete"}
        return real(sql, params)

    monkeypatch.setattr(core, "read_one", fake)
    assert ops_health() == ("degraded", "journal_mode delete")


# --- UT02-42 STRICT -------------------------------------------------------------------------


def test_ut02_42_strict_table_rejects_text_in_integer(ops_store: Path) -> None:
    """UT02-42 inserting text into `sync_slice.rows` (STRICT) is rejected."""
    migrate()
    sql = (
        "INSERT INTO sync_slice (source, entity, slice_start, slice_end, rows, updated_at)"
        " VALUES ('jira', 'issue', ?, ?, 'many', ?)"
    )
    with pytest.raises(SchemaViolation, match="cannot store TEXT value in INTEGER column"):
        _write(sql, (_TS, _TS, _TS))
    _write(sql.replace("'many'", "7"), (_TS, _TS, _TS))


# --- UT02-70 owner ranges -------------------------------------------------------------------


def test_ut02_70_out_of_range_file_stops_migrate(mig_dir: Path) -> None:
    """UT02-70 a file numbered 065 (no owner) raises out_of_range; nothing is applied."""
    (mig_dir / "065_x.sql").write_text("CREATE TABLE x (id INTEGER) STRICT;\n", encoding="utf-8")
    with pytest.raises(MigrationError) as info:
        migrate()
    assert (info.value.version, info.value.name, info.value.reason) == (65, "x", "out_of_range")
    assert _recorded() == []
    assert not _names("table") & (_TABLES_001_002 | {"x"})


@pytest.mark.parametrize(
    "name", ["012_Add-Col.sql", "12_x.sql", "012_x.SQL", "\u0660\u0661\u0662_arabic.sql"]
)
def test_ut02_70_misnamed_migration_warns_and_is_not_applied(mig_dir: Path, name: str) -> None:
    """UT02-70 a digit-leading file that is not `NNN_<slug>.sql` (incl. non-ASCII digits) is
    skipped with a `store.ops.migration_name_ignored` warning; nothing from it is applied."""
    (mig_dir / name).write_text("CREATE TABLE misnamed (id INTEGER) STRICT;\n", encoding="utf-8")
    with capture_logs() as logs:
        report = migrate()
    ignored = [e for e in logs if e["event"] == "store.ops.migration_name_ignored"]
    assert [(e["file"], e["log_level"]) for e in ignored] == [(name, "warning")]
    assert len(report.applied) == 6
    assert "misnamed" not in _names("table")
    assert all(version <= 6 for version, _ in _recorded())


def test_ut02_70_duplicate_version_stops_migrate(mig_dir: Path) -> None:
    """UT02-70 two files numbered 012 raise duplicate_version; nothing is applied."""
    (mig_dir / "012_a.sql").write_text("CREATE TABLE a (id INTEGER) STRICT;\n", encoding="utf-8")
    (mig_dir / "012_b.sql").write_text("CREATE TABLE b (id INTEGER) STRICT;\n", encoding="utf-8")
    with pytest.raises(MigrationError) as info:
        migrate()
    assert (info.value.version, info.value.reason) == (12, "duplicate_version")
    assert _recorded() == []
    assert not _names("table") & (_TABLES_001_002 | {"a", "b"})


# --- UT02-71 out-of-order owner migration ---------------------------------------------------


def test_ut02_71_lower_owner_migration_after_higher(mig_dir: Path) -> None:
    """UT02-71 012_b added after 070 is applied in numeric order and logged out of order."""
    (mig_dir / "070_a.sql").write_text("CREATE TABLE a70 (id INTEGER) STRICT;\n", encoding="utf-8")
    assert migrate().applied[-1] == "070_a"
    (mig_dir / "071_c.sql").write_text("CREATE TABLE c71 (id INTEGER) STRICT;\n", encoding="utf-8")
    (mig_dir / "012_b.sql").write_text("CREATE TABLE b12 (id INTEGER) STRICT;\n", encoding="utf-8")
    assert pending_migrations() == ["012_b", "071_c"]
    with capture_logs() as logs:
        report = migrate()
    assert report.applied == ("012_b", "071_c")
    out_of_order = [e for e in logs if e["event"] == "store.ops.migration_out_of_order"]
    assert [(e["version"], e["name"], e["highest_applied"]) for e in out_of_order] == [
        (12, "b", 70)
    ]
    assert out_of_order[0]["log_level"] == "info"
    migrated = [e for e in logs if e["event"] == "store.ops.migrated"]
    assert [(e["version"], e["owner"]) for e in migrated] == [(12, "01"), (71, "07")]
    assert all(isinstance(e["duration_ms"], int) for e in migrated)
    assert schema_version() == 71
    assert report.version == 71
