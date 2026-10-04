"""Integration test of the erasure flow F07-14 (impl 07 IT07-11; U07-100, U07-57, T07-26).

A spec 10 deletion request is opened for a record that two memory items cite (one of them
pending, with its `memory_write` review item); the deletion step calls
`MemoryStore.purge(record_id)` on the real facade (migrated `ops_store`, LanceDB in tmp),
records its count on the request (step 3b) and is then rerun, as a retried job step would be.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._write_env import (
    KIND_DATA,
    PATTERNS,
    make_writer,
    memory_rows,
    proposal,
    provenance,
)

from herness.harness.memory import MemoryStore
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder
from herness.store import ops
from herness.store.ops import core

pytestmark = pytest.mark.integration

RECORD = "src:incident:INC0042"
NOW = datetime(2026, 10, 1, 9, tzinfo=UTC)
REQUESTER = "e" * 32


def _fts(term: str) -> list[str]:
    rows = core.read_all(
        "SELECT m.memory_id FROM memory_fts JOIN memory_item m ON m.rowid = memory_fts.rowid"
        " WHERE memory_fts MATCH ?",
        (term,),
    )
    return [str(r[0]) for r in rows]


def test_it07_11_deletion_step_purges_memory(ops_store: OpsStoreHandle, tmp_path: Path) -> None:
    """IT07-11 the deletion step's `purge(record_id)` removes both items, their vectors and
    FTS rows, blanks and rejects the pending review item; the rerun returns 0 (R-54)."""
    env = make_writer(tmp_path)
    store = MemoryStore(
        MemoryConfig(injection_patterns=PATTERNS), conn_factory=core.connection,
        vectors=env.vectors, embedder=Embedder(env.embed, model_name="bge-m3"),
        redactor=env.redactor, llms=None, allowed_numeral_patterns=(r"(INC|CHG|PRB)\d+",),
        data_root=ops_store.data_root,
    )  # fmt: skip
    cites: dict[str, JsonValue] = {
        **KIND_DATA["glossary"],
        "entities": [{"type": "record", "id": RECORD}],
    }
    active = store.propose(proposal("Churn spiked after the kestrel outage.", data=cites))
    pending = store.propose(
        proposal("Backlog means open kestrel tickets.", provenance("agent"), data=cites)
    ).memory_id
    other = store.propose(proposal("MTTR means the mean time to restore a service.")).memory_id
    review_id = next(r for r in memory_rows() if r["memory_id"] == pending)["data"][
        "review_item_id"
    ]
    assert ops.get_review_item(review_id).status == "pending"
    assert active.status == "active"
    request = ops.create_deletion_request(
        record_id=RECORD, requested_by=REQUESTER, reason_ref="gdpr-17", now=NOW
    )
    ops.set_deletion_status(request.request_id, "running")

    removed = store.purge(RECORD, now=NOW)  # the impl 10 memory step (R-54)
    ops.record_deletion_step(
        request.request_id, "3b", status="done", at=NOW, counts={"memory_items": removed}
    )

    assert removed == 2
    assert [r["memory_id"] for r in memory_rows()] == [other]
    assert _fts("kestrel") == []
    assert env.vectors.vectors([active.memory_id, pending]) == {}
    assert [m.memory_id for m in env.vectors.list_ids("", 1000)] == [other]
    review = ops.get_review_item(review_id)
    assert (review.status, review.decided_by, review.note) == ("rejected", "system", "purged")
    assert review.payload["content"] == ""
    assert (review.payload["entities"], review.payload["provenance"]) == ([], {})
    payload = core.read_all("SELECT payload FROM review_item WHERE item_id = ?", (review_id,))
    assert RECORD not in str(payload[0][0])
    assert "kestrel" not in str(payload[0][0])
    steps = ops.get_deletion_request(request.request_id).steps
    assert steps["3b"]["counts"] == {"memory_items": 2}

    assert store.purge(RECORD, now=NOW) == 0  # the retried step finds nothing
    assert [r["memory_id"] for r in memory_rows()] == [other]
