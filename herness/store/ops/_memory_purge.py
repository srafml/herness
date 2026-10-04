"""Selection, delete and scrub steps behind ``memory.purge_rows`` (impl 07 U07-101; T07-26).

Private sibling of ``herness.store.ops.memory`` (impl 07 §2 row), split off for the line
budgets of ``memory.py`` and ``_memory_rows.py``; only ``memory`` imports it and it imports
only ``core`` and ``_memory_rows`` (``ops-areas-acyclic`` ignore entries). Privacy erasure
(R-54, TH07-21): no value it selects or deletes is logged.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from itertools import batched
from typing import Final, Literal

from pydantic import JsonValue

from herness.core.errors import ToolInputError

from . import core
from ._memory_rows import HEX32_RE, Conn, load_typed, marks, query

__all__ = ["RECORD_ID_MAX", "PurgeRows", "purge_apply", "purge_select", "purge_selector"]

# --- U07-101 purge (R-54, TH07-21) ----------------------------------------------------------

RECORD_ID_MAX: Final = 300
_RECORD_ID_RE: Final = re.compile(r"[a-z_]+:[a-z_]+:.+")
_PURGE_CHUNK: Final = 500
_PURGE_COLS: Final = (
    "SELECT memory_id, json_extract(data,'$.review_item_id'),"
    " json_extract(data,'$.derived_review_item_id') FROM memory_item WHERE "
)
# Structured citations only (T07-26 controller rulings): an entity id equal to the record id,
# or a cited number holding the record id or its key as a string value at depth <= 4 (the R-77
# rule of impl 06 findings.py `_cites`, repeated here: an area never imports another area).
# The SQL finds candidates; `_cites` then applies the exact depth rule.
_ENTITY: Final = (
    "EXISTS (SELECT 1 FROM json_each(data, '$.entities') e WHERE CASE WHEN e.type = 'object'"
    " THEN json_extract(e.value, '$.id') END = ?)"
)
_PURGE_SQL: Final = {
    "record": "SELECT memory_id, json_extract(data,'$.review_item_id'),"  # noqa: S608 - constant
    f" json_extract(data,'$.derived_review_item_id'), data, {_ENTITY} FROM memory_item"
    f" WHERE {_ENTITY} OR EXISTS (SELECT 1 FROM json_tree(data, '$.numbers') t"
    " WHERE t.type = 'text' AND t.atom IN (?, ?))",
    "author": _PURGE_COLS + "json_extract(provenance,'$.author_ref') = ?",
}
_CITE_DEPTH: Final = 4
_SCRUB_SQL: Final = (
    "SELECT memory_id, data FROM memory_item WHERE EXISTS (SELECT 1 FROM"
    " json_each(data, '$.provenance_history') h WHERE CASE WHEN h.type = 'object'"
    " THEN json_extract(h.value, '$.author_ref') END = ?)"
)
_SCRUB_SET: Final = "UPDATE memory_item SET data = ? WHERE memory_id = ?"
# FTS5 keeps a deleted row's tokens in older segments until they merge: rewrite them (TH07-21).
_FTS_OPTIMIZE: Final = "INSERT INTO memory_fts(memory_fts) VALUES('optimize')"


@dataclass(frozen=True, slots=True)
class PurgeRows:
    """What ``purge_rows`` selected or removed (U07-101); every list is sorted."""

    deleted_ids: list[str]
    scrubbed_ids: list[str]
    review_item_ids: list[str]


type PurgeSelector = tuple[Literal["record", "author"], str]
type _Scrubs = list[tuple[str, dict[str, JsonValue]]]


def purge_selector(record_id: object, author_ref: object) -> PurgeSelector:
    """The one valid selector, else ToolInputError (U07-101 precondition; nothing echoed)."""
    if author_ref is None and isinstance(record_id, str):
        if len(record_id) <= RECORD_ID_MAX and _RECORD_ID_RE.fullmatch(record_id):
            return "record", record_id
    elif record_id is None and isinstance(author_ref, str) and HEX32_RE.fullmatch(author_ref):
        return "author", author_ref
    msg = "purge_rows needs exactly one selector"
    raise ToolInputError(msg)


def _without(data: dict[str, JsonValue], author_ref: str) -> dict[str, JsonValue] | None:
    """``data`` minus the person's ``provenance_history`` entries; None when it names none."""
    history = data.get("provenance_history")
    if not isinstance(history, list):
        return None
    kept = [e for e in history if not (isinstance(e, dict) and e.get("author_ref") == author_ref)]
    return None if len(kept) == len(history) else {**data, "provenance_history": kept}


def _cites(value: object, targets: frozenset[str], depth: int) -> bool:
    """True when a string equal to a target sits at ``depth`` <= 4 inside ``value`` (R-77)."""
    if isinstance(value, str):
        return value in targets
    if depth >= _CITE_DEPTH or not isinstance(value, dict | list):
        return False
    children = value.values() if isinstance(value, dict) else value
    return any(_cites(child, targets, depth + 1) for child in children)


def _record_rows(record_id: str, conn: Conn, op: str) -> list[sqlite3.Row]:
    """Rows citing ``record_id`` by entity id, or by a number holding it or its key (R-77)."""
    key = record_id.split(":", 2)[2]  # the selector pattern guarantees three parts
    rows = query(_PURGE_SQL["record"], [record_id, record_id, record_id, key], conn, op)
    targets, out = frozenset({record_id, key}), list[sqlite3.Row]()
    for row in rows:
        numbers = (load_typed(row[3], dict, "memory_item.data", row[0]) or {}).get("numbers")
        cited = isinstance(numbers, list) and any(_cites(n, targets, 0) for n in numbers)
        if row[4] or cited:
            out.append(row)
    return out


def purge_select(selector: PurgeSelector, conn: Conn, op: str) -> tuple[PurgeRows, _Scrubs]:
    """Steps 1-3: the delete set, the scrub rows (with their new ``data``), the review ids."""
    how, value = selector
    record = how == "record"
    rows = _record_rows(value, conn, op) if record else query(_PURGE_SQL[how], [value], conn, op)
    deleted = sorted(str(r[0]) for r in rows)
    reviews = sorted({str(v) for r in rows for v in (r[1], r[2]) if v is not None})
    scrubs: _Scrubs = []
    if how == "author":
        gone = set(deleted)
        for mid, text in query(_SCRUB_SQL, [value], conn, op):
            data = load_typed(text, dict, "memory_item.data", mid) or {}
            if mid not in gone and (new := _without(data, value)) is not None:
                scrubs.append((str(mid), new))
    scrubs.sort(key=lambda s: s[0])
    return PurgeRows(deleted, [m for m, _ in scrubs], reviews), scrubs


def purge_apply(selector: PurgeSelector, conn: sqlite3.Connection, op: str) -> PurgeRows:
    """Steps 1-7 on the caller's write connection: select, delete in chunks of 500 (trigger
    ``memory_item_ad`` removes the FTS rows; ``optimize`` then drops their tokens from the
    index segments), then store the scrubbed ``data``."""
    found, scrubs = purge_select(selector, conn, op)
    for chunk in batched(found.deleted_ids, _PURGE_CHUNK):
        sql = f"DELETE FROM memory_item WHERE memory_id IN ({marks(len(chunk))})"  # noqa: S608
        conn.execute(sql, chunk)
    if found.deleted_ids:
        conn.execute(_FTS_OPTIMIZE)
    for mid, data in scrubs:
        conn.execute(_SCRUB_SET, (core.dump_json(data, field="data"), mid))
    return found
