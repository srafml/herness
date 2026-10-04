"""Review-item step of `MemoryLifecycle.purge` (impl 07 U07-57 step 3 (a), TH07-21; T07-26).

Private sibling of `lifecycle.py` (impl 07 §2 row; only `lifecycle.py` imports it). Per the
T07-26 review rulings a purged review item keeps a skeleton payload: the structural keys
(`memory_id`, `source_memory_id`, `layer`, `kind`, `flags`, `suggested_action`,
`conflicts_with`) stay, every content-bearing key the payload has is blanked, and no key the
payload lacked is added.
"""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from datetime import datetime
from typing import Final

from herness.store.errors import NotFoundError, ReviewItemConflict
from herness.store.ops.shared import decide_review_item, get_review_item, update_review_payload

__all__ = ["PURGED_FIELDS", "erase_review_item"]

# `content` (memory_write) and `statement` (derived items) hold the text; `entities` and
# `numbers` the citations; `provenance` the author and run references; `effective_date`
# a value taken from the purged statement.
PURGED_FIELDS: Final = ("content", "statement", "entities", "numbers", "provenance",
                        "effective_date")  # fmt: skip


def _blank(key: str, value: object) -> object:
    """The empty value of `value`'s JSON type ("" / [] / {}); null for the nullable date."""
    if isinstance(value, str) and key != "effective_date":
        return ""
    if isinstance(value, list):
        return []
    return {} if isinstance(value, dict) else None


def erase_review_item(item_id: str, *, now: datetime, conn: sqlite3.Connection) -> None:
    """Blank the `PURGED_FIELDS` the payload has, then reject the item if still pending
    (`system`, note `purged`), in the caller's transaction (R-33). `conn` is this thread's
    ops connection (`run_write`), so the payload read sees the transaction. An unknown item
    (`NotFoundError`) or an already decided one (`ReviewItemConflict`) is skipped: both are
    raised before any write, and a dangling link must not block the erasure."""
    try:
        payload = get_review_item(item_id).payload
    except NotFoundError:
        return
    if fields := {k: _blank(k, payload[k]) for k in PURGED_FIELDS if k in payload}:
        update_review_payload(item_id, fields, conn=conn)
    with suppress(ReviewItemConflict, NotFoundError):
        decide_review_item(
            item_id, "rejected", decided_by="system", note="purged", now=now, conn=conn
        )
