"""Ops functions owned by spec 02: every ``review_item`` function (impl 02 U02-55 … U02-60, R-08).

A decision and its ``review_decision`` audit line share one transaction, the caller's when
``conn`` is given (R-33, TH02-06). Filters reach SQL only as bound data: the listing picks one
of two fixed statements and binds statuses and ``payload_match`` as JSON text (RQ-01, RQ-02).
Messages, hints and log fields carry identifiers only, never payload or note text (ENG §3.4).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal, TypeGuard, cast, get_args

from herness.core import time as clock
from herness.core.audit import audit
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.ids import new_ulid
from herness.core.logging import get_logger
from herness.store.errors import NotFoundError, ReviewItemConflict

from . import _review_common
from .core import dump_json, load_json, read_all, read_one, run_write

_is_count = _review_common.is_count  # pure validators live in _review_common (budget, T02-24)
_is_json_object = _review_common.is_json_object
_check_decision = _review_common.check_decision

type ReviewKind = Literal["mapping_suggestion", "label_check", "memory_write", "weight_change"]
type ReviewStatus = Literal["pending", "approved", "rejected"]

_KINDS: Final[frozenset[str]] = frozenset(get_args(ReviewKind.__value__))
_STATUS_ORDER: Final[tuple[str, ...]] = get_args(ReviewStatus.__value__)
_ID_RE: Final = re.compile(r"rev_[0-9A-HJKMNP-TV-Z]{26}")
_MAX_ID: Final = "rev_" + "Z" * 26  # above every valid item_id: a plain datetime cursor
_MAX_CURSOR_ID_CHARS: Final = 64
_MAX_MATCH_KEYS: Final = 8
_MAX_MATCH_VALUE_CHARS: Final = 1024
_MAX_LIMIT: Final = 5000
_MEMORY_MSG: Final = (
    "memory_write items are decided through MemoryStore.approve or MemoryStore.reject"
)

_INSERT: Final = (
    "INSERT INTO review_item (item_id, kind, payload, status, created_at)"
    " VALUES (?, ?, ?, 'pending', ?)"
)
_BY_ID: Final = (
    "SELECT item_id, kind, payload, status, created_at, decided_by, decided_at, note"
    " FROM review_item WHERE item_id = ?"
)
_DECIDE_READ: Final = "SELECT kind, status FROM review_item WHERE item_id = ?"
_DECIDE: Final = (
    "UPDATE review_item SET status = ?, decided_by = ?, decided_at = ?, note = ?"
    " WHERE item_id = ? AND status = 'pending'"
)
# U02-58 step 3: two fixed statements, no string building. Parameters: kind, kind, statuses
# JSON array, payload_match JSON object (twice), then paging or cursor values.
_LIST_CREATED: Final = (
    "SELECT item_id, kind, payload, status, created_at, decided_by, decided_at, note"
    " FROM review_item WHERE (? IS NULL OR kind = ?)"
    " AND status IN (SELECT value FROM json_each(?))"
    " AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM json_each(?) AS m"
    " WHERE json_extract(review_item.payload, '$.' || m.key) IS NOT m.value))"
    " ORDER BY created_at, item_id LIMIT ? OFFSET ?"
)
_LIST_DECIDED: Final = (
    "SELECT item_id, kind, payload, status, created_at, decided_by, decided_at, note"
    " FROM review_item WHERE (? IS NULL OR kind = ?)"
    " AND status IN (SELECT value FROM json_each(?))"
    " AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM json_each(?) AS m"
    " WHERE json_extract(review_item.payload, '$.' || m.key) IS NOT m.value))"
    " AND decided_at IS NOT NULL AND (decided_at > ? OR (decided_at = ? AND item_id > ?))"
    " ORDER BY decided_at, item_id LIMIT ?"
)
_APPROVED_SUGGESTIONS: Final = (
    "SELECT item_id, kind, payload, status, created_at, decided_by, decided_at, note"
    " FROM review_item WHERE kind = 'mapping_suggestion' AND status = 'approved'"
    " ORDER BY decided_at, item_id"
)
_log = get_logger("store.ops")


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """Typed ``review_item`` row (U02-55); pending ⇔ no ``decided_at`` ⇔ no ``decided_by``."""

    item_id: str
    kind: ReviewKind
    payload: Mapping[str, object]
    status: ReviewStatus
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    note: str | None

    def __post_init__(self) -> None:
        pending = self.status == "pending"
        if not pending == (self.decided_at is None) == (self.decided_by is None):
            msg = f"review item {self.item_id} has inconsistent decision fields"
            raise SchemaViolation(msg)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ReviewItem:
        """Parse a ``review_item`` row; SchemaViolation for bad JSON, values or timestamps."""
        payload = load_json(row["payload"], field="payload")
        kind, status, decided_at = row["kind"], row["status"], row["decided_at"]
        if not isinstance(payload, dict) or kind not in _KINDS or status not in _STATUS_ORDER:
            msg = "review_item row has an invalid payload, kind or status"
            raise SchemaViolation(msg)
        return cls(
            item_id=str(row["item_id"]),
            kind=kind,
            payload=MappingProxyType(payload),
            status=status,
            created_at=clock.parse_utc(row["created_at"]),
            decided_by=row["decided_by"],
            decided_at=None if decided_at is None else clock.parse_utc(decided_at),
            note=row["note"],
        )


def _write[T](
    conn: sqlite3.Connection | None, fn: Callable[[sqlite3.Connection], T], *, op: str
) -> T:
    """Run ``fn`` on the caller's transaction connection, or in its own ``run_write``."""
    return fn(conn) if conn is not None else run_write(fn, op=op)


def _is_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def _not_found(item_id: object) -> NotFoundError:
    key = item_id if _is_id(item_id) else "invalid"  # a malformed input is never echoed
    return NotFoundError(f"review item {key} not found", kind="review_item", key=key)


def _ts_arg(value: object, name: str) -> str:
    """ts-text of an aware datetime argument, else ConfigError (precondition)."""
    if isinstance(value, datetime) and value.utcoffset() is not None:
        return clock.format_utc(value)
    msg = f"{name} must be an aware datetime"
    raise ConfigError(msg)


def create_review_item(
    kind: ReviewKind,
    payload: Mapping[str, object],
    *,
    now: datetime,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Insert a pending item and return its ``rev_<ulid>`` ID (U02-56, TH02-07).

    Raises SchemaViolation (payload not a mapping or over 64 KiB, unknown kind, naive ``now``)
    or StoreBusy. With ``conn`` the row commits with the caller's transaction.
    """
    if not isinstance(cast("object", payload), Mapping):  # runtime guard for untyped callers
        msg = "review payload must be a mapping"
        raise SchemaViolation(msg)
    if kind not in _KINDS:
        msg = "unknown review item kind"
        raise SchemaViolation(msg)
    item_id = "rev_" + new_ulid()
    params = (item_id, kind, dump_json(dict(payload), field="payload"), clock.format_utc(now))
    _write(conn, lambda tx: tx.execute(_INSERT, params), op="review_item_create")
    _log.info("store.ops.review_item_created", item_id=item_id, kind=kind)
    return item_id


def get_review_item(item_id: str) -> ReviewItem:
    """Fetch one item (U02-57); NotFoundError for an unknown or malformed ID, or StoreBusy."""
    row = read_one(_BY_ID, (item_id,)) if _is_id(item_id) else None
    if row is None:
        raise _not_found(item_id)
    return ReviewItem.from_row(row)


def _statuses_json(status: ReviewStatus | None, statuses: Collection[ReviewStatus] | None) -> str:
    if status is not None and statuses is not None:
        msg = "status and statuses are exclusive"
        raise ConfigError(msg)
    chosen: list[object] = list(_STATUS_ORDER) if status is None else [status]
    if statuses is not None:
        chosen = [] if isinstance(statuses, str) else list(statuses)
        if not 1 <= len(chosen) <= len(_STATUS_ORDER) or len(set(map(str, chosen))) < len(chosen):
            msg = "statuses must hold 1-3 distinct review statuses"
            raise ConfigError(msg)
    if not all(isinstance(s, str) and s in _STATUS_ORDER for s in chosen):
        msg = "unknown review status"
        raise ConfigError(msg)
    return dump_json(chosen, field="statuses")


def _cursor(decided_after: object) -> tuple[str, str]:
    """(ts-text, item_id) keyset cursor; a plain datetime sorts after all its own rows."""
    if isinstance(decided_after, datetime):
        return _ts_arg(decided_after, "decided_after"), _MAX_ID
    if (
        isinstance(decided_after, tuple)
        and len(decided_after) == 2  # noqa: PLR2004 - a (decided_at, item_id) pair
        and isinstance(decided_after[1], str)
        and len(decided_after[1]) <= _MAX_CURSOR_ID_CHARS
    ):
        return _ts_arg(decided_after[0], "decided_after"), decided_after[1]
    msg = "decided_after must be a datetime or a (datetime, item_id) pair"
    raise ConfigError(msg)


def _match_json(payload_match: object) -> str | None:
    if payload_match is None:
        return None
    if not (
        isinstance(payload_match, Mapping)
        and 1 <= len(payload_match) <= _MAX_MATCH_KEYS
        and all(
            isinstance(k, str)
            and _review_common.MATCH_KEY_RE.fullmatch(k)
            and isinstance(v, str)
            and len(v) <= _MAX_MATCH_VALUE_CHARS
            for k, v in payload_match.items()
        )
    ):
        msg = (
            "payload_match needs 1-8 keys matching ^[a-z_][a-z0-9_]{0,63}$"
            " with string values of at most 1024 characters"
        )
        raise ConfigError(msg)
    return dump_json(dict(payload_match), field="payload_match")


def list_review_items(  # noqa: PLR0913 - keyword-only filters fixed by impl 02 U02-58
    *,
    kind: ReviewKind | None = None,
    status: ReviewStatus | None = None,
    statuses: Collection[ReviewStatus] | None = None,
    decided_after: datetime | tuple[datetime, str] | None = None,
    payload_match: Mapping[str, str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ReviewItem]:
    """List items by (``created_at``, ``item_id``), or with ``decided_after`` decided items by
    (``decided_at``, ``item_id``) (U02-58, RQ-01, RQ-02). Raises ConfigError or StoreBusy."""
    if kind is not None and not (isinstance(cast("object", kind), str) and kind in _KINDS):
        msg = "unknown review item kind"
        raise ConfigError(msg)
    if not _is_count(limit, 1, _MAX_LIMIT) or not _is_count(offset, 0, 2**62):
        msg = f"limit must be 1..{_MAX_LIMIT} and offset >= 0"
        raise ConfigError(msg)
    match = _match_json(payload_match)
    filters = (kind, kind, _statuses_json(status, statuses), match, match)
    if decided_after is None:
        rows = read_all(_LIST_CREATED, (*filters, limit, offset), max_rows=_MAX_LIMIT)
    else:
        if offset != 0:
            msg = "offset must be 0 when decided_after is given"
            raise ConfigError(msg)
        ts, after_id = _cursor(decided_after)
        rows = read_all(_LIST_DECIDED, (*filters, ts, ts, after_id, limit), max_rows=_MAX_LIMIT)
    return [ReviewItem.from_row(row) for row in rows]


def decide_review_item(
    item_id: str,
    status: Literal["approved", "rejected"],
    *,
    decided_by: str,
    note: str | None = None,
    now: datetime,
    conn: sqlite3.Connection | None = None,
) -> ReviewItem:
    """Decide a pending item and audit it in the same transaction (U02-59, R-33, TH02-06).

    Without ``conn`` a ``memory_write`` item may only be rejected by ``system`` (R-54). Raises
    ConfigError, NotFoundError, ReviewItemConflict, StoreBusy or the audit error (rollback).
    With ``conn``, ``store.ops.review_item_decided`` is logged before the caller commits, so a
    later rollback leaves that line (like the audit line) for an attempt (accepted risk, §7.7).
    """
    _check_decision(item_id, status, decided_by, note)
    decided_at = _ts_arg(now, "now")
    audited: list[bool] = []

    def decide(tx: sqlite3.Connection) -> ReviewItem:
        row = tx.execute(_DECIDE_READ, (item_id,)).fetchone()
        if row is None:
            raise _not_found(item_id)
        kind, current = str(row["kind"]), str(row["status"])
        purge = status == "rejected" and decided_by == "system"
        if kind == "memory_write" and conn is None and not purge:
            raise ConfigError(_MEMORY_MSG)
        if kind == "label_check" and note is not None and not _is_json_object(note):
            msg = "a label_check note must be a JSON object string"  # the note is never echoed
            raise ConfigError(msg)
        if current != "pending":
            _log.info("store.ops.review_conflict", item_id=item_id, status=current)
            raise ReviewItemConflict(item_id, current)
        tx.execute(_DECIDE, (status, decided_by, decided_at, note, item_id))
        fields = {"item_id": item_id, "kind": kind, "status": status, "decided_by": decided_by}
        audit("review_decision", decided_by, **fields, note_len=len(note or ""))
        audited.append(True)
        return ReviewItem.from_row(tx.execute(_BY_ID, (item_id,)).fetchone())

    try:
        item = _write(conn, decide, op="review_item_decide")
    except Exception:
        if audited and conn is None:  # the audit line outlived a failed commit (§7.7)
            _log.error("store.ops.audit_orphan", item_id=item_id)
        raise
    _log.info("store.ops.review_item_decided", item_id=item_id, kind=item.kind, status=status)
    return item


def approved_mapping_suggestions() -> list[ReviewItem]:
    """Approved ``mapping_suggestion`` items by (``decided_at``, ``item_id``) (U02-60, LLM04).

    Raises StoreBusy, or SchemaViolation beyond the 100,000-row ``read_all`` cap."""
    return [ReviewItem.from_row(row) for row in read_all(_APPROVED_SUGGESTIONS)]


def create_review_item_if_absent(
    kind: ReviewKind,
    payload: Mapping[str, object],
    *,
    match_keys: Sequence[str],
    blocking_statuses: Collection[ReviewStatus] = ("pending",),  # type: ignore[assignment]
    now: datetime,
) -> tuple[str, bool]:
    """Insert unless a blocking item shares ``match_keys`` values (U02-130, TH02-07)."""
    keys = list(match_keys)
    if not _review_common.keys_ok(keys, _MAX_MATCH_KEYS, payload):
        msg = "match_keys must be 1-8 distinct keys present in the payload"
        raise ConfigError(msg)
    match = dump_json({k: payload[k] for k in keys}, field="match_keys")
    statuses = _statuses_json(None, blocking_statuses)

    def fn(tx: sqlite3.Connection) -> tuple[str, bool]:
        row = tx.execute(_review_common.MATCH_LOOKUP, (kind, statuses, match)).fetchone()
        if row is None:
            return create_review_item(kind, payload, now=now, conn=tx), True
        item_id = str(row["item_id"])
        _log.debug("store.ops.review_item_exists", item_id=item_id, kind=kind)
        return item_id, False

    return run_write(fn, op="review_item_create_if_absent")


def count_review_items(
    *, kind: ReviewKind, status: ReviewStatus, group_by_payload: str | None = None
) -> dict[str, int]:
    """Counts of ``kind``/``status`` items, grouped by a payload key if given (U02-131)."""
    if kind not in _KINDS or status not in _STATUS_ORDER:
        msg = "unknown review item kind or status"
        raise ConfigError(msg)
    if group_by_payload is None:
        row = read_one(_review_common.COUNT, (kind, status))
        return {"": int(row[0])} if row else {"": 0}
    if not _review_common.MATCH_KEY_RE.fullmatch(group_by_payload):
        msg = "group_by_payload must match ^[a-z_][a-z0-9_]{0,63}$"
        raise ConfigError(msg)
    rows = read_all(_review_common.COUNT_GROUPED, ("$." + group_by_payload, kind, status))
    return {str(row["k"]): int(row[1]) for row in rows}


def update_review_payload(
    item_id: str, fields: Mapping[str, object], *, conn: sqlite3.Connection | None = None
) -> None:
    """Replace named top-level payload fields; other columns unchanged (U02-132, R-54)."""
    keys = list(fields)
    if not (_is_id(item_id) and _review_common.keys_ok(keys, _review_common.MAX_FIELDS)):
        msg = "item_id or fields is invalid"
        raise ConfigError(msg)

    def fn(tx: sqlite3.Connection) -> None:
        row = tx.execute(_review_common.PAYLOAD_ONLY, (item_id,)).fetchone()
        if row is None:
            raise _not_found(item_id)
        payload = load_json(row["payload"], field="payload")
        if not isinstance(payload, dict):
            msg = "review_item row has an invalid payload"
            raise SchemaViolation(msg)
        payload |= dict(fields)
        tx.execute(_review_common.UPDATE_PAYLOAD, (dump_json(payload, field="payload"), item_id))
        _log.info("store.ops.review_payload_updated", item_id=item_id, keys=keys)

    _write(conn, fn, op="review_item_payload")
