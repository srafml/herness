"""The context-pressure hook and the notes summarizer (impl 07 U07-76, U07-77; design 07 §5.4).

``ContextCompactor`` is spec 05's ``CompactorLike``: ``pressure`` counts tokens (blocking; the
loop calls it in a worker thread, DD28) and ``on_context_pressure`` returns a NEW message list
that keeps every ``query_id`` and cited number (TH07-15). Summarizer output is model-written:
it is validated (``[[?]]`` for invented numbers, unknown ids removed; TH07-16) and rendered only
through the escaped scratchpad. The run ledger is the only source of budget errors (R-25).
"""

import asyncio
import dataclasses
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol, cast

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import (
    CircuitOpen,
    EgressBlocked,
    ModelRefused,
    ModelUnavailable,
    OutputValidationError,
    RateLimited,
    StoreBusy,
)
from herness.core.logging import get_logger
from herness.core.resilience import TracerLike, complete_validated
from herness.core.resilience.metrics import record_counter, record_histogram
from herness.core.types import LoopState, Message, ToolContext
from herness.harness.llm.base import LLMClient
from herness.harness.llm.settings import ClientConfig
from herness.harness.memory._compactor_ledger import (
    LedgerPass,
    enforce_cap,
    invariants_hold,
    kept_refs,
    query_ids_in,
    task_message,
)
from herness.harness.memory._compactor_llm import Completer, chunks, compaction_prompt, request
from herness.harness.memory.compact_build import (
    Group,
    build_compacted,
    deterministic_notes,
    split_groups,
    validate_notes,
)
from herness.harness.memory.settings import CompactionConfig
from herness.harness.memory.tokens import TokenCounter, compute_thresholds
from herness.harness.memory.types import ContextStats
from herness.harness.memory.working import CompactionNotes, Scratchpad

__all__ = ["CompactionReport", "CompactorOps", "ContextCompactor", "summarize_notes"]

MAX_REPAIRS: Final = 1
COMPACTIONS_METRIC: Final = "herness_memory_compactions_total"
LATENCY_METRIC: Final = "herness_memory_compaction_latency_seconds"
_FALLBACK: Final = (
    ModelUnavailable,
    ModelRefused,
    OutputValidationError,
    RateLimited,
    CircuitOpen,
    EgressBlocked,
    TimeoutError,
    json.JSONDecodeError,
)
_log = get_logger("harness.memory")

type NotesSource = Literal["llm", "deterministic"]


class CompactorOps(Protocol):
    """Checkpoint access bound by U07-97 (U07-29 read; T08-16 ``save_checkpoint``, R-21)."""

    def get_task_scratchpad(self, task_id: str) -> str | None: ...
    def save_scratchpad(self, task_id: str, value: dict[str, JsonValue]) -> None: ...


@dataclass(frozen=True, slots=True)
class CompactionReport:
    """What the last successful ``on_context_pressure`` did (U07-76)."""

    before_tokens: int
    after_tokens: int
    n_messages_removed: int
    fresh_conversation: bool
    notes_source: NotesSource
    k_final: int
    ledger_compacted: bool


async def summarize_notes(  # noqa: PLR0913 - the U07-77 signature
    client: LLMClient | None,
    profile: ClientConfig,
    dropped: Sequence[Group],
    messages: Sequence[Message],
    scratchpad: Scratchpad,
    *,
    cfg: CompactionConfig,
    ctx: ToolContext,
    budget: int,
    allowed: Sequence[re.Pattern[str]],
    step: int,
) -> tuple[CompactionNotes, NotesSource]:
    """LLM notes over the dropped tool groups, validated; deterministic on any model problem.

    Calls go through ``complete_validated`` (one repair) over a private completer that applies
    the client timeout, charges ``ctx.ledger`` for every response and traces ``llm_call``; a
    budget error raised by that ledger propagates unchanged (R-25).
    """
    prior = scratchpad.notes
    tools = [group for group in dropped if not group.is_preamble]
    if client is None or not tools:
        return deterministic_notes(dropped, messages, prior), "deterministic"
    completer, notes = Completer(client, ctx, profile.timeout_s), prior
    tracer = cast("TracerLike", ctx.tracer)  # Tracer.emit's type is positional-only
    try:
        for chunk in enumerate(chunks(tools, messages, budget)):
            req = request(profile, cfg, ctx, (notes, scratchpad), chunk, step)
            resp = await complete_validated(completer, req, max_repairs=MAX_REPAIRS, tracer=tracer)
            raw = resp.parsed if resp.parsed is not None else json.loads(resp.text)
            fresh = validate_notes(raw, scratchpad, allowed) if isinstance(raw, dict) else None
            if fresh is None:
                msg = "compaction notes failed validation"
                raise OutputValidationError(msg)
            notes = fresh.model_copy(update={"steps": [] if prior is None else prior.steps})
    except _FALLBACK as exc:
        _log.warning(
            "memory.compaction.notes_fallback", task_id=ctx.task_id, reason=type(exc).__name__
        )
        return deterministic_notes(dropped, messages, prior), "deterministic"
    return deterministic_notes(dropped, messages, notes), "llm"


@dataclass(slots=True)
class _Call:
    """The working state of one ``on_context_pressure`` call."""

    msgs: list[Message]
    m0: Message
    keep: list[Group]
    drop: list[Group]
    ledger: LedgerPass
    fresh: bool
    covers: tuple[int, int]
    prior: CompactionNotes | None
    ledger_compacted: bool = False
    notes: CompactionNotes | None = None
    source: NotesSource = "deterministic"
    new: list[Message] = field(default_factory=list)


class ContextCompactor:
    """The ``on_context_pressure`` hook and ``pressure()`` of one agent task (U07-76)."""

    def __init__(  # noqa: PLR0913 - the U07-76 constructor
        self,
        profile: ClientConfig,
        *,
        ctx: ToolContext,
        cfg: CompactionConfig,
        counter: TokenCounter,
        client: LLMClient | None,
        allowed: Sequence[re.Pattern[str]],
        ops: CompactorOps,
    ) -> None:
        compaction_prompt()  # a missing prompt file is a ConfigError here (U07-99)
        self._limits = compute_thresholds(profile, cfg)
        self._profile, self._ctx, self._cfg, self._counter = profile, ctx, cfg, counter
        self._client, self._allowed, self._ops = client, list(allowed), ops
        self._restored = False
        self.scratchpad = Scratchpad()
        self.last_report: CompactionReport | None = None

    def pressure(self, state: LoopState) -> ContextStats:
        """Token pressure of ``state``; blocking for exact counts (run in a thread, DD28)."""
        tokens, exact = self._counter.count_state(state, self._limits.budget)
        return compute_thresholds(self._profile, self._cfg, tokens, exact)

    async def on_context_pressure(self, state: LoopState) -> list[Message]:
        """A NEW, smaller message list; ``state`` is never changed (U07-76 steps 1-13)."""
        started = clock.monotonic()
        await self._restore()
        msgs = list(state.messages)
        if len(msgs) < 2:  # noqa: PLR2004 - the task message alone
            return [message.model_copy(deep=True) for message in msgs]
        budget = self._limits.budget
        before, _ = await asyncio.to_thread(self._counter.count_state, state, budget)
        call = self._begin(msgs, state.query_ids)
        call.notes, call.source = await summarize_notes(
            self._client, self._profile, call.drop, msgs, self.scratchpad,
            cfg=self._cfg, ctx=self._ctx, budget=budget, allowed=self._allowed, step=state.step,
        )  # fmt: skip
        self.scratchpad.compactions += 1
        self._check(call)
        after = await self._shrink(call)
        await self._save()
        self._finish(call, before, after, clock.monotonic() - started)
        return call.new

    async def _restore(self) -> None:
        if self._restored:
            return
        self._restored = True
        if self.scratchpad == Scratchpad():
            text = await asyncio.to_thread(self._ops.get_task_scratchpad, self._ctx.task_id)
            self.scratchpad = Scratchpad.from_checkpoint(text)

    def _begin(self, msgs: list[Message], state_ids: Sequence[str]) -> _Call:
        """Steps 3-5: groups, keep and drop, and the deterministic ledger."""
        pad, fresh = self.scratchpad, self._profile.kind == "anthropic"
        keep_k = self._cfg.keep_last_tool_groups
        k = keep_k.claude if fresh else keep_k.local
        groups = split_groups(msgs[1:], pad.covers_steps[1])
        keep = [group for group in groups if not group.is_preamble][-k:]
        drop = [group for group in groups if group not in keep]
        ledger = LedgerPass(pad, msgs, self._allowed, self._cfg.ledger_sample_max_cells)
        m0 = task_message(msgs[0])
        for group in [*drop, *keep] if fresh else drop:  # Claude: kept groups become text
            ledger.group(group)
        kept = [msgs[i] for group in keep for i in group.indices]
        ledger.rescue((query_ids_in(msgs) | set(state_ids)) - query_ids_in([m0, *kept]))
        return _Call(msgs, m0, keep, drop, ledger, fresh, pad.covers_steps, pad.notes)

    def _build(self, call: _Call) -> list[Message]:
        """Step 8 (with the U07-67 size cap applied to what is rendered and saved)."""
        pad = self.scratchpad
        done = [g.step for g in (call.drop + call.keep if call.fresh else call.drop)]
        start = call.covers[0] if pad.compactions > 1 else min(done, default=call.covers[0])
        pad.covers_steps = (start, max(done, default=call.covers[1]))
        pad.notes = call.notes
        call.ledger_compacted |= enforce_cap(pad)
        text = pad.render(self._ctx.build_id)
        return build_compacted(call.m0, text, call.keep, call.msgs, fresh_conversation=call.fresh)

    def _check(self, call: _Call) -> None:
        """Step 9: invariants, once more with deterministic notes, else OutputValidationError."""
        call.new = self._build(call)
        refs = kept_refs(call.keep, call.msgs)
        if invariants_hold(call.msgs, call.new, self.scratchpad, refs):
            return
        call.notes = deterministic_notes(call.drop, call.msgs, call.prior)
        call.source, call.new = "deterministic", self._build(call)
        if not invariants_hold(call.msgs, call.new, self.scratchpad, refs):
            _log.error("memory.compaction.invariant_failed", task_id=self._ctx.task_id)
            msg = f"compaction invariant failed for task {self._ctx.task_id}"
            raise OutputValidationError(msg)

    async def _count(self, messages: list[Message]) -> int:
        tokens, _ = await asyncio.to_thread(
            self._counter.count_messages, messages, self._limits.budget
        )
        return tokens

    async def _shrink(self, call: _Call) -> int:
        """Steps 10-11: fewer kept groups, then a compacted ledger, else OutputValidationError."""
        tokens = await self._count(call.new)
        while tokens > self._limits.target and len(call.keep) > 1:
            group = call.keep.pop(0)
            call.drop.append(group)
            call.ledger.group(group)
            call.notes = deterministic_notes([group], call.msgs, call.notes)
            call.new = self._build(call)
            tokens = await self._count(call.new)
        if tokens > self._limits.hard:
            self.scratchpad.compact()
            call.ledger_compacted, call.new = True, self._build(call)
            tokens = await self._count(call.new)
        if tokens > self._limits.hard:
            _log.error("memory.compaction.over_hard", task_id=self._ctx.task_id)
            msg = f"compaction cannot reach hard limit for task {self._ctx.task_id}"
            raise OutputValidationError(msg)
        return tokens

    async def _save(self) -> None:
        """Step 12: checkpoint key ``scratchpad``; a busy store is logged, not raised."""
        value = self.scratchpad.to_checkpoint()
        try:
            await asyncio.to_thread(self._ops.save_scratchpad, self._ctx.task_id, value)
        except StoreBusy as exc:
            _log.warning(
                "memory.compaction.checkpoint_failed",
                task_id=self._ctx.task_id,
                error_type=type(exc).__name__,
            )

    def _finish(self, call: _Call, before: int, after: int, elapsed: float) -> None:
        """Step 13: report, log and metrics."""
        report = CompactionReport(
            before_tokens=before,
            after_tokens=after,
            n_messages_removed=max(0, len(call.msgs) - len(call.new)),
            fresh_conversation=call.fresh,
            notes_source=call.source,
            k_final=len(call.keep),
            ledger_compacted=call.ledger_compacted,
        )
        self.last_report = report
        _log.info(
            "memory.compaction.completed",
            task_id=self._ctx.task_id,
            compactions=self.scratchpad.compactions,
            **dataclasses.asdict(report),
        )
        labels = {"backend": self._profile.kind, "notes": call.source}
        record_counter(COMPACTIONS_METRIC, component="memory", labels=labels)
        record_histogram(LATENCY_METRIC, max(elapsed, 0.0), component="memory")
