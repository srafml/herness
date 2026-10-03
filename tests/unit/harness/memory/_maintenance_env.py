"""Shared helpers for the memory maintenance tests (T07-22): job context, deps, seeds.

Not a test module. The handler runs on the migrated `ops_store` of the test with the real
`MemoryLifecycle`, `ProceduralDeps` (via `_procedural_env.make_deps`), a LanceDB `VectorIndex`
in tmp and an `Embedder` around the recording `FakeEmbed`; `FakeCtx` is a structural
`JobContext` that keeps its state in memory and can yield after chosen steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pytest
from pydantic import JsonValue
from tests.unit.harness.memory._procedural_env import LOW, make_deps
from tests.unit.harness.memory._write_env import (
    KIND_DATA,
    LAYER,
    NOW,
    FakeEmbed,
    memory_rows,
    provenance,
    review_rows,
)

from herness.core import time as clock
from herness.core.ids import new_ulid
from herness.core.types import JobOutcome
from herness.harness.memory import maintenance
from herness.harness.memory.lifecycle import MemoryLifecycle
from herness.harness.memory.maintenance import MaintenanceDeps
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder, VectorIndex, VectorMeta, VectorRow
from herness.store.ops import core
from herness.store.ops import memory as ops

STAMP = clock.format_utc(NOW)
MODEL = "bge-m3"


@dataclass
class FakeJob:
    """The `JobRow` fields the handler reads."""

    job_id: str = "job_maint"
    payload: dict[str, JsonValue] = field(default_factory=dict)


@dataclass
class FakeCtx:
    """A structural `JobContext`: in-memory state, recorded heartbeats, yields after steps."""

    state: dict[str, JsonValue] = field(default_factory=dict)
    yield_after: set[int] = field(default_factory=set)
    notes: list[str | None] = field(default_factory=list)
    saves: list[dict[str, JsonValue]] = field(default_factory=list)
    job: FakeJob = field(default_factory=FakeJob)
    kind: str = "memory_maintenance"
    attempt: int = 1

    @property
    def job_id(self) -> str:
        """The job id."""
        return self.job.job_id

    def should_yield(self) -> bool:
        """True right after a save of a step listed in `yield_after`."""
        return bool(self.saves) and self.saves[-1].get("step") in self.yield_after

    def heartbeat(self, note: str | None = None) -> None:
        """Record the note."""
        self.notes.append(note)

    def save_state(self, state: dict[str, JsonValue]) -> None:
        """Keep a copy as the state of the next attempt."""
        self.saves.append(state)
        self.state = core.load_json(core.dump_json(state, field="s"), field="s")

    def load_state(self) -> dict[str, JsonValue]:
        """The saved state."""
        return dict(self.state)


@dataclass
class MaintEnv:
    """Configured deps plus handles on their fakes."""

    deps: MaintenanceDeps
    vectors: VectorIndex
    embed: FakeEmbed
    opened: list[duckdb.DuckDBPyConnection]

    def run(self, ctx: FakeCtx | None = None) -> tuple[JobOutcome, FakeCtx]:
        """Run the handler once on `ctx` (a fresh one by default)."""
        ctx = ctx or FakeCtx()
        return maintenance.memory_maintenance_handler(ctx), ctx  # type: ignore[arg-type]


def make_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, model: str = MODEL) -> MaintEnv:
    """Deps on the test's ops store, configured into the module seam; time is NOW."""
    monkeypatch.setattr(clock, "now", lambda: NOW)
    proc = make_deps(tmp_path, LOW)
    env = proc.env
    lifecycle = MemoryLifecycle(
        MemoryConfig(), conn_factory=core.connection, vectors=env.vectors,
        writer=env.writer, redactor=env.redactor,
    )  # fmt: skip
    opened: list[duckdb.DuckDBPyConnection] = []

    def open_current() -> duckdb.DuckDBPyConnection:
        opened.append(duckdb.connect())
        return opened[-1]

    deps = MaintenanceDeps(
        lifecycle=lifecycle, procedural=proc.deps, vectors=env.vectors,
        embedder=Embedder(env.embed, model_name=model), open_current=open_current,
    )  # fmt: skip
    monkeypatch.setattr(maintenance, "_SEAM", {})  # restored after the test
    maintenance.configure_maintenance(deps)
    return MaintEnv(deps, env.vectors, env.embed, opened)


def seed_item(
    *,
    kind: str = "glossary",
    status: str = "active",
    content: str = "churn means customers who left",
    data: dict[str, JsonValue] | None = None,
    created_at: datetime = NOW - timedelta(days=1),
    pending: bool = False,
) -> str:
    """Insert one item row (no vector unless `put_vector`); return its id."""
    memory_id = "mem_" + new_ulid()
    stored: dict[str, JsonValue] = {
        **KIND_DATA[kind], "numbers": [], "entities": [], "content_hash": "h" + memory_id[-8:],
        "flags": ["embedding_pending"] if pending else [], "embedding_pending": pending,
        **(data or {}),
    }  # fmt: skip
    ops.insert_memory_item({
        "memory_id": memory_id, "layer": LAYER[kind], "kind": kind, "content": content,
        "data": stored, "provenance": provenance().model_dump(mode="json"),
        "confidence": 0.8, "status": status, "created_at": clock.format_utc(created_at),
        "expires_at": None, "last_used_at": None, "use_count": 0,
    })  # fmt: skip
    return memory_id


def put_vector(
    env: MaintEnv, memory_id: str, *, status: str = "active", content_hash: str | None = None,
    model: str = MODEL, kind: str = "glossary",
) -> None:  # fmt: skip
    """Upsert a vector row for `memory_id` (default: matching its SQLite row's hash)."""
    if content_hash is None:
        rows = ops.get_memory_items([memory_id])
        content_hash = str(rows[0]["data"]["content_hash"]) if rows else "h"
    vec = np.zeros(1024, dtype=np.float32)
    vec[len(memory_id) % 1024] = 1.0
    env.vectors.upsert([VectorRow(memory_id, LAYER[kind], kind, status, content_hash, model,  # type: ignore[arg-type]
                                  vec)])  # fmt: skip


def vector_metas(env: MaintEnv) -> dict[str, VectorMeta]:
    """Every vector row's metadata by id."""
    return {m.memory_id: m for m in env.vectors.list_ids("", 1000)}


def row_of(memory_id: str) -> dict[str, Any]:
    """The stored row of one item (data parsed)."""
    return next(r for r in memory_rows() if r["memory_id"] == memory_id)


def yearly_reviews() -> list[dict[str, Any]]:
    """The `memory_write` review items flagged `yearly_review`."""
    return [r for r in review_rows() if r["payload"].get("flags") == ["yearly_review"]]


def seed_done_run(finished: datetime) -> str:
    """A done `run` row finished at `finished`."""
    run_id = "run_" + new_ulid()
    at = clock.format_utc(finished)
    core.run_write(lambda c: c.execute(
        "INSERT INTO run (run_id, kind, depth, profile, config_hash, build_id, status,"
        " started_at, finished_at) VALUES (?, 'org_review', 'fast', 'p', 'h', NULL, 'done', ?, ?)",
        (run_id, at, at)), op="test_seed")  # fmt: skip
    return run_id
