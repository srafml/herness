"""OpenAI-compatible adapter for vLLM, Ollama and llama.cpp (impl 05 U05-24, U05-25).

Design 05 §5.1.1. The instance holds no SDK client: every call opens one inside ``async with``,
so the adapter works across separate ``asyncio.run`` calls. The API key lives only on the
instance as ``SecretStr`` and reaches the wire only in the SDK's ``Authorization`` header
(TH05-15). Off-network clients send only through the egress guard's HTTP client; without it the
call fails closed (TH05-13). Responses are bounded and checked before they reach the loop
(TH05-20).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast

import openai
from pydantic import SecretStr, ValidationError

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.egress import get_guard
from herness.core.errors import ConfigError, OutputValidationError
from herness.core.ids import canonical_json
from herness.core.logging import get_logger
from herness.core.registry import register
from herness.core.secrets import resolve
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
from herness.harness.llm.base import bound_response, egress_purpose_for
from herness.harness.llm.errors import translate_openai_error
from herness.harness.llm.pricing import cost_usd

if TYPE_CHECKING:
    import httpx2
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.completion_usage import CompletionUsage

    from herness.harness.llm.settings import ClientConfig

__all__ = ["OpenAICompatClient"]

_Server = Literal["vllm", "ollama", "llamacpp", "openai"]
_StopReason = Literal[
    "end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "content_filter", "other"
]
_STOP_REASONS: Final[dict[str, _StopReason]] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filter",
}
_NO_KEY: Final = "EMPTY"
_PAYLOAD_CLASS: Final = "aggregated_evidence"
_MAX_ID_CHARS: Final = 128

_log = get_logger("harness.llm")


class _GuardWithHttpClient(Protocol):
    """The part of the egress guard this adapter needs (T10-17 ``async_http_client``)."""

    def async_http_client(
        self, purpose: str, payload_class: str, *, run_id: str, task_id: str | None
    ) -> httpx2.AsyncClient: ...


def _joined_text(message: Message) -> list[str]:
    return [part.text for part in message.parts if isinstance(part, TextPart)]


def _tool_call_dict(call: ToolCall) -> dict[str, object]:
    arguments = call.raw_arguments or canonical_json(call.arguments)
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": arguments},
    }


def _message_dicts(message: Message) -> list[dict[str, object]]:
    """One ``Message`` as OpenAI chat messages; reasoning parts are never replayed."""
    if message.role == "user":
        return [{"role": "user", "content": "\n\n".join(_joined_text(message))}]
    if message.role == "tool":
        return [
            {"role": "tool", "tool_call_id": part.tool_call_id, "content": part.content}
            for part in message.parts
            if isinstance(part, ToolResultPart)
        ]
    texts = _joined_text(message)
    item: dict[str, object] = {
        "role": "assistant",
        "content": "\n\n".join(texts) if texts else None,
    }
    calls = [part.call for part in message.parts if isinstance(part, ToolCallPart)]
    if calls:
        item["tool_calls"] = [_tool_call_dict(call) for call in calls]
    return [item]


def _json_object(text: str) -> dict[str, Any] | None:
    """``text`` parsed as a JSON object, else ``None`` (bad JSON, too deep, or not an object)."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _tool_calls(message: ChatCompletionMessage) -> list[ToolCall]:
    out: list[ToolCall] = []
    for call in message.tool_calls or []:
        function = getattr(call, "function", None)
        if function is None:
            msg = f"tool call {call.id[:_MAX_ID_CHARS]} is not a function call"
            raise OutputValidationError(msg)
        raw = function.arguments
        arguments = {} if raw == "" else _json_object(raw)
        if arguments is None:
            msg = f"tool call {call.id[:_MAX_ID_CHARS]} arguments are not a JSON object"
            raise OutputValidationError(msg)
        try:
            out.append(
                ToolCall(id=call.id, name=function.name, arguments=arguments, raw_arguments=raw)
            )
        except ValidationError:
            msg = f"tool call {call.id[:_MAX_ID_CHARS]} has an invalid id or name"
            raise OutputValidationError(msg) from None
    return out


def _reasoning(message: ChatCompletionMessage) -> list[ReasoningPart]:
    """``message.reasoning``, else ``message.reasoning_content`` (server extras), as one part."""
    extra = message.model_extra or {}
    text = extra.get("reasoning") or extra.get("reasoning_content")
    if isinstance(text, str) and text:
        return [ReasoningPart(provider="vllm", text=text)]
    return []


def _usage(raw: CompletionUsage | None) -> Usage:
    if raw is None:
        return Usage()
    details = raw.completion_tokens_details
    reasoning = details.reasoning_tokens if details is not None else None
    return Usage(
        input_tokens=raw.prompt_tokens,
        output_tokens=raw.completion_tokens,
        reasoning_tokens=reasoning or 0,
    )


@register("llm_client", "openai_compat")
class OpenAICompatClient:
    """``LLMClient`` for OpenAI-compatible servers (vLLM, Ollama, llama.cpp, openai)."""

    name: str
    cfg: ClientConfig
    server: _Server

    def __init__(self, cfg: ClientConfig) -> None:
        if cfg.kind != "openai_compat":
            msg = f"client {cfg.name} is not an openai_compat client"
            raise ConfigError(msg, client=cfg.name)
        if cfg.off_network:
            config = get_config()
            if not config.security.egress.enabled:
                msg = (
                    f"client {cfg.name} is off-network but egress is disabled"
                    f" in profile {config.profile}"
                )
                raise ConfigError(msg, client=cfg.name)
        self.name = cfg.name
        self.cfg = cfg
        # The settings validator fills ``server`` for every openai_compat client (D05-08).
        self.server = cfg.server or "llamacpp"
        self._api_key = resolve(cfg.api_key) if cfg.api_key is not None else SecretStr(_NO_KEY)

    def _to_openai_messages(self, req: LLMRequest) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        if req.system:
            out.append({"role": "system", "content": "\n\n".join(b.text for b in req.system)})
        for message in req.messages:
            out.extend(_message_dicts(message))
        return out

    def _tool_params(self, req: LLMRequest) -> dict[str, object]:
        """``tools``, ``tool_choice`` and ``parallel_tool_calls``; all omitted without tools."""
        if not req.tools:
            return {}
        params: dict[str, object] = {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.input_schema,
                    },
                }
                for spec in req.tools
            ],
            "parallel_tool_calls": req.parallel_tool_calls,
        }
        if self.server != "ollama":
            params["tool_choice"] = req.tool_choice
        elif req.tool_choice == "auto":
            params["tool_choice"] = "auto"
        return params

    def _thinking_params(self, req: LLMRequest) -> dict[str, object]:
        thinking = req.thinking == "on"
        if thinking and req.response_schema is not None and self.cfg.reasoning_parser is None:
            msg = (
                f"client {self.name}: thinking with a response schema needs a reasoning parser"
                " (models.clients.<key>.reasoning_parser)"
            )
            raise ConfigError(msg, client=self.name)
        if self.server == "vllm":
            return {"extra_body": {"chat_template_kwargs": {"enable_thinking": thinking}}}
        if self.server == "ollama":
            return {"extra_body": {"think": thinking}}
        return {}

    def _build_params(self, req: LLMRequest) -> dict[str, object]:
        params: dict[str, object] = {
            "model": self.cfg.model,
            "messages": self._to_openai_messages(req),
            "max_tokens": req.max_output_tokens,
        }
        params.update(self._tool_params(req))
        if req.response_schema is not None:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": req.response_schema_name, "schema": req.response_schema},
            }
        params.update(self._thinking_params(req))
        if req.seed is not None:
            params["seed"] = req.seed
        if req.stop:
            params["stop"] = list(req.stop)
        if req.temperature is not None:
            params["temperature"] = req.temperature
        return params

    def _map_response(self, raw: ChatCompletion, req: LLMRequest, latency_ms: int) -> LLMResponse:
        if not raw.choices:
            msg = "response has no choices"
            raise OutputValidationError(msg, client=self.name)
        choice = raw.choices[0]
        message = choice.message
        text = bound_response(
            message.content or "", len(message.tool_calls or []), client=self.name
        )
        tool_calls = _tool_calls(message)
        if raw.model != self.cfg.model:
            _log.warning(
                "harness.llm.model_mismatch",
                client=self.name,
                expected=self.cfg.model,
                actual=raw.model[:_MAX_ID_CHARS],
            )
        # Servers may send null although the SDK types it as a literal.
        finish = cast("str | None", choice.finish_reason) or ""
        usage = _usage(raw.usage)
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            parsed=None if req.response_schema is None else _json_object(text),
            reasoning=_reasoning(message),
            stop_reason=_STOP_REASONS.get(finish, "other"),
            raw_stop_reason=finish,
            refusal_category=None,
            usage=usage,
            cost_usd=cost_usd(usage, self.cfg.price_per_mtok),
            client=self.name,
            model=raw.model,
            provider="openai_compat",
            latency_ms=latency_ms,
            request_id=raw.id,
        )

    def _guarded_http_client(self, req: LLMRequest) -> httpx2.AsyncClient:
        """The egress guard's HTTP client for an off-network call; fails closed without it."""
        guard = get_guard()
        if not hasattr(guard, "async_http_client"):
            msg = f"client {self.name} is off-network but the egress guard has no HTTP client"
            raise ConfigError(msg, client=self.name)
        meta = req.metadata
        return cast("_GuardWithHttpClient", guard).async_http_client(
            egress_purpose_for(meta.model_role),
            _PAYLOAD_CLASS,
            run_id=meta.run_id,
            task_id=meta.task_id,
        )

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        """One non-streaming call; no retries (spec 08 retries)."""
        if req.client != self.name:
            msg = f"request for client {req.client[:64]} sent to client {self.name}"
            raise ConfigError(msg, client=self.name)
        params = cast("dict[str, Any]", self._build_params(req))
        http_client = self._guarded_http_client(req) if self.cfg.off_network else None
        try:
            async with openai.AsyncOpenAI(
                base_url=self.cfg.base_url,
                api_key=self._api_key.get_secret_value(),
                timeout=req.timeout_s,
                max_retries=0,
                http_client=http_client,
            ) as client:
                t0 = clock.monotonic()
                raw = await client.chat.completions.create(**params)
                latency_ms = int((clock.monotonic() - t0) * 1000)
        except openai.OpenAIError as exc:
            raise translate_openai_error(exc) from exc
        return self._map_response(cast("ChatCompletion", raw), req, latency_ms)

    def complete(self, req: LLMRequest) -> LLMResponse:
        """Blocking ``acomplete``; refused inside a running event loop (design §3.2)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.acomplete(req))
        msg = "complete() called inside a running event loop; use acomplete()"
        raise RuntimeError(msg)
