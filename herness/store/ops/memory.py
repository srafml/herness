"""Ops area ``memory`` (impl 07 U07-21 … U07-30, U07-35; owner 07, R-08).

The only writer of ``memory_item`` (``memory_fts`` follows through the migration 004 triggers);
read-only lookups on ``evidence``, ``finding`` and the task checkpoint. Conventions C1 … C7 of
impl 07 §3.5: with ``conn`` a function joins the caller's ``run_write`` transaction, else it reads
on this thread's connection or wraps its write in ``run_write(op=<function>)``; SQL is constant
and bound. Row types and plumbing live in the private sibling ``_memory_rows``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Final, cast

from pydantic import JsonValue

from herness.core.errors import SchemaViolation, ToolInputError
from herness.core.ids import canonical_json

from . import core
from ._memory_rows import (
    CHECKED,
    COLUMNS,
    ENTITY_ID_MAX,
    ENTITY_IDS_MAX,
    FINDING_ID_RE,
    FTS_CHECK,
    FTS_REBUILD,
    HEX32_RE,
    INCREMENT_MAX,
    INSERT,
    KINDS,
    LAYERS,
    MAINTENANCE,
    MAINTENANCE_LIMIT_MAX,
    MEMORY_ID_RE,
    PENDING_COUNT,
    PROPOSAL_FILTERS,
    PROPOSALS,
    QUERY_ID_RE,
    SELECT,
    SESSION_ID_MAX,
    SESSION_IDS,
    STATUSES,
    TASK_ID_RE,
    TOUCH_IDS_MAX,
    UNCHANGED,
    UPDATABLE,
    Conn,
    EvidenceRow,
    FindingFact,
    MemoryItemRow,
    Selector,
    Unchanged,
    bad_filter,
    check_columns,
    filters_ok,
    identity_clause,
    in_set,
    is_count,
    item_from_row,
    limit_ok,
    load_typed,
    lookup,
    marks,
    query,
    read_error,
    valid_ids,
    write,
)

__all__ = [
    "UNCHANGED",
    "EvidenceRow",
    "FindingFact",
    "MemoryItemRow",
    "Selector",
    "Unchanged",
    "count_proposals",
    "entity_candidates",
    "evidence_rows",
    "existing_query_ids",
    "find_memory_item",
    "finding_facts",
    "fts_candidates",
    "fts_check_and_rebuild",
    "get_memory_items",
    "get_task_scratchpad",
    "insert_memory_item",
    "maintenance_rows",
    "pending_embedding_count",
    "session_memory_ids",
    "touch_memory_items",
    "update_memory_item",
]

_LIVE: Final = ("active", "pending_approval")


# --- U07-21 … U07-24 items ------------------------------------------------------------------


def insert_memory_item(row: MemoryItemRow, *, conn: Conn = None) -> None:
    """Insert one ``memory_item`` row; trigger ``memory_item_ai`` indexes it (U07-21)."""
    op = "insert_memory_item"
    check_columns({c: row.get(c, ...) for c in COLUMNS if c in CHECKED}, op)  # missing key fails
    values = {**row, "data": core.dump_json(row["data"], field="data")}
    values["provenance"] = core.dump_json(row["provenance"], field="provenance")
    params = [values[c] for c in COLUMNS]
    write(lambda c: c.execute(INSERT, params), conn, op)


def get_memory_items(memory_ids: Sequence[str], *, conn: Conn = None) -> list[MemoryItemRow]:
    """Rows in input order; missing ids omitted, repeated ids returned once (U07-22)."""
    op = "get_memory_items"
    rows = lookup(f"{SELECT} WHERE memory_id IN (", memory_ids, MEMORY_ID_RE, conn, op)
    found = {row[0]: item_from_row(row) for row in rows}
    return [found[i] for i in dict.fromkeys(memory_ids) if i in found]


def find_memory_item(  # noqa: PLR0913 - keyword-only signature fixed by U07-23
    *,
    layer: str | None = None,
    kind: str | None = None,
    content_hash: str | None = None,
    task_hash: tuple[str, str] | None = None,
    fingerprint: str | None = None,
    statuses: Sequence[str],
    conn: Conn = None,
) -> MemoryItemRow | None:
    """The oldest item by (created_at, memory_id) matching one identity selector (U07-23)."""
    op = "find_memory_item"
    where, params = identity_clause(content_hash, task_hash, fingerprint)
    if len(set(statuses)) != len(statuses) or not in_set(statuses, STATUSES, 5):
        raise bad_filter(op)
    filters = ((layer, LAYERS, " AND layer = ?"), (kind, KINDS, " AND kind = ?"))
    for value, allowed, clause in filters:
        if value is not None:
            if value not in allowed:
                raise bad_filter(op)
            where, params = where + clause, [*params, value]
    sql = f"{SELECT} WHERE {where} AND status IN ({marks(len(statuses))})"
    rows = query(sql + " ORDER BY created_at, memory_id LIMIT 1", [*params, *statuses], conn, op)
    return item_from_row(rows[0]) if rows else None


def update_memory_item(  # noqa: PLR0913 - keyword-only signature fixed by U07-24
    memory_id: str,
    *,
    status: str | None = None,
    content: str | None = None,
    data: dict[str, JsonValue] | None = None,
    provenance: dict[str, JsonValue] | None = None,
    confidence: float | None = None,
    expires_at: str | Unchanged | None = UNCHANGED,
    last_used_at: str | None = None,
    use_count_increment: int = 0,
    conn: Conn = None,
) -> int:
    """Change the given columns of one item; rows changed, 0 for an unknown id (U07-24)."""
    op = "update_memory_item"
    given: dict[str, object] = {"status": status, "content": content, "data": data}
    given |= {"provenance": provenance, "confidence": confidence, "last_used_at": last_used_at}
    values = {k: v for k, v in given.items() if v is not None}
    if not isinstance(expires_at, Unchanged):
        values["expires_at"] = expires_at
    if not values and use_count_increment == 0:
        msg = f"{op}: nothing to change"
        raise ToolInputError(msg)
    check_columns({"memory_id": memory_id, **values}, op)
    if not (is_count(use_count_increment) and use_count_increment <= INCREMENT_MAX):
        msg = f"{op}: invalid use_count"
        raise SchemaViolation(msg)
    for name in ("data", "provenance"):
        if name in values:
            values[name] = core.dump_json(values[name], field=name)
    names = [c for c in UPDATABLE if c in values]  # column names only from the constant list
    bump = use_count_increment > 0
    sets = [f"{c} = ?" for c in names] + ["use_count = use_count + ?"] * bump
    params = [values[c] for c in names] + [use_count_increment] * bump
    sql = f"UPDATE memory_item SET {', '.join(sets)} WHERE memory_id = ?"  # noqa: S608 - allowlist
    return write(lambda c: c.execute(sql, [*params, memory_id]).rowcount, conn, op)


def touch_memory_items(
    memory_ids: Sequence[str], *, now: str, statuses: Sequence[str] = _LIVE, conn: Conn = None
) -> int:
    """Record one use of each listed item whose status is in ``statuses`` (U07-24)."""
    op = "touch_memory_items"
    ids = valid_ids(memory_ids, MEMORY_ID_RE, op, most=TOUCH_IDS_MAX)
    if not ids:
        msg = f"invalid id: {op}"
        raise ToolInputError(msg)
    if not in_set(statuses, STATUSES, 5):
        raise bad_filter(op)
    sql = (
        "UPDATE memory_item SET last_used_at = ?, use_count = use_count + 1"  # noqa: S608 - placeholders only
        f" WHERE memory_id IN ({marks(len(ids))}) AND status IN ({marks(len(statuses))})"
    )
    return write(lambda c: c.execute(sql, [now, *ids, *statuses]).rowcount, conn, op)


# --- U07-25 … U07-27 recall candidates and proposal counts ----------------------------------


def fts_candidates(
    match: str, *, layers: Sequence[str], statuses: Sequence[str], limit: int, conn: Conn = None
) -> list[tuple[str, float]]:
    """(memory_id, bm25) keyword candidates, best first; FTS5 syntax error → [] (U07-25)."""
    op = "fts_candidates"
    if not filters_ok(layers, statuses, limit):
        raise bad_filter(op)
    sql = (
        "SELECT m.memory_id, bm25(memory_fts) FROM memory_fts"  # noqa: S608 - placeholders only
        " JOIN memory_item m ON m.rowid = memory_fts.rowid WHERE memory_fts MATCH ?"
        f" AND m.layer IN ({marks(len(layers))}) AND m.status IN ({marks(len(statuses))})"
        " ORDER BY bm25(memory_fts) LIMIT ?"
    )
    try:  # not through query(): the FTS5 message must stay visible
        rows = (conn or core.connection()).execute(sql, [match, *layers, *statuses, limit])
        return [(str(r[0]), float(r[1])) for r in rows.fetchall()]
    except sqlite3.Error as exc:
        if isinstance(exc, sqlite3.OperationalError) and "fts5: syntax error" in str(exc):
            return []  # TH07-08: a rejected query never widens to more rows
        raise read_error(exc, op) from exc


def entity_candidates(
    entity_ids: Sequence[str], *, layers: Sequence[str], statuses: Sequence[str], limit: int,
    conn: Conn = None,
) -> list[str]:  # fmt: skip
    """Distinct ids whose ``data.entities`` names one of ``entity_ids``, newest first (U07-26)."""
    op = "entity_candidates"
    good = all(1 <= len(e) <= ENTITY_ID_MAX for e in entity_ids)
    if not (
        good and 1 <= len(entity_ids) <= ENTITY_IDS_MAX and filters_ok(layers, statuses, limit)
    ):
        raise bad_filter(op)
    sql = (  # CASE: a non-object entity (bad data) is skipped instead of failing json_extract
        "SELECT DISTINCT m.memory_id, m.created_at"  # noqa: S608 - placeholders only
        " FROM memory_item m, json_each(m.data, '$.entities') e"
        " WHERE CASE WHEN e.type = 'object' THEN json_extract(e.value, '$.id') END"
        f" IN ({marks(len(entity_ids))}) AND m.layer IN ({marks(len(layers))})"
        f" AND m.status IN ({marks(len(statuses))})"
        " ORDER BY m.created_at DESC, m.memory_id DESC LIMIT ?"
    )
    return [str(r[0]) for r in query(sql, [*entity_ids, *layers, *statuses, limit], conn, op)]


def count_proposals(
    *,
    run_id: str | None = None,
    session_id: str | None = None,
    author_ref: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    conn: Conn = None,
) -> int:
    """Proposals (``provenance.via`` tool/chat/dashboard/cli) matching every filter (U07-27)."""
    op = "count_proposals"
    if run_id is None and session_id is None and author_ref is None:
        msg = "count_proposals needs a scope"
        raise ToolInputError(msg)
    bad = session_id is not None and len(session_id) > SESSION_ID_MAX
    bad |= author_ref is not None and not HEX32_RE.fullmatch(author_ref)
    if bad or (kind is not None and kind not in KINDS):
        raise bad_filter(op)
    given = {"run_id": run_id, "session_id": session_id, "author_ref": author_ref}
    given |= {"kind": kind, "since": since}
    used = [(clause, given[name]) for name, clause in PROPOSAL_FILTERS if given[name] is not None]
    sql = PROPOSALS + "".join(clause for clause, _ in used)
    return int(query(sql, [value for _, value in used], conn, op)[0][0])


# --- U07-28, U07-29 read-only lookups on other areas' tables --------------------------------


def existing_query_ids(query_ids: Sequence[str], *, conn: Conn = None) -> set[str]:
    """The ids of ``query_ids`` present in ``evidence`` (U07-28)."""
    sql = "SELECT query_id FROM evidence WHERE query_id IN ("
    return {str(r[0]) for r in lookup(sql, query_ids, QUERY_ID_RE, conn, "existing_query_ids")}


def finding_facts(finding_ids: Sequence[str], *, conn: Conn = None) -> dict[str, FindingFact]:
    """``{finding_id: FindingFact}`` for the ids that exist; NULL confidence reads 0.0 (U07-28)."""
    sql = (
        "SELECT finding_id, status, confidence, run_id, task_id, query_ids, verification"
        " FROM finding WHERE finding_id IN ("
    )
    rows = lookup(sql, finding_ids, FINDING_ID_RE, conn, "finding_facts")
    return {
        fid: FindingFact(
            status=status,
            confidence=0.0 if conf is None else float(conf),
            run_id=run_id,
            task_id=task_id,
            query_ids=load_typed(qids, list, "finding.query_ids", fid) or [],
            verification=load_typed(verification, dict, "finding.verification", fid),
        )
        for fid, status, conf, run_id, task_id, qids, verification in rows
    }


def evidence_rows(query_ids: Sequence[str], *, conn: Conn = None) -> dict[str, EvidenceRow]:
    """``{query_id: EvidenceRow}`` for the ids that exist, ``params`` parsed (U07-28)."""
    sql = "SELECT query_id, sql, params, build_id FROM evidence WHERE query_id IN ("
    rows = lookup(sql, query_ids, QUERY_ID_RE, conn, "evidence_rows")
    return {
        qid: EvidenceRow(
            sql=sql, params=load_typed(p, dict, "evidence.params", qid) or {}, build_id=b
        )
        for qid, sql, p, b in rows
    }


def get_task_scratchpad(task_id: str, *, conn: Conn = None) -> str | None:
    """Canonical JSON of the checkpoint envelope's ``scratchpad`` key, else None (U07-29, R-21).

    Reads ``task.checkpoint`` with its own SQL: an area never imports another area (the
    ``ops-areas-acyclic`` contract), so ``herness.store.ops.runs.get_task`` is not called."""
    op = "get_task_scratchpad"
    valid_ids([task_id], TASK_ID_RE, op)
    rows = query("SELECT checkpoint FROM task WHERE task_id = ?", [task_id], conn, op)
    if not rows:
        msg = f"task {task_id} missing"
        raise SchemaViolation(msg)
    value = (load_typed(rows[0][0], dict, "task.checkpoint", task_id) or {}).get("scratchpad")
    return None if value is None else canonical_json(value)


# --- U07-30, U07-35 maintenance and chat sessions -------------------------------------------


def maintenance_rows(
    *,
    selector: Selector,
    now: str,
    limit: int,
    after: str = "",
    review_cutoff: str | None = None,
    conn: Conn = None,
) -> list[MemoryItemRow] | list[tuple[str, str]]:
    """Rows for one maintenance selector, ordered by memory_id for stable paging (U07-30)."""
    op = "maintenance_rows"
    if selector not in MAINTENANCE:
        raise bad_filter(op, "invalid selector")
    if not limit_ok(limit, MAINTENANCE_LIMIT_MAX):
        raise bad_filter(op, "invalid limit")
    if selector == "business_rule_review_due" and review_cutoff is None:
        raise bad_filter(op, "review_cutoff required")
    params = {"now": now, "limit": limit, "after": after, "review_cutoff": review_cutoff}
    rows = query(MAINTENANCE[selector], params, conn, op)
    if selector == "all_ids_status":
        return [(str(r[0]), str(r[1])) for r in rows]
    return [item_from_row(r) for r in rows]


def fts_check_and_rebuild(*, conn: Conn = None) -> bool:
    """FTS5 integrity check against ``memory_item``; rebuild on failure, True if rebuilt (U07-30).

    ``rank = 1`` makes the check compare the index with the external-content table, which is how
    a rowid desync shows (e.g. a VACUUM renumbering the implicit rowids of this TEXT-keyed table).
    """
    try:
        write(lambda c: c.execute(FTS_CHECK), conn, "fts_check")
    except sqlite3.DatabaseError:  # only with the caller's conn: run_write maps it otherwise
        cast(sqlite3.Connection, conn).execute(FTS_REBUILD)
        return True
    except SchemaViolation as exc:
        if not isinstance(exc.__cause__, sqlite3.DatabaseError):
            raise
        core.run_write(lambda c: c.execute(FTS_REBUILD), op="fts_rebuild")
        return True
    return False


def pending_embedding_count(*, conn: Conn = None) -> int:
    """Items still waiting for an embedding, rejected ones excluded (U07-30)."""
    return int(query(PENDING_COUNT, (), conn, "pending_embedding_count")[0][0])


def session_memory_ids(session_id: str, *, conn: Conn = None) -> list[str]:
    """Ids of pending or active items written from one chat session, oldest first (U07-35)."""
    op = "session_memory_ids"
    if not 1 <= len(session_id) <= SESSION_ID_MAX:
        msg = f"invalid id: {op}"
        raise ToolInputError(msg)
    return [str(r[0]) for r in query(SESSION_IDS, [session_id], conn, op)]
