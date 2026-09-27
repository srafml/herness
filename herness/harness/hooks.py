"""Gated model calls, the loop hooks and the truncation fallback (U05-61, U05-62, U05-76).

Design 05 §3.3, §5.2.3. `HarnessHooks` (R-02) composes the spec 08 building blocks, looked up
on their packages at call time. A gate is held for one model call only, never across tool
execution (TH05-08). `truncate_context` is the R-25 fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast

from pydantic import BaseModel, ValidationError

from herness.core import resilience
from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError, ModelRefused, OutputValidationError
from herness.core.jobs import tasks as jobs_tasks
from herness.core.logging import get_logger
from herness.core.types import LLMRequest, LLMResponse, LoopSignal, LoopState, Message, TextPart
from herness.harness.llm.base import Done, LLMClient, StreamCapable, TextDelta
from herness.harness.llm.tokens import estimate_tokens
from herness.harness.tools import wrap_untrusted

if TYPE_CHECKING:
    from herness.core.resilience import ModelChain, TracerLike
    from herness.harness.llm.registry import LLMRegistry
    from herness.harness.tracing import Tracer

__all__ = [
    "CallGateLike",
    "CompactorLike",
    "GatedClient",
    "HarnessHooks",
    "StreamState",
    "truncate_context",
]

type TextSink = Callable[[str | None], Awaitable[None]]

_log = get_logger("harness.loop")

_DASH = chr(0x2013)  # en dash between the first and last removed step
_TRUNCATION_HEAD = "Earlier steps were removed to fit the context."


class CallGateLike(Protocol):
    """The spec 06 call gate as seen by `GatedClient` (an async context manager)."""

    async def __aenter__(self) -> object: ...
    async def __aexit__(self, *exc: object) -> object: ...


class _PressureLike(Protocol):  # context pressure: estimated tokens and the soft limit
    @property
    def tokens(self) -> int: ...
    @property
    def soft(self) -> int: ...


class CompactorLike(Protocol):
    """Structural view of the spec 07 `ContextCompactor` (T07-14)."""

    def pressure(self, state: LoopState) -> _PressureLike: ...
    async def on_context_pressure(self, state: LoopState) -> list[Message]: ...


@dataclass(slots=True)
class StreamState:
    """Shared by every `GatedClient` of one `HarnessHooks.call`: text was already shown."""

    emitted: bool = False


_NO_GATE: CallGateLike = contextlib.nullcontext()  # a client without a gate


class GatedClient:
    """Hold the call gate for exactly one model call; stream text; raise on refusal (U05-61)."""

    def __init__(
        self,
        inner: LLMClient,
        gate: CallGateLike | None,
        *,
        on_text_delta: TextSink | None = None,
        stream_state: StreamState | None = None,
    ) -> None:
        self.name: str = inner.name
        self.last_gate_wait_ms: int = 0
        self._inner = inner
        self._gate: CallGateLike = _NO_GATE if gate is None else gate
        self._on_text_delta = on_text_delta
        self._stream_state = StreamState() if stream_state is None else stream_state

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        """One call inside the gate; the gate is released before the refusal check."""
        t0 = clock.monotonic()
        async with self._gate:
            self.last_gate_wait_ms = max(0, round((clock.monotonic() - t0) * 1000))
            resp = await self._call(req)
        if resp.stop_reason == "refusal":
            msg = "model refused the request"
            raise ModelRefused(msg, category=resp.refusal_category, client=self.name)
        return resp

    def complete(self, req: LLMRequest) -> LLMResponse:
        """Blocking `acomplete`; refused inside a running event loop (U05-25 rule)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.acomplete(req))
        msg = "complete() called inside a running event loop; use acomplete()"
        raise RuntimeError(msg)

    async def _call(self, req: LLMRequest) -> LLMResponse:
        sink, inner = self._on_text_delta, self._inner
        if sink is None or req.response_schema is not None or not isinstance(inner, StreamCapable):
            return await inner.acomplete(req)
        return await self._stream(inner, req, sink)

    async def _stream(self, inner: StreamCapable, req: LLMRequest, sink: TextSink) -> LLMResponse:
        state = self._stream_state
        if state.emitted:
            await sink(None)  # an earlier attempt already showed text: reset the UI
            state.emitted = False
        resp: LLMResponse | None = None
        events = inner.astream(req)
        try:
            async for event in events:
                if isinstance(event, TextDelta):
                    await sink(event.text)
                    state.emitted = True
                elif isinstance(event, Done):
                    resp = event.response
        finally:
            if isinstance(events, AsyncGenerator):  # close a half-read stream inside the gate
                await events.aclose()
        if resp is None:
            msg = "stream ended without a final response"
            raise OutputValidationError(msg, client=self.name)
        return resp


class HarnessHooks:
    """The only `LoopHooks` implementation; composes the spec 08 building blocks (U05-62)."""

    def __init__(  # noqa: PLR0913 - signature fixed by U05-62
        self,
        *,
        registry: LLMRegistry,
        gates: Mapping[str, CallGateLike],
        chain: ModelChain | None,
        compactor: CompactorLike | None,
        task_id: str | None,
        phase: str | None,
        stop: Callable[[], bool] | None,
        on_text_delta: TextSink | None,
        tracer: Tracer,
    ) -> None:
        if (task_id is None) != (phase is None):
            msg = "task_id and phase must be both set or both None"
            raise ConfigError(msg)
        self._registry = registry
        self._gates = gates
        self._chain = chain
        self._compactor = compactor
        self._task_id = task_id
        self._phase = phase
        self._stop = stop
        self.on_text_delta = on_text_delta
        self.tracer = tracer
        # Tracer.emit's `type` is positional-only, TracerLike's is not; spec 08 passes it so.
        self._tracer_like = cast("TracerLike", tracer)
        self._last_save: float | None = None

    async def before_call(self, state: LoopState, req: LLMRequest) -> LLMRequest:
        """No request rewriting (design §3.3)."""
        return req

    async def call(
        self,
        client: LLMClient,
        req: LLMRequest,
        state: LoopState,
        schema: type[BaseModel] | None,
    ) -> tuple[LLMResponse, BaseModel | None]:
        """One model call through the chain (or the given client), each attempt gated."""
        ss, sink = StreamState(), self.on_text_delta if schema is None else None
        made: list[GatedClient] = []

        def wrap(inner: LLMClient, key: str) -> GatedClient:
            gated = GatedClient(inner, self._gates.get(key), on_text_delta=sink, stream_state=ss)
            made.append(gated)
            return gated

        def make(key: str) -> GatedClient:
            return wrap(self._registry.client(key), key)

        parsed: BaseModel | None
        if self._chain is not None:
            resp, parsed = await self._chain.acomplete(
                req, schema=schema, client_for=make, tracer=self._tracer_like
            )
        else:
            gated = wrap(client, client.name)
            if schema is None:
                resp, parsed = await gated.acomplete(req), None
            else:
                resp = await resilience.complete_validated(gated, req, tracer=self._tracer_like)
                parsed = _parse(schema, resp)
        state.note_gate_wait(sum(g.last_gate_wait_ms for g in made))
        return resp, parsed

    async def needs_compaction(self, state: LoopState) -> bool:
        """Estimated input tokens at or above the soft limit (design §3.3)."""
        if self._compactor is not None:
            pressure = self._compactor.pressure(state)
            return pressure.tokens >= pressure.soft
        return state.est_input_tokens() >= state.limits().soft_tokens

    async def on_context_pressure(self, state: LoopState) -> list[Message]:
        """The compactor's new list; truncation when there is none or it failed (R-25)."""
        if self._compactor is None:
            return truncate_context(state)
        try:
            return await self._compactor.on_context_pressure(state)
        except OutputValidationError:
            _log.warning(
                "harness.loop.truncation_fallback",
                task_id=self._task_id,
                phase=self._phase,
                reason="compactor_failed",
            )
            return truncate_context(state)

    async def on_loop_signal(
        self, state: LoopState, signal: LoopSignal
    ) -> Literal["nudge", "stop"]:
        """Delegate to the spec 08 loop-signal policy."""
        return resilience.loop_signal_policy(state, signal, tracer=self._tracer_like)

    async def after_step(self, state: LoopState) -> None:
        """Checkpoint the `loop` key when due (R-21); raise `CancelledError` on a stop."""
        stop_now = self._stop is not None and self._stop()
        if self._task_id is not None and self._save_due(state, stop_now=stop_now):
            await asyncio.to_thread(
                jobs_tasks.save_checkpoint, self._task_id, "loop", state.to_checkpoint()
            )
            self._last_save = clock.monotonic()
        if stop_now:
            raise asyncio.CancelledError

    def _save_due(self, state: LoopState, *, stop_now: bool) -> bool:
        if state._stopping or stop_now or self._last_save is None:
            return True
        interval = get_config().resilience.resilience.loop.checkpoint_min_interval_s
        return clock.monotonic() - self._last_save >= interval


def _parse(schema: type[BaseModel], resp: LLMResponse) -> BaseModel:
    if resp.parsed is None:
        msg = "response has no parsed output"
        raise OutputValidationError(msg, client=resp.client)
    try:
        return schema.model_validate(resp.parsed)
    except ValidationError:
        # The validation error echoes model output; keep it out of the raised error.
        msg = "parsed output does not match the schema"
        raise OutputValidationError(msg, client=resp.client) from None


def truncate_context(state: LoopState, /) -> list[Message]:
    """Task message, one untrusted `compaction_summary` note, then the newest whole tool
    groups that fit under the soft limit (U05-76, R-25). Never mutates `state.messages`."""
    first = state.messages[0]
    groups = _tool_groups(state.messages[1:])
    steps = _group_steps(groups, state.step)
    limits = state.limits()
    start = 0
    while (
        len(groups) - start > 1
        and limits.fixed_tokens + estimate_tokens(_assemble(first, state, groups, steps, start))
        >= limits.soft_tokens
    ):
        start += 1
    return _assemble(first, state, groups, steps, start)


def _tool_groups(messages: Sequence[Message]) -> list[list[Message]]:
    """Assistant message plus what follows it; old summaries dropped (the note has the ids)."""
    groups: list[list[Message]] = []
    for message in messages:
        if message.kind == "compaction_summary":
            continue
        if message.role == "assistant" or not groups:
            groups.append([message])
        else:
            groups[-1].append(message)
    return groups


def _group_steps(groups: Sequence[Sequence[Message]], step: int) -> list[int | None]:
    """The step of each assistant group (the newest is `step`); None for a leading group."""
    number = max(0, step - sum(1 for group in groups if group[0].role == "assistant"))
    steps: list[int | None] = []
    for group in groups:
        is_assistant = group[0].role == "assistant"
        number += is_assistant
        steps.append(number if is_assistant else None)
    return steps


def _assemble(
    first: Message,
    state: LoopState,
    groups: Sequence[Sequence[Message]],
    steps: Sequence[int | None],
    start: int,
) -> list[Message]:
    dropped = [s for s in steps[:start] if s is not None]
    span = f"{dropped[0]}{_DASH}{dropped[-1]}" if dropped else "none"
    body = "\n".join(
        (
            _TRUNCATION_HEAD,
            f"Steps removed: {span}",
            f"query_ids gathered: {', '.join(state.query_ids)}",
            f"findings posted: {', '.join(state.finding_ids)}",
        )
    )
    note = Message(
        role="user",
        parts=[TextPart(text=wrap_untrusted(body, source="truncation"))],
        kind="compaction_summary",
    )
    return [first, note, *(message for group in groups[start:] for message in group)]
