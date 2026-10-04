"""Row types, constant SQL and plumbing of the ops area ``memory`` (impl 07 §3.5 C1 … C7).

Private sibling of ``herness.store.ops.memory``, split off to keep that module inside its impl 07
§2 line budget; only ``memory`` imports it (``ops-areas-acyclic`` ignore entry). The id patterns
repeat ``herness.harness.memory.types`` (L4), which this L1 package cannot import. It also holds
the selection and delete steps behind ``memory.purge_rows`` (U07-101, T07-26 spec note).
"""

from __future__ import annotations

import enum
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import batched
from typing import Final, Literal, TypedDict, cast, get_args

from pydantic import JsonValue

from herness.core.errors import HernessError, SchemaViolation, StoreBusy, ToolInputError
from herness.core.types import Kind, Layer, Status

from . import core

type Conn = sqlite3.Connection | None
type Params = Sequence[object] | Mapping[str, object]
type Selector = Literal[
    "expirable", "embedding_pending", "all_ids_status", "business_rule_review_due", "templates"
]

_ULID = "[0-9A-HJKMNP-TV-Z]{26}"
MEMORY_ID_RE: Final = re.compile(rf"mem_{_ULID}")
QUERY_ID_RE: Final = re.compile(r"q_[0-9a-f]{16}")
FINDING_ID_RE: Final = re.compile(rf"fnd_{_ULID}")
TASK_ID_RE: Final = re.compile(rf"task_{_ULID}")
HEX32_RE: Final = re.compile(r"[0-9a-f]{32}")
_HEX16_RE: Final = re.compile(r"[0-9a-f]{16}")
LAYERS: Final = frozenset(get_args(Layer))
KINDS: Final = frozenset(get_args(Kind))
STATUSES: Final = frozenset(get_args(Status))
CONTENT_MAX: Final = 8_000
INCREMENT_MAX: Final = 1_000
SESSION_ID_MAX: Final = 64
ENTITY_ID_MAX: Final = 200
ENTITY_IDS_MAX: Final = 50
TOUCH_IDS_MAX: Final = 200
MAINTENANCE_LIMIT_MAX: Final = 10_000
_CANDIDATE_LIMIT_MAX: Final = 200

COLUMNS: Final = (
    "memory_id", "layer", "kind", "content", "data", "provenance", "confidence", "status",
    "created_at", "expires_at", "last_used_at", "use_count",
)  # fmt: skip
_COLS: Final = ", ".join(COLUMNS)
SELECT: Final = f"SELECT {_COLS} FROM memory_item"  # noqa: S608 - constant columns
INSERT: Final = f"INSERT INTO memory_item ({_COLS}) VALUES ({', '.join('?' * 12)})"  # noqa: S608
# U07-24: the only column names an UPDATE may name, in this order (C2 allowlist).
UPDATABLE: Final = (
    "status", "content", "data", "provenance", "confidence", "expires_at", "last_used_at",
)  # fmt: skip
_SELECTORS: Final = {  # U07-23 identity selectors, each served by a 070 index
    "content_hash": "json_extract(data,'$.content_hash') = ?",
    "task_hash": "json_extract(provenance,'$.task_id') = ?"
    " AND json_extract(data,'$.content_hash') = ?",
    "fingerprint": "kind = 'sql_template' AND json_extract(data,'$.fingerprint') = ?",
}
PROPOSALS: Final = (
    "SELECT COUNT(*) FROM memory_item"
    " WHERE json_extract(provenance,'$.via') IN ('tool','chat','dashboard','cli')"
)
PROPOSAL_FILTERS: Final = (
    ("run_id", " AND json_extract(provenance,'$.run_id') = ?"),
    ("session_id", " AND json_extract(provenance,'$.session_id') = ?"),
    ("author_ref", " AND json_extract(provenance,'$.author_ref') = ?"),
    ("kind", " AND kind = ?"),
    ("since", " AND created_at >= ?"),
)
_PENDING: Final = "json_extract(data,'$.embedding_pending') = 1 AND status <> 'rejected'"
PENDING_COUNT: Final = f"SELECT COUNT(*) FROM memory_item WHERE {_PENDING}"  # noqa: S608
_PAGE: Final = " ORDER BY memory_id LIMIT :limit"
MAINTENANCE: Final[Mapping[str, str]] = {
    "expirable": f"{SELECT} WHERE status IN ('candidate','pending_approval','active')"
    f" AND expires_at <= :now{_PAGE}",
    "embedding_pending": f"{SELECT} WHERE {_PENDING}{_PAGE}",
    "all_ids_status": f"SELECT memory_id, status FROM memory_item WHERE memory_id > :after{_PAGE}",  # noqa: S608
    "business_rule_review_due": f"{SELECT} WHERE kind = 'business_rule' AND status = 'active'"
    " AND created_at <= :review_cutoff AND (json_extract(data,'$.last_review_requested_at')"
    f" IS NULL OR json_extract(data,'$.last_review_requested_at') <= :review_cutoff){_PAGE}",
    "templates": f"{SELECT} WHERE kind = 'sql_template' AND status IN ('candidate','active')"
    + _PAGE,
}
# rank = 1: also compare the index with the external-content table (a rowid desync shows).
FTS_CHECK: Final = "INSERT INTO memory_fts(memory_fts, rank) VALUES('integrity-check', 1)"
FTS_REBUILD: Final = "INSERT INTO memory_fts(memory_fts) VALUES('rebuild')"
SESSION_IDS: Final = (
    "SELECT memory_id FROM memory_item WHERE json_extract(provenance,'$.session_id') = ?"
    " AND status IN ('pending_approval','active') ORDER BY created_at, memory_id"
)


class MemoryItemRow(TypedDict):
    """One ``memory_item`` row with ``data`` and ``provenance`` parsed (§3.5)."""

    memory_id: str
    layer: str
    kind: str
    content: str
    data: dict[str, JsonValue]
    provenance: dict[str, JsonValue]
    confidence: float
    status: str
    created_at: str
    expires_at: str | None
    last_used_at: str | None
    use_count: int


class FindingFact(TypedDict):
    """Provenance facts of one ``finding`` row (U07-28)."""

    status: str
    confidence: float
    run_id: str
    task_id: str | None
    query_ids: list[str]
    verification: dict[str, JsonValue] | None


class EvidenceRow(TypedDict):
    """The re-runnable part of one ``evidence`` row (U07-28)."""

    sql: str
    params: dict[str, JsonValue]
    build_id: str


class Unchanged(enum.Enum):
    """Type of ``UNCHANGED``: ``expires_at`` left as is by U07-24 (``None`` sets NULL)."""

    UNCHANGED = "unchanged"


UNCHANGED: Final = Unchanged.UNCHANGED


def is_count(value: object) -> bool:
    """A non-negative ``int`` that is not a ``bool``."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_unit(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 1


_CHECKS: Final[Mapping[str, Callable[[object], bool]]] = {
    "memory_id": lambda v: isinstance(v, str) and MEMORY_ID_RE.fullmatch(v) is not None,
    "layer": lambda v: isinstance(v, str) and v in LAYERS,
    "kind": lambda v: isinstance(v, str) and v in KINDS,
    "status": lambda v: isinstance(v, str) and v in STATUSES,
    "content": lambda v: isinstance(v, str) and len(v) <= CONTENT_MAX,
    "data": lambda v: isinstance(v, dict),
    "provenance": lambda v: isinstance(v, dict),
    "confidence": _is_unit,
    "created_at": lambda v: isinstance(v, str),
    "expires_at": lambda v: v is None or isinstance(v, str),
    "last_used_at": lambda v: v is None or isinstance(v, str),
    "use_count": is_count,
}
CHECKED: Final = frozenset(_CHECKS)


def check_columns(values: Mapping[str, object], op: str) -> None:
    """App-level checks standing in for the CHECKs 004 lacks (U07-21; §4.1 "(app)").

    Raises SchemaViolation("<op>: invalid <column>") for the first failing value."""
    for column, value in values.items():
        if column in _CHECKS and not _CHECKS[column](value):
            msg = f"{op}: invalid {column}"
            raise SchemaViolation(msg)


def bad_filter(op: str, what: str = "invalid filter") -> ToolInputError:
    """The ``ToolInputError("<op>: <what>")`` of a rejected filter or bound."""
    return ToolInputError(f"{op}: {what}")


def valid_ids(
    values: Sequence[str], pattern: re.Pattern[str], op: str, most: int = 500
) -> list[str]:
    """C2: at most ``most`` ids, each matching ``pattern``; deduplicated in input order."""
    if len(values) > most:
        msg = f"too many ids: {op}"
        raise ToolInputError(msg)
    if not all(pattern.fullmatch(v) for v in values):
        msg = f"invalid id: {op}"
        raise ToolInputError(msg)
    return list(dict.fromkeys(values))


def in_set(values: Sequence[str], allowed: frozenset[str], most: int) -> bool:
    """1 … ``most`` values, each a member of ``allowed``."""
    return 1 <= len(values) <= most and all(v in allowed for v in values)


def limit_ok(limit: int, most: int) -> bool:
    """An ``int`` (not ``bool``) in 1 … ``most``."""
    return not isinstance(limit, bool) and 1 <= limit <= most


def filters_ok(layers: Sequence[str], statuses: Sequence[str], limit: int) -> bool:
    """U07-25/U07-26 bounds: 1..3 layers, 1..2 statuses, limit 1..200."""
    ok = in_set(layers, LAYERS, 3) and in_set(statuses, STATUSES, 2)
    return ok and limit_ok(limit, _CANDIDATE_LIMIT_MAX)


def marks(n: int) -> str:
    """``?, ?, …``: a constant fragment repeated ``n`` times, never built from values (C2)."""
    return ", ".join("?" * n)


def identity_clause(
    content_hash: str | None, task_hash: tuple[str, str] | None, fingerprint: str | None
) -> tuple[str, list[str]]:
    """U07-23: the constant clause and parameters of the one identity selector given."""
    if [content_hash, task_hash, fingerprint].count(None) != 2:  # noqa: PLR2004 - two unset
        msg = "find_memory_item needs exactly one selector"
        raise ToolInputError(msg)
    if content_hash is not None:
        name, params, ok = "content_hash", [content_hash], HEX32_RE.fullmatch(content_hash)
    elif task_hash is not None:
        name, params = "task_hash", list(task_hash)
        ok = TASK_ID_RE.fullmatch(task_hash[0]) and HEX32_RE.fullmatch(task_hash[1])
    else:
        fp = cast(str, fingerprint)
        name, params, ok = "fingerprint", [fp], _HEX16_RE.fullmatch(fp)
    if not ok:
        op = "find_memory_item"
        raise bad_filter(op)
    return _SELECTORS[name], params


def read_error(exc: sqlite3.Error, op: str) -> HernessError:
    """C5: busy or locked → StoreBusy(op=…); any other sqlite3.Error → SchemaViolation."""
    text = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text):
        return StoreBusy(f"ops store busy in {op}", op=op)
    return SchemaViolation(f"{op}: {type(exc).__name__}", op=op)


def query(sql: str, params: Params, conn: Conn, op: str) -> list[sqlite3.Row]:
    """C1/C5: run a read on ``conn`` or this thread's connection, errors mapped per C5."""
    try:
        rows: list[sqlite3.Row] = (conn or core.connection()).execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise read_error(exc, op) from exc
    return rows


def lookup(
    sql: str, ids: Sequence[str], pattern: re.Pattern[str], conn: Conn, op: str
) -> list[sqlite3.Row]:
    """Rows of ``sql`` (ending in ``IN (``) for the valid, deduplicated ``ids``; [] for none."""
    unique = valid_ids(ids, pattern, op)
    return query(f"{sql}{marks(len(unique))})", unique, conn, op) if unique else []


def write[T](fn: Callable[[sqlite3.Connection], T], conn: Conn, op: str) -> T:
    """C1: run ``fn`` on the caller's connection (errors propagate), else in ``run_write``."""
    return fn(conn) if conn is not None else core.run_write(fn, op=op)


def load_typed[T](text: str | None, kind: type[T], field: str, ref: str) -> T | None:
    """C3: a JSON column that must hold ``kind`` or NULL; else SchemaViolation naming the row."""
    try:
        value = core.load_json(text, field=field)
    except SchemaViolation:
        value = ...
    if value is None or isinstance(value, kind):
        return value
    msg = f"{field} invalid JSON for {ref}"
    raise SchemaViolation(msg)


def item_from_row(row: Sequence[object]) -> MemoryItemRow:
    """A ``SELECT`` row (``COLUMNS`` order) as ``MemoryItemRow`` with JSON objects parsed."""
    item = dict(zip(COLUMNS, row, strict=True))
    mid = cast(str, item["memory_id"])
    for name in ("data", "provenance"):
        text = cast(str, item[name])
        item[name] = load_typed(text, dict, f"memory_item.{name}", mid) or {}
    return cast(MemoryItemRow, item)


# --- U07-101 purge (R-54, TH07-21) ----------------------------------------------------------

RECORD_ID_MAX: Final = 300
_RECORD_ID_RE: Final = re.compile(r"[a-z_]+:[a-z_]+:.+")
_PURGE_CHUNK: Final = 500
_PURGE_COLS: Final = (
    "SELECT memory_id, json_extract(data,'$.review_item_id'),"
    " json_extract(data,'$.derived_review_item_id') FROM memory_item WHERE "
)
_PURGE_SQL: Final = {
    "record": _PURGE_COLS + "instr(content, ?) > 0 OR instr(data, ?) > 0"
    " OR instr(provenance, ?) > 0",
    "author": _PURGE_COLS + "json_extract(provenance,'$.author_ref') = ?",
}
_SCRUB_SQL: Final = "SELECT memory_id, data FROM memory_item WHERE instr(data, ?) > 0"
_SCRUB_SET: Final = "UPDATE memory_item SET data = ? WHERE memory_id = ?"


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


def purge_select(selector: PurgeSelector, conn: Conn, op: str) -> tuple[PurgeRows, _Scrubs]:
    """Steps 1-3: the delete set, the scrub rows (with their new ``data``), the review ids.

    A record id is also matched in its JSON-escaped form inside the JSON columns."""
    how, value = selector
    escaped = json.dumps(value, ensure_ascii=False)[1:-1]
    rows = query(_PURGE_SQL[how], [value, escaped, escaped] if how == "record" else [value],
                 conn, op)  # fmt: skip
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
    ``memory_item_ad`` removes the FTS rows), then store the scrubbed ``data``."""
    found, scrubs = purge_select(selector, conn, op)
    for chunk in batched(found.deleted_ids, _PURGE_CHUNK):
        sql = f"DELETE FROM memory_item WHERE memory_id IN ({marks(len(chunk))})"  # noqa: S608
        conn.execute(sql, chunk)
    for mid, data in scrubs:
        conn.execute(_SCRUB_SET, (core.dump_json(data, field="data"), mid))
    return found
