"""The `MemoryStore` facade (impl 07 U07-97; design 07 §3.3); re-exported by the package.

The constructor builds the collaborators (`InjectionScanner`, `MemoryWriter`, `MemoryLifecycle`,
`MemoryRecaller`) and the deps bundles of the function units once; every public method then
delegates to exactly one unit. The only mutable state is in the lock-protected collaborator
caches (embedding LRU, relatedness cache, the `CURRENT` guard). Lives apart from the package
`__init__` so that importing `herness.harness.memory` stays cheap (T07-23 ruling).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Final, Literal

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.config import HernessConfig
from herness.core.errors import ModelUnavailable, ToolInputError
from herness.core.logging import get_logger
from herness.core.numbers import compile_allowed_patterns
from herness.core.redact import Redactor, get_redactor
from herness.core.types import (
    ConfidenceAdjustment,
    Layer,
    MemoryItem,
    MemoryProposal,
    MemoryRunContext,
    PriorContext,
    RecallHit,
    RecommendationDraft,
    ToolContext,
)
from herness.harness.llm.registry import LLMRegistry
from herness.harness.llm.settings import ClientConfig
from herness.harness.memory import _compose as cp
from herness.harness.memory import chat, episodic, lora, procedural, recommend
from herness.harness.memory.compactor import ContextCompactor
from herness.harness.memory.lifecycle import MemoryLifecycle, purge_args_ok
from herness.harness.memory.maintenance import MaintenanceDeps
from herness.harness.memory.outcome import OutcomeDeps
from herness.harness.memory.policy import InjectionScanner
from herness.harness.memory.recall import MemoryRecaller, RecallResult, RelatednessCache
from herness.harness.memory.render import render_records
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder, VectorIndex
from herness.harness.memory.tokens import TokenCounter
from herness.harness.memory.types import (
    ExportReport,
    PromotionReport,
    ProposeResult,
    RecallFilters,
    SessionContext,
)
from herness.harness.memory.write import MemoryWriter
from herness.store.ops import closed_loop, core
from herness.store.ops import memory as ops

__all__ = ["PENDING_EMBEDDING_MAX", "HealthResult", "MemoryStore"]

PENDING_EMBEDDING_MAX: Final = 1000  # health: more pending embeddings is `degraded`
_log = get_logger("harness.memory")


@dataclass(frozen=True, slots=True)
class HealthResult:
    """`MemoryStore.health()` (U07-97): `down`, `degraded` or `ok` with a short reason."""

    status: Literal["ok", "degraded", "down"]
    reason: str


class MemoryStore:
    """The design 07 §3.3 facade over the memory units (U07-97); thread-safe."""

    @classmethod
    def from_config(cls, cfg: HernessConfig) -> MemoryStore:
        """Build the collaborators exactly as U07-98 describes (impl 06 calls it)."""
        return cls(
            cfg.memory, conn_factory=core.connection, vectors=VectorIndex(),
            embedder=Embedder(model_name=cfg.decisions.embedding.model), redactor=get_redactor(),
            llms=LLMRegistry(cfg.models, profile=cfg.profile,
                             egress_enabled=cfg.security.egress.enabled),
            allowed_numeral_patterns=cfg.app.reports.allowed_numeral_patterns,
            data_root=cfg.paths.data, relatedness=RelatednessCache(),
        )  # fmt: skip

    def __init__(  # noqa: PLR0913 - the U07-97 constructor
        self,
        cfg: MemoryConfig,
        *,
        conn_factory: Callable[[], sqlite3.Connection],
        vectors: VectorIndex,
        embedder: Embedder,
        redactor: Redactor,
        llms: LLMRegistry | None,
        allowed_numeral_patterns: Sequence[str],
        data_root: Path,
        relatedness: RelatednessCache | None = None,
    ) -> None:
        allowed = compile_allowed_patterns(allowed_numeral_patterns)
        scanner = InjectionScanner(cfg.injection_patterns)
        writer = MemoryWriter(
            cfg, redactor=redactor, scanner=scanner, allowed_patterns=allowed,
            conn_factory=conn_factory, vectors=vectors, embedder=embedder,
        )  # fmt: skip
        self._cfg, self._llms, self._allowed, self._writer = cfg, llms, allowed, writer
        self._conn, self._vectors, self._embedder = conn_factory, vectors, embedder
        self._redactor, self._scanner, self._relatedness = redactor, scanner, relatedness
        self._export_root = data_root / "models" / "lora_data"
        self._lifecycle = MemoryLifecycle(cfg, conn_factory=conn_factory, vectors=vectors,
                                          writer=writer, redactor=redactor)  # fmt: skip
        self._recaller = MemoryRecaller(cfg, conn_factory=conn_factory, vectors=vectors,
                                        embedder=embedder, redactor=redactor,
                                        relatedness=relatedness)  # fmt: skip
        self._episodic = episodic.EpisodicDeps(conn_factory, writer, redactor, allowed,
                                               cfg.episodic, cfg.outcome)  # fmt: skip
        self._procedural = procedural.ProceduralDeps(
            conn_factory, writer, vectors, redactor, cfg.procedural, cp.CurrentGuard(), scanner
        )
        self._chat = chat.ChatDeps(cfg.chat, llms or cp.NoModels(), writer, redactor, allowed)
        self.maintenance_deps: Final = MaintenanceDeps(self._lifecycle, self._procedural,
                                                       vectors, embedder)  # fmt: skip
        self.outcome_deps: Final = OutcomeDeps(writer, cfg.outcome, allowed)
        try:
            vectors.ensure_table()
        except ModelUnavailable as exc:  # recall starts degraded; maintenance repairs later
            _log.warning("memory.recall.degraded", reason=type(exc).__name__, run_id=None)

    # --- generic (U07-50 … U07-62)

    def recall(
        self, query: str, layers: Sequence[Layer] | None = None,
        filters: RecallFilters | None = None, k: int = 10,
        run_ctx: MemoryRunContext | None = None,
    ) -> list[RecallHit]:  # fmt: skip
        """Hybrid recall hits in MMR order (U07-62)."""
        return self._recaller.recall(query, layers, filters, k, run_ctx).hits

    def recall_with_status(
        self, query: str, layers: Sequence[Layer] | None = None,
        filters: RecallFilters | None = None, k: int = 10,
        run_ctx: MemoryRunContext | None = None,
    ) -> RecallResult:  # fmt: skip
        """Hits plus `degraded` and the candidate count (U07-62)."""
        return self._recaller.recall(query, layers, filters, k, run_ctx)

    def propose(self, item: MemoryProposal, run_ctx: MemoryRunContext | None = None
                ) -> ProposeResult:  # fmt: skip
        """The write path (U07-50)."""
        return self._writer.propose(item, run_ctx)

    def approve(self, memory_id: str, user_ref: str, note: str | None = None,
                confidence: float | None = None) -> MemoryItem:  # fmt: skip
        """Activate a pending item (U07-51)."""
        return self._lifecycle.approve(memory_id, user_ref, note, confidence)

    def reject(self, memory_id: str, user_ref: str, note: str) -> None:
        """Reject a pending item (U07-52)."""
        self._lifecycle.reject(memory_id, user_ref, note)

    def expire(self, now: datetime | None = None) -> int:
        """TTL sweep (U07-54)."""
        return self._lifecycle.expire(now)

    def expire_item(self, memory_id: str, reason: str, superseded_by: str | None = None) -> None:
        """Expire one item (U07-55)."""
        self._lifecycle.expire_item(memory_id, reason, superseded_by)

    def record_use(self, memory_ids: Sequence[str], run_id: str) -> None:
        """Count one use per rendered item (U07-56)."""
        self._lifecycle.record_use(memory_ids, run_id)

    def purge(self, record_id: str | None = None, *, author_ref: str | None = None,
              now: datetime | None = None) -> int:  # fmt: skip
        """Privacy erasure (U07-100, R-54): items removed; impl 10 and the CLI audit it."""
        if not purge_args_ok(record_id, author_ref):
            msg = "purge needs exactly one of record_id, author_ref"
            raise ToolInputError(msg)
        return self._lifecycle.purge(record_id=record_id, author_ref=author_ref, now=now)

    def render(self, hits: Sequence[RecallHit], max_tokens: int) -> str:
        """The delimited `<memory_context>` block (U07-46)."""
        return render_records(hits, max_tokens).text

    # --- episodic (U07-78 … U07-82)

    def prior_context(self, run_ctx: MemoryRunContext, max_tokens: int = 3000) -> PriorContext:
        """Prior recommendations, decisions and outcomes for run start (U07-81)."""
        return episodic.prior_context(run_ctx, max_tokens, deps=self._episodic)

    def write_recommendations(
        self, run_id: str, recs: Sequence[RecommendationDraft | Mapping[str, JsonValue]]
    ) -> list[str]:
        """Persist a run's recommendations once (U07-78); dict drafts are converted first."""
        drafts = cp.drafts(recs)
        priors = closed_loop.outcomes_for_similarity(conn=self._conn()) if drafts else []
        deps = recommend.RecommendDeps(
            self._conn, self._writer, partial(self._adjust, priors), self._redactor,
            self._allowed, self._cfg.write.max_content_chars,
        )  # fmt: skip
        with cp.run_bound(run_id):
            return recommend.write_recommendations(run_id, drafts, deps=deps)

    def decide(
        self, rec_id: str, decision: Literal["accepted", "rejected", "deferred"], reason: str,
        user_ref: str, effective_at: datetime | None = None,
    ) -> None:  # fmt: skip
        """Record a human decision on a recommendation (U07-82)."""
        episodic.decide(rec_id, decision, reason, user_ref, effective_at, deps=self._episodic)

    def outcome_adjustment(self, draft: RecommendationDraft, base: float) -> ConfidenceAdjustment:
        """Confidence feedback from similar measured priors (U07-80)."""
        return self._adjust(closed_loop.outcomes_for_similarity(conn=self._conn()), draft, base)

    def _adjust(self, priors: Sequence[closed_loop.SimilarityRow], draft: RecommendationDraft,
                base: float) -> ConfidenceAdjustment:  # fmt: skip
        return recommend.outcome_adjustment(
            draft, base, priors=priors, embed=self._embedder.embed,
            related=partial(cp.related, self._relatedness), cfg=self._cfg.feedback,
            now=clock.now(),
        )  # fmt: skip

    # --- working memory (U07-76)

    def compactor(self, profile: ClientConfig, *, ctx: ToolContext) -> ContextCompactor:
        """A `ContextCompactor` for one agent task (R-21: scratchpad in the task checkpoint)."""
        client = self._llms.client(profile.name) if self._llms is not None else None
        return ContextCompactor(
            profile, ctx=ctx, cfg=self._cfg.compaction, counter=TokenCounter(profile),
            client=client, allowed=self._allowed, ops=cp.ScratchpadOps(),
        )  # fmt: skip

    # --- procedural and chat

    def promote_procedural(self, run_id: str) -> PromotionReport:
        """Grow procedural memory from one run's verified SQL (U07-90)."""
        return procedural.promote_procedural(run_id, deps=self._procedural)

    def export_lora(self, out_dir: Path, min_pass_lb: float = 0.8, *,
                    golden_questions: Sequence[str]) -> ExportReport:  # fmt: skip
        """LoRA JSONL export of active qa_pairs (U07-92)."""
        deps = lora.LoraDeps(
            self._conn, self._embedder, self._redactor, self._scanner, self._cfg.procedural.lora,
            self._export_root, cp.current_config_hash(),
        )  # fmt: skip
        return lora.export_lora(out_dir, min_pass_lb, golden_questions=golden_questions,
                                deps=deps)  # fmt: skip

    def session_load(self, session_id: str) -> SessionContext:
        """Chat session summary, last messages and memory ids (U07-93)."""
        return chat.session_load(session_id, deps=self._chat)

    def session_save_turn(self, session_id: str, run_id: str) -> str | None:
        """Correction capture and summary refresh after a chat turn (U07-94, R-32)."""
        return chat.session_save_turn(session_id, run_id, deps=self._chat)

    # --- health (impl 07 §8)

    def health(self) -> HealthResult:
        """`down` when the ops store is unreadable; `degraded` when LanceDB is unavailable or
        more than 1,000 items wait for an embedding; else `ok`. Never raises."""
        try:
            conn = self._conn()
            conn.execute("SELECT 1 FROM memory_item LIMIT 1").fetchall()
            pending = ops.pending_embedding_count(conn=conn)
        except Exception as exc:  # noqa: BLE001 - health reports, never raises (doctor)
            return HealthResult("down", f"ops store unavailable ({type(exc).__name__})")
        try:
            self._vectors.list_ids("", 1)
        except Exception as exc:  # noqa: BLE001 - health reports, never raises (doctor)
            return HealthResult("degraded", f"vector store unavailable ({type(exc).__name__})")
        if pending > PENDING_EMBEDDING_MAX:
            return HealthResult("degraded", f"{pending} items wait for an embedding")
        return HealthResult("ok", "")
