"""Shared helpers for the memory tool tests (T07-11): runs, contexts, hits, fake and real stores.

Not a test module. `FakeStore` records what the tools pass to the three `MemoryToolStore`
methods; `RealStore` is the test-side adapter that composes the real `MemoryRecaller`,
`MemoryWriter` and `MemoryLifecycle` over the migrated `ops_store` (the `MemoryStore` facade is
T07-23's).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue
from tests.support.dispatch_standin import BUILD_ID, make_tool_ctx
from tests.unit.harness.memory._lifecycle_env import make_lifecycle
from tests.unit.harness.memory._write_env import NOW, PLANTED_NAME, FakeEmbed, unit

from herness.core import redact
from herness.core.ids import new_ulid
from herness.core.redact import Redactor
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.core.types import (
    Layer,
    MemoryItem,
    MemoryProposal,
    MemoryRunContext,
    Provenance,
    RecallHit,
    ToolContext,
)
from herness.harness.memory.lifecycle import MemoryLifecycle
from herness.harness.memory.recall import MemoryRecaller, RecallResult
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder
from herness.harness.memory.types import ProposeResult, RecallFilters
from herness.harness.memory.write import MemoryWriter
from herness.store.ops import core, runs

USER_A = "a" * 32
USER_B = "b" * 32
QID = "q_" + "1" * 16


def seed_run(kind: str = "org_review", meta: dict[str, object] | None = None) -> str:
    """Insert one `run` row on the test's ops store; returns its id."""
    run_id = "run_" + new_ulid()
    row = runs.RunRow(
        run_id=run_id, kind=kind, depth="standard", profile="local", build_id=BUILD_ID,
        status="running", started_at=NOW, finished_at=None, token_usage={},
        cost_usd=Decimal(0), config_hash="c" * 16, meta=dict(meta or {}),
    )  # fmt: skip
    core.run_write(lambda conn: runs.insert_run(conn, row), op="test_seed")
    return run_id


def chat_meta(user_ref: str = USER_A) -> dict[str, object]:
    """The chat run meta the tools read (session, user, message)."""
    return {"session_id": "ses_1", "user_ref": user_ref, "message_id": "msg_1"}


def ctx_for(run_id: str, role: str = "analyst", specialty: str = "ops") -> ToolContext:
    """A `ToolContext` of `run_id` with a fresh task id and the given role."""
    base = make_tool_ctx()
    update = {"run_id": run_id, "task_id": "task_" + new_ulid(), "role": role,
              "specialty": specialty}  # fmt: skip
    return base.model_copy(update=update)


def make_hit(
    content: str = "churn: customers who left",
    *,
    status: str = "active",
    score: float = 0.71234,
    kind: str = "glossary",
    layer: str = "semantic",
    numbers: list[JsonValue] | None = None,
) -> RecallHit:
    """A RecallHit around an agent-authored item."""
    prov = Provenance(
        author_type="agent", author_role="analyst_ops", author_ref=None,
        run_id="run_" + new_ulid(), task_id=None, query_ids=[QID], via="tool",
    )  # fmt: skip
    item = MemoryItem(
        memory_id="mem_" + new_ulid(), layer=layer, kind=kind, content=content,  # type: ignore[arg-type]
        data={"numbers": numbers or []}, provenance=prov, confidence=0.9,
        status=status, created_at=NOW - timedelta(days=1),  # type: ignore[arg-type]
        expires_at=None, last_used_at=None, use_count=0,
    )  # fmt: skip
    comps: dict[Any, float] = {"sim": 0, "kw": 0, "ent": 0, "rec": 0, "conf": 0, "final": score}
    return RecallHit(
        item=item, score=score, components=comps, unconfirmed=status == "pending_approval"
    )


@dataclass
class FakeStore:
    """Records the calls of the tools; returns the configured results or raises."""

    hits: list[RecallHit] = field(default_factory=list)
    degraded: bool = False
    result: ProposeResult | None = None
    propose_error: Exception | None = None
    recall_error: Exception | None = None
    use_error: Exception | None = None
    recalls: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[tuple[MemoryProposal, MemoryRunContext | None]] = field(default_factory=list)
    uses: list[tuple[list[str], str]] = field(default_factory=list)

    def recall_with_status(
        self, query: str, layers: Sequence[Layer] | None = None,
        filters: RecallFilters | None = None, k: int = 10,
        run_ctx: MemoryRunContext | None = None,
    ) -> RecallResult:  # fmt: skip
        """Record the arguments; return the configured hits."""
        self.recalls.append(
            {"query": query, "layers": layers, "filters": filters, "k": k, "run_ctx": run_ctx}
        )
        if self.recall_error is not None:
            raise self.recall_error
        return RecallResult(hits=list(self.hits), degraded=self.degraded, n_candidates=9)

    def propose(
        self, item: MemoryProposal, run_ctx: MemoryRunContext | None = None
    ) -> ProposeResult:
        """Record the proposal; return the configured result."""
        self.proposals.append((item, run_ctx))
        if self.propose_error is not None:
            raise self.propose_error
        return self.result or ProposeResult(
            memory_id="mem_" + new_ulid(), status="pending_approval",
            review_item_id="rev_1", merged_into=None, flags=[],
        )  # fmt: skip

    def record_use(self, memory_ids: Sequence[str], run_id: str) -> None:
        """Record the use; raise the configured error."""
        self.uses.append((list(memory_ids), run_id))
        if self.use_error is not None:
            raise self.use_error


@dataclass
class RealStore:
    """Test-side adapter: the three tool methods over the real memory units."""

    recaller: MemoryRecaller
    writer: MemoryWriter
    lifecycle: MemoryLifecycle
    embed: FakeEmbed
    proposals: int = 0

    def same_vector(self, *texts: str) -> None:
        """Embed every text in `texts` as the same unit vector (recall finds them by sim)."""
        for text in texts:
            self.embed.overrides[text] = unit(0)

    def recall_with_status(
        self, query: str, layers: Sequence[Layer] | None = None,
        filters: RecallFilters | None = None, k: int = 10,
        run_ctx: MemoryRunContext | None = None,
    ) -> RecallResult:  # fmt: skip
        """`MemoryRecaller.recall`."""
        return self.recaller.recall(query, layers, filters, k, run_ctx)

    def propose(
        self, item: MemoryProposal, run_ctx: MemoryRunContext | None = None
    ) -> ProposeResult:
        """`MemoryWriter.propose` (counted)."""
        self.proposals += 1
        return self.writer.propose(item, run_ctx)

    def record_use(self, memory_ids: Sequence[str], run_id: str) -> None:
        """`MemoryLifecycle.record_use`."""
        self.lifecycle.record_use(memory_ids, run_id)


def real_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RealStore:
    """The real composition on the test's migrated ops store (LanceDB in `tmp_path`)."""
    env = make_lifecycle(tmp_path, monkeypatch)
    base = env.writer
    recaller = MemoryRecaller(
        MemoryConfig(), conn_factory=core.connection, vectors=base.vectors,
        embedder=Embedder(base.embed, model_name="bge-m3"), redactor=base.redactor,
    )  # fmt: skip
    monkeypatch.setattr(redact, "get_redactor", lambda: base.redactor)  # the write path's
    return RealStore(recaller, base.writer, env.lifecycle, base.embed)


def use_process_redactor(monkeypatch: pytest.MonkeyPatch) -> Redactor:
    """Make `get_redactor()` return a redactor that masks PLANTED_NAME and emails."""
    directory = NameDirectory.from_files(None, (PLANTED_NAME,), None)
    redactor = Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(redact, "get_redactor", lambda: redactor)
    return redactor
