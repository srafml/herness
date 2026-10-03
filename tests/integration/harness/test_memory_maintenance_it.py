"""Integration test of the memory maintenance flow F07-13 (impl 07 IT07-10; U07-96, T07-23).

The `MemoryStore` facade writes real items to the migrated `ops_store` and LanceDB in tmp;
the test then desynchronises the two stores (missing vector, stale vector hash, orphan
vector, a vector that still says `active` for a rejected item) and runs the
`memory_maintenance` handler that `register_memory_components` registered: SQLite stays the
source of truth (TH07-13) and every vector row ends up matching its item.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._maintenance_env import FakeCtx, row_of
from tests.unit.harness.memory._write_env import (
    AUTHOR,
    PATTERNS,
    make_writer,
    proposal,
    provenance,
)

from herness.core.ids import new_ulid
from herness.core.jobs.handlers import resolve_handler
from herness.core.resilience import ProcessState
from herness.harness.memory import MemoryStore, register_memory_components
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder, VectorRow
from herness.harness.tools import ToolRegistry
from herness.store.ops import core

pytestmark = pytest.mark.integration


def test_it07_10_maintenance_repairs_desynced_stores(
    ops_store: OpsStoreHandle, tmp_path: Path, reset_process_state: ProcessState
) -> None:
    """IT07-10 desync LanceDB and SQLite, run the registered maintenance handler: the
    missing and stale vectors are re-embedded, the orphan is deleted, the rejected item's
    vector is re-statused, and recall never returns the rejected item."""
    del reset_process_state
    env = make_writer(tmp_path)
    store = MemoryStore(
        MemoryConfig(injection_patterns=PATTERNS), conn_factory=core.connection,
        vectors=env.vectors, embedder=Embedder(env.embed, model_name="bge-m3"),
        redactor=env.redactor, llms=None, allowed_numeral_patterns=(r"(INC|CHG|PRB)\d+",),
        data_root=ops_store.data_root,
    )  # fmt: skip
    register_memory_components(ToolRegistry(), store)
    missing = store.propose(proposal("Churn means customers who left the service.")).memory_id
    stale = store.propose(proposal("MTTR means the mean time to restore a service.")).memory_id
    agent = provenance("agent", run_id="run_" + new_ulid(), task_id="task_" + new_ulid())
    rejected = store.propose(proposal("Backlog means open tickets.", agent)).memory_id
    store.reject(rejected, AUTHOR, "not a definition we use")
    orphan = "mem_" + new_ulid()

    vec = np.zeros(1024, dtype=np.float32)
    vec[7] = 1.0
    env.vectors.delete([missing])  # 1. the vector write never happened
    env.vectors.upsert([VectorRow(stale, "semantic", "glossary", "active", "h_old", "bge-m3",
                                  vec)])  # 2. stale content hash  # fmt: skip
    env.vectors.upsert([VectorRow(orphan, "semantic", "glossary", "active", "h", "bge-m3",
                                  vec)])  # 3. vector without an item  # fmt: skip
    env.vectors.set_status([rejected], "active")  # 4. TH07-13: vector says active

    outcome = resolve_handler("memory_maintenance")(FakeCtx())  # type: ignore[arg-type]

    assert outcome.status == "done"
    counts = outcome.result
    assert counts["vectors"]["deleted"] == 1
    assert counts["vectors"]["restatused"] == 1
    assert counts["vectors"]["flagged"] == 2
    assert counts["backfill"] == {"embedded": 2, "stopped": False}
    assert counts["templates"] == {"skipped": "no_current_build"}
    metas = {m.memory_id: m for m in env.vectors.list_ids("", 1000)}
    assert sorted(metas) == sorted([missing, stale, rejected])
    for memory_id in (missing, stale, rejected):
        row = row_of(memory_id)
        assert metas[memory_id].status == row["status"]
        assert row["data"].get("embedding_pending") in (None, False)
        if row["status"] == "active":
            assert metas[memory_id].content_hash == row["data"]["content_hash"]
    assert row_of(rejected)["status"] == "rejected"
    hits = store.recall("backlog open tickets churn mttr", k=10)
    assert rejected not in {h.item.memory_id for h in hits}
    assert store.health().status == "ok"
