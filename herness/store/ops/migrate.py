"""Forward-only ops migration runner over every owner range and ops health (impl 02 §3.6).

Units U02-44 … U02-48 and U02-129 (R-11). Migration files live in the package resource
``herness.store.migrations`` and are named ``NNN_<slug>.sql``; each number lies in the range
of its owning spec (``MIGRATION_RANGES``). Pending files are applied in numeric order, one
``run_write`` transaction per file, and recorded in ``schema_migration`` with the SHA-256 of
their LF-normalised bytes; an applied file that changes stops startup (TH02-15).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Final, Literal

from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.core.logging import get_logger
from herness.store.errors import MigrationError

from . import core

MIGRATION_RANGES: Final[tuple[tuple[int, int, str], ...]] = (
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

_PACKAGE: Final = "herness.store.migrations"
_FILE_RE: Final = re.compile(r"([0-9]{3})_([a-z0-9_]+)\.sql")
_LEADING_COMMENTS: Final = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.DOTALL)
# ENG §3.5: migration files never manage transactions, PRAGMAs or attachments themselves.
_FORBIDDEN: Final = re.compile(
    r"(PRAGMA|BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE|ATTACH|DETACH|VACUUM)\b",
    re.IGNORECASE,
)
_BOOTSTRAP: Final = (
    "CREATE TABLE IF NOT EXISTS schema_migration (version INTEGER PRIMARY KEY,"
    " name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL) STRICT"
)
_HAS_TABLE: Final = "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'schema_migration'"

type HealthStatus = Literal["ok", "degraded", "down"]

_log = get_logger("store.ops")


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Result of ``migrate()`` (U02-44): names applied now, highest applied version, time."""

    applied: tuple[str, ...]
    version: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class _Migration:
    version: int
    slug: str
    sql: str
    checksum: str

    @property
    def name(self) -> str:
        return f"{self.version:03d}_{self.slug}"


def _owner_of(version: int) -> str | None:
    """The owner spec of the range that contains ``version``, else None (U02-129)."""
    for first, last, owner in MIGRATION_RANGES:
        if first <= version <= last:
            return owner
    return None


def _migrations_root() -> Traversable:
    """The migrations directory; tests point it at a temp copy."""
    return resources.files(_PACKAGE)


def _discover() -> list[_Migration]:
    """Parse every ``NNN_<slug>.sql`` file, sorted by version (U02-45 step 2)."""
    found: dict[int, _Migration] = {}
    for entry in sorted(_migrations_root().iterdir(), key=lambda e: e.name):
        match = _FILE_RE.fullmatch(entry.name)
        if match is None or not entry.is_file():
            if entry.name[:1].isdigit() and match is None:  # likely a mis-named migration
                _log.warning("store.ops.migration_name_ignored", file=entry.name)
            continue  # not a migration file (README, __init__.py, drafts starting with "_")
        version, slug = int(match[1]), match[2]
        if _owner_of(version) is None:
            raise MigrationError(version, slug, "out_of_range", "no owner range holds it")
        if version in found:
            other = found[version].name
            raise MigrationError(version, slug, "duplicate_version", f"also {other}")
        data = entry.read_bytes().replace(b"\r\n", b"\n")
        checksum = hashlib.sha256(data).hexdigest()
        found[version] = _Migration(version, slug, data.decode("utf-8"), checksum)
    return [found[v] for v in sorted(found)]


def _read_applied() -> dict[int, tuple[str, str]]:
    """Applied ``version -> (name, checksum)``; empty when ``schema_migration`` is missing."""
    if core.read_one(_HAS_TABLE) is None:
        return {}
    rows = core.read_all("SELECT version, name, checksum FROM schema_migration")
    return {int(r["version"]): (str(r["name"]), str(r["checksum"])) for r in rows}


def _pending(discovered: list[_Migration], applied: dict[int, tuple[str, str]]) -> list[_Migration]:
    """Verify applied rows against the files (U02-45 step 3) and return the pending files."""
    files = {m.version: m for m in discovered}
    for version, (name, checksum) in sorted(applied.items()):
        migration = files.get(version)
        if migration is None:
            raise MigrationError(version, name, "unknown_applied", "database newer than code")
        if migration.checksum != checksum:
            detail = "applied file changed; add a new migration instead"
            raise MigrationError(version, migration.slug, "checksum_mismatch", detail)
    return [m for m in discovered if m.version not in applied]


def _statements(sql: str) -> list[str]:
    """Split a file into statements with ``sqlite3.complete_statement`` (triggers stay whole)."""
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if _LEADING_COMMENTS.sub("", buffer, count=1).strip():
        statements.append(buffer.strip())  # a last statement without its ";"
    return statements


class _Apply:
    """``run_write`` callback applying one file; remembers the statement being executed."""

    def __init__(self, migration: _Migration) -> None:
        self.migration = migration
        self.index = 0

    def __call__(self, conn: sqlite3.Connection) -> bool:
        m = self.migration
        self.index = 0
        seen = "SELECT 1 FROM schema_migration WHERE version = ?"
        if conn.execute(seen, (m.version,)).fetchone() is not None:
            return False  # another process applied it after our read (U02-45 step 5)
        for index, statement in enumerate(_statements(m.sql), start=1):
            self.index = index
            if _FORBIDDEN.match(_LEADING_COMMENTS.sub("", statement, count=1)):
                detail = f"statement {index}: statement kind not allowed in a migration"
                raise MigrationError(m.version, m.slug, "apply_failed", detail)
            conn.execute(statement)
        self.index = 0
        conn.execute(
            "INSERT INTO schema_migration (version, name, checksum, applied_at)"
            " VALUES (?, ?, ?, ?)",
            (m.version, m.slug, m.checksum, clock.format_utc(clock.now())),
        )
        return True


def _apply(migration: _Migration) -> bool:
    """Apply one file in one transaction; a SchemaViolation becomes ``apply_failed``."""
    callback = _Apply(migration)
    try:
        return core.run_write(callback, op="migrate")
    except MigrationError:
        raise
    except SchemaViolation as exc:
        cause = exc.__cause__
        detail = f"{type(cause).__name__}: {cause}" if cause is not None else type(exc).__name__
        if callback.index:
            detail = f"statement {callback.index}: {detail}"
        raise MigrationError(migration.version, migration.slug, "apply_failed", detail) from exc


def _bootstrap(conn: sqlite3.Connection) -> None:
    conn.execute(_BOOTSTRAP)


def migrate() -> MigrationReport:
    """Apply every pending migration of every owner range in numeric order (U02-45).

    Raises MigrationError, StoreBusy after retries, or ConfigError from ``connection()``."""
    started = clock.monotonic()
    core.run_write(_bootstrap, op="migrate_bootstrap")
    applied = _read_applied()
    pending = _pending(_discover(), applied)
    highest = max(applied, default=0)
    done: list[str] = []
    for migration in pending:
        if migration.version < highest:
            _log.info(
                "store.ops.migration_out_of_order",
                version=migration.version,
                name=migration.slug,
                highest_applied=highest,
            )
        file_started = clock.monotonic()
        if _apply(migration):
            done.append(migration.name)
            _log.info(
                "store.ops.migrated",
                version=migration.version,
                name=migration.slug,
                owner=_owner_of(migration.version),
                duration_ms=int((clock.monotonic() - file_started) * 1000),
            )
        highest = max(highest, migration.version)
    duration_ms = int((clock.monotonic() - started) * 1000)
    return MigrationReport(tuple(done), schema_version(), duration_ms)


def pending_migrations() -> list[str]:
    """Names ``NNN_<slug>`` not yet applied, in numeric order (U02-46); [] when current."""
    return [m.name for m in _pending(_discover(), _read_applied())]


def schema_version() -> int:
    """Highest applied migration version, 0 when none (U02-47)."""
    if core.read_one(_HAS_TABLE) is None:
        return 0
    row = core.read_one("SELECT coalesce(max(version), 0) AS v FROM schema_migration")
    return int(row["v"]) if row is not None else 0


def ops_health() -> tuple[HealthStatus, str]:
    """``down`` when the store cannot be read, ``degraded`` when pending migrations or not WAL,
    else ``ok`` (U02-48). Never raises; a failure's reason is its error class name."""
    try:
        core.read_one("SELECT 1")
        row = core.read_one("PRAGMA journal_mode")
        pending = pending_migrations()
    except Exception as exc:  # noqa: BLE001 - U02-48 maps every failure to "down"
        return "down", type(exc).__name__
    mode = str(row["journal_mode"]).lower() if row is not None else "unknown"
    if pending:
        return "degraded", f"{len(pending)} pending migrations"
    if mode != "wal":
        return "degraded", f"journal_mode {mode}"
    return "ok", "ops store current"
