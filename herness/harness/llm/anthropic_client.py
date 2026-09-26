"""Anthropic Messages API adapter (impl 05 U05-27, U05-28; design §5.1.2).

Every SDK client this module builds gets ``max_retries=0`` (spec 08 owns retries) and an
``http_client`` from the egress guard, never one of its own (R-06, TH05-13). Opaque thinking
blocks go back only to Anthropic (TH05-14). Errors and logs carry no key, prompt or body (TH05-15).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Coroutine, Sequence
from typing import TYPE_CHECKING, Any, Final, Literal, NoReturn, Protocol, cast

import anthropic
from pydantic import JsonValue, ValidationError

from herness.core import config as _config
from herness.core import egress as _egress
from herness.core import secrets as _secrets
from herness.core import time as clock
from herness.core.errors import ConfigError, EgressBlocked, OutputValidationError
from herness.core.logging import get_logger
from herness.core.registry import register
from herness.core.types import (
    LLMRequest,
    LLMResponse,
    Message,
    ReasoningPart,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
    Usage,
)
from herness.harness.llm import _anthropic_batch
from herness.harness.llm._anthropic_batch import BATCH_MAX_WAIT_S
from herness.harness.llm.base import Done, StreamEvent, TextDelta, ToolCallDelta, egress_purpose_for
from herness.harness.llm.errors import translate_anthropic_error
from herness.harness.llm.pricing import cost_usd

__all__ = ["BATCH_MAX_WAIT_S", "AnthropicClient"]

if TYPE_CHECKING:
    import httpx2
    from anthropic.types import ContentBlock

    from herness.harness.llm.settings import ClientConfig

# carry-over: use base.MAX_RESPONSE_TEXT_CHARS once T05-06 lands
_MAX_RESPONSE_TEXT_CHARS: Final = 1_000_000
_MAX_TEXT_PART_CHARS: Final = 200_000  # TextPart.text cap: longer text is truncated
_TRUNCATED_MARKER: Final = "[truncated]"
_MAX_TOOL_CALLS: Final = 64
_STREAM_ABOVE_TOKENS: Final = 16_000  # longer non-streamed calls use messages.stream
_MIN_BUDGET_TOKENS: Final = 1024
_EPHEMERAL: Final = {"type": "ephemeral"}
_STOP_REASONS: Final = frozenset({"end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal"})
_StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "other"]
_log = get_logger("harness.llm")


class _GuardedClientFactory(Protocol):
    """U10-53 ``EgressGuard.async_http_client``; not on the base yet (T10-17 carry-over)."""

    def async_http_client(
        self,
        purpose: str,
        payload_class: str,
        *,
        run_id: str | None,
        task_id: str | None,
        timeout: float,
    ) -> httpx2.AsyncClient: ...


def _user_block(part: TextPart | ToolResultPart) -> dict[str, object]:
    if isinstance(part, ToolResultPart):
        return part.model_dump(exclude={"tool_call_id"}) | {"tool_use_id": part.tool_call_id}
    return part.model_dump()


def _assistant_blocks(message: Message) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    for part in message.parts:
        if isinstance(part, ReasoningPart):
            if part.provider == "anthropic" and part.opaque is not None:
                blocks.append(dict(part.opaque))  # TH05-14: only back to its provider
        elif isinstance(part, TextPart):
            if part.text:
                blocks.append(part.model_dump())
        elif isinstance(part, ToolCallPart):
            call = part.call
            blocks.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            )
    return blocks


def _thinking(cfg: ClientConfig, req: LLMRequest) -> dict[str, object] | None:
    if cfg.thinking_mode == "adaptive_optional" and req.thinking != "auto":
        return {"type": "adaptive"} if req.thinking == "on" else {"type": "disabled"}
    if cfg.thinking_mode != "budget" or req.thinking != "on":
        return None  # adaptive_always: key omitted; budget with "off": omitted
    wanted = req.thinking_budget_tokens or max(_MIN_BUDGET_TOKENS, req.max_output_tokens // 4)
    budget = min(max(wanted, _MIN_BUDGET_TOKENS), req.max_output_tokens - 1)
    if budget < _MIN_BUDGET_TOKENS:
        msg = f"client {cfg.name}: max_output_tokens too small for a thinking budget"
        raise ConfigError(msg, client=cfg.name)
    return {"type": "enabled", "budget_tokens": budget}


def _cap_text(text: str) -> str:
    if len(text) > _MAX_RESPONSE_TEXT_CHARS:
        msg = "response text exceeds limit"
        raise OutputValidationError(msg, length=len(text))
    if len(text) <= _MAX_TEXT_PART_CHARS:
        return text
    _log.warning("harness.llm.response_truncated", length=len(text), kept=_MAX_TEXT_PART_CHARS)
    return text[: _MAX_TEXT_PART_CHARS - len(_TRUNCATED_MARKER)] + _TRUNCATED_MARKER


def _parse_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _tool_call(call_id: str, name: str, arguments: object) -> ToolCall:
    if not isinstance(arguments, dict):
        msg = f"tool call {call_id[:128]} input is not a JSON object"
        raise OutputValidationError(msg)
    try:
        return ToolCall(id=call_id, name=name, arguments=arguments)
    except ValidationError:
        msg = "tool call id or name is invalid"
        raise OutputValidationError(msg) from None


def _reasoning(block: ContentBlock) -> ReasoningPart:
    text = block.thinking if block.type == "thinking" else ""
    opaque = cast("dict[str, JsonValue]", block.to_dict())  # replayed unchanged (TH05-14)
    return ReasoningPart(provider="anthropic", text=text, opaque=opaque)


def _usage(raw: anthropic.types.Message) -> Usage:
    u = raw.usage
    return Usage(
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_read_tokens=u.cache_read_input_tokens or 0,
        cache_write_tokens=u.cache_creation_input_tokens or 0,
    )


@register("llm_client", "anthropic")
class AnthropicClient:
    """Anthropic adapter: ``LLMClient``, ``StreamCapable`` and ``BatchCapable`` (T05-09)."""

    def __init__(self, cfg: ClientConfig) -> None:
        if cfg.kind != "anthropic":
            msg = f"client {cfg.name} is not an anthropic client"
            raise ConfigError(msg, client=cfg.name)
        root = _config.get_config()
        if not root.security.egress.enabled:
            msg = (
                f"anthropic client {cfg.name} cannot be constructed in profile "
                f"{root.profile}: egress disabled"
            )
            raise ConfigError(msg, client=cfg.name)
        if cfg.api_key is None:
            msg = f"anthropic client {cfg.name} has no api_key reference"
            raise ConfigError(msg, client=cfg.name)
        self.name = cfg.name
        self.cfg = cfg
        self._api_key = _secrets.resolve(cfg.api_key)
        self._pending: dict[str, LLMRequest] = {}
        self._pending_lock = asyncio.Lock()

    def _to_anthropic_messages(self, messages: list[Message]) -> list[dict[str, object]]:
        """Map messages to Messages API turns; consecutive user turns merge, results first."""
        turns: list[tuple[str, list[dict[str, object]]]] = []
        for message in messages:
            if message.role == "assistant":
                turns.append(("assistant", _assistant_blocks(message)))
                continue
            blocks = [
                _user_block(p) for p in message.parts if isinstance(p, TextPart | ToolResultPart)
            ]
            if turns and turns[-1][0] == "user":  # stable sort: tool results first
                merged = turns[-1][1] + blocks
                turns[-1] = ("user", sorted(merged, key=lambda b: b["type"] != "tool_result"))
            else:
                turns.append(("user", blocks))
        return [{"role": role, "content": content} for role, content in turns]

    def _build_params(self, req: LLMRequest) -> dict[str, object]:
        """The Messages API keyword arguments of one request (U05-27)."""
        cfg = self.cfg
        params: dict[str, object] = {
            "model": cfg.model,
            "max_tokens": req.max_output_tokens,
            "messages": self._to_anthropic_messages(req.messages),
            "cache_control": dict(_EPHEMERAL),  # VI-9: automatic caching of the growing tail
        }
        if req.system:
            params["system"] = [
                {"type": "text", "text": b.text}
                | ({"cache_control": dict(_EPHEMERAL)} if b.cache else {})
                for b in req.system
            ]
        if req.tools:  # already sorted by name (LLMRequest validator)
            params["tools"] = [spec.model_dump() for spec in req.tools]
            params["tool_choice"] = {"type": req.tool_choice}  # auto or none; never forced
        thinking = _thinking(cfg, req)
        if thinking is not None:
            params["thinking"] = thinking
        output_config: dict[str, object] = {}
        if cfg.supports.effort and req.effort is not None:
            output_config["effort"] = req.effort
        if req.response_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": req.response_schema}
        if output_config:
            params["output_config"] = output_config
        if req.temperature is not None:  # SDK 1.8 has no temperature keyword: body field
            params["extra_body"] = {"temperature": req.temperature}
        if req.stop:
            params["stop_sequences"] = list(req.stop)
        return params

    def _map_message(
        self,
        raw: anthropic.types.Message,
        req: LLMRequest | None,
        latency_ms: int,
        *,
        batch: bool,
    ) -> LLMResponse:
        """Normalize one SDK message; limits per the adapter-boundary ruling (U05-27)."""
        texts = [b.text for b in raw.content if b.type == "text"]
        calls = [_tool_call(b.id, b.name, b.input) for b in raw.content if b.type == "tool_use"]
        reasoning = [
            _reasoning(b) for b in raw.content if b.type in {"thinking", "redacted_thinking"}
        ]
        if len(calls) > _MAX_TOOL_CALLS:
            msg = "response has too many tool calls"
            raise OutputValidationError(msg, count=len(calls))
        text = _cap_text("".join(texts))
        parsed = None
        if req is not None and req.response_schema is not None and texts:
            parsed = _parse_object(texts[0])
        raw_stop = raw.stop_reason or ""
        stop: _StopReason = cast("_StopReason", raw_stop) if raw_stop in _STOP_REASONS else "other"
        usage = _usage(raw)
        return LLMResponse(
            text=text,
            tool_calls=calls,
            parsed=parsed,
            reasoning=reasoning,
            stop_reason=stop,
            raw_stop_reason=raw_stop,
            refusal_category=raw.stop_details.category if raw.stop_details else None,
            usage=usage,
            cost_usd=cost_usd(usage, self.cfg.price_per_mtok, batch=batch),
            client=self.name,
            model=raw.model,
            provider="anthropic",
            latency_ms=latency_ms,
            request_id=getattr(raw, "_request_id", None),
            batch=batch,
        )

    def _sdk(self, req: LLMRequest | None) -> anthropic.AsyncAnthropic:
        """The guarded SDK client for ``req``, or the batch-admin client when ``None`` (U05-29).

        Fails closed without a guarded client (R-06, TH05-13); a batch is not scoped to one task.
        """
        if req is None:
            purpose, run_id, task_id, timeout = "reasoning", None, None, self.cfg.timeout_s
        else:
            meta = req.metadata
            purpose = egress_purpose_for(meta.model_role)
            run_id, task_id, timeout = meta.run_id, meta.task_id, req.timeout_s
        guard = _egress.get_guard()
        if not hasattr(guard, "async_http_client"):
            msg = f"anthropic client {self.name}: egress guard has no guarded http client"
            raise EgressBlocked(msg, reason="guard_client_unavailable")
        http_client: httpx2.AsyncClient = cast("_GuardedClientFactory", guard).async_http_client(
            purpose, "aggregated_evidence", run_id=run_id, task_id=task_id, timeout=timeout
        )
        return anthropic.AsyncAnthropic(
            api_key=self._api_key.get_secret_value(),
            max_retries=0,
            timeout=timeout,
            http_client=http_client,
        )

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        """One Messages API call; long outputs go through ``messages.stream`` (U05-28)."""
        params: dict[str, Any] = self._build_params(req)
        start = clock.monotonic()
        try:
            async with self._sdk(req) as client:
                if req.max_output_tokens > _STREAM_ABOVE_TOKENS:
                    async with client.messages.stream(**params) as stream:
                        raw: anthropic.types.Message = await stream.get_final_message()
                else:
                    raw = await client.messages.create(**params)
        except (anthropic.AnthropicError, EgressBlocked) as exc:
            self._fail(exc, req.metadata.task_id)
        return self._map_message(raw, req, _elapsed_ms(start), batch=False)

    def _fail(self, exc: anthropic.AnthropicError | EgressBlocked, task_id: str | None) -> NoReturn:
        """Translate per U05-30 and log the §8 adapter events (no body, prompt or key)."""
        err = exc if isinstance(exc, EgressBlocked) else translate_anthropic_error(exc)
        if isinstance(err, EgressBlocked):
            _log.warning("harness.llm.egress_blocked", client=self.name, task_id=task_id)
            raise err from None  # the guard's own exception; chaining ``exc`` would form a cycle
        if isinstance(err, ConfigError) and isinstance(exc, anthropic.APIStatusError):
            _log.error("harness.llm.bad_request", client=self.name, status=exc.status_code)
        raise err from exc

    def complete(self, req: LLMRequest) -> LLMResponse:
        """Blocking wrapper of ``acomplete``; refused inside a running event loop (U05-25)."""
        return _run_sync(self.acomplete(req))

    async def astream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]:
        """Stream text and tool-argument deltas, then one ``Done`` (U05-28)."""
        params: dict[str, Any] = self._build_params(req)
        start = clock.monotonic()
        try:
            async with self._sdk(req) as client, client.messages.stream(**params) as stream:
                tool: tuple[str, str] | None = None
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        tool = (block.id, block.name) if block.type == "tool_use" else None
                    elif event.type == "text" and event.text:
                        yield TextDelta(event.text)
                    elif event.type == "input_json" and tool is not None and event.partial_json:
                        yield ToolCallDelta(tool[0], tool[1], event.partial_json)
                raw = await stream.get_final_message()
        except (anthropic.AnthropicError, EgressBlocked) as exc:
            self._fail(exc, req.metadata.task_id)
        yield Done(self._map_message(raw, req, _elapsed_ms(start), batch=False))

    async def submit_batch(self, reqs: Sequence[LLMRequest]) -> str:
        """Create a Message Batch; record ``request_key -> LLMRequest`` pending (U05-29)."""
        sdk_requests, pending = _anthropic_batch.prepare_submit(self.cfg, reqs, self._build_params)
        try:
            async with self._sdk(None) as client:
                batch = await client.messages.batches.create(requests=cast("Any", sdk_requests))
        except (anthropic.AnthropicError, EgressBlocked) as exc:
            self._fail(exc, None)
        async with self._pending_lock:
            self._pending.update(pending)
        return batch.id

    async def collect_batch(self, batch_id: str, poll_s: float = 30.0) -> dict[str, LLMResponse]:
        """Poll until ended; map succeeded results, log the rest, drop collected keys (U05-29)."""
        _anthropic_batch.validate_collect(self.cfg, batch_id, poll_s)
        try:
            async with self._sdk(None) as client:
                await _anthropic_batch.poll_until_ended(
                    batch_id,
                    poll_s,
                    retrieve=client.messages.batches.retrieve,
                    sleep=asyncio.sleep,
                    monotonic=clock.monotonic,
                )
                async with self._pending_lock:
                    snapshot = dict(self._pending)
                responses, seen = await _anthropic_batch.collect_results(
                    batch_id,
                    await client.messages.batches.results(batch_id),
                    snapshot,
                    lambda msg, req: self._map_message(msg, req, 0, batch=True),
                )
        except (anthropic.AnthropicError, EgressBlocked) as exc:
            self._fail(exc, None)
        async with self._pending_lock:
            for key in seen:
                self._pending.pop(key, None)
        return responses


def _elapsed_ms(start: float) -> int:
    return int((clock.monotonic() - start) * 1000)


def _run_sync(coro: Coroutine[Any, Any, LLMResponse]) -> LLMResponse:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    msg = "complete() called inside a running event loop; use acomplete()"
    raise RuntimeError(msg)
