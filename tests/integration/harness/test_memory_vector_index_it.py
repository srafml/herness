"""Integration tests of the memory ANN index (impl 07 IT07-12, IT07-13; U07-49, U07-96, T07-27).

IT07-12: a real process kill (the T05-27 / FT07-01 `memory_kill` child, `--bulk` mode) between
a bulk write of memory items and their vectors and the index refresh leaves a usable,
unindexed table that flat search answers; the next `memory_maintenance` run (registered by
`register_memory_components`) builds the index in step 8. IT07-13 (TH07-21): `purge` after
the index is built deletes the vectors, so neither the index nor the flat scan returns them.
The threshold `_vector_ann.MIN_ROWS` is lowered so a few hundred rows stand for 10,000.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import JsonValue
from tests.support.memory_bench_corpus import mem_id, seed_vectors
from tests.support.memory_kill import SURVIVED
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._maintenance_env import FakeCtx
from tests.unit.harness.memory._write_env import (
    KIND_DATA,
    PATTERNS,
    ROOT,
    FakeEmbed,
    make_writer,
    proposal,
    provenance,
)

from herness.core.jobs.handlers import resolve_handler
from herness.core.resilience import ProcessState
from herness.harness.memory import MemoryStore, register_memory_components
from herness.harness.memory import _vector_ann as ann
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder, VectorIndex
from herness.harness.tools import ToolRegistry
from herness.store.ops import core
from herness.store.vectors import VectorStore

pytestmark = pytest.mark.integration

BULK = 600
SMALL = 500
LAYERS = ["semantic", "procedural", "episodic"]
RECORD = "src:incident:INC0077"


def _store(vectors: VectorIndex, data_root: Path, tmp_path: Path) -> MemoryStore:
    return MemoryStore(
        MemoryConfig(injection_patterns=PATTERNS), conn_factory=core.connection,
        vectors=vectors, embedder=Embedder(FakeEmbed(), model_name="bge-m3"),
        redactor=make_writer(tmp_path).redactor, llms=None,
        allowed_numeral_patterns=(r"(INC|CHG|PRB)\d+",), data_root=data_root,
    )  # fmt: skip


def _indexed(vectors: VectorIndex) -> bool:
    return any("vector" in i.columns for i in vectors._tbl(latest=True).list_indices())


def test_it07_12_kill_between_bulk_write_and_refresh_then_maintenance_builds(
    ops_store: OpsStoreHandle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_process_state: ProcessState,
) -> None:
    """IT07-12 the child bulk-writes items and vectors and is killed (fault plan `kill` on
    `enrich.after_batch_write`) before its index refresh: the table has every row and no
    index, flat search answers; the next maintenance run builds the index in step 8, the
    run after it optimizes, and search answers through the index."""
    del reset_process_state
    monkeypatch.setattr(ann, "MIN_ROWS", SMALL)
    plan = tmp_path / "kill_plan.json"
    plan.write_text(json.dumps([{"point": "enrich.after_batch_write", "action": "kill",
                                 "kind": "memory_bulk"}]), encoding="utf-8")  # fmt: skip
    root = tmp_path / "bulk"
    child_env = {**os.environ, "HERNESS_ENV": "test", "HERNESS_FAULTS": str(plan)}
    out = subprocess.run(  # noqa: S603 - fixed interpreter and module
        [sys.executable, "-m", "tests.support.memory_kill", "--bulk", str(ops_store.db_path),
         str(root), str(BULK)],
        cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    assert out.returncode != 0, out.stdout[-500:] + out.stderr[-1500:]
    assert SURVIVED not in out.stdout
    assert "resilience.faults.kill" in out.stdout + out.stderr  # killed, not crashed
    assert core.read_all("SELECT count(*) AS n FROM memory_item")[0]["n"] == BULK
    vectors = VectorIndex(lambda: VectorStore(root / "vectors"))
    assert len(vectors.list_ids("", 1000)) == BULK
    assert not _indexed(vectors)
    probe = vectors.vectors([mem_id(42)])[mem_id(42)]
    assert vectors.search(probe, LAYERS, ["active"], 3)[0][0] == mem_id(42)  # flat answer

    register_memory_components(ToolRegistry(), _store(vectors, ops_store.data_root, tmp_path))
    outcome = resolve_handler("memory_maintenance")(FakeCtx())  # type: ignore[arg-type]

    assert outcome.status == "done"
    assert outcome.result["vectors"]["deleted"] == 0
    assert outcome.result["index"] == {"action": "built", "rows": BULK}
    assert _indexed(vectors)
    assert vectors.search(probe, LAYERS, ["active"], 3)[0][0] == mem_id(42)
    again = resolve_handler("memory_maintenance")(FakeCtx())  # type: ignore[arg-type]
    assert again.result["index"] == {"action": "optimized", "rows": BULK}


def test_it07_13_purge_after_indexing_deletes_vectors(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT07-13 TH07-21 two items cite a record and the index is built over their vectors;
    `purge(record)` deletes the vector rows (the row count drops, `vectors()` finds none) and
    neither the indexed search nor the optimized index after maintenance returns them."""
    monkeypatch.setattr(ann, "MIN_ROWS", SMALL)
    seed_vectors(tmp_path, BULK)  # bulk vectors in tmp_path/vectors, as make_writer's index
    env = make_writer(tmp_path)
    store = _store(env.vectors, ops_store.data_root, tmp_path)
    cites: dict[str, JsonValue] = {**KIND_DATA["glossary"],
                                   "entities": [{"type": "record", "id": RECORD}]}  # fmt: skip
    active = store.propose(proposal("Churn spiked after the osprey outage.", data=cites))
    pending = store.propose(
        proposal("Backlog means open osprey tickets.", provenance("agent"), data=cites)
    )
    purged = [active.memory_id, pending.memory_id]
    kept = store.propose(proposal("MTTR means the mean time to restore a service.")).memory_id
    assert env.vectors.refresh_index() == {"action": "built", "rows": BULK + 3}
    found = env.vectors.vectors([*purged, kept])
    for memory_id in purged:
        hits = env.vectors.search(found[memory_id], LAYERS, ["active", "pending_approval"], 5)
        assert hits[0] == (memory_id, pytest.approx(0.0, abs=1e-5))

    assert store.purge(RECORD) == 2

    table = env.vectors._tbl(latest=True)
    assert table.count_rows() == BULK + 1
    assert env.vectors.vectors(purged) == {}
    statuses = ["active", "pending_approval", "candidate", "rejected", "expired"]
    for memory_id in purged:
        hits = env.vectors.search(found[memory_id], LAYERS, statuses, 20)
        assert memory_id not in {h[0] for h in hits}
    assert env.vectors.refresh_index() == {"action": "optimized", "rows": BULK + 1}
    for memory_id in purged:
        hits = env.vectors.search(found[memory_id], LAYERS, statuses, 20)
        assert memory_id not in {h[0] for h in hits}
    assert env.vectors.search(found[kept], LAYERS, ["active"], 1)[0][0] == kept
