"""Private sibling of ``fake_llm`` (T11-23 spec note): scripted answers on the wire.

Shared by ``respx_router`` and ``FakeLLMServer`` (U11-43, U11-45): the call is keyed by the
DD11-02 headers (absent → ``*``), tool results are rebuilt from the request body for
``render_turn``, and the turn, fault or mismatch becomes OpenAI chat-completion JSON,
Anthropic message JSON or OpenAI SSE chunks. Imported only by ``tests.support.fake_llm``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from tests.support.stub_http import StubRequest, StubResponse

from herness.core.types import Message, TextPart, ToolResultPart
from herness.eval.scripted import FaultKind, ScriptBook, ScriptMismatch, ScriptTurn
from herness.eval.scripted_render import RenderedTurn, render_turn
from herness.harness.llm.tokens import estimate_tokens

__all__ = ["Served", "Wire", "answer", "error_response", "json_body", "stream_chunks"]

type Wire = Literal["openai", "anthropic"]

_CHUNK: Final = 16
_MAX_RESULT_CHARS: Final = 12_000  # ToolResultPart.content cap
_MALFORMED_ARGS: Final = '{"a": '
_MALFORMED_TEXT: Final = '{"truncated": '
_TEXT_FINALS: Final = frozenset({"text", "output"})
_HEADERS: Final = ("x-herness-role", "x-herness-model-role", "x-herness-dedup-key")


@dataclass(frozen=True, slots=True)
class Served:
    """One call's outcome: a rendered turn, a fault, or a mismatch (``call_index`` -1)."""

    call_index: int
    prompt_hash: str
    rendered: RenderedTurn | None = None
    fault: FaultKind | None = None
    tool_name: str | None = None  # the turn's first tool, for malformed_json


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text", "")) for b in content if isinstance(b, Mapping))
    return ""


def _result(call_id: object, content: object) -> Message:
    text = _text_of(content)[:_MAX_RESULT_CHARS]
    part = ToolResultPart(tool_call_id=str(call_id or "call"), content=text)
    return Message(role="tool", parts=[part])


def _tool_messages(body: Mapping[str, Any], wire: Wire) -> list[Message]:
    """Tool results of the request body, enough for ``render_turn``."""
    out: list[Message] = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, Mapping):
            continue
        if wire == "openai" and msg.get("role") == "tool":
            out.append(_result(msg.get("tool_call_id"), msg.get("content")))
        elif wire == "anthropic" and isinstance(msg.get("content"), list):
            out.extend(
                _result(b.get("tool_use_id"), b.get("content"))
                for b in msg["content"]
                if isinstance(b, Mapping) and b.get("type") == "tool_result"
            )
    return out


def _prompt_hash(body: Mapping[str, Any]) -> str:
    payload = {"system": body.get("system"), "messages": body.get("messages")}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _tool_name(turn: ScriptTurn | None) -> str | None:
    if turn is None:
        return None
    if turn.tool_calls is not None:
        return turn.tool_calls[0].name
    key = next(iter(turn.final or {}), "text")
    return None if key in _TEXT_FINALS else key


def _serve(
    book: ScriptBook, headers: Mapping[str, str], body: Mapping[str, Any], wire: Wire
) -> Served:
    """Match the call by its DD11-02 headers (absent → ``*``) and render its turn."""
    role, model_role, key = (headers.get(name) or "*" for name in _HEADERS)
    prompt_hash = _prompt_hash(body)
    try:
        action = book.next_action(role, model_role, key, prompt_hash=prompt_hash)
        turns = action.script.turns
        turn = turns[action.turn_index] if action.turn_index < len(turns) else None
        if action.fault is not None:
            kind = action.fault.kind
            return Served(action.call_index, prompt_hash, None, kind, _tool_name(turn))
        assert turn is not None  # next_action raises ScriptMismatch when turns run out
        rendered = render_turn(turn, _tool_messages(body, wire), call_index=action.call_index)
    except ScriptMismatch:
        return Served(-1, prompt_hash)
    return Served(action.call_index, prompt_hash, rendered)


def _broken_tool(served: Served) -> str | None:
    """The tool whose arguments a ``malformed_json`` fault breaks (None: break the text)."""
    return served.tool_name if served.fault == "malformed_json" else None


def _malformed(served: Served) -> RenderedTurn:
    """The ``malformed_json`` stand-in turn; broken tool arguments are added per wire."""
    if served.tool_name is None:
        return RenderedTurn([], _MALFORMED_TEXT, None, "end_turn")
    return RenderedTurn([], "", None, "tool_use")


def _out_tokens(text: str) -> int:
    return estimate_tokens([Message(role="assistant", parts=[TextPart(text=text)])]) if text else 0


def error_response(status: int, message: str, wire: Wire, **headers: str) -> StubResponse:
    """An error body in the wire's own shape."""
    if wire == "anthropic":
        kind = "rate_limit_error" if status == 429 else "api_error"
        payload: dict[str, Any] = {"type": "error", "error": {"type": kind, "message": message}}
    else:
        payload = {"error": {"message": message, "type": "server_error"}}
    return StubResponse.json(status, payload, **headers)


def _openai_body(served: Served, turn: RenderedTurn, model: str, in_tokens: int) -> dict[str, Any]:
    calls = [
        {
            "id": c.id,
            "type": "function",
            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
        }
        for c in turn.tool_calls
    ]
    if (broken := _broken_tool(served)) is not None:
        fn = {"name": broken, "arguments": _MALFORMED_ARGS}
        calls = [{"id": f"call_{served.call_index}_0", "type": "function", "function": fn}]
    message: dict[str, Any] = {"role": "assistant", "content": turn.text or None}
    if calls:
        message["tool_calls"] = calls
    out_tokens = _out_tokens(turn.text)
    finish = "tool_calls" if calls else "stop"
    return {
        "id": f"chatcmpl-scripted-{served.call_index}",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": in_tokens,
            "completion_tokens": out_tokens,
            "total_tokens": in_tokens + out_tokens,
        },
    }


def _anthropic_body(
    served: Served, turn: RenderedTurn, model: str, in_tokens: int
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": turn.text}] if turn.text else []
    blocks += [
        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
        for c in turn.tool_calls
    ]
    if (broken := _broken_tool(served)) is not None:  # input is JSON text, cut short
        tool_id = f"call_{served.call_index}_0"
        blocks = [{"type": "tool_use", "id": tool_id, "name": broken, "input": _MALFORMED_ARGS}]
    tool = any(b["type"] == "tool_use" for b in blocks)
    return {
        "id": f"msg_scripted_{served.call_index}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks or [{"type": "text", "text": ""}],
        "stop_reason": "tool_use" if tool else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": in_tokens, "output_tokens": _out_tokens(turn.text)},
    }


def json_body(raw: bytes) -> dict[str, Any]:
    """The request body as a JSON object (``{}`` when it is not one)."""
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def answer(book: ScriptBook, request: StubRequest, wire: Wire) -> tuple[Served, StubResponse]:
    """The full HTTP answer of one chat call (faults ``hang``/``disconnect`` left to callers)."""
    body = json_body(request.body)
    served = _serve(book, request.headers, body, wire)
    if served.call_index < 0:
        err = {"error": {"message": "no script", "prompt_hash": served.prompt_hash}}
        return served, StubResponse.json(500, err)
    if served.fault == "http_500":
        return served, error_response(500, "scripted fault http_500", wire)
    if served.fault == "http_429":
        return served, error_response(429, "scripted fault http_429", wire, **{"Retry-After": "1"})
    turn = _malformed(served) if served.fault == "malformed_json" else served.rendered
    if turn is None:  # hang / disconnect: the transport decides
        return served, StubResponse(500)
    model = str(body.get("model") or "scripted")
    in_tokens = max(1, len(request.body) // 4)
    build = _anthropic_body if wire == "anthropic" else _openai_body
    return served, StubResponse.json(200, build(served, turn, model, in_tokens))


def _sse(payload: object) -> bytes:
    return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"


def stream_chunks(completion: Mapping[str, Any]) -> Iterator[bytes]:
    """SSE: 16-char content deltas, one delta per tool call, finish, usage, ``[DONE]``."""
    head = {k: completion[k] for k in ("id", "created", "model")}
    head["object"] = "chat.completion.chunk"
    choice = completion["choices"][0]
    text = choice["message"]["content"] or ""
    for start in range(0, len(text), _CHUNK):
        delta = {"role": "assistant", "content": text[start : start + _CHUNK]}
        yield _sse(head | {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
    for index, call in enumerate(choice["message"].get("tool_calls", [])):
        delta = {"tool_calls": [{"index": index, **call}]}
        yield _sse(head | {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
    final = {"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}
    yield _sse(head | {"choices": [final]})
    yield _sse(head | {"choices": [], "usage": completion["usage"]})
    yield b"data: [DONE]\n\n"
