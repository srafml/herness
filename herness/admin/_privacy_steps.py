"""Step bodies of privacy deletion and the ops-store hygiene they need (impl 10 U10-99 steps
2, 3, 3b, 4, 5 and 7; TH10-42, TH10-50). Private sibling of ``herness.admin.privacy``.

Deleted ops-store bytes must survive neither in freed pages nor in old WAL frames: every step
runs with ``secure_delete`` on, the findings scrub sets it inside its own ``run_write``
attempt, a step whose connection was reopened mid-step (``run_write`` drops a connection
whose ROLLBACK failed) rewrites the file with ``VACUUM``, and steps 4 and 7 truncate the WAL.
A busy or locked SQLite answer to any of these statements is ``StoreBusy`` (retried). Record
IDs and record text are never logged or put into a message.
"""

from __future__ import annotations

import dataclasses
import datetime
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Final

from herness.core.config import HernessConfig
from herness.core.errors import HernessError, StoreBusy
from herness.enrich import purge_record
from herness.store.lake_purge import purge_record_ids
from herness.store.layout import DataLayout
from herness.store.ops.core import connection, run_write
from herness.store.ops.evidence import scrub_record_from_evidence
from herness.store.ops.findings import scrub_record_from_findings
from herness.store.warehouse import list_builds

__all__ = [
    "TRACE_CHUNK",
    "built_at",
    "caches",
    "lake",
    "memory",
    "older_builds",
    "reopened",
    "scrub",
    "secure",
    "vacuum",
]

type Counts = dict[str, int]

TRACE_CHUNK: Final = 1 << 20
_BUSY_CODES: Final = frozenset({5, 6})  # SQLITE_BUSY, SQLITE_LOCKED (primary result codes)
_BUILD_TIME: Final = "%Y%m%d-%H%M%S"
_SECURE: Final = "PRAGMA secure_delete = ON"


def _pragma(conn: sqlite3.Connection, sql: str) -> tuple[object, ...] | None:
    """Run one maintenance statement outside ``run_write``; busy or locked is StoreBusy."""
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.OperationalError as exc:
        if (getattr(exc, "sqlite_errorcode", 0) & 0xFF) in _BUSY_CODES:
            msg = "ops store busy"
            raise StoreBusy(msg) from exc
        raise
    return None if row is None else tuple(row)


def secure() -> sqlite3.Connection:
    """Turn ``secure_delete`` on for this thread's ops connection and return it."""
    conn = connection()
    _pragma(conn, _SECURE)
    return conn


def reopened(conn: sqlite3.Connection) -> bool:
    """Whether the thread's connection is no longer ``conn`` or lost ``secure_delete``
    (fail closed: True when it cannot be checked)."""
    try:
        current = connection()
        row = _pragma(current, "PRAGMA secure_delete")
    except (HernessError, sqlite3.Error):
        return True
    return current is not conn or row is None or row[0] != 1


def _seal() -> None:
    """Fold the WAL into the main file and truncate it (old frames hold deleted rows)."""
    row = _pragma(connection(), "PRAGMA wal_checkpoint(TRUNCATE)")
    if row is None or row[0]:
        msg = "ops store checkpoint busy"
        raise StoreBusy(msg)


def vacuum() -> None:
    """Rewrite the ops file so pages freed without ``secure_delete`` are gone, then seal."""
    conn = secure()
    _pragma(conn, "VACUUM")
    _seal()


def lake(record_id: str, counts: Counts) -> None:
    """A lake pass (T02-03, R-57); counts include ``rows_removed``."""
    result = purge_record_ids([record_id])
    counts.update({k: int(v) for k, v in dataclasses.asdict(result).items()})


def caches(record_id: str, counts: Counts) -> None:
    """Decision cache, labels and vectors (T03-34)."""
    counts.update({k: int(v) for k, v in purge_record(record_id).items()})


def memory(memory_purge: Callable[[str], int], record_id: str, counts: Counts) -> None:
    """Memory items, memory vectors and FTS rows citing the record (step 3b, R-54)."""
    counts["memory_items"] = int(memory_purge(record_id))


def _holds(path: Path, needle: bytes) -> bool:
    """Whether the bytes of ``path`` contain ``needle`` (chunked; overlap spans chunk ends)."""
    tail = b""
    with path.open("rb") as fh:
        while chunk := fh.read(TRACE_CHUNK):
            if needle in tail + chunk:
                return True
            tail = chunk[-(len(needle) - 1) :] if len(needle) > 1 else b""
    return False


def _traces(cfg: HernessConfig, record_id: str) -> int:
    """Delete ``data/traces/*.jsonl`` whose bytes contain the record ID (whole files, by byte
    substring as U10-99 says: a longer ID with this prefix also matches, fail closed)."""
    needle, deleted = record_id.encode("utf-8"), 0
    for path in sorted((Path(cfg.paths.data) / "traces").glob("*.jsonl")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if _holds(path, needle):
                path.unlink()
                deleted += 1
        except PermissionError as exc:  # a trace writer or scanner holds the file
            msg = "trace file busy"
            raise StoreBusy(msg) from exc
    return deleted


def _findings(record_id: str, conn: sqlite3.Connection) -> int:
    conn.execute(_SECURE)  # every run_write attempt, also on a reopened connection
    return scrub_record_from_findings(record_id, conn=conn)


def scrub(cfg: HernessConfig, record_id: str, counts: Counts) -> None:
    """Step 4: evidence (T05-12), findings (T06-06, R-77), traces; then truncate the WAL."""
    counts["evidence_rows"] = scrub_record_from_evidence(record_id)
    counts["finding_rows"] = run_write(
        lambda conn: _findings(record_id, conn), op="privacy_scrub_findings"
    )
    counts["trace_files"] = _traces(cfg, record_id)
    _seal()


def built_at(build_id: str) -> datetime.datetime:
    """The UTC start time encoded in a build ID (``YYYYMMDD-HHMMSS-…``)."""
    return datetime.datetime.strptime(build_id[:15], _BUILD_TIME).replace(tzinfo=datetime.UTC)


def older_builds(layout: DataLayout, since: datetime.datetime) -> int:
    """Warehouse build files other than ``CURRENT`` started before ``since`` (any status):
    each may still hold the record, so step 5 is not done while one remains."""
    floor = since.replace(microsecond=0)
    builds = list_builds(layout=layout)
    return sum(1 for b in builds if not b.is_current and built_at(b.build_id) < floor)
