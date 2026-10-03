"""Shared helpers for the MemoryLifecycle tests (T07-09): lifecycle factory, seeds, fakes.

Not a test module. The lifecycle runs on the migrated `ops_store` of the test with the real
`Redactor` and `MemoryWriter` of `_write_env`; vectors are a recording fake (status mirror
calls) that can be switched to fail like an unavailable LanceDB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue
from tests.unit.harness.memory._write_env import (
    KIND_DATA,
    LAYER,
    NOW,
    make_writer,
    memory_rows,
    provenance,
)
from tests.unit.harness.memory._write_env import (
    Env as WriterEnv,
)

from herness.core import time as clock
from herness.core.errors import ModelUnavailable
from herness.core.ids import new_ulid
from herness.harness.memory.lifecycle import MemoryLifecycle
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import VectorIndex
from herness.store.ops import core, shared
from herness.store.ops import memory as ops

REVIEWER = "b" * 32


@dataclass
class FakeVectors:
    """Records `set_status` calls; `fail` raises ModelUnavailable like a down LanceDB."""

    calls: list[tuple[list[str], str]] = field(default_factory=list)
    fail: bool = False

    def set_status(self, memory_ids: list[str], status: str) -> None:
        """Record the mirror call, or fail."""
        if self.fail:
            msg = "memory vector store unavailable: set_status"
            raise ModelUnavailable(msg)
        self.calls.append((list(memory_ids), status))


@dataclass
class Env:
    """A lifecycle with handles on its fake vectors and the recorded audit calls."""

    lifecycle: MemoryLifecycle
    vectors: FakeVectors
    audits: list[tuple[str, str, dict[str, Any]]]
    writer: WriterEnv


def make_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, real_audit: bool = False
) -> Env:
    """MemoryLifecycle on the test's ops store; `shared.audit` is recorded unless `real_audit`."""
    audits: list[tuple[str, str, dict[str, Any]]] = []
    if not real_audit:

        def record(event: str, actor: str, **fields: Any) -> None:
            audits.append((event, actor, fields))

        monkeypatch.setattr(shared, "audit", record)
    base = make_writer(tmp_path)
    fake = FakeVectors()
    lifecycle = MemoryLifecycle(
        MemoryConfig(),
        conn_factory=core.connection,
        vectors=cast("VectorIndex", fake),
        writer=base.writer,
        redactor=base.redactor,
    )
    return Env(lifecycle, fake, audits, base)


def seed_item(  # noqa: PLR0913 - one keyword per seeded column under test
    *,
    kind: str = "glossary",
    status: str = "pending_approval",
    confidence: float = 0.5,
    data: dict[str, JsonValue] | None = None,
    content: str = "churn means customers who left",
    expires_at: datetime | None = None,
    review: bool = True,
) -> str:
    """Insert one item (a pending one with its `memory_write` review item); return its id."""
    memory_id = "mem_" + new_ulid()
    stored: dict[str, JsonValue] = {**KIND_DATA[kind], "entities": [], **(data or {})}
    if review and status == "pending_approval":
        payload = {"memory_id": memory_id, "content": content,
                   "conflicts_with": stored.get("conflicts_with", [])}  # fmt: skip
        stored["review_item_id"] = shared.create_review_item("memory_write", payload, now=NOW)
    ops.insert_memory_item({
        "memory_id": memory_id, "layer": LAYER[kind], "kind": kind, "content": content,
        "data": stored, "provenance": provenance().model_dump(mode="json"),
        "confidence": confidence, "status": status,
        "created_at": clock.format_utc(NOW - timedelta(days=1)),
        "expires_at": None if expires_at is None else clock.format_utc(expires_at),
        "last_used_at": None, "use_count": 0,
    })  # fmt: skip
    return memory_id


def row_of(memory_id: str) -> dict[str, Any]:
    """The stored row of one item (data parsed)."""
    return next(r for r in memory_rows() if r["memory_id"] == memory_id)


def review_of(item_id: str) -> shared.ReviewItem:
    """The stored review item."""
    return shared.get_review_item(item_id)
