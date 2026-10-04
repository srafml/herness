"""Memory item lifecycle: approve, reject, expiry, use counting and purge (impl 07 §3.10).

Every method writes SQLite first in one `run_write` (statuses re-checked inside it), then
mirrors the status to LanceDB; a LanceDB failure is logged (`memory.vector.sync_failed`) and
left to maintenance. A `memory_write` review item is decided only here, inside the same
transaction (R-33). Logs and errors carry ids, kinds, rules and counts, never memory text.
"""

import re
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime
from functools import partial
from typing import Final, Literal, TypeGuard, cast

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import ModelUnavailable, PolicyViolation, ToolInputError
from herness.core.logging import get_logger
from herness.core.redact import Redactor
from herness.core.types import MemoryItem, Status
from herness.harness.memory._purge_review import erase_review_item
from herness.harness.memory.policy import APPROVAL_FLOOR
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import VectorIndex
from herness.harness.memory.types import MEMORY_ID_RE, MemoryNotFound
from herness.harness.memory.write import MemoryWriter
from herness.store.errors import NotFoundError, ReviewItemConflict
from herness.store.ops import core
from herness.store.ops import memory as ops
from herness.store.ops.shared import create_review_item, decide_review_item

__all__ = ["MemoryLifecycle", "purge_args_ok"]

type Conn = sqlite3.Connection
type Row = ops.MemoryItemRow

_USER_REF_RE: Final = re.compile(r"^[0-9a-f]{32}$")
_RECORD_ID_RE: Final = re.compile(r"[a-z_]+:[a-z_]+:.+")  # fullmatch: a spec 00 record_id
_RECORD_ID_MAX: Final = 300
_ITEM: Final = "memory_item"
_APPROVE_NOTE_MAX: Final = 500
_REJECT_NOTE_MAX: Final = 1_000
_REASON_MAX: Final = 200
_USE_IDS_MAX: Final = 200
_CONFLICTS_MAX: Final = 10
_EXPIRE_BATCH: Final = 1_000
_DERIVING_KINDS: Final = frozenset({"user_correction", "business_rule"})
_DERIVED_ACTIONS: Final = ("weight_change", "mapping_suggestion")
_TERMINAL: Final = frozenset({"expired", "rejected"})
_log = get_logger("memory")


def _check(ok: object, what: str) -> None:
    """ToolInputError naming only the argument unless `ok` (inputs are never echoed)."""
    if not ok:
        msg = f"invalid {what}"
        raise ToolInputError(msg)


def purge_args_ok(record_id: object, author_ref: object) -> bool:
    """Exactly one purge selector, on its pattern (U07-57, U07-100 preconditions)."""
    if author_ref is None and isinstance(record_id, str):
        return len(record_id) <= _RECORD_ID_MAX and _RECORD_ID_RE.fullmatch(record_id) is not None
    return (
        record_id is None
        and isinstance(author_ref, str)
        and bool(_USER_REF_RE.fullmatch(author_ref))
    )


def _is_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and MEMORY_ID_RE.fullmatch(value) is not None


def _text(value: object, what: str, most: int, *, least: int = 0) -> str:
    """`value` stripped, checked to hold `least`..`most` characters."""
    _check(isinstance(value, str) and least <= len(value.strip()) <= most, what)
    return str(value).strip()


def _done(row: Row, status: Literal["active", "rejected"], rule: str) -> bool:
    """True when `row` already reached `status` (approved: with `approved_by`), the
    idempotent repeat; False when pending; PolicyViolation(rule) otherwise."""
    if row["status"] == status and (status != "active" or row["data"].get("approved_by")):
        return True
    if row["status"] != "pending_approval":
        _log.info("memory.review.stale", memory_id=row["memory_id"])
        raise PolicyViolation(rule, details={"rule": rule})
    return False


def _check_user(value: object) -> None:
    _check(isinstance(value, str) and _USER_REF_RE.fullmatch(value), "user_ref")


def _load(memory_id: str, conn: Conn) -> Row:
    if rows := ops.get_memory_items([memory_id], conn=conn):
        return rows[0]
    raise MemoryNotFound(_ITEM, memory_id)


def _str(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def _supersede(row: Row, conn: Conn) -> list[str]:
    """U07-51 step 4 (b): only listed conflicts that are still active expire (TH07-17)."""
    listed = row["data"].get("conflicts_with")
    ids = [i for i in listed if _is_id(i)] if isinstance(listed, list) else []
    out: list[str] = []
    for old in ops.get_memory_items(ids[:_CONFLICTS_MAX], conn=conn):
        if old["status"] == "active":  # the approved item itself is pending, never listed-active
            data = {**old["data"], "superseded_by": row["memory_id"],
                    "expired_reason": "superseded"}  # fmt: skip
            ops.update_memory_item(old["memory_id"], status="expired", data=data, conn=conn)
            out.append(old["memory_id"])
    return out


def _derive(row: Row, data: dict[str, JsonValue], now: datetime, conn: Conn) -> None:
    """U07-51 step 4 (c): a derived review item only, never a score or mapping (TH07-04)."""
    action = data.get("suggested_action")
    if row["kind"] not in _DERIVING_KINDS or data.get("derived_review_item_id") is not None:
        return
    for kind in _DERIVED_ACTIONS:
        if action == kind:
            payload = {"source_memory_id": row["memory_id"], "statement": row["content"],
                       "entities": data.get("entities", []), "suggested_action": action,
                       "effective_date": data.get("effective_date")}  # fmt: skip
            data["derived_review_item_id"] = create_review_item(kind, payload, now=now, conn=conn)


def _decide(data: dict[str, JsonValue], status: Literal["approved", "rejected"], user_ref: str,
            note: str | None, now: datetime, conn: Conn) -> None:  # fmt: skip
    """Decide the linked review item while it is pending, in the caller's transaction."""
    if (item_id := _str(data.get("review_item_id"))) is not None:
        with suppress(ReviewItemConflict, NotFoundError):  # raised before any write
            decide_review_item(item_id, status, decided_by=user_ref, note=note, now=now, conn=conn)


class MemoryLifecycle:
    """Status transitions of memory items (U07-51 … U07-56); thread-safe."""

    def __init__(self, cfg: MemoryConfig, *, conn_factory: Callable[[], Conn],
                 vectors: VectorIndex, writer: MemoryWriter,
                 redactor: Redactor) -> None:  # fmt: skip
        self._cfg, self._conn, self._vectors = cfg, conn_factory, vectors
        self._writer, self._redactor = writer, redactor

    def approve(self, memory_id: str, user_ref: str, note: str | None = None,
                confidence: float | None = None, *,
                now: datetime | None = None) -> MemoryItem:  # fmt: skip
        """Activate a pending item and decide its review item in one transaction (U07-51)."""
        _check(_is_id(memory_id), "memory_id")
        _check_user(user_ref)
        number = isinstance(confidence, int | float) and not isinstance(confidence, bool)
        _check(confidence is None or (number and 0.0 <= confidence <= 1.0), "confidence")
        clean = None if note is None else self._redact(_text(note, "note", _APPROVE_NOTE_MAX))
        at = now or clock.now()
        row = _load(memory_id, self._conn())
        if _done(row, "active", "approve.not_pending"):
            return MemoryItem.model_validate(row)
        conf = confidence if confidence is not None else max(row["confidence"], APPROVAL_FLOOR)

        def tx(conn: Conn) -> tuple[Row, list[str]] | None:
            cur = _load(memory_id, conn)
            if _done(cur, "active", "approve.not_pending"):
                _log.info("memory.review.stale", memory_id=memory_id)  # a concurrent approval won
                return None
            data = {**cur["data"], "approved_by": user_ref,
                    "approved_at": clock.format_utc(at), "approval_note": clean}  # fmt: skip
            superseded = _supersede(cur, conn)
            _derive(cur, data, at, conn)
            ops.update_memory_item(
                memory_id, status="active", confidence=conf, data=data, conn=conn
            )
            _decide(data, "approved", user_ref, clean, at, conn)
            return {**cur, "data": data}, superseded

        if (done := core.run_write(tx, op="memory_approve")) is not None:
            self._mirror([memory_id], "active", "approve")
            if done[1]:
                self._mirror(done[1], "expired", "approve")
            self._logged("memory.item.approved", done[0])
        return MemoryItem.model_validate(_load(memory_id, self._conn()))

    def reject(
        self, memory_id: str, user_ref: str, note: str, *, now: datetime | None = None
    ) -> None:
        """Reject a pending item and its review item in one transaction (U07-52)."""
        _check(_is_id(memory_id), "memory_id")
        _check_user(user_ref)
        clean = self._redact(_text(note, "note", _REJECT_NOTE_MAX, least=1))
        at = now or clock.now()
        if _done(_load(memory_id, self._conn()), "rejected", "reject.not_pending"):
            return

        def tx(conn: Conn) -> Row | None:
            cur = _load(memory_id, conn)
            if _done(cur, "rejected", "reject.not_pending"):
                _log.info("memory.review.stale", memory_id=memory_id)  # a concurrent rejection won
                return None
            data = {**cur["data"], "rejected_by": user_ref,
                    "rejected_at": clock.format_utc(at), "rejection_note": clean}  # fmt: skip
            ops.update_memory_item(memory_id, status="rejected", data=data, conn=conn)
            _decide(data, "rejected", user_ref, clean, at, conn)
            return {**cur, "data": data}

        if (stored := core.run_write(tx, op="memory_reject")) is not None:
            self._mirror([memory_id], "rejected", "reject")
            self._logged("memory.item.rejected", stored)

    def expire(self, now: datetime | None = None) -> int:
        """TTL sweep: due candidate, pending and active items expire with reason ttl (U07-54)."""
        at, total, after = clock.format_utc(now or clock.now()), 0, ""

        def tx(conn: Conn) -> list[str]:
            found = ops.maintenance_rows(
                selector="expirable", now=at, limit=_EXPIRE_BATCH, after=after, conn=conn
            )
            rows = cast("list[Row]", found)  # the "expirable" selector returns item rows
            for row in rows:
                data = {**row["data"], "expired_reason": "ttl"}
                ops.update_memory_item(row["memory_id"], status="expired", data=data, conn=conn)
            return [row["memory_id"] for row in rows]

        while batch := core.run_write(tx, op="memory_expire"):
            total, after = total + len(batch), batch[-1]
            self._mirror(batch, "expired", "expire")
        if total:
            _log.info("memory.item.expired", count=total, reason="ttl")
        return total

    def expire_item(self, memory_id: str, reason: str, superseded_by: str | None = None) -> None:
        """Expire one item with a reason (U07-55); an expired or rejected item is unchanged."""
        _check(_is_id(memory_id), "memory_id")
        why = _text(reason, "reason", _REASON_MAX, least=1)
        _check(superseded_by is None or _is_id(superseded_by), "superseded_by")

        def tx(conn: Conn) -> bool:
            cur = _load(memory_id, conn)
            if superseded_by is not None:
                _check(ops.get_memory_items([superseded_by], conn=conn), "superseded_by")
            if cur["status"] in _TERMINAL:
                return False
            data: dict[str, JsonValue] = {**cur["data"], "expired_reason": why}
            if superseded_by is not None:
                data["superseded_by"] = superseded_by
            ops.update_memory_item(memory_id, status="expired", data=data, conn=conn)
            return True

        if core.run_write(tx, op="memory_expire_item"):
            self._mirror([memory_id], "expired", "expire_item")

    def record_use(
        self, memory_ids: Sequence[str], run_id: str, *, now: datetime | None = None
    ) -> None:
        """Count one use of each distinct live id rendered into a prompt (U07-56).

        `run_id` is part of the fixed signature; U07-56 stores and logs nothing of it."""
        del run_id
        ok = not isinstance(memory_ids, str) and len(memory_ids) <= _USE_IDS_MAX
        _check(ok and all(_is_id(i) for i in memory_ids), "memory_ids")
        if ids := list(dict.fromkeys(memory_ids)):
            at = clock.format_utc(now or clock.now())
            core.run_write(
                lambda conn: ops.touch_memory_items(ids, now=at, conn=conn), op="memory_record_use"
            )

    def purge(self, *, author_ref: str | None = None, record_id: str | None = None,
              now: datetime | None = None) -> int:  # fmt: skip
        """Erase the items citing a record or authored by a person (U07-57, R-54, TH07-21).

        Vectors first, then one transaction for the review items and the rows, so a retry
        after a failure finds the same ids in SQLite. Logs counts only, never purged text."""
        if not purge_args_ok(record_id, author_ref):
            msg = "purge needs exactly one of author_ref, record_id"
            raise ToolInputError(msg)
        at, rows = now or clock.now(), partial(ops.purge_rows, record_id=record_id,
                                               author_ref=author_ref)  # fmt: skip
        sel = rows(dry_run=True, conn=self._conn())
        try:
            self._vectors.delete(sel.deleted_ids)
        except ModelUnavailable as exc:
            _log.error("memory.purge.vector_failed", count=len(sel.deleted_ids))
            msg = "memory vector purge failed"
            raise ModelUnavailable(msg) from exc

        def tx(conn: Conn) -> ops.PurgeRows:
            # Re-selected inside the transaction: an item that began citing the record since
            # the dry run still has its review payload blanked before its row goes.
            for item_id in rows(dry_run=True, conn=conn).review_item_ids:
                erase_review_item(item_id, now=at, conn=conn)
            return rows(conn=conn)

        done = core.run_write(tx, op="memory_purge")
        if late := sorted(set(done.deleted_ids) - set(sel.deleted_ids)):
            self._drop_vectors(late)
        counts = (len(done.deleted_ids), len(done.scrubbed_ids), len(done.review_item_ids))
        _log.info("memory.purge.completed", count=counts[0], scrubbed=counts[1],
                  review_items=counts[2])  # fmt: skip
        return len(done.deleted_ids)

    def _drop_vectors(self, ids: Sequence[str]) -> None:
        """Vectors of rows that started citing the record after the dry run; a failure leaves
        orphans that maintenance deletes (U07-96 step 5)."""
        try:
            self._vectors.delete(ids)
        except ModelUnavailable:
            _log.warning("memory.vector.sync_failed", op="purge", count=len(ids))

    def _redact(self, text: str) -> str:
        found = self._redactor.redact(text)
        return text if found is None else found.text

    def _mirror(self, ids: Sequence[str], status: Status, op: str) -> None:
        """Mirror a committed status to LanceDB; a failure is logged for maintenance."""
        try:
            self._vectors.set_status(ids, status)
        except ModelUnavailable:
            _log.warning("memory.vector.sync_failed", op=op, count=len(ids))

    @staticmethod
    def _logged(event: str, row: Row) -> None:
        data = row["data"]
        _log.info(
            event, memory_id=row["memory_id"], kind=row["kind"],
            review_item_id=_str(data.get("review_item_id")),
            derived_review_item_id=_str(data.get("derived_review_item_id")),
        )  # fmt: skip
