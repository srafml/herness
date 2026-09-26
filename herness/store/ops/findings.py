"""Ops area ``findings`` (impl 06 U06-41 … U06-44, U06-144, R-08, R-77): ``finding`` rows.

Write functions run inside a write transaction on ``conn`` (the ``run_write`` callback, or a
spec 08 ``writes`` callback); read functions use ``read_one`` / ``read_all`` on the thread's
connection (R-10). ``transition_finding`` is the only status mutation (design 06 §6.5): a
compare-and-set that changes only ``status``, ``challenge`` (append), ``verification`` and
``merged_into`` (TH06-10). The only dynamic SQL is the ``IN (...)`` placeholder list, built from
the count of values, and fixed column terms (ENG §3.5). Evidence and chat rows belong to areas
``evidence`` (05) and ``chat`` (09) (R-09).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from typing import Final

from pydantic import ValidationError

from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.numbers import parse_markers
from herness.core.types import Challenge, Finding, FindingStatus, VerificationRecord

from . import core

_COLS: Final = (
    "finding_id, run_id, task_id, author_role, claim, entity_type, entity_id, supersedes,"
    " merged_into, numbers, query_ids, confidence, status, challenge, verification, created_at"
)
_SELECT: Final = f"SELECT {_COLS} FROM finding AS f"  # noqa: S608 - constant column list
# Reads skip rows whose numbers a full privacy scrub emptied (U06-144; Finding needs 1-20).
_KEPT: Final = "(json_type(f.numbers) <> 'array' OR json_array_length(f.numbers) > 0)"
_ORDER: Final = " ORDER BY created_at, finding_id"
_REVIEW_KINDS: Final = ("funding_review", "org_review")
_MAX_LIMIT: Final = 500
_MAX_DEPTH: Final = 4
_MAX_RECORD_ID: Final = 300
_REDACTED: Final = "[redacted]"


def _in(values: Collection[str]) -> tuple[str, list[str]]:
    """``(?, ?, ...)`` for ``values`` (empty ``()`` matches nothing in SQLite) and its params."""
    items = list(values)
    return "(" + ", ".join("?" * len(items)) + ")", items


def _invalid(finding_id: str) -> SchemaViolation:
    """The row error of U06-43 / U06-144; it names the id, never row content."""
    return SchemaViolation(f"finding invalid: finding_id={finding_id}")


def _from_row(row: sqlite3.Row) -> Finding:
    finding_id: str = row["finding_id"]
    try:
        values = {key: row[key] for key in row.keys()}  # noqa: SIM118 - sqlite3.Row
        for key in ("numbers", "query_ids", "verification"):
            values[key] = core.load_json(row[key], field=f"finding.{key}")
        values["challenge"] = core.load_json(row["challenge"], field="finding.challenge") or []
        return Finding.model_validate(values)
    except (ValidationError, SchemaViolation) as exc:
        raise _invalid(finding_id) from exc


def _check_limit(limit: int) -> None:
    if not 1 <= limit <= _MAX_LIMIT:
        msg = f"limit must be 1..{_MAX_LIMIT}"
        raise ValueError(msg)


# --- writes (U06-41, U06-42) ----------------------------------------------------------------


def insert_finding(conn: sqlite3.Connection, f: Finding) -> bool:
    """Insert one ``finding`` row; False when ``finding_id`` exists (U06-41).

    ``ON CONFLICT(finding_id) DO NOTHING`` rather than ``INSERT OR IGNORE``, so a CHECK
    violation raises (``SchemaViolation`` via ``run_write``) instead of reading as a replay."""
    data = f.model_dump(mode="json")
    ver = data["verification"]
    cursor = conn.execute(
        f"INSERT INTO finding ({_COLS}) VALUES ({', '.join('?' * 16)})"  # noqa: S608 - constants
        " ON CONFLICT(finding_id) DO NOTHING",
        (
            f.finding_id,
            f.run_id,
            f.task_id,
            f.author_role,
            f.claim,
            f.entity_type,
            f.entity_id,
            f.supersedes,
            f.merged_into,
            core.dump_json(data["numbers"], field="finding.numbers"),
            core.dump_json(data["query_ids"], field="finding.query_ids"),
            f.confidence,
            f.status,
            core.dump_json(data["challenge"], field="finding.challenge"),
            None if ver is None else core.dump_json(ver, field="finding.verification"),
            clock.format_utc(f.created_at),
        ),
    )
    return cursor.rowcount == 1


def transition_finding(  # noqa: PLR0913 - U06-42 signature
    conn: sqlite3.Connection,
    finding_id: str,
    to: FindingStatus,
    allowed_from: Collection[FindingStatus],
    *,
    append_challenge: Challenge | None = None,
    verification: VerificationRecord | None = None,
    merged_into: str | None = None,
) -> bool:
    """Compare-and-set ``status`` from ``allowed_from`` to ``to`` (U06-42, design 06 §6.5).

    True when one row changed. ``append_challenge`` is appended to ``challenge``;
    ``verification`` and ``merged_into`` are set only when given. ``to == "merged"`` without
    ``merged_into``, or ``merged_into`` with another ``to``, raises ConfigError."""
    if (to == "merged") != (merged_into is not None):
        msg = "merged_into is required for status 'merged' and only for it"
        raise ConfigError(msg)
    sets = ["status = ?"]
    params: list[object] = [to]
    if append_challenge is not None:
        sets.append("challenge = json_insert(coalesce(challenge, '[]'), '$[#]', json(?))")
        dumped = append_challenge.model_dump(mode="json")
        params.append(core.dump_json(dumped, field="finding.challenge"))
    if verification is not None:
        sets.append("verification = ?")
        dumped = verification.model_dump(mode="json")
        params.append(core.dump_json(dumped, field="finding.verification"))
    if merged_into is not None:
        sets.append("merged_into = ?")
        params.append(merged_into)
    marks, allowed = _in(allowed_from)
    cursor = conn.execute(
        f"UPDATE finding SET {', '.join(sets)}"  # noqa: S608 - fixed column terms
        f" WHERE finding_id = ? AND status IN {marks}",
        (*params, finding_id, *allowed),
    )
    return cursor.rowcount == 1


# --- reads (U06-43, U06-44) -----------------------------------------------------------------


def _filters(
    entity_type: str | None, lists: tuple[tuple[str, Collection[str] | None], ...]
) -> tuple[list[str], list[object]]:
    """``WHERE`` terms and params for the filters that are not None."""
    clauses: list[str] = []
    params: list[object] = []
    if entity_type is not None:
        clauses.append("f.entity_type = ?")
        params.append(entity_type)
    for column, values in lists:
        if values is not None:
            marks, items = _in(values)
            clauses.append(f"f.{column} IN {marks}")
            params += items
    return clauses, params


def query_findings(  # noqa: PLR0913 - U06-43 signature
    run_id: str,
    *,
    statuses: Collection[str] | None = None,
    entity_type: str | None = None,
    entity_ids: Collection[str] | None = None,
    task_ids: Collection[str] | None = None,
    author_roles: Collection[str] | None = None,
    min_confidence: float | None = None,
    limit: int,
) -> list[Finding]:
    """Findings of ``run_id`` matching every filter that is not None (U06-43).

    Ordered by ``created_at, finding_id``; ``limit`` 1-500 (ValueError otherwise)."""
    _check_limit(limit)
    lists = (
        ("status", statuses),
        ("entity_id", entity_ids),
        ("task_id", task_ids),
        ("author_role", author_roles),
    )
    clauses, params = _filters(entity_type, lists)
    if min_confidence is not None:
        clauses.append("f.confidence >= ?")
        params.append(min_confidence)
    where = " AND ".join(["f.run_id = ?", _KEPT, *clauses])
    sql = f"{_SELECT} WHERE {where}{_ORDER} LIMIT ?"
    rows = core.read_all(sql, [run_id, *params, limit])
    return [_from_row(r) for r in rows]


def get_findings(finding_ids: Collection[str]) -> dict[str, Finding]:
    """The findings with ``finding_ids`` by id; unknown ids are absent (U06-43)."""
    marks, params = _in(finding_ids)
    rows = core.read_all(f"{_SELECT} WHERE {_KEPT} AND finding_id IN {marks}{_ORDER}", params)
    return {r["finding_id"]: _from_row(r) for r in rows}


def list_task_findings(task_id: str) -> list[Finding]:
    """Findings posted by ``task_id``, ordered by ``created_at, finding_id`` (U06-43)."""
    rows = core.read_all(f"{_SELECT} WHERE {_KEPT} AND task_id = ?{_ORDER}", (task_id,))
    return [_from_row(r) for r in rows]


def query_verified_findings_recent(
    *,
    entity_type: str | None = None,
    entity_ids: Collection[str] | None = None,
    limit: int,
    max_runs: int = 5,
) -> list[Finding]:
    """``verified`` findings of the ``max_runs`` newest ``done`` review runs, for chat (U06-44).

    Ordered by run recency (``finished_at`` descending), then ``created_at``."""
    _check_limit(limit)
    if max_runs < 1:
        msg = "max_runs must be >= 1"
        raise ValueError(msg)
    clauses, params = _filters(entity_type, (("entity_id", entity_ids),))
    where = " AND ".join(["f.status = 'verified'", _KEPT, *clauses])
    columns = ", ".join(f"f.{c.strip()}" for c in _COLS.split(","))
    sql = (
        f"SELECT {columns} FROM finding AS f JOIN ("  # noqa: S608 - constants and placeholders
        "SELECT run_id, finished_at FROM run WHERE kind IN (?, ?) AND status = 'done'"
        " ORDER BY finished_at DESC, run_id DESC LIMIT ?) AS r ON r.run_id = f.run_id"
        f" WHERE {where}"
        " ORDER BY r.finished_at DESC, r.run_id DESC, f.created_at, f.finding_id LIMIT ?"
    )
    rows = core.read_all(sql, [*_REVIEW_KINDS, max_runs, *params, limit])
    return [_from_row(r) for r in rows]


# --- privacy scrub (U06-144, R-77) ----------------------------------------------------------


def _cites(value: object, targets: frozenset[str], depth: int) -> bool:
    """True when a string equal to a target sits at ``depth`` ≤ 4 inside ``value``."""
    if isinstance(value, str):
        return value in targets
    if depth >= _MAX_DEPTH or not isinstance(value, dict | list):
        return False
    children = value.values() if isinstance(value, dict) else value
    return any(_cites(child, targets, depth + 1) for child in children)


def _redact_markers(claim: str, dropped: set[str]) -> str:
    """Replace the ``[[nX]]`` markers of ``dropped`` ids with ``[redacted]``, right to left."""
    for marker in reversed(parse_markers(claim).markers):
        if marker.id in dropped:
            claim = claim[: marker.start] + _REDACTED + claim[marker.end :]
    return claim


def scrub_record_from_findings(record_id: str, *, conn: sqlite3.Connection) -> int:
    """Privacy deletion: drop ``numbers`` elements citing ``record_id`` (U06-144, R-77).

    An element is dropped when a string value at depth ≤ 4 equals ``record_id`` or its key
    (the text after the second ``:``); its ``[[nX]]`` markers in ``claim`` become
    ``[redacted]``. Returns the rows changed; a second call returns 0. Values are never
    logged. Invalid ``numbers`` JSON → SchemaViolation("finding invalid: finding_id=<id>")."""
    parts = record_id.split(":", 2)
    key = parts[2] if len(parts) == 3 else record_id  # noqa: PLR2004 - <source>:<kind>:<key>
    if not key or len(record_id) > _MAX_RECORD_ID:  # an empty key would match everything
        msg = "invalid record_id"
        raise SchemaViolation(msg)
    targets = frozenset({record_id, key})
    # Needles are the JSON-escaped forms, as the values appear inside the stored TEXT.
    needles = [core.dump_json(t, field="record_id")[1:-1] for t in (record_id, key)]
    rows = conn.execute(
        "SELECT finding_id, numbers, claim FROM finding"
        " WHERE instr(numbers, ?) > 0 OR instr(numbers, ?) > 0",
        needles,
    ).fetchall()
    updated = 0
    for finding_id, numbers_text, claim in rows:
        try:
            numbers = core.load_json(numbers_text, field="finding.numbers")
        except SchemaViolation as exc:
            raise _invalid(finding_id) from exc
        if not isinstance(numbers, list):
            raise _invalid(finding_id)
        kept = [n for n in numbers if not _cites(n, targets, 0)]
        if len(kept) == len(numbers):
            continue
        dropped = {str(n.get("id")) for n in numbers if isinstance(n, dict) and n not in kept}
        reduced = core.dump_json(kept, field="finding.numbers")
        sql = "UPDATE finding SET numbers = ?, claim = ? WHERE finding_id = ?"
        conn.execute(sql, (reduced, _redact_markers(claim, dropped), finding_id))
        updated += 1
    return updated
