"""Privacy ops-store area (impl 10 §3.8, R-08, R-09): `deletion_request` rows and the
deletion-set read used by impl 01's deletion filter and impl 02's staging (U10-105, U10-111).

The table and its indexes are created by impl 02 migration 005 (U02-53); this area needs no
migration of its own. Every write goes through `herness.store.ops.core.run_write` and reads
the row it needs on the same connection before writing it back (impl 02 §2.3, R-10).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

from herness.core import time as clock
from herness.core.errors import ConfigError, NotFound
from herness.core.ids import IdKind, new_id

from .core import dump_json, load_json, read_all, read_one, run_write

# U10-73 pattern (also enforced by design 10 §4.1, TH10-42): source:entity:source_key.
_RECORD_ID_RE: Final = re.compile(
    r"[a-z][a-z0-9_]{0,31}:[a-z][a-z0-9_]{0,63}:[A-Za-z0-9._:@/+-]{1,200}"
)
_REQUESTED_BY_RE: Final = re.compile(r"[0-9a-f]{32}")
_REASON_REF_RE: Final = re.compile(r"[A-Za-z0-9._:/-]{1,64}")
_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")

# Status moves only pending -> running or running -> done|failed (U10-105 invariant).
_VALID_TRANSITIONS: Final = frozenset(
    {("pending", "running"), ("running", "done"), ("running", "failed")}
)
_OPEN_STATUSES: Final = ("pending", "running")
_STEPS: Final = frozenset({"1", "2", "3", "3b", "4", "5", "6", "7"})
_STEP_STATUSES: Final = frozenset({"done", "failed"})

_SELECT = "SELECT * FROM deletion_request WHERE request_id = ?"


@dataclass(frozen=True, slots=True)
class DeletionRequest:
    """One `deletion_request` row (U10-105)."""

    request_id: str
    record_id: str
    requested_by: str
    reason_ref: str
    status: Literal["pending", "running", "done", "failed"]
    steps: dict[str, dict[str, Any]]
    created_at: datetime
    completed_at: datetime | None


def _invalid(field: str) -> ConfigError:
    """A ConfigError naming ``field`` only; the caller's value is never echoed (TH10-42)."""
    msg = f"invalid {field}"
    return ConfigError(msg)


def _check_record_id(record_id: str) -> None:
    field = "record_id"
    if _RECORD_ID_RE.fullmatch(record_id) is None or ".." in record_id:
        raise _invalid(field)


def _check_requested_by(requested_by: str) -> None:
    field = "requested_by"
    if _REQUESTED_BY_RE.fullmatch(requested_by) is None:
        raise _invalid(field)


def _check_reason_ref(reason_ref: str) -> None:
    field = "reason_ref"
    if _REASON_REF_RE.fullmatch(reason_ref) is None:
        raise _invalid(field)


def _check_name(value: str, field: str) -> None:
    if _NAME_RE.fullmatch(value) is None:
        raise _invalid(field)


def _row_to_request(row: sqlite3.Row) -> DeletionRequest:
    completed_at = row["completed_at"]
    steps = load_json(row["steps"], field="steps")
    return DeletionRequest(
        request_id=str(row["request_id"]),
        record_id=str(row["record_id"]),
        requested_by=str(row["requested_by"]),
        reason_ref=str(row["reason_ref"]),
        status=cast('Literal["pending", "running", "done", "failed"]', str(row["status"])),
        steps=cast("dict[str, dict[str, Any]]", steps if isinstance(steps, dict) else {}),
        created_at=clock.parse_utc(row["created_at"]),
        completed_at=None if completed_at is None else clock.parse_utc(completed_at),
    )


def _require(conn: sqlite3.Connection, request_id: str) -> sqlite3.Row:
    row = conn.execute(_SELECT, (request_id,)).fetchone()
    if row is None:
        msg = "deletion request not found"
        raise NotFound(msg, request_id=request_id)
    return cast("sqlite3.Row", row)


def create_deletion_request(
    *, record_id: str, requested_by: str, reason_ref: str, now: datetime
) -> DeletionRequest:
    """Insert a pending request, or return the record's existing open one (U10-105).

    Idempotency key: at most one `pending`/`running` request per `record_id`. Raises
    ConfigError naming the bad field."""
    _check_record_id(record_id)
    _check_requested_by(requested_by)
    _check_reason_ref(reason_ref)

    def op(conn: sqlite3.Connection) -> DeletionRequest:
        existing = conn.execute(
            "SELECT * FROM deletion_request WHERE record_id = ? AND status IN (?, ?)"
            " ORDER BY request_id DESC LIMIT 1",
            (record_id, *_OPEN_STATUSES),
        ).fetchone()
        if existing is not None:
            return _row_to_request(existing)
        request_id = new_id(IdKind.REQUEST)
        conn.execute(
            "INSERT INTO deletion_request (request_id, record_id, requested_by, reason_ref,"
            " status, steps, created_at) VALUES (?, ?, ?, ?, 'pending', '{}', ?)",
            (request_id, record_id, requested_by, reason_ref, clock.format_utc(now)),
        )
        return DeletionRequest(
            request_id=request_id,
            record_id=record_id,
            requested_by=requested_by,
            reason_ref=reason_ref,
            status="pending",
            steps={},
            created_at=now,
            completed_at=None,
        )

    return run_write(op, op="privacy_create_deletion_request")


def get_deletion_request(request_id: str) -> DeletionRequest:
    """Return the request (U10-105). Raises NotFound (request_id) when missing."""
    row = read_one(_SELECT, (request_id,))
    if row is None:
        msg = "deletion request not found"
        raise NotFound(msg, request_id=request_id)
    return _row_to_request(row)


def open_deletion_request(record_id: str) -> DeletionRequest | None:
    """The newest `pending`/`running` request for `record_id`, else None (U10-105)."""
    _check_record_id(record_id)
    row = read_one(
        "SELECT * FROM deletion_request WHERE record_id = ? AND status IN (?, ?)"
        " ORDER BY request_id DESC LIMIT 1",
        (record_id, *_OPEN_STATUSES),
    )
    return None if row is None else _row_to_request(row)


def set_deletion_status(
    request_id: str,
    status: Literal["running", "done", "failed"],
    *,
    completed_at: datetime | None = None,
) -> None:
    """Move `pending -> running`, `running -> done` or `running -> failed` (U10-105).

    Raises NotFound (request_id), or ConfigError("invalid deletion status transition")."""

    def op(conn: sqlite3.Connection) -> None:
        row = _require(conn, request_id)
        current = str(row["status"])
        if (current, status) not in _VALID_TRANSITIONS:
            msg = "invalid deletion status transition"
            raise ConfigError(msg)
        completed_text = None if completed_at is None else clock.format_utc(completed_at)
        conn.execute(
            "UPDATE deletion_request SET status = ?, completed_at = ? WHERE request_id = ?",
            (status, completed_text, request_id),
        )

    run_write(op, op="privacy_set_deletion_status")


def record_deletion_step(
    request_id: str,
    step: Literal["1", "2", "3", "3b", "4", "5", "6", "7"],
    *,
    status: Literal["done", "failed"],
    at: datetime,
    counts: Mapping[str, int],
    error: str | None = None,
) -> None:
    """Replace `steps[step]` with this outcome (U10-105). Raises NotFound (request_id) or
    ConfigError naming the bad field."""
    step_field, status_field = "step", "status"
    if step not in _STEPS:
        raise _invalid(step_field)
    if status not in _STEP_STATUSES:
        raise _invalid(status_field)

    def op(conn: sqlite3.Connection) -> None:
        row = _require(conn, request_id)
        steps = load_json(row["steps"], field="steps")
        merged = dict(steps) if isinstance(steps, dict) else {}
        merged[step] = {
            "status": status,
            "at": clock.format_utc(at),
            "counts": dict(counts),
            "error": error,
        }
        conn.execute(
            "UPDATE deletion_request SET steps = ? WHERE request_id = ?",
            (dump_json(merged, field="steps"), request_id),
        )

    run_write(op, op="privacy_record_deletion_step")


def deleted_record_ids(source: str, entity: str) -> list[str]:
    """Sorted, unique `record_id`s with a `running`/`done` deletion request (U10-111).

    Every returned ID starts with `<source>:<entity>:`. Raises ConfigError naming the bad
    parameter; StoreBusy or SchemaViolation (more than 1,000,000 rows) from `read_all`."""
    _check_name(source, "source")
    _check_name(entity, "entity")
    prefix = f"{source}:{entity}:"
    rows = read_all(
        "SELECT DISTINCT record_id FROM deletion_request WHERE status IN ('running', 'done')"
        " AND substr(record_id, 1, ?) = ? ORDER BY record_id",
        (len(prefix), prefix),
        max_rows=1_000_000,
    )
    return [str(row["record_id"]) for row in rows]
