"""Fault tests for the memory maintenance job (impl 07 FT07-01; U07-96, T07-22).

In process: `fault_point("sqlite.write")` rules through `fault_env`, an embedder that raises,
and a caller transaction whose `embed_after_commit` never runs; each leaves the item
`embedding_pending` and the next maintenance run backfills it. Real kill (T07-23, the T05-27
`loop_kill` pattern): the child `tests.support.memory_kill` stores an item through the
`MemoryStore` facade and is killed by the fault plan before the vector or the pending flag is
written; the parent's registered `memory_maintenance` handler flags and backfills it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.support.fault_env import FaultEnv
from tests.support.memory_kill import SURVIVED
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._maintenance_env import (
    FakeCtx,
    MaintEnv,
    make_env,
    row_of,
    vector_metas,
)
from tests.unit.harness.memory._write_env import (
    NOW,
    PATTERNS,
    ROOT,
    RUN_ID,
    FakeEmbed,
    make_writer,
    proposal,
    provenance,
)

from herness.core.errors import StoreBusy
from herness.core.jobs.handlers import resolve_handler
from herness.core.resilience import ProcessState
from herness.harness.memory import MemoryStore, register_memory_components
from herness.harness.memory.policy import keyed_hash
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder, VectorIndex
from herness.harness.tools import ToolRegistry
from herness.store.ops import core
from herness.store.vectors import VectorStore

pytestmark = pytest.mark.fault


@pytest.fixture
def env(
    ops_store: OpsStoreHandle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_process_state: ProcessState,
) -> MaintEnv:
    """Configured maintenance deps; no-op retry sleeps from `reset_process_state`."""
    del ops_store, reset_process_state
    return make_env(tmp_path, monkeypatch)


def _backfilled(env: MaintEnv, memory_id: str) -> None:
    row = row_of(memory_id)
    assert row["data"]["embedding_pending"] is False
    assert "embedding_pending" not in row["data"]["flags"]
    assert vector_metas(env)[memory_id].content_hash == row["data"]["content_hash"]


def test_ft07_01_sqlite_retry_and_embed_failure_then_backfill(
    env: MaintEnv, fault_env: FaultEnv
) -> None:
    """FT07-01 fault_point sqlite.write (StoreBusy once) on the propose transaction plus an
    embedding failure: the committed item has `embedding_pending` and no vector; the next
    maintenance run backfills it."""
    fault_env([{"point": "sqlite.write", "action": "error:StoreBusy", "kind": "memory_propose",
                "count": 1}])  # fmt: skip
    env.embed.fail = True
    writer = env.deps.procedural.writer
    result = writer.propose(proposal("Churn means customers who left."), now=NOW)
    assert "embedding_pending" in result.flags
    assert row_of(result.memory_id)["data"]["embedding_pending"] is True
    assert vector_metas(env) == {}
    env.embed.fail = False
    outcome, _ = env.run()
    assert outcome.result["backfill"] == {"embedded": 1, "stopped": False}
    _backfilled(env, result.memory_id)


def test_ft07_01_kill_between_commit_and_vector_upsert(env: MaintEnv) -> None:
    """FT07-01 a caller transaction commits a system item and the process dies before
    `embed_after_commit`: the item is `embedding_pending`; maintenance backfills it."""
    item = proposal("Run recorded recommendations.", provenance("system"), kind="run_summary")
    writer = env.deps.procedural.writer
    result = core.run_write(
        lambda conn: writer.insert_system_item(
            item, key_hash=keyed_hash("run_summary:" + RUN_ID), conn=conn, now=NOW),
        op="test_caller",
    )  # fmt: skip
    assert row_of(result.memory_id)["data"]["embedding_pending"] is True
    assert vector_metas(env) == {}
    env.run()
    _backfilled(env, result.memory_id)


def test_ft07_01_store_busy_in_backfill_resumes_idempotently(
    env: MaintEnv, fault_env: FaultEnv
) -> None:
    """FT07-01 StoreBusy on every attempt of the backfill flag-clear transaction: the job
    fails with StoreBusy after the vector upsert, the item stays pending with step 5 saved;
    the retried attempt resumes at step 6 and clears it (one vector row, no duplicate)."""
    writer = env.deps.procedural.writer
    env.embed.fail = True
    result = writer.propose(proposal("MTTR means mean time to restore."), now=NOW)
    env.embed.fail = False
    fault_env([{"point": "sqlite.write", "action": "error:StoreBusy",
                "kind": "memory_maintenance_backfill", "count": 7}])  # fmt: skip
    ctx = FakeCtx()
    with pytest.raises(StoreBusy):
        env.run(ctx)
    assert ctx.state["step"] == 5
    assert row_of(result.memory_id)["data"]["embedding_pending"] is True
    assert list(vector_metas(env)) == [result.memory_id]
    fault_env([])
    outcome, resumed = env.run(FakeCtx(state=ctx.state))
    assert outcome.status == "done"
    assert [s["step"] for s in resumed.saves] == [6, 7]
    _backfilled(env, result.memory_id)
    assert list(vector_metas(env)) == [result.memory_id]


def test_ft07_01_process_killed_after_commit_then_maintenance_backfills(
    ops_store: OpsStoreHandle, tmp_path: Path, reset_process_state: ProcessState
) -> None:
    """FT07-01 a real process kill between the SQLite commit and the vector upsert: the
    child dies (fault plan `kill` on the pending-flag write after the failed upsert); the item
    is stored with neither a vector nor `embedding_pending`; maintenance (registered by
    `register_memory_components`) flags it and backfills the vector."""
    del reset_process_state
    plan = tmp_path / "kill_plan.json"
    plan.write_text(json.dumps([{"point": "sqlite.write", "action": "kill",
                                 "kind": "memory_embedding_flag"}]), encoding="utf-8")  # fmt: skip
    vectors_dir = tmp_path / "vectors"
    child_env = {**os.environ, "HERNESS_ENV": "test", "HERNESS_FAULTS": str(plan)}
    content = "Churn means customers who left the service."
    out = subprocess.run(  # noqa: S603 - fixed interpreter and module
        [sys.executable, "-m", "tests.support.memory_kill", str(ops_store.db_path),
         str(vectors_dir), content],
        cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=180, check=False,
    )  # fmt: skip
    assert out.returncode != 0, out.stdout[-500:] + out.stderr[-1500:]
    assert SURVIVED not in out.stdout
    (row,) = core.read_all("SELECT memory_id FROM memory_item")
    memory_id = row["memory_id"]
    assert row_of(memory_id)["data"].get("embedding_pending") in (None, False)
    vectors = VectorIndex(lambda: VectorStore(vectors_dir))
    store = MemoryStore(
        MemoryConfig(injection_patterns=PATTERNS), conn_factory=core.connection,
        vectors=vectors, embedder=Embedder(FakeEmbed(), model_name="bge-m3"),
        redactor=make_writer(tmp_path).redactor, llms=None,
        allowed_numeral_patterns=(r"(INC|CHG|PRB)\d+",), data_root=ops_store.data_root,
    )  # fmt: skip
    assert vectors.list_ids("", 10) == []
    register_memory_components(ToolRegistry(), store)
    outcome = resolve_handler("memory_maintenance")(FakeCtx())  # type: ignore[arg-type]
    assert outcome.result["vectors"]["flagged"] == 1
    assert outcome.result["backfill"] == {"embedded": 1, "stopped": False}
    (meta,) = vectors.list_ids("", 10)
    assert meta.memory_id == memory_id
    assert meta.content_hash == row_of(memory_id)["data"]["content_hash"]
    assert row_of(memory_id)["data"]["embedding_pending"] is False
