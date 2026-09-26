"""Unit tests for herness.store.ops.memory (impl 07 U07-20 … U07-30, U07-35).

UT07-06 … UT07-09. Every test runs on a fresh ops store migrated through 070; rows are written
through the memory functions or raw SQL for set-up (task, finding, evidence belong to other areas).
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from herness.core import time as clock
from herness.core.errors import FatalError, SchemaViolation, StoreBusy, ToolInputError
from herness.core.ids import new_ulid
from herness.store import ops
from herness.store.ops import _memory_rows, core, memory
from herness.store.ops.memory import UNCHANGED, MemoryItemRow

pytestmark = pytest.mark.unit

_T0 = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=datetime.UTC)
_INDEXES_070 = (
    "ix_memory_expires",
    "ix_memory_content_hash",
    "ix_memory_task",
    "ix_memory_fingerprint",
    "ix_memory_rec",
    "ix_memory_prov_run",
    "ix_memory_prov_session",
    "ix_memory_prov_author",
    "ix_rec_target",
    "ix_rec_metric",
)
_HASH = "0123456789abcdef" * 2
_AUTHOR = "ab" * 16


@pytest.fixture(autouse=True)
def migrated(ops_store: Path) -> Path:
    ops.migrate()
    return ops_store


def _at(minutes: int) -> str:
    return clock.format_utc(_T0 + datetime.timedelta(minutes=minutes))


def _mid() -> str:
    return "mem_" + new_ulid()


def _item(**over: Any) -> MemoryItemRow:
    row: dict[str, Any] = {
        "memory_id": _mid(),
        "layer": "semantic",
        "kind": "glossary",
        "content": "churn means cancelled subscriptions",
        "data": {"content_hash": _HASH},
        "provenance": {"author_type": "agent", "run_id": None},
        "confidence": 0.5,
        "status": "active",
        "created_at": _at(0),
        "expires_at": None,
        "last_used_at": None,
        "use_count": 0,
    }
    row.update(over)
    return row  # type: ignore[return-value]


def _put(**over: Any) -> MemoryItemRow:
    row = _item(**over)
    memory.insert_memory_item(row)
    return row


def _write(sql: str, params: tuple[object, ...] = ()) -> None:
    core.run_write(lambda c: c.execute(sql, params), op="test_write")


def _fts(term: str) -> list[str]:
    rows = core.read_all(
        "SELECT m.memory_id FROM memory_fts JOIN memory_item m ON m.rowid = memory_fts.rowid"
        " WHERE memory_fts MATCH ? ORDER BY m.memory_id",
        (term,),
    )
    return [str(r[0]) for r in rows]


def _plan(sql: str, params: tuple[object, ...]) -> str:
    rows = core.read_all(f"EXPLAIN QUERY PLAN {sql}", params)
    return " ".join(str(r["detail"]) for r in rows)


# --- UT07-06 070 indexes and FTS triggers ---------------------------------------------------


def test_ut07_06_the_ten_070_indexes_exist() -> None:
    """UT07-06 the ten 070 indexes exist on their tables; 070 is recorded in schema_migration."""
    rows = core.read_all("SELECT name, tbl_name FROM sqlite_schema WHERE type = 'index'")
    tables = {str(r["name"]): str(r["tbl_name"]) for r in rows}
    for name in _INDEXES_070:
        expected = "recommendation" if name.startswith("ix_rec_") else "memory_item"
        assert tables[name] == expected, name
    names = [str(r[0]) for r in core.read_all("SELECT name FROM schema_migration")]
    assert "memory_indexes" in names


def test_ut07_06_expression_indexes_serve_the_lookups() -> None:
    """UT07-06 the selector queries of U07-23, U07-27, U07-30, U07-35 use the 070 indexes."""
    base = "SELECT memory_id FROM memory_item WHERE "
    cases = {
        "ix_memory_content_hash": ("json_extract(data,'$.content_hash') = ?", ("x",)),
        "ix_memory_fingerprint": (
            "kind = 'sql_template' AND json_extract(data,'$.fingerprint') = ?",
            ("x",),
        ),
        "ix_memory_prov_session": ("json_extract(provenance,'$.session_id') = ?", ("s",)),
        "ix_memory_expires": ("expires_at <= ?", (_at(0),)),
    }
    for index, (where, params) in cases.items():
        assert index in _plan(base + where, params), index


def test_ut07_06_fts_reflects_insert_update_delete() -> None:
    """UT07-06 memory_fts MATCH reflects insert, content update and delete (004 triggers)."""
    row = _put(content="alpha revenue")
    assert _fts("alpha") == [row["memory_id"]]
    assert memory.update_memory_item(row["memory_id"], content="beta revenue") == 1
    assert _fts("alpha") == []
    assert _fts("beta") == [row["memory_id"]]
    _write("DELETE FROM memory_item WHERE memory_id = ?", (row["memory_id"],))
    assert _fts("beta") == []
    assert _fts("revenue") == []


# --- UT07-07 insert, get, find, update, touch -----------------------------------------------


def test_ut07_07_round_trip_in_input_order() -> None:
    """UT07-07 inserted rows come back equal, in input order, missing omitted, repeats once."""
    a = _put(data={"content_hash": _HASH, "nested": {"k": [1, 2]}}, expires_at=_at(60))
    b = _put(layer="procedural", kind="sql_template", last_used_at=_at(5), use_count=3)
    missing = _mid()
    got = memory.get_memory_items([b["memory_id"], missing, a["memory_id"], b["memory_id"]])
    assert got == [b, a]
    assert memory.get_memory_items([]) == []


def test_ut07_07_get_rejects_bad_ids() -> None:
    """UT07-07 more than 500 ids or a malformed id raise ToolInputError before SQL."""
    with pytest.raises(ToolInputError, match="too many ids: get_memory_items"):
        memory.get_memory_items([_mid() for _ in range(501)])
    with pytest.raises(ToolInputError, match="invalid id: get_memory_items"):
        memory.get_memory_items(["mem_bad"])


def test_ut07_07_insert_validates_each_column() -> None:
    """UT07-07 each app-level column check raises SchemaViolation naming the column."""
    bad = {
        "memory_id": "mem_x",
        "layer": "working",
        "kind": "note",
        "status": "gone",
        "content": "x" * 8001,
        "confidence": 1.5,
        "use_count": -1,
        "data": [1],
        "provenance": "{}",
    }
    for column, value in bad.items():
        with pytest.raises(SchemaViolation, match=rf"insert_memory_item: invalid {column}$"):
            memory.insert_memory_item(_item(**{column: value}))
    for column in ("use_count", "created_at", "expires_at"):
        row = _item()
        del row[column]  # type: ignore[misc]
        with pytest.raises(SchemaViolation, match=rf"insert_memory_item: invalid {column}$"):
            memory.insert_memory_item(row)
    assert memory.pending_embedding_count() == 0
    assert core.read_one("SELECT COUNT(*) FROM memory_item")[0] == 0  # type: ignore[index]


def test_ut07_07_insert_duplicate_and_caller_transaction() -> None:
    """UT07-07 a duplicate id is a SchemaViolation; with conn the insert joins that transaction."""
    row = _put()
    with pytest.raises(SchemaViolation, match="ops constraint failed in insert_memory_item"):
        memory.insert_memory_item(row)
    other = _item()

    def fn(conn: sqlite3.Connection) -> None:
        memory.insert_memory_item(other, conn=conn)
        assert memory.get_memory_items([other["memory_id"]], conn=conn) == [other]
        raise RuntimeError

    with pytest.raises(FatalError):
        core.run_write(fn, op="test_write")
    assert memory.get_memory_items([other["memory_id"]]) == []


def test_ut07_07_find_by_hash_task_fingerprint_oldest_first() -> None:
    """UT07-07 each selector finds the oldest match by (created_at, memory_id)."""
    task = "task_" + new_ulid()
    newer = _put(created_at=_at(5), provenance={"task_id": task})
    older = _put(created_at=_at(1), provenance={"task_id": task})
    _put(created_at=_at(0), status="rejected")
    tpl = _put(layer="procedural", kind="sql_template", data={"fingerprint": "0123456789abcdef"})
    active = ("active", "pending_approval")
    assert memory.find_memory_item(content_hash=_HASH, statuses=active) == older
    assert memory.find_memory_item(task_hash=(task, _HASH), statuses=active) == older
    assert (
        memory.find_memory_item(
            fingerprint="0123456789abcdef", layer="procedural", kind="sql_template", statuses=active
        )
        == tpl
    )
    assert memory.find_memory_item(content_hash=_HASH, kind="insight", statuses=active) is None
    assert memory.find_memory_item(content_hash=_HASH, layer="episodic", statuses=active) is None
    _write("UPDATE memory_item SET status = 'expired' WHERE memory_id = ?", (older["memory_id"],))
    assert memory.find_memory_item(content_hash=_HASH, statuses=active) == newer


def test_ut07_07_find_rejects_bad_selectors_and_filters() -> None:
    """UT07-07 zero or two selectors, and filters outside their sets, raise ToolInputError."""
    one = "find_memory_item needs exactly one selector"
    with pytest.raises(ToolInputError, match=one):
        memory.find_memory_item(statuses=["active"])
    with pytest.raises(ToolInputError, match=one):
        memory.find_memory_item(content_hash=_HASH, fingerprint="0" * 16, statuses=["active"])
    bad: list[dict[str, Any]] = [
        {"content_hash": _HASH, "statuses": []},
        {"content_hash": _HASH, "statuses": ["active", "active"]},
        {"content_hash": _HASH, "statuses": ["nope"]},
        {"content_hash": _HASH, "statuses": ["active"], "layer": "working"},
        {"content_hash": _HASH, "statuses": ["active"], "kind": "note"},
        {"content_hash": "XYZ", "statuses": ["active"]},
        {"fingerprint": "short", "statuses": ["active"]},
        {"task_hash": ("task_bad", _HASH), "statuses": ["active"]},
    ]
    for kwargs in bad:
        with pytest.raises(ToolInputError, match="find_memory_item: invalid filter"):
            memory.find_memory_item(**kwargs)


def test_ut07_07_update_changes_named_columns() -> None:
    """UT07-07 update sets the given columns, increments use_count, UNCHANGED keeps expires_at."""
    row = _put(expires_at=_at(60))
    mid = row["memory_id"]
    changed = memory.update_memory_item(
        mid,
        status="expired",
        data={"a": 1},
        provenance={"author_type": "human"},
        confidence=0.9,
        last_used_at=_at(2),
        use_count_increment=2,
    )
    assert changed == 1
    got = memory.get_memory_items([mid])[0]
    assert got == {
        **row,
        "status": "expired",
        "data": {"a": 1},
        "provenance": {"author_type": "human"},
        "confidence": 0.9,
        "last_used_at": _at(2),
        "use_count": 2,
    }
    assert memory.update_memory_item(mid, expires_at=None) == 1
    assert memory.get_memory_items([mid])[0]["expires_at"] is None
    assert memory.update_memory_item(mid, expires_at=UNCHANGED, use_count_increment=1) == 1
    assert memory.get_memory_items([mid])[0]["use_count"] == 3
    assert memory.update_memory_item(_mid(), status="active") == 0


def test_ut07_07_update_rejects_bad_values() -> None:
    """UT07-07 nothing to change is a ToolInputError; invalid values are SchemaViolations."""
    mid = _put()["memory_id"]
    with pytest.raises(ToolInputError, match="update_memory_item: nothing to change"):
        memory.update_memory_item(mid)
    cases: list[tuple[str, dict[str, Any]]] = [
        ("status", {"status": "gone"}),
        ("content", {"content": "x" * 8001}),
        ("confidence", {"confidence": -0.1}),
        ("data", {"data": [1]}),
        ("use_count", {"use_count_increment": 1001}),
    ]
    for column, kwargs in cases:
        with pytest.raises(SchemaViolation, match=rf"update_memory_item: invalid {column}$"):
            memory.update_memory_item(mid, **kwargs)
    with pytest.raises(SchemaViolation, match=r"update_memory_item: invalid memory_id$"):
        memory.update_memory_item("mem_bad", status="active")


def test_ut07_07_update_with_conn_propagates() -> None:
    """UT07-07 with conn the update runs in the caller's transaction."""
    mid = _put()["memory_id"]
    n = core.run_write(
        lambda c: memory.update_memory_item(mid, status="rejected", conn=c), op="test_write"
    )
    assert n == 1
    assert memory.get_memory_items([mid])[0]["status"] == "rejected"


def test_ut07_07_touch_counts_only_allowed_statuses() -> None:
    """UT07-07 touch sets last_used_at and adds one use per listed id in the allowed statuses."""
    a = _put()
    b = _put(status="pending_approval")
    c = _put(status="rejected")
    ids = [a["memory_id"], b["memory_id"], c["memory_id"], a["memory_id"], _mid()]
    assert memory.touch_memory_items(ids, now=_at(9)) == 2
    got = {r["memory_id"]: r for r in memory.get_memory_items(ids)}
    assert (got[a["memory_id"]]["use_count"], got[a["memory_id"]]["last_used_at"]) == (1, _at(9))
    assert got[b["memory_id"]]["use_count"] == 1
    assert (got[c["memory_id"]]["use_count"], got[c["memory_id"]]["last_used_at"]) == (0, None)
    assert memory.touch_memory_items([c["memory_id"]], now=_at(9), statuses=["rejected"]) == 1


def test_ut07_07_touch_rejects_bad_input() -> None:
    """UT07-07 touch needs 1..200 valid ids and valid statuses."""
    with pytest.raises(ToolInputError, match="invalid id: touch_memory_items"):
        memory.touch_memory_items([], now=_at(0))
    with pytest.raises(ToolInputError, match="too many ids: touch_memory_items"):
        memory.touch_memory_items([_mid() for _ in range(201)], now=_at(0))
    with pytest.raises(ToolInputError, match="touch_memory_items: invalid filter"):
        memory.touch_memory_items([_mid()], now=_at(0), statuses=["gone"])


# --- UT07-08 FTS and entity candidates ------------------------------------------------------


def test_ut07_08_fts_candidates_ordered_and_filtered() -> None:
    """UT07-08 FTS candidates are ordered by bm25 ascending and filtered by layer and status."""
    best = _put(content="revenue revenue revenue churn")
    mid = _put(content="revenue and many other unrelated words in this text")
    _put(content="revenue", status="rejected")
    _put(content="revenue", layer="episodic", kind="run_summary")
    got = memory.fts_candidates('"revenue"', layers=["semantic"], statuses=["active"], limit=10)
    assert [m for m, _ in got] == [best["memory_id"], mid["memory_id"]]
    assert got[0][1] <= got[1][1]
    assert (
        len(memory.fts_candidates('"revenue"', layers=["semantic"], statuses=["active"], limit=1))
        == 1
    )


def test_ut07_08_fts_syntax_error_returns_empty() -> None:
    """UT07-08 an FTS5 syntax error returns [] instead of raising or widening the query."""
    _put(content="revenue")
    for bad in ("AND OR", "revenue)", '"a" OR'):
        assert memory.fts_candidates(bad, layers=["semantic"], statuses=["active"], limit=5) == []


def test_ut07_08_fts_other_errors_and_bad_filters() -> None:
    """UT07-08 a non-syntax FTS error is a SchemaViolation; bad filters are ToolInputErrors."""
    for other in ("nosuchcol:x", '"unterminated'):  # not "fts5: syntax error" (U07-25 step 3)
        with pytest.raises(SchemaViolation, match="fts_candidates: OperationalError"):
            memory.fts_candidates(other, layers=["semantic"], statuses=["active"], limit=5)
    bad: list[dict[str, Any]] = [
        {"layers": [], "statuses": ["active"], "limit": 5},
        {"layers": ["x"], "statuses": ["active"], "limit": 5},
        {"layers": ["semantic"], "statuses": ["active", "rejected", "expired"], "limit": 5},
        {"layers": ["semantic"], "statuses": ["active"], "limit": 0},
        {"layers": ["semantic"], "statuses": ["active"], "limit": 201},
        {"layers": ["semantic"], "statuses": ["active"], "limit": True},
    ]
    for kwargs in bad:
        with pytest.raises(ToolInputError, match="fts_candidates: invalid filter"):
            memory.fts_candidates('"x"', **kwargs)


def test_ut07_08_entity_candidates_newest_first() -> None:
    """UT07-08 entity candidates: distinct ids, newest created_at first, filtered, limited."""
    ent = {"entities": [{"type": "org", "id": "org_1"}, {"type": "org", "id": "org_2"}]}
    old = _put(created_at=_at(1), data=ent)
    new = _put(created_at=_at(3), data={"entities": [{"id": "org_2"}, "not-an-object"]})
    _put(created_at=_at(4), data=ent, status="rejected")
    _put(created_at=_at(5), data={"entities": [{"id": "org_9"}]})
    _put(created_at=_at(6), data={})
    kw: dict[str, Any] = {"layers": ["semantic"], "statuses": ["active"]}
    got = memory.entity_candidates(["org_1", "org_2"], limit=10, **kw)
    assert got == [new["memory_id"], old["memory_id"]]
    assert memory.entity_candidates(["org_1", "org_2"], limit=1, **kw) == [new["memory_id"]]
    assert memory.entity_candidates(["org_x"], limit=10, **kw) == []


def test_ut07_08_entity_candidates_bad_filters() -> None:
    """UT07-08 entity ids outside 1..50 or over 200 chars, bad sets or limits raise."""
    kw: dict[str, Any] = {"layers": ["semantic"], "statuses": ["active"], "limit": 5}
    for ids in ([], ["x"] * 51, ["x" * 201], [""]):
        with pytest.raises(ToolInputError, match="entity_candidates: invalid filter"):
            memory.entity_candidates(ids, **kw)
    with pytest.raises(ToolInputError, match="entity_candidates: invalid filter"):
        memory.entity_candidates(["x"], layers=["semantic"], statuses=["x"], limit=5)


# --- UT07-09 task scratchpad ---------------------------------------------------------------


def _task(checkpoint: str | None) -> str:
    run_id, task_id = "run_" + new_ulid(), "task_" + new_ulid()
    _write(
        "INSERT INTO run (run_id, kind, depth, profile, config_hash, started_at)"
        " VALUES (?, 'funding_review', 'fast', 'p', 'h', ?)",
        (run_id, _at(0)),
    )
    _write(
        "INSERT INTO task (task_id, run_id, role, spec, checkpoint, created_at, updated_at)"
        " VALUES (?, ?, 'analyst', '{}', ?, ?, ?)",
        (task_id, run_id, checkpoint, _at(0), _at(0)),
    )
    return task_id


def test_ut07_09_scratchpad_from_envelope() -> None:
    """UT07-09 the scratchpad key of the envelope comes back as canonical JSON text."""
    envelope = (
        '{"schema_version": 1, "loop": {"turn": 3}, "state": {"a": 1},'
        ' "scratchpad": {"notes": ["x"], "b": 2}}'
    )
    task_id = _task(envelope)
    assert memory.get_task_scratchpad(task_id) == '{"b":2,"notes":["x"]}'


def test_ut07_09_scratchpad_absent_is_none() -> None:
    """UT07-09 a checkpoint without the key, a null key or a NULL checkpoint returns None."""
    assert memory.get_task_scratchpad(_task('{"schema_version": 1, "loop": {}}')) is None
    assert memory.get_task_scratchpad(_task('{"scratchpad": null}')) is None
    task_id = _task(None)
    assert memory.get_task_scratchpad(task_id) is None
    assert (
        core.run_write(lambda c: memory.get_task_scratchpad(task_id, conn=c), op="test_write")
        is None
    )


def test_ut07_09_unknown_or_bad_task() -> None:
    """UT07-09 an unknown task is a SchemaViolation; a bad id a ToolInputError."""
    unknown = "task_" + new_ulid()
    with pytest.raises(SchemaViolation, match=f"task {unknown} missing"):
        memory.get_task_scratchpad(unknown)
    with pytest.raises(ToolInputError, match="invalid id: get_task_scratchpad"):
        memory.get_task_scratchpad("task_bad")
    task_id = _task("[1, 2]")
    with pytest.raises(SchemaViolation, match=f"task.checkpoint invalid JSON for {task_id}"):
        memory.get_task_scratchpad(task_id)


# --- supporting coverage (U07-27, U07-28, U07-30, U07-35; C3, C5) ---------------------------


def test_ut07_07_count_proposals_scopes() -> None:
    """UT07-07 count_proposals counts agent/human proposals per scope and filters."""
    run_id = "run_" + new_ulid()
    prov = {"via": "tool", "run_id": run_id, "session_id": "s1", "author_ref": _AUTHOR}
    _put(provenance=prov, created_at=_at(1))
    _put(provenance={**prov, "via": "chat"}, kind="insight", created_at=_at(5))
    _put(provenance={**prov, "via": "system"})
    assert memory.count_proposals(run_id=run_id) == 2
    assert memory.count_proposals(session_id="s1", kind="insight") == 1
    assert memory.count_proposals(author_ref=_AUTHOR, since=_at(2)) == 1
    with pytest.raises(ToolInputError, match="count_proposals needs a scope"):
        memory.count_proposals(kind="insight")
    for kwargs in ({"session_id": "s" * 65}, {"author_ref": "XY"}, {"run_id": "r", "kind": "n"}):
        with pytest.raises(ToolInputError, match="count_proposals: invalid filter"):
            memory.count_proposals(**kwargs)


def test_ut07_07_evidence_and_finding_lookups() -> None:
    """UT07-07 existing_query_ids, evidence_rows and finding_facts return only existing ids."""
    q1, q2 = "q_" + "a" * 16, "q_" + "b" * 16
    _write(
        "INSERT INTO evidence (query_id, build_id, sql, result_hash, params, row_count,"
        " duration_ms, executed_at) VALUES (?, 'b1', 'SELECT 1', 'h', '{\"p\": 1}', 1, 1, ?)",
        (q1, _at(0)),
    )
    f1, f2 = "fnd_" + new_ulid(), "fnd_" + new_ulid()
    _write(
        "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, query_ids,"
        " confidence, status, verification, created_at)"
        " VALUES (?, 'run_x', 'task_x', 'analyst', 'c', ?, 0.7, 'verified', '{\"ok\": true}', ?)",
        (f1, f'["{q1}"]', _at(0)),
    )
    _write(
        "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, created_at)"
        " VALUES (?, 'run_x', 'task_y', 'analyst', 'c', ?)",
        (f2, _at(0)),
    )
    assert memory.existing_query_ids([q1, q2, q1]) == {q1}
    assert memory.existing_query_ids([]) == set()
    assert memory.evidence_rows([q1, q2]) == {
        q1: {"sql": "SELECT 1", "params": {"p": 1}, "build_id": "b1"}
    }
    facts = memory.finding_facts([f1, f2, "fnd_" + new_ulid()])
    assert facts[f1] == {
        "status": "verified",
        "confidence": 0.7,
        "run_id": "run_x",
        "task_id": "task_x",
        "query_ids": [q1],
        "verification": {"ok": True},
    }
    assert (facts[f2]["confidence"], facts[f2]["verification"]) == (0.0, None)
    assert memory.finding_facts([]) == {}
    assert memory.evidence_rows([]) == {}
    for fn, bad in ((memory.existing_query_ids, "q_1"), (memory.finding_facts, "fnd_x")):
        with pytest.raises(ToolInputError, match="invalid id: "):
            fn([bad])


def test_ut07_07_maintenance_selectors() -> None:
    """UT07-07 maintenance_rows selectors, ordering by memory_id and paging."""
    exp = _put(expires_at=_at(1), status="candidate")
    _put(expires_at=_at(100))
    _put(expires_at=_at(1), status="rejected")
    emb = _put(data={"embedding_pending": 1})
    _put(data={"embedding_pending": 1}, status="rejected")
    rule = _put(kind="business_rule", created_at=_at(0))
    _put(kind="business_rule", created_at=_at(0), data={"last_review_requested_at": _at(9)})
    tpl = _put(layer="procedural", kind="sql_template", status="candidate")
    kw: dict[str, Any] = {"now": _at(10), "limit": 100}
    assert [r["memory_id"] for r in memory.maintenance_rows(selector="expirable", **kw)] == [
        exp["memory_id"]
    ]
    pending = memory.maintenance_rows(selector="embedding_pending", **kw)
    assert [r["memory_id"] for r in pending] == [emb["memory_id"]]  # type: ignore[index]
    assert memory.pending_embedding_count() == 1
    due = memory.maintenance_rows(selector="business_rule_review_due", review_cutoff=_at(5), **kw)
    assert [r["memory_id"] for r in due] == [rule["memory_id"]]  # type: ignore[index]
    tpls = memory.maintenance_rows(selector="templates", **kw)
    assert [r["memory_id"] for r in tpls] == [tpl["memory_id"]]  # type: ignore[index]
    everything = memory.maintenance_rows(selector="all_ids_status", now=_at(10), limit=3)
    assert len(everything) == 3
    ids = [m for m, _ in everything]  # type: ignore[misc]
    assert ids == sorted(ids)
    rest = memory.maintenance_rows(selector="all_ids_status", now=_at(10), limit=100, after=ids[-1])
    assert len(rest) == 5
    with pytest.raises(ToolInputError, match="maintenance_rows: review_cutoff required"):
        memory.maintenance_rows(selector="business_rule_review_due", **kw)
    with pytest.raises(ToolInputError, match="maintenance_rows: invalid limit"):
        memory.maintenance_rows(selector="templates", now=_at(0), limit=10_001)
    with pytest.raises(ToolInputError, match="maintenance_rows: invalid selector"):
        memory.maintenance_rows(selector="bogus", now=_at(0), limit=1)  # type: ignore[arg-type]


def test_ut07_07_fts_check_and_rebuild() -> None:
    """UT07-07 a consistent index is left alone; a desynced one is rebuilt (False, then True)."""
    row = _put(content="alpha")
    assert memory.fts_check_and_rebuild() is False
    _write("INSERT INTO memory_fts (rowid, content, kind) VALUES (999, 'ghost', 'glossary')")
    assert memory.fts_check_and_rebuild() is True
    assert memory.fts_check_and_rebuild() is False
    assert _fts("alpha") == [row["memory_id"]]
    _write("INSERT INTO memory_fts (rowid, content, kind) VALUES (998, 'ghost', 'glossary')")
    assert core.run_write(lambda c: memory.fts_check_and_rebuild(conn=c), op="test_write")
    assert core.run_write(lambda c: memory.fts_check_and_rebuild(conn=c), op="test_write") is False


def test_ut07_07_fts_check_other_failures_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-07 a SchemaViolation not caused by an FTS integrity failure is not a rebuild."""

    def fail(*_: object, **__: object) -> None:
        msg = "ops write failed"
        raise SchemaViolation(msg)

    monkeypatch.setattr(core, "run_write", fail)
    with pytest.raises(SchemaViolation, match="ops write failed"):
        memory.fts_check_and_rebuild()


def test_ut07_07_session_memory_ids() -> None:
    """UT07-07 session items with status pending_approval or active, oldest first."""
    later = _put(provenance={"session_id": "s1"}, created_at=_at(2))
    first = _put(provenance={"session_id": "s1"}, created_at=_at(1), status="pending_approval")
    _put(provenance={"session_id": "s1"}, status="rejected")
    _put(provenance={"session_id": "s2"})
    assert memory.session_memory_ids("s1") == [first["memory_id"], later["memory_id"]]
    for bad in ("", "s" * 65):
        with pytest.raises(ToolInputError, match="invalid id: session_memory_ids"):
            memory.session_memory_ids(bad)


def test_ut07_07_corrupt_json_and_read_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-07 stored non-object JSON is a C3 SchemaViolation; busy reads are StoreBusy (C5)."""
    row = _put()
    mid = row["memory_id"]
    _write("UPDATE memory_item SET data = '[1]' WHERE memory_id = ?", (mid,))
    with pytest.raises(SchemaViolation, match=f"memory_item.data invalid JSON for {mid}"):
        memory.get_memory_items([mid])

    with pytest.raises(SchemaViolation, match=r"task.checkpoint invalid JSON for task_x"):
        _memory_rows.load_typed("{", dict, "task.checkpoint", "task_x")

    class _Busy:
        def execute(self, *_: object) -> None:
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(core, "connection", _Busy)
    with pytest.raises(StoreBusy) as info:
        memory.session_memory_ids("s1")
    assert info.value.context["op"] == "session_memory_ids"
    with pytest.raises(StoreBusy):
        memory.fts_candidates('"x"', layers=["semantic"], statuses=["active"], limit=5)


def test_ut07_07_package_reexports_the_07_block() -> None:
    """UT07-07 herness.store.ops re-exports the memory functions as the same objects."""
    for name in ("insert_memory_item", "find_memory_item", "maintenance_rows", "MemoryItemRow"):
        assert getattr(ops, name) is getattr(memory, name)
