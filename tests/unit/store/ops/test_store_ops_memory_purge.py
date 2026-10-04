"""Unit tests for herness.store.ops.memory.purge_rows (impl 07 U07-101, UT07-89; T07-26).

Every test runs on a fresh ops store migrated through 070. Items cite a record in their
content, `data` or `provenance`, or name a person as author or only inside
`data.provenance_history`; the selection is checked as a dry run, then for real.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from herness.core import time as clock
from herness.core.errors import FatalError, ToolInputError
from herness.core.ids import new_ulid
from herness.store import ops
from herness.store.ops import _memory_rows, core, memory
from herness.store.ops.memory import MemoryItemRow, PurgeRows

pytestmark = pytest.mark.unit

_T0 = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=datetime.UTC)
_RECORD = "src:incident:INC001"
_LONGER = "src:incident:INC0012"  # shares the whole of _RECORD as a prefix
_OTHER_RECORD = "src:incident:INC0043"
_AUTHOR = "ab" * 16
_OTHER = "cd" * 16


@pytest.fixture(autouse=True)
def migrated(ops_store: Path) -> Path:
    ops.migrate()
    return ops_store


def _put(content: str = "churn means cancelled subscriptions", **over: Any) -> str:
    row: dict[str, Any] = {
        "memory_id": "mem_" + new_ulid(), "layer": "semantic", "kind": "glossary",
        "content": content, "data": {"entities": []},
        "provenance": {"author_type": "human", "author_ref": _OTHER, "via": "cli"},
        "confidence": 0.5, "status": "active", "created_at": clock.format_utc(_T0),
        "expires_at": None, "last_used_at": None, "use_count": 0,
    }  # fmt: skip
    row.update(over)
    memory.insert_memory_item(row)  # type: ignore[arg-type]
    return str(row["memory_id"])


def _review() -> str:
    return ops.create_review_item("memory_write", {"content": "x"}, now=_T0)


def _ids() -> list[str]:
    return [str(r[0]) for r in core.read_all("SELECT memory_id FROM memory_item")]


def _fts(term: str) -> list[str]:
    rows = core.read_all(
        "SELECT m.memory_id FROM memory_fts JOIN memory_item m ON m.rowid = memory_fts.rowid"
        " WHERE memory_fts MATCH ?",
        (term,),
    )
    return [str(r[0]) for r in rows]


def _fts_rows() -> int:
    return int(core.read_all("SELECT COUNT(*) FROM memory_fts")[0][0])


def _row(memory_id: str) -> MemoryItemRow:
    return memory.get_memory_items([memory_id])[0]


def _cites(record_id: str, **data: Any) -> dict[str, Any]:
    """`data` citing `record_id` as an entity (the structured citation)."""
    return {"entities": [{"type": "record", "id": record_id}], **data}


def _number(record_id: str) -> dict[str, Any]:
    """`data` citing `record_id` through a cited number's row key."""
    ref = {"id": "n1", "value": 3, "unit": "count", "query_id": "q_" + "a" * 16,
           "column": "n", "row_key": {"record_id": record_id}}  # fmt: skip
    return {"entities": [], "numbers": [ref]}


def test_ut07_89_record_dry_run_then_real_delete(tmp_path: Path) -> None:
    """UT07-89 items citing the record (entity id, number row key) are selected identically
    by the dry run and the real call; their rows and FTS rows go, linked review ids are
    returned; the next purge finds nothing."""
    review, derived = _review(), _review()
    by_entity = _put("zebrafish rule", data=_cites(_RECORD, review_item_id=review,
                                                   derived_review_item_id=derived))  # fmt: skip
    by_number = _put("zebrafish count", data=_number(_RECORD))
    other = _put("unrelated zebrafish", data=_cites(_OTHER_RECORD))
    before = _fts_rows()

    dry = memory.purge_rows(record_id=_RECORD, dry_run=True)
    assert sorted(_ids()) == sorted([by_entity, by_number, other])
    real = memory.purge_rows(record_id=_RECORD)

    expected = PurgeRows(sorted([by_entity, by_number]), [], sorted([review, derived]))
    assert dry == real == expected
    assert _ids() == [other]
    assert _fts("zebrafish") == [other]
    assert _fts_rows() == before - 2
    assert memory.purge_rows(record_id=_RECORD) == PurgeRows([], [], [])  # idempotent


def test_ut07_89_prefix_sharing_and_mentions_are_not_citations() -> None:
    """UT07-89 (T07-26 controller ruling) only a structured citation equal to the record id
    selects an item: a prefix-sharing record, a free-text mention, an id in another JSON
    field or in the provenance leave the item, its FTS rows and its review link untouched."""
    cited = _put("kestrel cited", data=_cites(_RECORD))
    longer = _put("kestrel longer", data=_cites(_LONGER, review_item_id=_review()))
    longer_num = _put("kestrel longer number", data=_number(_LONGER))
    mention = _put(f"kestrel about {_RECORD} in passing", data=_cites(_OTHER_RECORD))
    elsewhere = _put("kestrel note", data={"entities": [], "source": _RECORD,
                                            "rows": [_RECORD]})  # fmt: skip
    in_prov = _put("kestrel prov", provenance={"author_type": "pipeline", "source": _RECORD})
    kept = sorted([longer, longer_num, mention, elsewhere, in_prov])

    assert memory.purge_rows(record_id=_RECORD) == PurgeRows([cited], [], [])
    assert sorted(_ids()) == kept
    assert sorted(_fts("kestrel")) == kept
    assert memory.purge_rows(record_id=_LONGER).deleted_ids == sorted([longer, longer_num])


def test_ut07_89_author_deletes_and_scrubs_history() -> None:
    """UT07-89 items authored by the person go; an item naming the person only in
    `provenance_history` stays with those entries removed (scrubbed_ids)."""
    authored = _put("authored", provenance={"author_type": "human", "author_ref": _AUTHOR})
    history = [{"author_ref": _AUTHOR, "via": "chat"}, {"author_ref": _OTHER, "via": "cli"},
               {"author_ref": _AUTHOR, "via": "cli"}]  # fmt: skip
    merged = _put("merged", data={"entities": [], "provenance_history": history, "k": 1})
    untouched = _put("untouched", data={"provenance_history": [{"author_ref": _OTHER}]})
    approved = _put("approved", data={"approved_by": _AUTHOR})  # named outside the history
    on_behalf = [{"author_ref": _OTHER, "on_behalf_of": _AUTHOR}]  # not the entry's author
    behalf = _put("behalf", data={"provenance_history": on_behalf})

    dry = memory.purge_rows(author_ref=_AUTHOR, dry_run=True)
    assert _row(merged)["data"]["provenance_history"] == history  # dry run changes nothing
    real = memory.purge_rows(author_ref=_AUTHOR)

    assert dry == real == PurgeRows([authored], [merged], [])
    assert sorted(_ids()) == sorted([merged, untouched, approved, behalf])
    assert _row(behalf)["data"]["provenance_history"] == on_behalf
    assert _row(approved)["data"] == {"approved_by": _AUTHOR}  # neither deleted nor changed
    data = _row(merged)["data"]
    assert data == {"entities": [], "k": 1,
                    "provenance_history": [{"author_ref": _OTHER, "via": "cli"}]}  # fmt: skip
    stored = core.read_all(
        "SELECT data FROM memory_item WHERE memory_id NOT IN (?, ?)", (approved, behalf)
    )
    assert not any(_AUTHOR in str(r[0]) for r in stored)
    assert memory.purge_rows(author_ref=_AUTHOR) == PurgeRows([], [], [])


def test_ut07_89_unknown_record_changes_nothing() -> None:
    """UT07-89 a record no item cites selects nothing and deletes nothing."""
    kept = _put("cites another", data=_cites(_OTHER_RECORD))
    assert memory.purge_rows(record_id="jira:issue:NOPE-1") == PurgeRows([], [], [])
    assert _ids() == [kept]


def test_ut07_89_deletes_in_chunks_of_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-89 more than 500 citing items are deleted in statements of at most 500 ids."""
    ids = [_put("bulk", data=_cites(_RECORD)) for _ in range(503)]
    seen: set[str] = set()  # trigger steps re-report their statement: keep distinct ones
    real_write = _memory_rows.write

    def spy(fn: Any, conn: Any, op: str) -> Any:
        def traced(c: sqlite3.Connection) -> Any:
            c.set_trace_callback(lambda sql: seen.add(sql) if sql.startswith(
                "DELETE FROM memory_item WHERE memory_id IN") else None)  # fmt: skip
            try:
                return fn(c)
            finally:
                c.set_trace_callback(None)

        return real_write(traced, conn, op)

    monkeypatch.setattr(memory, "write", spy)
    assert memory.purge_rows(record_id=_RECORD).deleted_ids == sorted(ids)
    assert _ids() == []
    assert sorted(sql.count("'mem_") for sql in seen) == [3, 500]  # the chunked DELETEs


def test_ut07_89_joins_the_callers_transaction() -> None:
    """UT07-89 with `conn` the delete is part of the caller's transaction (C1): a rollback
    keeps every row."""
    kept = _put("cites", data=_cites(_RECORD))

    def tx(conn: sqlite3.Connection) -> None:
        assert memory.purge_rows(record_id=_RECORD, conn=conn).deleted_ids == [kept]
        msg = "roll back"
        raise RuntimeError(msg)

    with pytest.raises(FatalError, match="RuntimeError"):  # run_write rolls back, then maps
        core.run_write(tx, op="test_purge")
    assert _ids() == [kept]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"record_id": _RECORD, "author_ref": _AUTHOR},
        {"record_id": "not-a-record"},
        {"record_id": "a:b:" + "x" * 297},
        {"record_id": "a:b:x\n"},
        {"author_ref": "AB" * 16},
        {"author_ref": _AUTHOR[:-1]},
        {"record_id": 7},
    ],
)
def test_ut07_89_needs_exactly_one_valid_selector(kwargs: dict[str, Any]) -> None:
    """UT07-89 neither, both or a selector off its pattern is ToolInputError (nothing echoed)."""
    _put("cites", data=_cites(_RECORD))
    with pytest.raises(ToolInputError, match=r"^purge_rows needs exactly one selector$"):
        memory.purge_rows(**kwargs)
    assert len(_ids()) == 1


def test_ut07_89_exported_from_the_ops_package() -> None:
    """UT07-89 `purge_rows` and `PurgeRows` are re-exported in the 07 memory block."""
    assert ops.purge_rows is memory.purge_rows
    assert ops.PurgeRows is memory.PurgeRows
