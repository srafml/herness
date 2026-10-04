"""Review-item step of `MemoryLifecycle.purge` (impl 07 U07-57 step 3 (a), TH07-21; T07-26).

Private sibling of `lifecycle.py` (impl 07 §2 row; only `lifecycle.py` imports it). Per the
T07-26 review ruling, TH07-21 "blanks review payloads" governs: every payload field that can
hold purged text, a record citation or the erased person is overwritten (the 02 function
merges top-level keys, so a field the payload lacked is added blank).
"""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from datetime import datetime
from typing import Final

from herness.store.errors import NotFoundError, ReviewItemConflict
from herness.store.ops.shared import decide_review_item, update_review_payload

__all__ = ["PURGED_FIELDS", "erase_review_item"]

# `content` (memory_write) and `statement` (derived items) hold the text; `entities` and
# `numbers` the citations; `provenance` the author and run references.
PURGED_FIELDS: Final = ("content", "statement", "entities", "numbers", "provenance")
_BLANK: Final[dict[str, object]] = {"content": "", "statement": "", "entities": [],
                                    "numbers": [], "provenance": {}}  # fmt: skip


def erase_review_item(item_id: str, *, now: datetime, conn: sqlite3.Connection) -> None:
    """Blank the payload fields of `PURGED_FIELDS`, then reject the item if still pending
    (`system`, note `purged`), both in the caller's transaction (R-33). An unknown item
    (`NotFoundError`) or an already decided one (`ReviewItemConflict`) is skipped: both are
    raised before any write, and a dangling link must not block the erasure."""
    with suppress(NotFoundError):
        update_review_payload(item_id, {k: _BLANK[k] for k in PURGED_FIELDS}, conn=conn)
    with suppress(ReviewItemConflict, NotFoundError):
        decide_review_item(
            item_id, "rejected", decided_by="system", note="purged", now=now, conn=conn
        )
