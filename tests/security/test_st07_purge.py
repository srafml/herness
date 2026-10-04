"""ST07-21: erasure leaves no text in FTS, SQLite or vectors (TH07-21, R-54; T07-26).

The `MemoryStore` facade writes real items to the migrated `ops_store` and LanceDB in tmp:
two items cite a deleted record (one pending with its `memory_write` review item, one
active), two do not. After `MemoryStore.purge(record_id)` an FTS search, a scan of every
SQLite row (text and hex-encoded blobs, so FTS5 shadow tables are covered) and a vector
search for the record's text find nothing, while the unrelated items are untouched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import JsonValue
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._write_env import (
    KIND_DATA,
    PATTERNS,
    Env,
    make_writer,
    memory_rows,
    proposal,
    provenance,
)

from herness.core.types import Layer, Status
from herness.harness.memory import MemoryStore
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder
from herness.store.ops import core, shared
from herness.store.ops import memory as ops

pytestmark = pytest.mark.integration

RECORD = "src:incident:INC001"
LONGER = "src:incident:INC0012"  # shares the whole of RECORD as a prefix
OTHER = "src:incident:INC0077"
PLANTED = "zebrafish"  # the record's text: a word no other planted item uses
LAYERS: list[Layer] = ["episodic", "semantic", "procedural"]
STATUSES: list[Status] = ["candidate", "pending_approval", "active", "expired", "rejected"]


def _store(env: Env, data_root: Path) -> MemoryStore:
    return MemoryStore(
        MemoryConfig(injection_patterns=PATTERNS), conn_factory=core.connection,
        vectors=env.vectors, embedder=Embedder(env.embed, model_name="bge-m3"),
        redactor=env.redactor, llms=None, allowed_numeral_patterns=(r"(INC|CHG|PRB)\d+",),
        data_root=data_root,
    )  # fmt: skip


def _fts(term: str) -> list[str]:
    rows = core.read_all(
        "SELECT m.memory_id FROM memory_fts JOIN memory_item m ON m.rowid = memory_fts.rowid"
        " WHERE memory_fts MATCH ?",
        (term,),
    )
    return [str(r[0]) for r in rows]


def _dump(path: Path) -> str:
    """Every row of every table (FTS5 shadow tables included) as SQL text, blobs as hex."""
    conn = sqlite3.connect(path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def _found(text: str, needle: str) -> bool:
    raw = needle.encode("utf-8").hex()
    return needle in text or raw in text.lower()


def _vector_hits(env: Env, store: MemoryStore, texts: list[str]) -> set[str]:
    hits: set[str] = set()
    for text in texts:
        vector = Embedder(env.embed, model_name="bge-m3").embed_item("glossary", text)
        hits |= {i for i, _ in env.vectors.search(vector, LAYERS, STATUSES, 200)}
        hits |= {h.item.memory_id for h in store.recall(text, k=10)}
    return hits


def _cites(record_id: str) -> dict[str, JsonValue]:
    """Glossary data citing `record_id` as an entity (the structured citation)."""
    return {**KIND_DATA["glossary"], "entities": [{"type": "record", "id": record_id}]}


def test_st07_21_purge_leaves_no_text_anywhere(ops_store: OpsStoreHandle, tmp_path: Path) -> None:
    """ST07-21 after `MemoryStore.purge(record_id)` FTS search, SQLite scan and vector
    search for the record's text find nothing; items citing a prefix-sharing record, items
    that only mention the id in their text and unrelated items are untouched (row, vector,
    FTS; T07-26 controller ruling)."""
    env = make_writer(tmp_path)
    store = _store(env, ops_store.data_root)
    cited_texts = [f"Churn spiked after the {PLANTED} outage.",
                   f"Backlog means open {PLANTED} tickets."]  # fmt: skip
    active = store.propose(proposal(cited_texts[0], data=_cites(RECORD))).memory_id
    agent = provenance("agent")
    pending = store.propose(proposal(cited_texts[1], agent, data=_cites(RECORD))).memory_id
    fix = {**KIND_DATA["business_rule"], "suggested_action": "weight_change",
           "entities": [{"type": "record", "id": RECORD}]}  # fmt: skip
    correction = store.propose(
        proposal(f"Weight the {PLANTED} outage lower.", kind="business_rule", data=fix)
    ).memory_id
    store.approve(correction, "b" * 32)  # creates the derived weight_change review item
    kept_texts = [("MTTR means the mean time to restore a heron service.", _cites(LONGER)),
                  (f"Heron churn is unrelated to {RECORD} here.", _cites(OTHER)),
                  ("Churn means customers who left the heron service.", None)]  # fmt: skip
    kept = sorted(store.propose(proposal(t, data=d)).memory_id for t, d in kept_texts)
    rows = {r["memory_id"]: r for r in memory_rows()}
    assert rows[pending]["status"] == "pending_approval"
    review_id = rows[pending]["data"]["review_item_id"]
    derived = rows[correction]["data"]["derived_review_item_id"]
    assert PLANTED in shared.get_review_item(derived).payload["statement"]
    assert sorted(_fts(PLANTED)) == sorted([active, pending, correction])  # searchable
    assert {active, pending} <= _vector_hits(env, store, cited_texts)
    kept_vectors = env.vectors.vectors(kept)
    assert sorted(kept_vectors) == kept

    assert store.purge(RECORD) == 3

    assert _fts(PLANTED) == []
    assert ops.fts_candidates(PLANTED, layers=LAYERS[:3], statuses=STATUSES[:2],
                              limit=200) == []  # fmt: skip
    assert not ops.fts_check_and_rebuild()  # index consistent: nothing left behind
    assert not _found(_dump(ops_store.db_path), PLANTED)
    assert ops.purge_rows(record_id=RECORD, dry_run=True).deleted_ids == []
    assert _vector_hits(env, store, cited_texts) & {active, pending} == set()
    assert env.vectors.vectors([active, pending, correction]) == {}
    payloads = core.read_all("SELECT item_id, payload FROM review_item")
    assert {str(r[0]) for r in payloads} >= {review_id, derived}
    for _, payload in payloads:  # no text, citation or quoted id left in any review payload
        assert PLANTED not in str(payload)
        assert f'"{RECORD}"' not in str(payload)
    assert f'"{RECORD}"' not in _dump(ops_store.db_path)
    review = shared.get_review_item(review_id)
    assert (review.status, review.note, review.payload["content"]) == ("rejected", "purged", "")
    after = {r["memory_id"]: r for r in memory_rows()}
    assert sorted(after) == kept
    assert [after[i] for i in kept] == [rows[i] for i in kept]
    assert sorted(_fts("heron")) == kept
    after_vectors = env.vectors.vectors(kept)
    assert all((after_vectors[i] == kept_vectors[i]).all() for i in kept)
    assert sorted(m.memory_id for m in env.vectors.list_ids("", 1000)) == kept
    assert store.purge(RECORD) == 0
