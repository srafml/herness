"""Wire mapping and response bounds of the OpenAI-compatible adapter (impl 05 U05-24).

Private sibling of ``openai_compat`` (module-map row): request mapping (shared with ``tokens``)
and response mapping with the adapter bounds (TH05-20), enforced before data is decoded or kept.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from pydantic import ValidationError

from herness.core.errors import OutputValidationError
from herness.core.ids import canonical_json
from herness.core.logging import get_logger
from herness.core.types import (
    LLMRequest,
    LLMResponse,
    Message,
    ReasoningPart,
    SystemBlock,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    Usage,
)
from herness.harness.llm.base import MAX_RESPONSE_TEXT_CHARS, bound_response
from herness.harness.llm.pricing import cost_usd

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.completion_usage import CompletionUsage

    from herness.harness.llm.settings import ClientConfig

_StopReason = Literal[
    "end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "content_filter", "other"
]
_STOP_REASONS: Final[dict[str, _StopReason]] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filter",
}
_MAX_ID_CHARS: Final = 128

_log = get_logger("harness.llm")


def _joined_text(message: Message) -> list[str]:
    return [part.text for part in message.parts if isinstance(part, TextPart)]


def _tool_call_dict(call: ToolCall) -> dict[str, object]:
    arguments = call.raw_arguments or canonical_json(call.arguments)
    function = {"name": call.name, "arguments": arguments}
    return {"id": call.id, "type": "function", "function": function}


def message_dicts(message: Message) -> list[dict[str, object]]:
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


def chat_messages(
    system: Sequence[SystemBlock], messages: Sequence[Message]
) -> list[dict[str, Any]]:
    """System blocks joined into one first message, then each message (U05-24; ``tokens``)."""
    head = [{"role": "system", "content": "\n\n".join(b.text for b in system)}] if system else []
    return head + [item for message in messages for item in message_dicts(message)]


def tool_dicts(tools: Sequence[ToolSpec]) -> list[dict[str, object]]:
    """Tool specs in the OpenAI ``function`` shape (U05-24; also the ``/tokenize`` tools)."""
    return [{"type": "function", "function": _function(t)} for t in tools]


def _function(t: ToolSpec) -> dict[str, object]:
    return {"name": t.name, "description": t.description, "parameters": t.input_schema}


def _json_object(text: str) -> dict[str, Any] | None:
    """``text`` parsed as a JSON object, else ``None`` (bad JSON, too deep, or not an object)."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _fail(call_id: str, what: str) -> OutputValidationError:
    return OutputValidationError(f"tool call {call_id[:_MAX_ID_CHARS]} {what}")


def _tool_calls(message: ChatCompletionMessage) -> list[ToolCall]:
    out: list[ToolCall] = []
    for call in message.tool_calls or []:
        function = getattr(call, "function", None)
        if function is None:
            raise _fail(call.id, "is not a function call")
        raw = function.arguments
        if len(raw) > MAX_RESPONSE_TEXT_CHARS:  # bounded before any decoding
            raise _fail(call.id, "arguments exceed limit")
        arguments = {} if raw == "" else _json_object(raw)
        if arguments is None:
            raise _fail(call.id, "arguments are not a JSON object")
        try:
            out.append(
                ToolCall(id=call.id, name=function.name, arguments=arguments, raw_arguments=raw)
            )
        except ValidationError:
            raise _fail(call.id, "has an invalid id or name") from None
    return out


def _reasoning(message: ChatCompletionMessage, client: str) -> list[ReasoningPart]:
    """``message.reasoning``, else ``message.reasoning_content``, bounded like the text."""
    extra = message.model_extra or {}
    text = extra.get("reasoning") or extra.get("reasoning_content")
    if isinstance(text, str) and text:
        bounded = bound_response(text, 0, client=client, field="reasoning")
        return [ReasoningPart(provider="vllm", text=bounded)]
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


def map_response(
    raw: ChatCompletion, req: LLMRequest, cfg: ClientConfig, latency_ms: int
) -> LLMResponse:
    """Map one completion (U05-24 ``_map_response``); malformed shapes raise builtin errors."""
    if not raw.choices:
        msg = "response has no choices"
        raise OutputValidationError(msg, client=cfg.name)
    choice = raw.choices[0]
    message = choice.message
    text = bound_response(message.content or "", len(message.tool_calls or []), client=cfg.name)
    tool_calls = _tool_calls(message)
    model = raw.model
    if not isinstance(model, str):
        raise TypeError  # caught by the adapter as a malformed response
    if model != cfg.model:
        _log.warning(
            "harness.llm.model_mismatch",
            client=cfg.name,
            expected=cfg.model,
            actual=model[:_MAX_ID_CHARS],
        )
    # Servers may send null although the SDK types it as a literal.
    finish = cast("str | None", choice.finish_reason) or ""
    usage = _usage(raw.usage)
    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        parsed=None if req.response_schema is None else _json_object(text),
        reasoning=_reasoning(message, cfg.name),
        stop_reason=_STOP_REASONS.get(finish, "other"),
        raw_stop_reason=finish,
        refusal_category=None,
        usage=usage,
        cost_usd=cost_usd(usage, cfg.price_per_mtok),
        client=cfg.name,
        model=model,
        provider="openai_compat",
        latency_ms=latency_ms,
        request_id=raw.id,
    )
