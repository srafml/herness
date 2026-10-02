"""The memory write path: `MemoryWriter.propose` and `insert_system_item` (impl 07 U07-50).

Design 07 §5.8. Nothing is stored before the schema, limit, redaction, numeral, injection,
provenance, policy, idempotency and rate steps pass; the insert (item + review item) is one
`run_write` that repeats the idempotency and rate checks; the vector follows after commit.
Logs and errors carry ids, rule names, counts and pattern indices only, never memory text.
"""

import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Final

import numpy as np
from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import ModelUnavailable, PolicyViolation
from herness.core.logging import get_logger
from herness.core.redact import Redactor
from herness.core.types import KIND_LAYER, MemoryProposal, MemoryRunContext
from herness.harness.memory import _write_steps as st
from herness.harness.memory._write_steps import ID_KEYS, Conn, Draft, reject
from herness.harness.memory.policy import (
    InjectionScanner,
    PolicyDecision,
    check_limits,
    content_hash,
    decide_policy,
    merge_confidence,
)
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder, VectorIndex, VectorRow
from herness.harness.memory.types import ProposeResult
from herness.store.ops import core
from herness.store.ops import memory as ops
from herness.store.ops.shared import create_review_item

__all__ = ["ID_KEYS", "MemoryWriter"]

_ALL_STATUSES: Final = ("candidate", "pending_approval", "active", "expired", "rejected")
_LIVE: Final = ("active", "pending_approval")
_DEDUPE_STATUSES: Final = ("active", "pending_approval", "candidate")
_RATE_VIA: Final = frozenset({"tool", "chat", "dashboard", "cli"})
_SESSION_VIA: Final = frozenset({"chat", "dashboard"})
_HISTORY_MAX: Final = 20
_CONFLICTS_MAX: Final = 10
_NEAR_K: Final = 10
_DAY: Final = timedelta(hours=24)
_log = get_logger("memory")


class MemoryWriter:
    """The design 07 §5.8 propose pipeline; thread-safe (only immutable collaborators)."""

    def __init__(  # noqa: PLR0913 - collaborator set fixed by U07-50
        self,
        cfg: MemoryConfig,
        *,
        redactor: Redactor,
        scanner: InjectionScanner,
        allowed_patterns: Sequence[re.Pattern[str]],
        conn_factory: Callable[[], sqlite3.Connection],
        vectors: VectorIndex,
        embedder: Embedder,
    ) -> None:
        self._cfg, self._redactor, self._scanner = cfg, redactor, scanner
        self._allowed = tuple(allowed_patterns)
        self._conn, self._vectors, self._embedder = conn_factory, vectors, embedder

    def propose(
        self,
        item: MemoryProposal,
        run_ctx: MemoryRunContext | None = None,
        *,
        now: datetime | None = None,
    ) -> ProposeResult:
        """Insert, merge or repeat one proposal; `PolicyViolation` with nothing stored."""
        return self._guarded(item, run_ctx, None, None, now or clock.now())

    def insert_system_item(
        self,
        item: MemoryProposal,
        *,
        key_hash: str,
        conn: sqlite3.Connection | None = None,
        now: datetime | None = None,
    ) -> ProposeResult:
        """The same steps keyed by `key_hash`; with `conn`, inside the caller's transaction.

        With `conn` the vector step is skipped and `embedding_pending` is set; the caller
        calls `embed_after_commit(memory_id)` after its commit."""
        return self._guarded(item, None, key_hash, conn, now or clock.now())

    def embed_after_commit(self, memory_id: str) -> None:
        """Embed a committed item and clear its `embedding_pending`; a failure keeps it set."""
        rows = ops.get_memory_items([memory_id], conn=self._conn())
        if rows and self._upsert(rows[0], None):
            self._set_pending(memory_id, value=False)

    # ------------------------------------------------------------------ pipeline

    def _guarded(
        self,
        item: MemoryProposal,
        run_ctx: MemoryRunContext | None,
        key_hash: str | None,
        conn: Conn | None,
        now: datetime,
    ) -> ProposeResult:
        prov = item.provenance
        try:
            return self._pipeline(item, run_ctx, key_hash, conn, now)
        except PolicyViolation as exc:
            rule = str(exc.details.get("rule", "unknown"))
            _log.warning(
                "memory.proposal.rejected", rule=rule, layer=item.layer, kind=item.kind,
                via=prov.via, run_id=prov.run_id, task_id=prov.task_id,
            )  # fmt: skip
            st.count("herness_memory_policy_violations_total", rule=rule)
            raise

    def _pipeline(
        self,
        item: MemoryProposal,
        run_ctx: MemoryRunContext | None,
        key_hash: str | None,
        conn: Conn | None,
        now: datetime,
    ) -> ProposeResult:
        if KIND_LAYER[item.kind] != item.layer:  # 1. schema
            reject("schema.layer_kind")
        check_limits(item.kind, item.content, item.data, item.numbers, self._cfg.write)  # 2.
        content, data, flags = st.redact_payload(self._redactor, item)  # 3.
        st.check_numerals(item, content, self._allowed, flags)  # 4.
        if hits := self._scanner.scan_payload(content, data):  # 5.
            flags.append("instruction_like")
            task_id = item.provenance.task_id
            _log.warning("memory.injection.flagged", task_id=task_id, pattern_indices=hits)
            st.count("herness_memory_injection_flags_total", kind=item.kind)
        read = conn if conn is not None else self._conn()
        verified = self._provenance(item, run_ctx, read)  # 6.
        key = key_hash if key_hash is not None else content_hash(content)
        draft = Draft(item, content, data, flags, self._decide(item, data, flags), key,
                      system=key_hash is not None)  # 7.  # fmt: skip
        if (found := self._repeat(draft, read)) is not None:  # 8.
            return found
        self._rate(draft, now, read)  # 9.
        draft.confidence = st.confidence(item, verified)  # 10.
        dedupe = not draft.system and item.layer != "procedural"  # 11. (keyed / fingerprint)
        if dedupe and "instruction_like" not in flags and (merged := self._dedupe(draft, read)):
            return merged
        if conn is not None and "embedding_pending" not in flags:
            flags.append("embedding_pending")  # the caller embeds after its commit
        result, row = self._insert(draft, conn, now)  # 12.
        if row is None:  # a concurrent identical proposal won inside the transaction
            return result
        if (
            conn is None
            and "embedding_pending" not in flags
            and not self._upsert(row, draft.vector)
        ):
            self._set_pending(result.memory_id, value=True)  # 13.
            flags.append("embedding_pending")
            result = result.model_copy(update={"flags": list(flags)})
        self._stored(result, draft)  # 14.
        return result

    def _decide(
        self, item: MemoryProposal, data: dict[str, JsonValue], flags: Sequence[str]
    ) -> PolicyDecision:
        """Step 7: the §4.2 policy matrix (instruction_like and conflict force pending)."""
        expiry = {str(k): v for k, v in self._cfg.write.expiry_days.items()}
        return decide_policy(item.kind, item.provenance, data, flags, expiry)

    def _stored(self, result: ProposeResult, draft: Draft) -> None:
        """Step 14: log and count one stored or merged proposal."""
        item, prov = draft.item, draft.prov
        _log.info(
            "memory.proposal.stored", memory_id=result.memory_id, layer=item.layer,
            kind=item.kind, status=result.status, flags=list(result.flags),
            merged_into=result.merged_into, run_id=prov.run_id, task_id=prov.task_id,
        )  # fmt: skip
        labels = {"layer": item.layer, "kind": item.kind, "status": result.status}
        st.count("herness_memory_proposals_total", **labels)

    def _provenance(
        self, item: MemoryProposal, run_ctx: MemoryRunContext | None, read: Conn
    ) -> list[float]:
        """Step 6 (a)-(d); returns the confidences of the cited verified findings."""
        prov = item.provenance
        ctx_ids = None if run_ctx is None else (run_ctx.run_id, run_ctx.task_id)
        if prov.author_type == "agent" and ctx_ids not in (None, (prov.run_id, prov.task_id)):
            reject("provenance.mismatch")
        query_ids = sorted({*prov.query_ids, *(n.query_id for n in item.numbers)})
        if query_ids and ops.existing_query_ids(query_ids, conn=read) != set(query_ids):
            reject("provenance.query_ids")
        facts = ops.finding_facts(prov.finding_ids, conn=read) if prov.finding_ids else {}
        verified = [f["confidence"] for f in facts.values() if f["status"] == "verified"]
        if item.kind == "insight" and len(verified) != len(set(prov.finding_ids)):
            reject("provenance.findings")
        if item.kind == "user_correction" and prov.via in _SESSION_VIA:
            st.check_session(prov)
        return verified

    def _repeat(self, draft: Draft, conn: Conn) -> ProposeResult | None:
        """Step 8: the same task and hash (system path: the same key) returns that item."""
        task_id, row = draft.prov.task_id, None
        if task_id is not None:
            task_hash = (task_id, draft.key)
            row = ops.find_memory_item(task_hash=task_hash, statuses=_ALL_STATUSES, conn=conn)
        if row is None and draft.system:
            row = ops.find_memory_item(content_hash=draft.key, statuses=_ALL_STATUSES, conn=conn)
        if row is None:
            return None
        _log.debug("memory.proposal.repeated", memory_id=row["memory_id"], task_id=task_id)
        return st.result(row)

    def _rate(self, draft: Draft, now: datetime, conn: Conn) -> None:
        """Step 9: per run, per chat session, and corrections per user and day."""
        prov, limits = draft.prov, self._cfg.write.rate_limits
        if prov.via not in _RATE_VIA:
            return
        if prov.run_id and ops.count_proposals(run_id=prov.run_id, conn=conn) >= limits.per_run:
            reject("rate.per_run")
        sid = prov.session_id
        if sid and ops.count_proposals(session_id=sid, conn=conn) >= limits.per_chat_session:
            reject("rate.per_chat_session")
        if draft.item.kind == "user_correction" and prov.author_ref:
            since = clock.format_utc(now - _DAY)
            n = ops.count_proposals(
                author_ref=prov.author_ref, kind="user_correction", since=since, conn=conn
            )
            if n >= limits.corrections_per_user_day:
                reject("rate.corrections_per_user_day")

    def _dedupe(self, draft: Draft, read: Conn) -> ProposeResult | None:
        """Step 11: an exact or near duplicate is merged; a close active item is a conflict."""
        item = draft.item
        exact = ops.find_memory_item(
            layer=item.layer, kind=item.kind, content_hash=draft.key,
            statuses=_DEDUPE_STATUSES, conn=read,
        )  # fmt: skip
        target = exact["memory_id"] if exact is not None else self._near(draft, read)
        return None if target is None else self._merge(draft, target)

    def _near(self, draft: Draft, read: Conn) -> str | None:
        """Step 11 (b)-(d): embed the redacted text, ANN search, cosine near-dup or conflict."""
        item, dedupe = draft.item, self._cfg.write.dedupe
        try:
            draft.vector = self._embedder.embed_item(item.kind, draft.content)
            hits = dict(self._vectors.search(draft.vector, [item.layer], _LIVE, _NEAR_K))
        except ModelUnavailable:
            if draft.vector is None:
                draft.flags.append("embedding_pending")
            return None
        mine = st.entity_ids(draft.data)
        best: tuple[float, str] | None = None
        conflicts: list[tuple[float, str]] = []
        for row in ops.get_memory_items(list(hits), conn=read):
            if row["kind"] != item.kind or row["status"] not in _LIVE:
                continue
            cos, theirs = 1.0 - hits[row["memory_id"]], st.entity_ids(row["data"])
            if cos >= dedupe.merge_cosine and (mine & theirs or not (mine or theirs)):
                best = max(best or (cos, row["memory_id"]), (cos, row["memory_id"]))
            elif row["status"] == "active" and cos >= dedupe.conflict_cosine and mine & theirs:
                conflicts.append((cos, row["memory_id"]))
        if best is not None:
            return best[1]
        if conflicts:
            draft.conflicts = [i for _, i in sorted(conflicts, reverse=True)][:_CONFLICTS_MAX]
            draft.flags.append("conflict")
            draft.decision = self._decide(item, draft.data, draft.flags)  # step 7 again
        return None

    def _merge(self, draft: Draft, target: str) -> ProposeResult | None:
        """Merge into `target` in one run_write; None (insert instead) when it vanished."""

        def tx(conn: Conn) -> ProposeResult | None:
            rows = ops.get_memory_items([target], conn=conn)
            if not rows:
                return None
            old = rows[0]
            data, history = dict(old["data"]), old["data"].get("provenance_history")
            kept = history if isinstance(history, list) else []
            entry = draft.prov.model_dump(mode="json")
            data["provenance_history"] = [*kept, entry][-_HISTORY_MAX:]
            conf = None
            if draft.decision.status == "active":
                conf = merge_confidence(old["confidence"], draft.confidence)
            ops.update_memory_item(target, data=data, confidence=conf, conn=conn)
            return st.result({**old, "data": data}, merged=True, flags=list(draft.flags))

        result = core.run_write(tx, op="memory_merge")
        if result is not None:
            self._stored(result, draft)
        return result

    def _insert(
        self, draft: Draft, conn: Conn | None, now: datetime
    ) -> tuple[ProposeResult, ops.MemoryItemRow | None]:
        """Step 12: item (+ review item when pending) in one transaction; steps 8-9 repeated."""

        def tx(c: Conn) -> tuple[ProposeResult, ops.MemoryItemRow | None]:
            if (found := self._repeat(draft, c)) is not None:
                return found, None
            self._rate(draft, now, c)
            row = st.new_row(draft, now)
            if draft.decision.status == "pending_approval":
                payload = st.review_payload(row)
                review_id = create_review_item("memory_write", payload, now=now, conn=c)
                row["data"]["review_item_id"] = review_id
            ops.insert_memory_item(row, conn=c)
            return st.result(row, flags=list(draft.flags)), row

        return tx(conn) if conn is not None else core.run_write(tx, op="memory_propose")

    def _upsert(self, row: ops.MemoryItemRow, vector: np.ndarray | None) -> bool:
        """Step 13: write the vector row; False (logged) when embedding or LanceDB fails."""
        kind, layer, status = row["kind"], row["layer"], row["status"]
        try:
            if vector is None:
                vector = self._embedder.embed_item(kind, row["content"])  # type: ignore[arg-type]
            key, model = str(row["data"].get("content_hash")), self._embedder.model_name
            vrow = VectorRow(row["memory_id"], layer, kind, status, key, model, vector)  # type: ignore[arg-type]
            self._vectors.upsert([vrow])
        except ModelUnavailable:
            _log.warning("memory.embedding.failed", memory_id=row["memory_id"], op="upsert")
            return False
        return True

    def _set_pending(self, memory_id: str, *, value: bool) -> None:
        """Set `data.embedding_pending` and the matching flag in one run_write."""

        def tx(conn: Conn) -> None:
            rows = ops.get_memory_items([memory_id], conn=conn)
            if rows:
                data = dict(rows[0]["data"])
                flags = [f for f in st.stored_flags(data) if f != "embedding_pending"]
                data["flags"] = [*flags, "embedding_pending"] if value else list(flags)
                data["embedding_pending"] = value
                ops.update_memory_item(memory_id, data=data, conn=conn)

        core.run_write(tx, op="memory_embedding_flag")
