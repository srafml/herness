"""Tests for herness.harness.llm.anthropic_client.AnthropicClient (U05-27, U05-28)."""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
import yaml
from pydantic import JsonValue, SecretStr
from structlog.testing import capture_logs
from tests.support.egress_harness import load
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import egress, registry, secrets
from herness.core.errors import ConfigError, EgressBlocked, ModelUnavailable, OutputValidationError
from herness.core.types import (
    LLMRequest,
    Message,
    ReasoningPart,
    RequestMeta,
    SystemBlock,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
)
from herness.harness.llm import anthropic_client as ac
from herness.harness.llm.base import Done, LLMClient, StreamCapable, TextDelta, ToolCallDelta
from herness.harness.llm.settings import ClientConfig

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
_CLIENTS: dict[str, dict[str, Any]] = yaml.safe_load(
    (ROOT / "config" / "models.yaml").read_text(encoding="utf-8")
)["models"]["clients"]
_KEY = "sk-ant-test-" + "k" * 24  # pragma: allowlist secret
_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"a": {"type": "integer"}}}
_Handler = Callable[[httpx2.Request], httpx2.Response]


def _cfg(key: str = "claude-opus") -> ClientConfig:
    return ClientConfig.model_validate({"name": key, **_CLIENTS[key]})


def _req(**kw: Any) -> LLMRequest:
    base: dict[str, Any] = {
        "client": "claude-opus",
        "messages": [Message(role="user", parts=[TextPart(text="hi")])],
        "max_output_tokens": 4000,
        "timeout_s": 30.0,
        "metadata": RequestMeta(
            run_id="run_1",
            task_id="task_1",
            role="analyst",
            model_role="writer",
            step=0,
            request_key="task_1:0:step",
        ),
    }
    return LLMRequest(**{**base, **kw})


class _StubGuard:
    """Stands in for the T10-17 guard: hands out httpx2 clients over a mock transport."""

    def __init__(self, handler: _Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[Any, ...]] = []
        self.transports: list[httpx2.MockTransport] = []
        self.requests: list[httpx2.Request] = []

    def _record(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.handler(request)

    def async_http_client(
        self,
        purpose: str,
        payload_class: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        timeout: float = 120.0,
    ) -> httpx2.AsyncClient:
        self.calls.append((purpose, payload_class, run_id, task_id, timeout))
        transport = httpx2.MockTransport(self._record)
        self.transports.append(transport)
        return httpx2.AsyncClient(transport=transport)


@pytest.fixture
def hybrid(
    tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    c.reset_config()
    load(tmp_path, "hybrid")
    monkeypatch.setattr(secrets, "resolve", lambda ref: SecretStr(_KEY))
    yield
    c.reset_config()


@pytest.fixture
def client(hybrid: None) -> ac.AnthropicClient:
    return ac.AnthropicClient(_cfg())


def _install(monkeypatch: pytest.MonkeyPatch, handler: _Handler) -> _StubGuard:
    guard = _StubGuard(handler)
    monkeypatch.setattr(egress, "get_guard", lambda: guard)
    return guard


def _message_json(content: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    return {**body, **kw}


def _raw(content: list[dict[str, Any]], **kw: Any) -> anthropic.types.Message:
    return anthropic.types.Message.model_validate(_message_json(content, **kw))


def _json_handler(body: dict[str, Any]) -> _Handler:
    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=body, headers={"request-id": "req_42"})

    return handle


def _sse(events: list[dict[str, Any]]) -> _Handler:
    text = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)

    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, content=text.encode(), headers={"content-type": "text/event-stream"}
        )

    return handle


_STREAM_EVENTS: list[dict[str, Any]] = [
    {"type": "message_start", "message": {**_message_json([]), "stop_reason": None}},
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "thinking_delta", "thinking": "hm"},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "signature_delta", "signature": "sig"},
    },
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hel"}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "lo"}},
    {"type": "content_block_stop", "index": 1},
    {
        "type": "content_block_start",
        "index": 2,
        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "run_sql", "input": {}},
    },
    {
        "type": "content_block_delta",
        "index": 2,
        "delta": {"type": "input_json_delta", "partial_json": '{"sql": '},
    },
    {
        "type": "content_block_delta",
        "index": 2,
        "delta": {"type": "input_json_delta", "partial_json": '"select 1"}'},
    },
    {"type": "content_block_stop", "index": 2},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
        "usage": {"output_tokens": 30},
    },
    {"type": "message_stop"},
]


# --- UT05-31 -----------------------------------------------------------------------------


def test_ut05_31_opus_golden_params(client: ac.AnthropicClient) -> None:
    """UT05-31 Opus (adaptive_always): no thinking key, effort sent, no temperature."""
    params = client._build_params(_req(effort="high", thinking="off"))
    assert params == {
        "model": "claude-opus-5-5",
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "cache_control": {"type": "ephemeral"},
        "output_config": {"effort": "high"},
    }


@pytest.mark.parametrize(("thinking", "expected"), [("on", "adaptive"), ("off", "disabled")])
def test_ut05_31_sonnet_adaptive_or_disabled(hybrid: None, thinking: str, expected: str) -> None:
    """UT05-31 Sonnet (adaptive_optional): on → adaptive, off → disabled; auto → omitted."""
    sonnet = ac.AnthropicClient(_cfg("claude-sonnet"))
    assert sonnet._build_params(_req(thinking=thinking))["thinking"] == {"type": expected}
    assert "thinking" not in sonnet._build_params(_req(thinking="auto"))


def test_ut05_31_haiku_enabled_budget(hybrid: None) -> None:
    """UT05-31 Haiku (budget): enabled budget = max(1024, max/4), explicit and clamped."""
    haiku = ac.AnthropicClient(_cfg("claude-haiku"))
    on = haiku._build_params(_req(thinking="on", max_output_tokens=8000))
    assert on["thinking"] == {"type": "enabled", "budget_tokens": 2000}
    assert "output_config" not in on  # supports.effort is false
    small = haiku._build_params(_req(thinking="on", max_output_tokens=2000))
    assert small["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    explicit = haiku._build_params(
        _req(thinking="on", max_output_tokens=3000, thinking_budget_tokens=9000)
    )
    assert explicit["thinking"] == {"type": "enabled", "budget_tokens": 2999}
    assert "thinking" not in haiku._build_params(_req(thinking="off"))
    with pytest.raises(ConfigError, match="thinking budget"):
        haiku._build_params(_req(thinking="on", max_output_tokens=1024))


def test_ut05_31_output_config_format_and_sampling(hybrid: None) -> None:
    """UT05-31 schema → output_config.format; temperature and stop sequences when set."""
    haiku = ac.AnthropicClient(_cfg("claude-haiku"))
    params = haiku._build_params(
        _req(
            response_schema=_SCHEMA,
            response_schema_name="Out",
            temperature=0.2,
            stop=["END"],
            effort="low",
        )
    )
    assert params["output_config"] == {"format": {"type": "json_schema", "schema": _SCHEMA}}
    assert params["extra_body"] == {"temperature": 0.2}
    assert params["stop_sequences"] == ["END"]
    opus = ac.AnthropicClient(_cfg())
    both = opus._build_params(
        _req(response_schema=_SCHEMA, response_schema_name="Out", effort="max")
    )
    assert both["output_config"] == {
        "effort": "max",
        "format": {"type": "json_schema", "schema": _SCHEMA},
    }


# --- UT05-32 -----------------------------------------------------------------------------


def _tool(name: str, *, strict: bool = True) -> ToolSpec:
    return ToolSpec(
        name=name, description=f"{name} tool", input_schema={"type": "object"}, strict=strict
    )


def test_ut05_32_cache_block_tools_sorted_strict(client: ac.AnthropicClient) -> None:
    """UT05-32 cache_control on the cached block only; tools sorted and strict; auto choice."""
    req = _req(
        system=[SystemBlock(text="stable", cache=True), SystemBlock(text="volatile")],
        tools=[_tool("run_sql"), _tool("get_metric", strict=False)],
    )
    params = client._build_params(req)
    assert params["system"] == [
        {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "volatile"},
    ]
    assert params["tools"] == [
        {
            "name": "get_metric",
            "description": "get_metric tool",
            "input_schema": {"type": "object"},
            "strict": False,
        },
        {
            "name": "run_sql",
            "description": "run_sql tool",
            "input_schema": {"type": "object"},
            "strict": True,
        },
    ]
    assert params["tool_choice"] == {"type": "auto"}
    none = client._build_params(_req(tools=[_tool("run_sql")], tool_choice="none"))
    assert none["tool_choice"] == {"type": "none"}
    assert "tool_choice" not in client._build_params(_req())


def test_ut05_32_message_mapping_merges_user_turns(client: ac.AnthropicClient) -> None:
    """UT05-32 messages: opaque anthropic reasoning replayed, others dropped; user turns merged."""
    opaque: dict[str, JsonValue] = {"type": "thinking", "thinking": "t", "signature": "s"}
    messages = [
        Message(role="user", parts=[TextPart(text="q")]),
        Message(
            role="assistant",
            parts=[
                ReasoningPart(provider="anthropic", text="t", opaque=opaque),
                ReasoningPart(provider="vllm", text="local"),
                ReasoningPart(provider="anthropic", text="no opaque"),
                TextPart(text=""),
                TextPart(text="calling"),
                ToolCallPart(call=ToolCall(id="toolu_1", name="run_sql", arguments={"sql": "x"})),
            ],
        ),
        Message(role="user", parts=[TextPart(text="nudge")]),
        Message(
            role="tool", parts=[ToolResultPart(tool_call_id="toolu_1", content="ok", is_error=True)]
        ),
    ]
    assert client._to_anthropic_messages(messages) == [
        {"role": "user", "content": [{"type": "text", "text": "q"}]},
        {
            "role": "assistant",
            "content": [
                opaque,
                {"type": "text", "text": "calling"},
                {"type": "tool_use", "id": "toolu_1", "name": "run_sql", "input": {"sql": "x"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "ok",
                    "is_error": True,
                },
                {"type": "text", "text": "nudge"},
            ],
        },
    ]


# --- UT05-33 -----------------------------------------------------------------------------


def test_ut05_33_thinking_opaque_kept(client: ac.AnthropicClient) -> None:
    """UT05-33 thinking and redacted_thinking → ReasoningPart with the block unchanged."""
    raw = _raw(
        [
            {"type": "thinking", "thinking": "plan", "signature": "sig"},
            {"type": "redacted_thinking", "data": "blob"},
            {"type": "text", "text": "a"},
            {"type": "tool_use", "id": "toolu_1", "name": "run_sql", "input": {"sql": "s"}},
            {"type": "text", "text": "b"},
        ],
        stop_reason="tool_use",
    )
    resp = client._map_message(raw, _req(), 12, batch=False)
    assert resp.reasoning == [
        ReasoningPart(
            provider="anthropic",
            text="plan",
            opaque={"type": "thinking", "thinking": "plan", "signature": "sig"},
        ),
        ReasoningPart(
            provider="anthropic", text="", opaque={"type": "redacted_thinking", "data": "blob"}
        ),
    ]
    assert resp.text == "ab"
    assert resp.tool_calls == [ToolCall(id="toolu_1", name="run_sql", arguments={"sql": "s"})]
    assert (resp.stop_reason, resp.raw_stop_reason, resp.provider) == (
        "tool_use",
        "tool_use",
        "anthropic",
    )
    assert (resp.latency_ms, resp.model, resp.client) == (12, "claude-opus-5-5", "claude-opus")


def test_ut05_33_refusal_category_and_other_stop(client: ac.AnthropicClient) -> None:
    """UT05-33 refusal is returned with its category; unknown stop reasons map to other."""
    refusal = _raw(
        [{"type": "text", "text": ""}],
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": None},
    )
    resp = client._map_message(refusal, _req(), 1, batch=False)
    assert (resp.stop_reason, resp.refusal_category) == ("refusal", "cyber")
    paused = client._map_message(_raw([], stop_reason="pause_turn"), None, 1, batch=False)
    assert (paused.stop_reason, paused.raw_stop_reason, paused.refusal_category) == (
        "other",
        "pause_turn",
        None,
    )


def test_ut05_33_cache_usage_fields_and_cost(client: ac.AnthropicClient) -> None:
    """UT05-33 cache read/creation tokens map to cache_read/cache_write; batch halves the cost."""
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
    }
    raw = _raw([{"type": "text", "text": "x"}], usage=usage)
    resp = client._map_message(raw, _req(), 1, batch=False)
    assert (resp.usage.cache_read_tokens, resp.usage.cache_write_tokens) == (1_000_000, 1_000_000)
    assert str(resp.cost_usd) == "9.200000"  # 4.00 + 0.20 + 5.00
    halved = client._map_message(raw, _req(), 1, batch=True)
    assert (str(halved.cost_usd), halved.batch) == ("4.600000", True)
    assert resp.request_id is None


def test_ut05_33_parsed_from_first_text_block(client: ac.AnthropicClient) -> None:
    """UT05-33 parsed = json of the first text block when a schema is set and it is an object."""
    req = _req(response_schema=_SCHEMA, response_schema_name="Out")
    ok = client._map_message(_raw([{"type": "text", "text": '{"a": 1}'}]), req, 1, batch=False)
    assert ok.parsed == {"a": 1}
    for text in ("[1]", "not json"):
        bad = client._map_message(_raw([{"type": "text", "text": text}]), req, 1, batch=False)
        assert bad.parsed is None
    no_schema = client._map_message(
        _raw([{"type": "text", "text": '{"a": 1}'}]), _req(), 1, batch=False
    )
    assert no_schema.parsed is None


def test_ut05_33_tool_input_not_object(client: ac.AnthropicClient) -> None:
    """UT05-33 a tool_use block whose input is not an object → OutputValidationError."""
    raw = _raw([{"type": "tool_use", "id": "toolu_1", "name": "run_sql", "input": {}}])
    raw.content[0].input = [1, 2]  # type: ignore[union-attr]
    with pytest.raises(OutputValidationError, match="toolu_1"):
        client._map_message(raw, _req(), 1, batch=False)


def test_ut05_33_tool_name_invalid(client: ac.AnthropicClient) -> None:
    """UT05-33 a tool_use block with a name outside the ToolCall pattern → OutputValidationError."""
    raw = _raw([{"type": "tool_use", "id": "toolu_1", "name": "Bad-Name", "input": {}}])
    with pytest.raises(OutputValidationError, match="invalid"):
        client._map_message(raw, _req(), 1, batch=False)


def test_ut05_33_text_over_max_raises(client: ac.AnthropicClient) -> None:
    """UT05-33 response text over MAX_RESPONSE_TEXT_CHARS (1,000,000) → OutputValidationError."""
    assert ac._MAX_RESPONSE_TEXT_CHARS == 1_000_000
    raw = _raw([{"type": "text", "text": "x" * 1_000_001}])
    with pytest.raises(OutputValidationError, match="exceeds limit"):
        client._map_message(raw, _req(), 1, batch=False)


def test_ut05_33_text_over_part_cap_truncated(client: ac.AnthropicClient) -> None:
    """UT05-33 text over the 200,000 TextPart cap is truncated with a marker and a WARNING."""
    raw = _raw([{"type": "text", "text": "y" * 1_000_000}])
    with capture_logs() as logs:
        resp = client._map_message(raw, _req(), 1, batch=False)
    assert len(resp.text) == 200_000
    assert resp.text.endswith("[truncated]")
    TextPart(text=resp.text)  # fits the part cap
    [entry] = [e for e in logs if e["event"] == "harness.llm.response_truncated"]
    assert entry["log_level"] == "warning"
    assert (entry["length"], entry["kept"]) == (1_000_000, 200_000)
    assert "yyyy" not in json.dumps(entry)


def test_ut05_33_more_than_64_tool_calls(client: ac.AnthropicClient) -> None:
    """UT05-33 more than 64 tool calls → OutputValidationError; 64 are accepted."""
    blocks = [
        {"type": "tool_use", "id": f"toolu_{i}", "name": "run_sql", "input": {}} for i in range(65)
    ]
    with pytest.raises(OutputValidationError, match="too many tool calls"):
        client._map_message(_raw(blocks), _req(), 1, batch=False)
    assert len(client._map_message(_raw(blocks[:64]), _req(), 1, batch=False).tool_calls) == 64


# --- UT05-34 -----------------------------------------------------------------------------


def test_ut05_34_large_max_output_uses_stream(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-34 max_output_tokens=20000 → the messages.stream path (SSE request) is used."""
    guard = _install(monkeypatch, _sse(_STREAM_EVENTS))
    resp = asyncio.run(client.acomplete(_req(max_output_tokens=20000)))
    [request] = guard.requests
    assert json.loads(request.content)["stream"] is True
    assert resp.text == "Hello"
    assert resp.tool_calls == [
        ToolCall(id="toolu_1", name="run_sql", arguments={"sql": "select 1"})
    ]


def test_ut05_34_small_max_output_uses_create(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-34 max_output_tokens=16000 → messages.create (no stream flag); request id kept."""
    guard = _install(monkeypatch, _json_handler(_message_json([{"type": "text", "text": "ok"}])))
    resp = asyncio.run(client.acomplete(_req(max_output_tokens=16000)))
    body = json.loads(guard.requests[0].content)
    assert "stream" not in body or body["stream"] is False
    assert (resp.text, resp.request_id) == ("ok", "req_42")


# --- UT05-35 -----------------------------------------------------------------------------


def test_ut05_35_status_529_translated(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-35 an Anthropic 529 through acomplete → ModelUnavailable, one attempt, no body."""
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "secret-body-text"}}
    guard = _install(monkeypatch, lambda request: httpx2.Response(529, json=body))
    with pytest.raises(ModelUnavailable) as info:
        asyncio.run(client.acomplete(_req()))
    assert len(guard.requests) == 1
    assert "secret-body-text" not in str(info.value)
    assert _KEY not in str(info.value)
    assert isinstance(info.value.__cause__, anthropic.APIStatusError)


# --- UT05-36 -----------------------------------------------------------------------------


def test_ut05_36_local_profile_construct_fails(tmp_path: Path, fake_keyring: MemoryKeyring) -> None:
    """UT05-36 local profile (egress disabled) → ConfigError naming client and profile."""
    c.reset_config()
    try:
        load(tmp_path, "local")
        with pytest.raises(
            ConfigError, match="claude-opus cannot be constructed in profile local: egress disabled"
        ):
            ac.AnthropicClient(_cfg())
    finally:
        c.reset_config()


def test_ut05_36_wrong_kind_or_missing_key(hybrid: None) -> None:
    """UT05-36 a non-anthropic config or one without api_key → ConfigError."""
    local = ClientConfig.model_validate({"name": "local-30b", **_CLIENTS["local-30b"]})
    with pytest.raises(ConfigError, match="not an anthropic client"):
        ac.AnthropicClient(local)
    keyless = _cfg().model_copy(update={"api_key": None})
    with pytest.raises(ConfigError, match="no api_key"):
        ac.AnthropicClient(keyless)


def test_ut05_36_key_resolved_once_and_registered(
    hybrid: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-36 key resolved once as SecretStr at construction; registered as llm_client."""
    refs: list[str] = []

    def resolve(ref: str) -> SecretStr:
        refs.append(ref)
        return SecretStr(_KEY)

    monkeypatch.setattr(secrets, "resolve", resolve)
    client = ac.AnthropicClient(_cfg())
    assert refs == ["secret:anthropic.api_key"]
    assert isinstance(client._api_key, SecretStr)
    assert _KEY not in repr(vars(client))
    assert isinstance(client, LLMClient)
    assert isinstance(client, StreamCapable)
    module = importlib.reload(ac)
    assert registry.get("llm_client", "anthropic") is module.AnthropicClient


# --- UT05-37 -----------------------------------------------------------------------------


def test_ut05_37_sdk_uses_guard_client(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-37 hybrid + stub guard: the SDK sent through the guard's transport with guard args."""
    guard = _install(monkeypatch, _json_handler(_message_json([{"type": "text", "text": "ok"}])))
    asyncio.run(client.acomplete(_req()))
    assert guard.calls == [("reasoning_final", "aggregated_evidence", "run_1", "task_1", 120.0)]
    [transport] = guard.transports
    assert type(transport) is httpx2.MockTransport
    [request] = guard.requests
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == _KEY
    assert _KEY not in request.content.decode()


def test_ut05_37_missing_guard_client_fails_closed(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-37 a guard without async_http_client → EgressBlocked; no client is built."""
    monkeypatch.setattr(egress, "get_guard", object)
    with pytest.raises(EgressBlocked) as info:
        asyncio.run(client.acomplete(_req()))
    assert info.value.reason == "guard_client_unavailable"


# --- UT05-38 -----------------------------------------------------------------------------


def test_ut05_38_astream_deltas_and_one_done(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-38 fake stream: text and tool deltas in order, thinking not yielded, one Done last."""
    _install(monkeypatch, _sse(_STREAM_EVENTS))

    async def collect() -> list[Any]:
        return [event async for event in client.astream(_req())]

    events = asyncio.run(collect())
    assert events[:-1] == [
        TextDelta("Hel"),
        TextDelta("lo"),
        ToolCallDelta("toolu_1", "run_sql", '{"sql": '),
        ToolCallDelta("toolu_1", "run_sql", '"select 1"}'),
    ]
    done = events[-1]
    assert isinstance(done, Done)
    assert sum(isinstance(e, Done) for e in events) == 1
    assert done.response.text == "Hello"
    assert done.response.stop_reason == "tool_use"
    assert done.response.reasoning[0].opaque == {
        "type": "thinking",
        "thinking": "hm",
        "signature": "sig",
    }
    assert done.response.usage.output_tokens == 30


def test_ut05_38_astream_error_translated(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-38 an error before any event → translated; no Done is yielded."""
    _install(monkeypatch, lambda request: httpx2.Response(500, json={"type": "error"}))
    events: list[Any] = []

    async def collect() -> None:
        events.extend([event async for event in client.astream(_req())])

    with pytest.raises(ModelUnavailable):
        asyncio.run(collect())
    assert events == []


def test_ut05_38_complete_sync_and_inside_loop(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-38 complete() runs acomplete outside a loop and raises RuntimeError inside one."""
    _install(monkeypatch, _json_handler(_message_json([{"type": "text", "text": "sync"}])))
    assert client.complete(_req()).text == "sync"

    async def inside() -> None:
        client.complete(_req())

    with pytest.raises(RuntimeError, match="running event loop"):
        asyncio.run(inside())


# --- ST05-13 -----------------------------------------------------------------------------

_SDK_CLIENTS = {"Anthropic", "AsyncAnthropic"}
_HTTPX = {"httpx", "httpx2"}
_HTTPX_CLIENTS = {"Client", "AsyncClient"}


def _call_name(node: ast.Call) -> tuple[str | None, str | None]:
    func = node.func
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else None
        return owner, func.attr
    if isinstance(func, ast.Name):
        return None, func.id
    return None, None


def _violations(source: str, path: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        owner, name = _call_name(node)
        keywords = {kw.arg for kw in node.keywords}
        if name in _SDK_CLIENTS and "http_client" not in keywords:
            found.append(f"{path}:{node.lineno} {name} without http_client")
        if owner in _HTTPX and name in _HTTPX_CLIENTS:
            found.append(f"{path}:{node.lineno} {owner}.{name} constructed")
    return found


def test_st05_13_ast_lint_harness_builds_no_unguarded_clients() -> None:
    """ST05-13 (a) herness/harness: no SDK client without http_client=, no httpx client."""
    found: list[str] = []
    for path in sorted((ROOT / "herness" / "harness").rglob("*.py")):
        found += _violations(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
    assert found == []


def test_st05_13_ast_lint_detects_violations() -> None:
    """ST05-13 (a) the lint itself flags the forbidden constructions."""
    bad = (
        "anthropic.AsyncAnthropic(api_key=k)\nAnthropic()\nhttpx.Client()\n"
        "httpx2.AsyncClient(timeout=1)\nanthropic.AsyncAnthropic(http_client=h)\n"
    )
    assert len(_violations(bad, "x.py")) == 4


def test_st05_13_egress_blocked_inside_transport(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST05-13 (b) the guard raising EgressBlocked in the transport surfaces it; zero retries."""
    blocked = EgressBlocked("egress refused", egress_id="egr_1", reason="profile")

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise blocked

    guard = _install(monkeypatch, refuse)
    with pytest.raises(EgressBlocked) as info:
        asyncio.run(client.acomplete(_req()))
    assert info.value is blocked
    assert len(guard.requests) == 1

    async def stream() -> None:
        async for _ in client.astream(_req()):
            pass

    with pytest.raises(EgressBlocked):
        asyncio.run(stream())
    assert len(guard.requests) == 2


def test_st05_13_local_profile_zero_connects(
    tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST05-13 (c) local profile with a fake socket: construction fails and nothing connects."""
    connects: list[object] = []
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: connects.append(addr))
    c.reset_config()
    try:
        load(tmp_path, "local")
        with pytest.raises(ConfigError, match="egress disabled"):
            ac.AnthropicClient(_cfg())
    finally:
        c.reset_config()
    assert connects == []
