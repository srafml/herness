"""Ingest area of the ops store (impl 01 U01-27 … U01-32, U01-92; R-08).

Functions for ``watermark``, ``sync_slice`` and ``file_ingest``, re-exported by
``herness.store.ops``. The deletion-set read is impl 10's ``privacy`` area (R-68).
Timestamps are stored as fixed-width UTC text, so text order is time order (spec 00 §8).
"""

from __future__ import annotations

import datetime
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast

from herness.core import time as clock
from herness.core.errors import SchemaViolation

from .core import dump_json, load_json, read_all, read_one, run_write

type SliceStatus = Literal["pending", "running", "done", "failed"]

_FINGERPRINT_RE: Final = re.compile(r"[0-9a-f]{64}")
_IN_CHUNK: Final = 500
_MAX_WATERMARKS: Final = 10_000
_MAX_ERROR_CHARS: Final = 500
_SELECT_WATERMARK: Final = "SELECT source, entity, field, value, updated_at FROM watermark"
_SET_WATERMARK: Final = (
    "INSERT INTO watermark(source, entity, field, value, updated_at) VALUES (?, ?, ?, ?, ?)"
    " ON CONFLICT(source, entity) DO UPDATE SET field = excluded.field,"
    " value = excluded.value, updated_at = excluded.updated_at"
    " WHERE excluded.value > watermark.value"
)
_ENSURE_SLICE: Final = (
    "INSERT INTO sync_slice(source, entity, slice_start, slice_end, status, rows, files,"
    " attempts, last_error, updated_at) VALUES (?, ?, ?, ?, 'pending', 0, '[]', 0, NULL, ?)"
    " ON CONFLICT(source, entity, slice_start) DO UPDATE SET slice_end = excluded.slice_end,"
    " status = 'pending', rows = 0, files = '[]', updated_at = excluded.updated_at"
    " WHERE sync_slice.slice_end <> excluded.slice_end"
    " AND NOT (sync_slice.status = 'done' AND sync_slice.slice_end > excluded.slice_end)"
)
_SELECT_SLICES: Final = (
    "SELECT source, entity, slice_start, slice_end, status, rows, files, attempts,"
    " last_error, updated_at FROM sync_slice"
    " WHERE source = ? AND entity = ? AND slice_start IN ({marks}) ORDER BY slice_start"
)
_SLICE_KEY: Final = " WHERE source = ? AND entity = ? AND slice_start = ?"
_SELECT_FILE: Final = (
    "SELECT fingerprint, source, entity, path, size_bytes, mtime, rows, files, ingested_at"
    " FROM file_ingest WHERE fingerprint = ?"
)
_RECORD_FILE: Final = (
    "INSERT OR IGNORE INTO file_ingest(fingerprint, source, entity, path, size_bytes, mtime,"
    " rows, files, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


@dataclass(frozen=True, slots=True)
class Watermark:
    """High-water mark of one stream (U01-27); ``value`` and ``updated_at`` aware UTC."""

    source: str
    entity: str
    field: str
    value: datetime.datetime
    updated_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class SliceRow:
    """One ``sync_slice`` backfill plan row (U01-30)."""

    source: str
    entity: str
    slice_start: datetime.datetime
    slice_end: datetime.datetime
    status: SliceStatus
    rows: int
    files: tuple[str, ...]
    attempts: int
    last_error: str | None
    updated_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class FileIngestRow:
    """One ingested inbox file (U01-32); ``fingerprint`` is 64 lower-case hex."""

    fingerprint: str
    source: str
    entity: str
    path: str
    size_bytes: int
    mtime: datetime.datetime
    rows: int
    files: tuple[str, ...]
    ingested_at: datetime.datetime


def _watermark(row: sqlite3.Row) -> Watermark:
    source, entity = str(row["source"]), str(row["entity"])
    try:
        value, updated_at = clock.parse_utc(row["value"]), clock.parse_utc(row["updated_at"])
    except SchemaViolation as exc:
        msg = "corrupt watermark"
        raise SchemaViolation(msg, source=source, entity=entity) from exc
    return Watermark(source, entity, str(row["field"]), value, updated_at)


def get_watermark(source: str, entity: str) -> Watermark | None:
    """The watermark of ``(source, entity)`` or None (U01-27); SchemaViolation, StoreBusy."""
    row = read_one(f"{_SELECT_WATERMARK} WHERE source = ? AND entity = ?", (source, entity))
    return None if row is None else _watermark(row)


def list_watermarks() -> list[Watermark]:
    """Every stored watermark ordered by ``(source, entity)`` (U01-92)."""
    rows = read_all(f"{_SELECT_WATERMARK} ORDER BY source, entity", (), max_rows=_MAX_WATERMARKS)
    return [_watermark(row) for row in rows]


def set_watermark(
    source: str, entity: str, field: str, value: datetime.datetime, *, now: datetime.datetime
) -> bool:
    """Move the watermark forward, never back (U01-28); True when the stored value changed.

    Naive datetimes raise SchemaViolation; StoreBusy once the write retries are exhausted."""
    params = (source, entity, field, clock.format_utc(value), clock.format_utc(now))

    def fn(conn: sqlite3.Connection) -> bool:
        return conn.execute(_SET_WATERMARK, params).rowcount == 1

    return run_write(fn, op="set_watermark")


def _paths(row: sqlite3.Row, table: str) -> tuple[str, ...]:
    value = load_json(row["files"], field=f"{table}.files")
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        msg = f"invalid JSON in {table}.files"
        raise SchemaViolation(msg, source=str(row["source"]), entity=str(row["entity"]))
    return tuple(cast(list[str], value))


def _slice(row: sqlite3.Row) -> SliceRow:
    keys = ("slice_start", "slice_end", "updated_at")
    start, end, stamp = (clock.parse_utc(row[k]) for k in keys)
    files = _paths(row, "sync_slice")
    head = (row["source"], row["entity"], start, end, cast(SliceStatus, row["status"]))
    return SliceRow(*head, row["rows"], files, row["attempts"], row["last_error"], stamp)


def ensure_slices(
    source: str,
    entity: str,
    slices: Sequence[tuple[datetime.datetime, datetime.datetime]],
    *,
    now: datetime.datetime,
) -> list[SliceRow]:
    """Create or refresh the backfill plan rows (U01-30); rows ordered by ``slice_start``.

    A row whose stored end differs is reset to pending, except a done row that already
    reaches past the planned end. StoreBusy from the write or the reads."""
    stamp = clock.format_utc(now)
    plan = [(clock.format_utc(start), clock.format_utc(end)) for start, end in slices]

    def fn(conn: sqlite3.Connection) -> None:
        conn.executemany(_ENSURE_SLICE, [(source, entity, s, e, stamp) for s, e in plan])

    run_write(fn, op="ensure_slices")
    starts = [start for start, _ in plan]
    rows: list[SliceRow] = []
    for i in range(0, len(starts), _IN_CHUNK):
        chunk = starts[i : i + _IN_CHUNK]
        sql = _SELECT_SLICES.format(marks=", ".join("?" * len(chunk)))
        rows.extend(_slice(r) for r in read_all(sql, (source, entity, *chunk), max_rows=len(chunk)))
    return sorted(rows, key=lambda r: r.slice_start)


def _update_slice(
    op: str, assignments: str, values: tuple[object, ...], key: tuple[str, str, datetime.datetime]
) -> None:
    source, entity, slice_start = key
    sql = f"UPDATE sync_slice SET {assignments}{_SLICE_KEY}"  # noqa: S608 - module constants
    params = (*values, source, entity, clock.format_utc(slice_start))

    def fn(conn: sqlite3.Connection) -> None:
        if conn.execute(sql, params).rowcount == 0:  # raised inside: the write rolls back
            msg = "slice not found"
            raise SchemaViolation(msg, source=source, entity=entity)

    run_write(fn, op=op)


def mark_slice_running(
    source: str, entity: str, slice_start: datetime.datetime, *, now: datetime.datetime
) -> None:
    """Mark a slice running and count the attempt (U01-31); SchemaViolation if missing."""
    assignments = "status = 'running', attempts = attempts + 1, updated_at = ?"
    stamp = clock.format_utc(now)
    _update_slice("mark_slice_running", assignments, (stamp,), (source, entity, slice_start))


def mark_slice_done(
    source: str,
    entity: str,
    slice_start: datetime.datetime,
    *,
    rows: int,
    files: Sequence[str],
    now: datetime.datetime,
) -> None:
    """Mark a slice done with its row count and lake files; clears ``last_error`` (U01-31)."""
    assignments = "status = 'done', rows = ?, files = ?, last_error = NULL, updated_at = ?"
    values = (rows, dump_json(list(files), field="sync_slice.files"), clock.format_utc(now))
    _update_slice("mark_slice_done", assignments, values, (source, entity, slice_start))


def mark_slice_failed(
    source: str, entity: str, slice_start: datetime.datetime, *, error: str, now: datetime.datetime
) -> None:
    """Mark a slice failed; ``error`` (class name and message) cut to 500 chars (U01-31)."""
    assignments = "status = 'failed', last_error = ?, updated_at = ?"
    values = (error[:_MAX_ERROR_CHARS], clock.format_utc(now))
    _update_slice("mark_slice_failed", assignments, values, (source, entity, slice_start))


def _check_fingerprint(fingerprint: str) -> None:
    if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        msg = "fingerprint must be 64 lower-case hex characters"
        raise SchemaViolation(msg)


def get_file_ingest(fingerprint: str) -> FileIngestRow | None:
    """The ingest record of one inbox file fingerprint, or None (U01-32)."""
    _check_fingerprint(fingerprint)
    row = read_one(_SELECT_FILE, (fingerprint,))
    if row is None:
        return None
    mtime, ingested_at = clock.parse_utc(row["mtime"]), clock.parse_utc(row["ingested_at"])
    files = _paths(row, "file_ingest")
    head = (row["fingerprint"], row["source"], row["entity"], row["path"], row["size_bytes"])
    return FileIngestRow(*head, mtime, row["rows"], files, ingested_at)


def record_file_ingest(row: FileIngestRow) -> bool:
    """Record an ingested inbox file once per fingerprint (U01-32); True when inserted."""
    _check_fingerprint(row.fingerprint)
    head = (row.fingerprint, row.source, row.entity, row.path, row.size_bytes)
    files = dump_json(list(row.files), field="file_ingest.files")
    mtime, ingested_at = clock.format_utc(row.mtime), clock.format_utc(row.ingested_at)
    params = (*head, mtime, row.rows, files, ingested_at)

    def fn(conn: sqlite3.Connection) -> bool:
        return conn.execute(_RECORD_FILE, params).rowcount == 1

    return run_write(fn, op="record_file_ingest")
