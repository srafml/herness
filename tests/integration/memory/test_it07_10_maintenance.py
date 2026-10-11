"""IT07-10 (F07-13): maintenance repairs a desynced LanceDB and SQLite after a real run
(T07-24).

A funding review runs through the real swarm driver (`drv`, scripted agents) with the real
spec 07 `MemoryStore`, so its record step writes the run's `run_summary` item and vector. A
human glossary item and an agent item that is then rejected are written through the facade.
The test desynchronises the stores (the run summary's vector lost, a stale content hash, an
orphan vector, a vector still `active` for the rejected item) and runs the
`memory_maintenance` job through the spec 08 queue and `run_inline` (the job kind
`register_memory_components` registered), unlike the T07-23 row in
`tests/integration/harness/test_memory_maintenance_it.py`, which calls the handler directly.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.integration.memory._it07_env import (
    memory_store,
    record_portfolio,
    run_review,
    run_summaries,
)
from tests.unit.harness._blackboard_env import test_redactor
from tests.unit.harness.memory._maintenance_env import row_of
from tests.unit.harness.memory._write_env import AUTHOR, proposal, provenance

from herness.core.ids import new_ulid
from herness.core.jobs import enqueue, run_inline
from herness.harness.memory import register_memory_components
from herness.harness.memory.store import VectorRow
from herness.harness.tools import ToolRegistry
from herness.store.ops import select_tasks

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
__all__ = ["drv", "test_redactor"]


def test_it07_10_desync_after_a_run_then_queued_maintenance_repairs(
    drv: Drv, tmp_path: Path
) -> None:
    """IT07-10 desync LanceDB and SQLite after a real run, run the queued
    `memory_maintenance` job: the lost and stale vectors are re-embedded, the orphan deleted,
    the rejected item's vector re-statused; every vector matches its item and recall never
    returns the rejected item."""
    record_portfolio()
    mem = memory_store(tmp_path, Path(drv.hcfg.paths.data))
    register_memory_components(ToolRegistry(), mem.store)
    res = run_review(drv, mem.store)
    assert res.status == "done"
    [summary] = run_summaries(res.run_id)
    lost = str(summary["memory_id"])
    stale = mem.store.propose(proposal("MTTR means the mean time to restore a service.")).memory_id
    task_id = select_tasks(res.run_id)[0].task_id
    agent = provenance("agent", run_id=res.run_id, task_id=task_id)
    rejected = mem.store.propose(proposal("Backlog means open tickets.", agent)).memory_id
    mem.store.reject(rejected, AUTHOR, "not a definition we use")
    orphan = "mem_" + new_ulid()
    vec = np.zeros(1024, dtype=np.float32)
    vec[7] = 1.0
    vectors = mem.env.vectors
    assert {m.memory_id for m in vectors.list_ids("", 100)} == {lost, stale, rejected}
    vectors.delete([lost])  # 1. the run summary's vector is lost
    vectors.upsert([VectorRow(stale, "semantic", "glossary", "active", "h_old", "bge-m3", vec)])
    vectors.upsert([VectorRow(orphan, "semantic", "glossary", "active", "h", "bge-m3", vec)])
    vectors.set_status([rejected], "active")  # 4. TH07-13: the vector says active

    job_id = enqueue("memory_maintenance", {}, "none")
    outcome = run_inline(job_id)

    assert outcome.status == "done"
    counts = outcome.result
    assert counts["vectors"]["deleted"] == 1
    assert counts["vectors"]["restatused"] == 1
    assert counts["vectors"]["flagged"] == 2
    assert counts["backfill"] == {"embedded": 2, "stopped": False}
    metas = {m.memory_id: m for m in vectors.list_ids("", 100)}
    assert sorted(metas) == sorted([lost, stale, rejected])
    for memory_id in (lost, stale, rejected):
        row = row_of(memory_id)
        assert metas[memory_id].status == row["status"]
        assert row["data"].get("embedding_pending") in (None, False)
        if row["status"] == "active":
            assert metas[memory_id].content_hash == row["data"]["content_hash"]
    assert row_of(rejected)["status"] == "rejected"
    hits = mem.store.recall("glossary: Backlog means open tickets.", k=10)
    assert rejected not in {h.item.memory_id for h in hits}
    assert lost in {
        h.item.memory_id for h in mem.store.recall(f"run_summary: {summary['content']}")
    }
    assert mem.store.health().status == "ok"
