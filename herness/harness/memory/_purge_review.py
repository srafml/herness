"""Review-item step of `MemoryLifecycle.purge` (impl 07 U07-57 step 3 (a), TH07-21; T07-26).

Private sibling of `lifecycle.py` (impl 07 §2 row; only `lifecycle.py` imports it). A purged
review item keeps a skeleton payload (T07-26 review rulings): only the structural keys of
`KEPT_FIELDS` survive, because none of them can hold personal or record-derived data:
`memory_id`, `source_memory_id`, `review_item_id`, `derived_review_item_id` and
`conflicts_with` are generated `mem_`/`rev_` ids (linkage); `kind` and `layer` are fixed
enums; `flags` lists policy flag names; `suggested_action` is an enum the reviewer UI shows.
Every other key the payload has (text, citations, provenance, hashes such as `content_hash`,
and any future key the open-ended writers add) is blanked by JSON type; no key is added.
"""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from datetime import datetime
from typing import Final

from herness.store.errors import NotFoundError, ReviewItemConflict
from herness.store.ops.shared import decide_review_item, get_review_item, update_review_payload

__all__ = ["KEPT_FIELDS", "erase_review_item"]

KEPT_FIELDS: Final = frozenset({
    "memory_id", "source_memory_id", "review_item_id", "derived_review_item_id",
    "conflicts_with", "kind", "layer", "flags", "suggested_action",
})  # fmt: skip


def _blank(key: str, value: object) -> object:
    """A string becomes "" (a date or time key `*_date` / `*_at`: null), a list [], an object
    {}; every other value (numbers, booleans, null) becomes null, so no purged value remains."""
    if isinstance(value, str) and not key.endswith(("_date", "_at")):
        return ""
    if isinstance(value, list):
        return []
    return {} if isinstance(value, dict) else None


def erase_review_item(item_id: str, *, now: datetime, conn: sqlite3.Connection) -> None:
    """Blank every payload key outside `KEPT_FIELDS`, then reject the item if still pending
    (`system`, note `purged`), in the caller's transaction (R-33). `conn` is this thread's
    ops connection (`run_write`), so the payload read sees the transaction. An unknown item
    (`NotFoundError`) or an already decided one (`ReviewItemConflict`) is skipped: both are
    raised before any write, and a dangling link must not block the erasure."""
    try:
        payload = get_review_item(item_id).payload
    except NotFoundError:
        return
    if fields := {k: _blank(k, v) for k, v in payload.items() if k not in KEPT_FIELDS}:
        update_review_payload(item_id, fields, conn=conn)
    with suppress(ReviewItemConflict, NotFoundError):
        decide_review_item(
            item_id, "rejected", decided_by="system", note="purged", now=now, conn=conn
        )
