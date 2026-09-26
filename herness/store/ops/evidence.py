"""Ops evidence area: the only writer of `evidence` and `evidence_use` (impl 05 U05-71, U05-75).

Design 05 §4.1, §4.4; R-08, R-09, R-13. Writes go through `T02-04 (herness.store.ops.run_write)`
in their own `BEGIN IMMEDIATE` transaction; reads through `read_one` / `read_all`; JSON columns
through `dump_json` / `load_json` (impl 02 core API, R-10). `record_evidence` and
`record_evidence_use` are `INSERT OR IGNORE` on their primary keys, so a repeat after a crash
changes nothing. `get_evidence` recomputes `query_id` from the stored `sql`, `params` and
`build_id` before trusting a row (TH05-12): a mismatch is treated as tampering, logged and
answered with `None`, never raised. `scrub_record_from_evidence` is the impl 10 privacy
deletion hook for this area (TH05-24): it rewrites `evidence.result_sample` only, leaving
`query_id`, `sql`, `params`, `result_hash` and `row_count` untouched so the Verifier keeps
re-running queries instead of trusting samples.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import Final, cast

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.core.ids import query_id as compute_query_id
from herness.core.ids import sha256_hex, split_record_id
from herness.core.logging import get_logger
from herness.core.types import Evidence

from . import core

# fnd_ + 26 Crockford chars, per U05-71's own pattern (broader than herness.core.ids ULIDs:
# no restriction on the first char). Not for validation elsewhere, only for dropping bad ids.
_FINDING_ID_RE: Final = re.compile(r"fnd_[0-9A-HJKMNP-TV-Z]{26}")
_MAX_FINDING_IDS: Final = 500

_log = get_logger("store.ops")


def _insert_evidence(conn: sqlite3.Connection, ev: Evidence, params: str, sample: str) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO evidence (query_id, run_id, build_id, sql, params, result_hash,"
        " row_count, result_sample, executed_at, duration_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ev.query_id,
            ev.run_id,
            ev.build_id,
            ev.sql,
            params,
            ev.result_hash,
            ev.row_count,
            sample,
            clock.format_utc(ev.executed_at),
            ev.duration_ms,
        ),
    )
    return cur.rowcount == 1


def record_evidence(ev: Evidence) -> bool:
    """Insert one evidence row; True when inserted, False when `query_id` already existed."""
    params = core.dump_json(ev.params, field="evidence.params")
    sample = core.dump_json(ev.result_sample, field="evidence.result_sample")
    return core.run_write(
        lambda conn: _insert_evidence(conn, ev, params, sample), op="record_evidence"
    )


def record_evidence_use(query_id: str, run_id: str, task_id: str | None, used_at: datetime) -> bool:
    """Insert one evidence-use row; `task_id=None` dedupes as `""`. True when newly inserted."""
    used_text = clock.format_utc(used_at)
    task_key = task_id if task_id is not None else ""

    def _insert(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "INSERT OR IGNORE INTO evidence_use (query_id, run_id, task_id, used_at)"
            " VALUES (?, ?, ?, ?)",
            (query_id, run_id, task_key, used_text),
        )
        return cur.rowcount == 1

    return core.run_write(_insert, op="record_evidence_use")


def get_evidence(query_id: str) -> Evidence | None:
    """Return the row as `Evidence`, or None when absent or tampered (TH05-12).

    A tampered row (the stored `query_id` does not recompute from `sql`, `params` and
    `build_id`) logs a WARNING `harness.evidence.tampered` and is never raised.
    """
    row = core.read_one("SELECT * FROM evidence WHERE query_id = ?", (query_id,))
    if row is None:
        return None
    params = cast(dict[str, JsonValue], core.load_json(row["params"], field="evidence.params"))
    sql_text, build_id = str(row["sql"]), str(row["build_id"])
    try:
        expected = compute_query_id(sql_text, params, build_id)
    except SchemaViolation:
        expected = None
    if expected != row["query_id"]:
        _log.warning("harness.evidence.tampered", query_id=str(row["query_id"]))
        return None
    sample = cast(
        list[dict[str, JsonValue]],
        core.load_json(row["result_sample"], field="evidence.result_sample"),
    )
    return Evidence(
        query_id=str(row["query_id"]),
        run_id=row["run_id"],
        build_id=build_id,
        sql=sql_text,
        params=params,
        result_hash=str(row["result_hash"]),
        row_count=int(row["row_count"]),
        result_sample=sample,
        executed_at=clock.parse_utc(row["executed_at"]),
        duration_ms=int(row["duration_ms"]),
    )


def finding_statuses(finding_ids: Sequence[str]) -> dict[str, str]:
    """Return `{finding_id: status}` for the (at most 500) valid ids; unknown ids are absent."""
    ids = [fid for fid in finding_ids if _FINDING_ID_RE.fullmatch(fid)][:_MAX_FINDING_IDS]
    if not ids:
        return {}
    placeholders = ", ".join("?" * len(ids))
    # Only the placeholder count is interpolated, never data; ids are bound as query params.
    query = f"SELECT finding_id, status FROM finding WHERE finding_id IN ({placeholders})"  # noqa: S608
    rows = core.read_all(query, ids)
    return {str(r["finding_id"]): str(r["status"]) for r in rows}


def _kept_sample(sample: object, record_id: str) -> list[object] | None:
    """None when `sample` is not a list, else the rows that do not hold `record_id`."""
    if not isinstance(sample, list):
        return None
    return [
        item
        for item in sample
        if not (
            isinstance(item, dict)
            and any(isinstance(cell, str) and record_id in cell for cell in item.values())
        )
    ]


def _scrub_all(conn: sqlite3.Connection, record_id: str, needle: str) -> int:
    rows = conn.execute(
        "SELECT query_id, result_sample FROM evidence WHERE instr(result_sample, ?) > 0",
        (needle,),
    ).fetchall()
    updated = 0
    for row in rows:
        sample = core.load_json(row["result_sample"], field="evidence.result_sample")
        kept = _kept_sample(sample, record_id)
        if kept is None or len(kept) == len(cast(list[object], sample)):
            continue
        conn.execute(
            "UPDATE evidence SET result_sample = ? WHERE query_id = ?",
            (core.dump_json(kept, field="evidence.result_sample"), row["query_id"]),
        )
        updated += 1
    return updated


def scrub_record_from_evidence(record_id: str, /) -> int:
    """Privacy deletion: remove `record_id` from every `evidence.result_sample` (TH05-24).

    Idempotent: a second call returns 0. Raises SchemaViolation("invalid record_id") for a
    malformed id. `query_id`, `sql`, `params`, `result_hash` and `row_count` are untouched.
    """
    try:
        split_record_id(record_id)
    except SchemaViolation as exc:
        msg = "invalid record_id"
        raise SchemaViolation(msg) from exc
    needle = json.dumps(record_id, ensure_ascii=False)[1:-1]
    updated = core.run_write(lambda conn: _scrub_all(conn, record_id, needle), op="scrub_evidence")
    _log.info("harness.evidence.scrubbed", record_id_hash=sha256_hex(record_id)[:16], rows=updated)
    return updated
