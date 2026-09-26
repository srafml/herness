"""Tests for herness.harness.llm.openai_compat.OpenAICompatClient (U05-24, U05-25).

The ``openai`` SDK sends through ``httpx2``, which ``respx`` does not patch. On-network calls
use the real ``egress.aloopback_http_client`` (T05-06b); the HTTP fake swaps the pool transport
that client builds for an ``httpx2.MockTransport`` over the test's handler (``_serve``), so the
loopback byte cap, encoding checks and redirect refusal all run for real.
"""

from __future__ import annotations

import asyncio
import gzip
import importlib
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from openai.types.chat import ChatCompletion
from structlog.testing import capture_logs
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import egress_clients, registry
from herness.core.errors import (
    AuthError,
    ConfigError,
    EgressBlocked,
    ModelUnavailable,
    OutputValidationError,
    RateLimited,
)
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
from herness.harness.llm import base, openai_compat
from herness.harness.llm.openai_compat import OpenAICompatClient
from herness.harness.llm.settings import ClientConfig, PricePerMTok

pytestmark = pytest.mark.unit

_GOLDEN = Path(__file__).parents[2] / "fixtures" / "harness" / "golden_requests"
_ZERO = Decimal("0")
_ZERO_PRICE = PricePerMTok(input=_ZERO, output=_ZERO, cache_read=_ZERO, cache_write=_ZERO)
_URLS = {
    "vllm": "http://127.0.0.1:8000/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "llamacpp": "http://127.0.0.1:8200/v1",
}
_KEY_VALUE = "sk-local-test-value-0123456789"  # pragma: allowlist secret
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def _cfg(server: str = "vllm", **updates: Any) -> ClientConfig:
    data: dict[str, Any] = {
        "name": "local-30b",
        "kind": "openai_compat",
        "base_url": _URLS.get(server, _URLS["vllm"]),
        "model": "qwen3-30b",
        "context_window": 32768,
        "max_output_tokens": 4096,
        "tokenizer": "estimate",
        "max_concurrency": 4,
        "reasoning_parser": "qwen3",
        "price_per_mtok": _ZERO_PRICE,
        "server": server,
    }
    data.update(updates)
    return ClientConfig.model_validate(data)


def _history() -> list[Message]:
    call = ToolCall(id="call_1", name="run_sql", arguments={"sql": "select 1", "limit": 5})
    raw_call = ToolCall(
        id="call_2", name="get_schema", arguments={"table": "t"}, raw_arguments='{"table": "t"}'
    )
    return [
        Message(role="user", parts=[TextPart(text="How many incidents?"), TextPart(text="P1")]),
        Message(
            role="assistant",
            parts=[
                ReasoningPart(provider="vllm", text="private thoughts"),
                TextPart(text="Checking."),
                ToolCallPart(call=call),
                ToolCallPart(call=raw_call),
            ],
        ),
        Message(
            role="tool",
            parts=[
                ToolResultPart(tool_call_id="call_1", content="[[1]]"),
                ToolResultPart(tool_call_id="call_2", content="{}", is_error=True),
            ],
        ),
        Message(role="assistant", parts=[ToolCallPart(call=call)]),
        Message(role="tool", parts=[ToolResultPart(tool_call_id="call_1", content="[[1]]")]),
    ]


def _request(**updates: Any) -> LLMRequest:
    data: dict[str, Any] = {
        "client": "local-30b",
        "system": [SystemBlock(text="You are the planner."), SystemBlock(text="Be brief.")],
        "messages": _history(),
        "tools": [
            ToolSpec(name="run_sql", description="Run SQL.", input_schema={"type": "object"}),
            ToolSpec(name="get_schema", description="Schema.", input_schema={"type": "object"}),
        ],
        "tool_choice": "auto",
        "response_schema": _SCHEMA,
        "response_schema_name": "PlannerOutput",
        "max_output_tokens": 2048,
        "temperature": 0.2,
        "thinking": "on",
        "stop": ["</done>"],
        "seed": 7,
        "timeout_s": 30.0,
        "metadata": RequestMeta(
            run_id="run_1",
            task_id="task_1",
            role="planner",
            model_role="writer",
            step=3,
            request_key="task_1:3:loop",
        ),
    }
    data.update(updates)
    return LLMRequest(**data)


def _completion(**message: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "qwen3-30b",
        "choices": [
            {
                "index": 0,
                "finish_reason": message.pop("finish_reason", "stop"),
                "message": {"role": "assistant", "content": None, **message},
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }
    return body


def _map(body: dict[str, Any], req: LLMRequest | None = None, cfg: ClientConfig | None = None):
    client = OpenAICompatClient(cfg or _cfg())
    # construct() is how the SDK builds responses (no strict validation), so null fields pass.
    return client._map_response(ChatCompletion.construct(**body), req or _request(), 12)


def _tool_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class _FakeServer:
    """Records every request the SDK sends and answers with the queued responses."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self.responses: list[tuple[int, dict[str, Any], dict[str, str]]] = []
        self.streamed = False  # True: a real streamed body with no Content-Length (m2)

    def respond(self, status: int, body: dict[str, Any], **headers: str) -> None:
        self.responses.append((status, body, headers))

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        self.requests.append(request)
        self.bodies.append(json.loads(request.content))
        status, body, headers = self.responses.pop(0)
        if self.streamed:
            headers = {"content-type": "application/json", **headers}
            stream = _Streamed(json.dumps(body).encode())
            return httpx2.Response(status, headers=headers, stream=stream, request=request)
        return httpx2.Response(status, json=body, headers=headers, request=request)


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[dict[str, Any]]:
    """Answer the loopback client's requests with ``handler`` (sync or async) via MockTransport.

    Replaces the pool-transport name ``egress_clients`` builds; the loopback transport that
    wraps it (host check, identity encoding, byte cap) stays real. Returns the build kwargs.
    """
    built: list[dict[str, Any]] = []

    def transport(**kwargs: Any) -> httpx2.MockTransport:
        built.append(kwargs)
        return httpx2.MockTransport(handler)

    monkeypatch.setattr(egress_clients.httpx2, "AsyncHTTPTransport", transport)
    return built


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _FakeServer:
    fake = _FakeServer()
    _serve(monkeypatch, fake.handle)
    return fake


@pytest.fixture
def herness_cfg(tmp_path: Path, fake_keyring: MemoryKeyring) -> Iterator[c.HernessConfig]:
    """A full config so ``parse_retry_after``'s default ``max_s`` can read it (U08-17)."""
    del fake_keyring
    c.reset_config()
    yield c.init_config("local", config_dir=write_full_config(tmp_path), env={})
    c.reset_config()


def _golden(name: str) -> dict[str, Any]:
    return json.loads((_GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


# --- UT05-23 / UT05-24: golden request mapping -------------------------------------------------


def test_ut05_23_vllm_params_match_golden() -> None:
    """UT05-23 vLLM params equal the golden file (response_format, chat_template_kwargs, tools)."""
    params = OpenAICompatClient(_cfg("vllm"))._build_params(_request())
    assert params == _golden("vllm")
    assert params["tool_choice"] == "auto"
    assert params["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}


def test_ut05_23_vllm_sends_tool_choice_none_and_thinking_off() -> None:
    """UT05-23 vLLM passes tool_choice none through and sends enable_thinking false."""
    req = _request(tool_choice="none", thinking="off")
    params = OpenAICompatClient(_cfg("vllm"))._build_params(req)
    assert params["tool_choice"] == "none"
    assert params["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_ut05_23_messages_mapping() -> None:
    """UT05-23 message mapping: system first, joined text, tool calls, reasoning dropped."""
    messages = OpenAICompatClient(_cfg())._to_openai_messages(_request())
    assert messages[0] == {"role": "system", "content": "You are the planner.\n\nBe brief."}
    assert messages[1] == {"role": "user", "content": "How many incidents?\n\nP1"}
    assistant = messages[2]
    assert assistant["content"] == "Checking."
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"limit":5,"sql":"select 1"}'
    assert assistant["tool_calls"][1]["function"]["arguments"] == '{"table": "t"}'
    assert "private thoughts" not in json.dumps(messages)
    assert [m["tool_call_id"] for m in messages[3:5]] == ["call_1", "call_2"]
    assert messages[5]["content"] is None
    no_system = OpenAICompatClient(_cfg())._to_openai_messages(_request(system=[]))
    assert no_system[0]["role"] == "user"


def test_ut05_23_minimal_request_omits_optional_keys() -> None:
    """UT05-23 without tools, schema, seed, stop or temperature those keys are absent."""
    req = _request(
        tools=[],
        response_schema=None,
        response_schema_name=None,
        stop=[],
        seed=None,
        temperature=None,
        thinking="off",
        messages=[Message(role="user", parts=[TextPart(text="hi")])],
    )
    params = OpenAICompatClient(_cfg("llamacpp"))._build_params(req)
    assert set(params) == {"model", "messages", "max_tokens"}
    openai_params = OpenAICompatClient(_cfg("openai"))._build_params(req)
    assert set(openai_params) == {"model", "messages", "max_tokens"}


@pytest.mark.parametrize("server_name", ["ollama", "llamacpp"])
def test_ut05_24_ollama_and_llamacpp_match_golden(server_name: str) -> None:
    """UT05-24 Ollama and llama.cpp params equal their golden files."""
    params = OpenAICompatClient(_cfg(server_name))._build_params(_request())
    assert params == _golden(server_name)


def test_ut05_24_ollama_think_and_tool_choice_none_omitted() -> None:
    """UT05-24 Ollama sends extra_body.think and omits tool_choice for none."""
    client = OpenAICompatClient(_cfg("ollama"))
    params = client._build_params(_request(tool_choice="none", thinking="off"))
    assert "tool_choice" not in params
    assert params["parallel_tool_calls"] is True
    assert params["extra_body"] == {"think": False}
    assert client._build_params(_request())["extra_body"] == {"think": True}


def test_ut05_24_llamacpp_sends_no_thinking_key() -> None:
    """UT05-24 llama.cpp sends tool_choice as given and no extra_body."""
    params = OpenAICompatClient(_cfg("llamacpp"))._build_params(_request(tool_choice="none"))
    assert params["tool_choice"] == "none"
    assert "extra_body" not in params


# --- UT05-25 / UT05-26: response mapping -------------------------------------------------------


def test_ut05_25_map_text_reasoning_usage_cost() -> None:
    """UT05-25 text, reasoning part, finish mapping, usage and zero cost."""
    body = _completion(content='{"answer": "42"}', reasoning_content="thinking...")
    body["usage"]["completion_tokens_details"] = {"reasoning_tokens": 11}
    resp = _map(body)
    assert resp.text == '{"answer": "42"}'
    assert resp.parsed == {"answer": "42"}
    assert resp.reasoning == [ReasoningPart(provider="vllm", text="thinking...")]
    assert (resp.stop_reason, resp.raw_stop_reason) == ("end_turn", "stop")
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (120, 30)
    assert resp.usage.reasoning_tokens == 11
    assert resp.usage.cache_read_tokens == resp.usage.cache_write_tokens == 0
    assert resp.cost_usd == Decimal("0.000000")
    assert (resp.provider, resp.model, resp.request_id) == (
        "openai_compat",
        "qwen3-30b",
        "chatcmpl-1",
    )
    assert (resp.client, resp.latency_ms, resp.refusal_category) == ("local-30b", 12, None)


def test_ut05_25_map_tool_calls() -> None:
    """UT05-25 tool calls keep raw arguments; an empty string counts as {}."""
    body = _completion(
        finish_reason="tool_calls",
        reasoning="r1",
        reasoning_content="r2",
        tool_calls=[
            _tool_call("call_a", "run_sql", '{"sql": "select 1"}'),
            _tool_call("call_b", "list_tables", ""),
        ],
    )
    resp = _map(body)
    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls[0] == ToolCall(
        id="call_a",
        name="run_sql",
        arguments={"sql": "select 1"},
        raw_arguments='{"sql": "select 1"}',
    )
    assert resp.tool_calls[1].arguments == {}
    assert resp.reasoning[0].text == "r1"
    assert resp.text == ""


@pytest.mark.parametrize(
    ("finish", "expected"),
    [
        ("length", "max_tokens"),
        ("content_filter", "content_filter"),
        ("function_call", "other"),
        (None, "other"),
    ],
)
def test_ut05_25_finish_reason_mapping(finish: str | None, expected: str) -> None:
    """UT05-25 finish_reason maps to stop_reason; unknown or null is other."""
    body = _completion(content="x", finish_reason=finish)
    resp = _map(body)
    assert resp.stop_reason == expected
    assert resp.raw_stop_reason == (finish or "")


def test_ut05_25_parsed_only_with_schema_and_object() -> None:
    """UT05-25 parsed is None without a schema, for a non-object or for bad JSON."""
    no_schema = _request(response_schema=None, response_schema_name=None, thinking="off")
    assert _map(_completion(content='{"a": 1}'), no_schema).parsed is None
    assert _map(_completion(content="[1, 2]")).parsed is None
    assert _map(_completion(content="not json")).parsed is None


def test_ut05_25_usage_absent_and_no_reasoning() -> None:
    """UT05-25 missing usage maps to zeros; no reasoning extras means no reasoning part."""
    body = _completion(content="x")
    body["usage"] = None
    resp = _map(body)
    assert resp.usage.input_tokens == resp.usage.output_tokens == 0
    assert resp.reasoning == []


def test_ut05_25_model_mismatch_warns() -> None:
    """UT05-25 a served model other than cfg.model logs harness.llm.model_mismatch (TB8)."""
    body = _completion(content="x")
    body["model"] = "impostor"
    with capture_logs() as logs:
        resp = _map(body)
    assert resp.model == "impostor"
    event = next(e for e in logs if e["event"] == "harness.llm.model_mismatch")
    assert (event["client"], event["expected"], event["actual"]) == (
        "local-30b",
        "qwen3-30b",
        "impostor",
    )
    assert event["log_level"] == "warning"


@pytest.mark.parametrize("arguments", ["{not json", "[1, 2]", '"text"', "[" * 5000])
def test_ut05_26_bad_tool_call_json_raises(arguments: str) -> None:
    """UT05-26 tool-call arguments that are not a JSON object raise OutputValidationError."""
    body = _completion(tool_calls=[_tool_call("call_x", "run_sql", arguments)])
    with pytest.raises(OutputValidationError, match="tool call call_x arguments are not a JSON"):
        _map(body)


def test_ut05_26_invalid_tool_name_or_type_raises() -> None:
    """UT05-26 a tool name outside the pattern or a non-function call raises."""
    with pytest.raises(OutputValidationError, match="invalid id or name"):
        _map(_completion(tool_calls=[_tool_call("call_x", "Bad-Name", "{}")]))
    custom = {"id": "call_c", "type": "custom", "custom": {"name": "x", "input": "y"}}
    with pytest.raises(OutputValidationError, match="not a function call"):
        _map(_completion(tool_calls=[custom]))


def test_ut05_26_no_choices_raises() -> None:
    """UT05-26 a response without choices raises OutputValidationError."""
    body = _completion(content="x")
    body["choices"] = []
    with pytest.raises(OutputValidationError, match="no choices"):
        _map(body)


# --- Adapter response bounds (program ruling) --------------------------------------------------


def test_ut05_25_text_bounds_exact() -> None:
    """UT05-25 200,000 chars kept verbatim; 200,001 cut to 200,000 ending in the marker."""
    exact = "a" * base.MAX_RESPONSE_PART_CHARS
    assert _map(_completion(content=exact)).text == exact
    with capture_logs() as logs:
        cut = _map(_completion(content=exact + "b")).text
    assert len(cut) == base.MAX_RESPONSE_PART_CHARS
    assert cut.endswith(base.TRUNCATION_MARKER)
    assert cut[: -len(base.TRUNCATION_MARKER)] == exact[: len(cut) - len(base.TRUNCATION_MARKER)]
    event = next(e for e in logs if e["event"] == "harness.llm.response_truncated")
    assert event == {
        "event": "harness.llm.response_truncated",
        "component": "harness.llm",
        "client": "local-30b",
        "field": "text",
        "length": base.MAX_RESPONSE_PART_CHARS + 1,
        "log_level": "warning",
    }


def test_ut05_25_text_over_hard_limit_raises() -> None:
    """UT05-25 text over MAX_RESPONSE_TEXT_CHARS (1,000,000) raises OutputValidationError."""
    assert base.MAX_RESPONSE_TEXT_CHARS == 1_000_000
    with pytest.raises(OutputValidationError, match="response text exceeds limit"):
        _map(_completion(content="a" * (base.MAX_RESPONSE_TEXT_CHARS + 1)))


def test_ut05_25_tool_call_count_bounds() -> None:
    """UT05-25 64 tool calls map; 65 raise OutputValidationError."""
    calls = [_tool_call(f"call_{i}", "run_sql", "{}") for i in range(base.MAX_TOOL_CALLS)]
    assert len(_map(_completion(tool_calls=calls)).tool_calls) == 64
    calls.append(_tool_call("call_extra", "run_sql", "{}"))
    with pytest.raises(OutputValidationError, match="more than 64 tool calls"):
        _map(_completion(tool_calls=calls))


# --- UT05-27: thinking with a schema needs a reasoning parser ----------------------------------


def test_ut05_27_thinking_schema_without_parser_raises() -> None:
    """UT05-27 thinking on + schema + no reasoning parser raises ConfigError at build."""
    client = OpenAICompatClient(_cfg("vllm", reasoning_parser=None))
    with pytest.raises(ConfigError, match="reasoning parser"):
        client._build_params(_request())
    assert "response_format" in client._build_params(_request(thinking="off"))
    no_schema = _request(response_schema=None, response_schema_name=None)
    assert "response_format" not in client._build_params(no_schema)


# --- Construction preconditions ----------------------------------------------------------------


def test_ut05_24_construction_attributes_and_key(fake_keyring: MemoryKeyring) -> None:
    """UT05-24 name, cfg, server; key resolved once to SecretStr; EMPTY without a key."""
    fake_keyring.store[("herness", "vllm_key")] = _KEY_VALUE
    client = OpenAICompatClient(_cfg(api_key="secret:vllm_key"))
    assert (client.name, client.server) == ("local-30b", "vllm")
    assert client._api_key.get_secret_value() == _KEY_VALUE
    assert _KEY_VALUE not in repr(client.__dict__)
    assert OpenAICompatClient(_cfg())._api_key.get_secret_value() == "EMPTY"


def test_ut05_24_wrong_kind_raises() -> None:
    """UT05-24 a non-openai_compat config raises ConfigError."""
    cfg = ClientConfig(
        name="claude",
        kind="anthropic",
        model="claude-opus-5-5",
        context_window=200_000,
        max_output_tokens=8192,
        tokenizer="anthropic",
        max_concurrency=4,
        thinking_mode="adaptive_always",
        price_per_mtok=PricePerMTok(
            input=Decimal(1), output=Decimal(1), cache_read=_ZERO, cache_write=_ZERO
        ),
    )
    with pytest.raises(ConfigError, match="not an openai_compat client"):
        OpenAICompatClient(cfg)


def _off_network_cfg() -> ClientConfig:
    return _cfg("openai", base_url="https://llm.example.invalid/v1", off_network=True)


def _stub_config(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    stub = SimpleNamespace(
        profile="hybrid", security=SimpleNamespace(egress=SimpleNamespace(enabled=enabled))
    )
    monkeypatch.setattr(openai_compat, "get_config", lambda: stub)


def test_ut05_24_off_network_needs_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-24 an off-network client with egress disabled raises ConfigError naming the profile."""
    _stub_config(monkeypatch, enabled=False)
    with pytest.raises(ConfigError) as info:
        OpenAICompatClient(_off_network_cfg())
    assert info.value.message == (
        "client local-30b is off-network but egress is disabled in profile hybrid"
    )
    _stub_config(monkeypatch, enabled=True)
    assert OpenAICompatClient(_off_network_cfg()).server == "openai"


def test_ut05_24_registered_as_llm_client() -> None:
    """UT05-24 the class registers as llm_client openai_compat."""
    module = importlib.reload(openai_compat)
    assert registry.get("llm_client", "openai_compat") is module.OpenAICompatClient


# --- acomplete / complete ----------------------------------------------------------------------


def test_ut05_25_acomplete_round_trip(server: _FakeServer, fake_keyring: MemoryKeyring) -> None:
    """UT05-25 acomplete sends the built params once with the key only in Authorization."""
    fake_keyring.store[("herness", "vllm_key")] = _KEY_VALUE
    client = OpenAICompatClient(_cfg(api_key="secret:vllm_key"))
    server.respond(200, _completion(content='{"answer": "1"}'))
    req = _request()
    resp = asyncio.run(client.acomplete(req))
    assert resp.parsed == {"answer": "1"}
    assert resp.latency_ms >= 0
    (sent,) = server.requests
    assert str(sent.url) == "http://127.0.0.1:8000/v1/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {_KEY_VALUE}"
    expected = {k: v for k, v in client._build_params(req).items() if k != "extra_body"}
    expected.update(client._build_params(req)["extra_body"])
    assert server.bodies[0] == json.loads(json.dumps(expected))
    assert _KEY_VALUE not in sent.content.decode()


def test_ut05_25_complete_outside_loop(server: _FakeServer) -> None:
    """UT05-25 complete runs acomplete in its own loop; each call builds its own SDK client."""
    server.respond(200, _completion(content="one"))
    server.respond(200, _completion(content="two"))
    client = OpenAICompatClient(_cfg())
    assert client.complete(_request()).text == "one"
    assert client.complete(_request()).text == "two"


def test_ut05_25_wrong_client_raises() -> None:
    """UT05-25 a request for another client raises ConfigError before any call."""
    client = OpenAICompatClient(_cfg())
    with pytest.raises(ConfigError, match="sent to client local-30b"):
        asyncio.run(client.acomplete(_request(client="other")))


def test_ut05_30_complete_in_running_loop_raises() -> None:
    """UT05-30 complete() inside a running event loop raises RuntimeError."""
    client = OpenAICompatClient(_cfg())

    async def call() -> None:
        client.complete(_request())

    with pytest.raises(RuntimeError, match=r"running event loop; use acomplete\(\)"):
        asyncio.run(call())


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (429, {"retry-after": "3"}, RateLimited),
        (500, {}, ModelUnavailable),
        (401, {}, AuthError),
        (400, {}, ConfigError),
    ],
)
def test_ut05_28_acomplete_translates_errors_without_retry(
    server: _FakeServer,
    fake_keyring: MemoryKeyring,
    herness_cfg: c.HernessConfig,
    status: int,
    headers: dict[str, str],
    expected: type[Exception],
) -> None:
    """UT05-28 provider errors are translated, sent once (max_retries=0), key never leaks."""
    del herness_cfg
    fake_keyring.store[("herness", "vllm_key")] = _KEY_VALUE
    client = OpenAICompatClient(_cfg(api_key="secret:vllm_key"))
    server.respond(status, {"error": {"message": f"bad key {_KEY_VALUE}"}}, **headers)
    with capture_logs() as logs, pytest.raises(expected) as info:
        asyncio.run(client.acomplete(_request()))
    assert len(server.requests) == 1
    assert _KEY_VALUE not in str(info.value)
    assert _KEY_VALUE not in repr(info.value.context)  # type: ignore[attr-defined]
    assert _KEY_VALUE not in json.dumps(logs, default=str)


def test_ut05_28_connection_error_is_model_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-28 a transport failure maps to ModelUnavailable."""

    def fail(request: httpx2.Request) -> httpx2.Response:
        msg = "refused"
        raise httpx2.ConnectError(msg, request=request)

    _serve(monkeypatch, fail)
    with pytest.raises(ModelUnavailable):
        asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request()))


# --- Off-network: the egress guard's HTTP client only (TH05-13) --------------------------------


class _FakeGuard:
    def __init__(self, handler: Any) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._handler = handler

    def async_http_client(
        self, purpose: str, payload_class: str, *, run_id: str, task_id: str | None
    ) -> httpx2.AsyncClient:
        self.calls.append((purpose, payload_class, run_id, task_id))
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self._handler))


def test_ut05_25_off_network_uses_guard_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-25 off-network calls go through the guard's client with the role's purpose."""
    _stub_config(monkeypatch, enabled=True)
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_completion(content="ok"))

    guard = _FakeGuard(handler)
    monkeypatch.setattr(openai_compat, "get_guard", lambda: guard)
    resp = asyncio.run(OpenAICompatClient(_off_network_cfg()).acomplete(_request()))
    assert resp.text == "ok"
    assert guard.calls == [("reasoning_final", "aggregated_evidence", "run_1", "task_1")]
    assert str(seen[0].url) == "https://llm.example.invalid/v1/chat/completions"


def test_ut05_25_off_network_egress_block_not_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-25 an EgressBlocked raised in the guard's transport surfaces as EgressBlocked."""
    _stub_config(monkeypatch, enabled=True)

    def handler(_request: httpx2.Request) -> httpx2.Response:
        msg = "egress refused"
        raise EgressBlocked(msg, reason="purpose")

    monkeypatch.setattr(openai_compat, "get_guard", lambda: _FakeGuard(handler))
    with pytest.raises(EgressBlocked):
        asyncio.run(OpenAICompatClient(_off_network_cfg()).acomplete(_request()))


def test_ut05_25_off_network_guard_without_client_fails_closed(
    monkeypatch: pytest.MonkeyPatch, server: _FakeServer
) -> None:
    """UT05-25 a guard without async_http_client raises ConfigError and sends nothing."""
    _stub_config(monkeypatch, enabled=True)
    monkeypatch.setattr(openai_compat, "get_guard", object)
    with pytest.raises(ConfigError, match="egress guard has no HTTP client"):
        asyncio.run(OpenAICompatClient(_off_network_cfg()).acomplete(_request()))
    assert server.requests == []


# --- Fix round 1: malformed shapes, byte cap, reasoning and argument bounds, deadline -------------

_DELETE = object()


def _set(path: tuple[Any, ...], value: Any) -> Any:
    def mutate(body: dict[str, Any]) -> None:
        node: Any = body
        for key in path[:-1]:
            node = node[key]
        if value is _DELETE:
            del node[path[-1]]
        else:
            node[path[-1]] = value

    return mutate


_ARGS = ("choices", 0, "message", "tool_calls", 0)
_MALFORMED = {
    "model_missing": _set(("model",), _DELETE),
    "model_int": _set(("model",), 5),
    "usage_no_prompt_tokens": _set(("usage",), {"completion_tokens": 1, "total_tokens": 1}),
    "arguments_null": _set((*_ARGS, "function", "arguments"), None),
    "tool_call_id_null": _set((*_ARGS, "id"), None),
    "message_null": _set(("choices", 0, "message"), None),
    "content_list": _set(("choices", 0, "message", "content"), [{"type": "text", "text": "x"}]),
    "id_int": _set(("id",), 5),
}


def _malformed_body(name: str) -> dict[str, Any]:
    body = _completion(content="x", tool_calls=[_tool_call("call_1", "run_sql", "{}")])
    _MALFORMED[name](body)
    return body


@pytest.mark.parametrize("name", sorted(_MALFORMED))
def test_ut05_26_malformed_response_is_output_validation_error(name: str) -> None:
    """UT05-26 malformed shapes raise OutputValidationError("malformed response"), no data."""
    with pytest.raises(OutputValidationError) as info:
        _map(_malformed_body(name))
    assert str(info.value) == "malformed response"
    assert info.value.__suppress_context__


@pytest.mark.parametrize("name", sorted(_MALFORMED))
def test_ut05_26_malformed_response_via_acomplete(server: _FakeServer, name: str) -> None:
    """UT05-26 the same shapes sent by a server surface from acomplete as malformed."""
    server.respond(200, _malformed_body(name))
    with pytest.raises(OutputValidationError, match=r"^malformed response$"):
        asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request()))


def test_ut05_25_exactly_hard_limit_is_truncated_and_order() -> None:
    """UT05-25 exactly 1,000,000 chars are truncated; 1,000,001 + 65 calls fail on the text."""
    resp = _map(_completion(content="a" * base.MAX_RESPONSE_TEXT_CHARS))
    assert len(resp.text) == base.MAX_RESPONSE_PART_CHARS
    calls = [_tool_call(f"call_{i}", "run_sql", "{}") for i in range(base.MAX_TOOL_CALLS + 1)]
    body = _completion(content="a" * (base.MAX_RESPONSE_TEXT_CHARS + 1), tool_calls=calls)
    with pytest.raises(OutputValidationError, match=r"^response text exceeds limit$"):
        _map(body)


def test_ut05_25_reasoning_bounds() -> None:
    """UT05-25 reasoning is bounded like text: 200,000 kept, 200,001 cut, > 1,000,000 raises."""
    exact = "r" * base.MAX_RESPONSE_PART_CHARS
    assert _map(_completion(content="x", reasoning=exact)).reasoning[0].text == exact
    with capture_logs() as logs:
        cut = _map(_completion(content="x", reasoning=exact + "s")).reasoning[0].text
    assert len(cut) == base.MAX_RESPONSE_PART_CHARS
    assert cut.endswith(base.TRUNCATION_MARKER)
    event = next(e for e in logs if e["event"] == "harness.llm.response_truncated")
    assert event["field"] == "reasoning"
    too_long = "r" * (base.MAX_RESPONSE_TEXT_CHARS + 1)
    with pytest.raises(OutputValidationError, match="response text exceeds limit"):
        _map(_completion(content="x", reasoning_content=too_long))


def test_ut05_26_tool_arguments_size_bound() -> None:
    """UT05-26 arguments of exactly the cap decode; one more char raises before decoding."""
    limit = base.MAX_RESPONSE_TEXT_CHARS
    exact = '{"a":"' + "x" * (limit - 8) + '"}'
    assert len(exact) == limit
    resp = _map(_completion(tool_calls=[_tool_call("call_1", "run_sql", exact)]))
    assert len(resp.tool_calls[0].arguments["a"]) == limit - 8
    over = _completion(tool_calls=[_tool_call("call_1", "run_sql", exact + " ")])
    with pytest.raises(OutputValidationError, match="tool call call_1 arguments exceed limit"):
        _map(over)


def _spy_clients(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Any, ...]] | None = None
) -> list[httpx2.AsyncClient]:
    """Record every client ``egress.aloopback_http_client`` builds for the adapter."""
    made: list[httpx2.AsyncClient] = []
    real = openai_compat.aloopback_http_client

    def spy(base_url: str, **kwargs: Any) -> httpx2.AsyncClient:
        if calls is not None:
            calls.append((base_url, kwargs))
        made.append(real(base_url, **kwargs))
        return made[-1]

    monkeypatch.setattr(openai_compat, "aloopback_http_client", spy)
    return made


def test_ut05_25_byte_cap_while_reading(
    server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-25 an on-network body of cap bytes maps; cap + 1 raises and the client is closed.

    The body is streamed without Content-Length, so the loopback counter decides while reading.
    """
    made = _spy_clients(monkeypatch)
    server.streamed = True
    body = _completion(content="ok")
    size = len(json.dumps(body).encode())
    monkeypatch.setattr(openai_compat, "MAX_RESPONSE_BYTES", size)
    server.respond(200, body)
    assert asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request())).text == "ok"
    assert server.requests[0].headers["accept-encoding"] == "identity"
    monkeypatch.setattr(openai_compat, "MAX_RESPONSE_BYTES", size - 1)
    server.respond(200, body)
    with pytest.raises(OutputValidationError, match=r"^response body exceeds limit$"):
        asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request()))
    assert len(made) == 2
    assert all(client.is_closed for client in made)


@pytest.mark.parametrize(
    "headers",
    [{"content-length": str(52_428_800 + 1)}, {"content-encoding": "br"}],
)
def test_ut05_25_byte_cap_rejects_headers_up_front(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    """UT05-25 an over-cap Content-Length or an unsupported encoding is refused unread."""
    read: list[bytes] = []

    class _Body(httpx2.AsyncByteStream):
        async def __aiter__(self) -> Any:
            read.append(b"chunk")
            yield b"{}"

    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers=headers, stream=_Body(), request=request)

    _serve(monkeypatch, handle)
    assert openai_compat.MAX_RESPONSE_BYTES == 52_428_800
    with pytest.raises(OutputValidationError, match=r"^response body exceeds limit$") as info:
        asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request()))
    assert read == []
    # The SDK lets the loopback refusal through unwrapped (not an httpx2.RequestError).
    assert type(info.value.__cause__) is EgressBlocked


def test_ut05_25_whole_call_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-25 a server slower than timeout_s ends in ModelUnavailable like APITimeoutError."""

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200, json=_completion(content="late"), request=request)

    _serve(monkeypatch, slow)
    with pytest.raises(ModelUnavailable) as info:
        asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request(timeout_s=0.05)))
    assert info.value.message == "openai call failed: APITimeoutError"
    assert info.value.__cause__ is None


def test_ut05_25_http_client_closed_when_sdk_init_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-25 the HTTP client is closed even if AsyncOpenAI(...) raises."""
    made = _spy_clients(monkeypatch)

    def broken(**_kwargs: Any) -> None:
        msg = "init failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(openai_compat.openai, "AsyncOpenAI", broken)
    with pytest.raises(RuntimeError, match="init failed"):
        asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request()))
    assert len(made) == 1
    assert made[0].is_closed


def test_ut05_25_off_network_accepts_legacy_httpx_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-25 a guard returning a legacy httpx.AsyncClient still carries the call."""
    import httpx  # noqa: PLC0415 - the legacy client type the guard may return

    _stub_config(monkeypatch, enabled=True)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(content="legacy"))

    class _LegacyGuard:
        def async_http_client(self, *_args: Any, **_kwargs: Any) -> Any:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: TID251

    monkeypatch.setattr(openai_compat, "get_guard", _LegacyGuard)
    resp = asyncio.run(OpenAICompatClient(_off_network_cfg()).acomplete(_request()))
    assert resp.text == "legacy"


def test_ut05_25_byte_cap_counts_streamed_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-25 a chunked body without Content-Length stops at the chunk that passes the cap."""
    sent: list[int] = []

    class _Chunks(httpx2.AsyncByteStream):
        async def __aiter__(self) -> Any:
            for _ in range(10):
                sent.append(1)
                yield b"x" * 100

    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=_Chunks(), request=request)

    _serve(monkeypatch, handle)
    monkeypatch.setattr(openai_compat, "MAX_RESPONSE_BYTES", 250)
    with pytest.raises(OutputValidationError, match=r"^response body exceeds limit$"):
        asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request()))
    assert len(sent) == 3


def test_ut05_25_on_network_uses_egress_loopback_client(
    server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-25 on-network calls use egress.aloopback_http_client: base URL, deadline, no bearer.

    The pool transport is built with retries=0, the SDK's Authorization header carries the key
    (TH05-15) and the client is closed after the call.
    """
    calls: list[tuple[Any, ...]] = []
    made = _spy_clients(monkeypatch, calls)
    built = _serve(monkeypatch, server.handle)
    server.respond(200, _completion(content="ok"))
    cfg = _cfg()
    assert asyncio.run(OpenAICompatClient(cfg).acomplete(_request(timeout_s=7.5))).text == "ok"
    assert calls == [
        (cfg.base_url, {"timeout_s": 7.5, "bearer": None, "max_response_bytes": 52_428_800})
    ]
    assert built == [{"retries": 0}]
    assert server.requests[0].headers["authorization"] == "Bearer EMPTY"
    assert server.requests[0].headers["accept-encoding"] == "identity"
    assert made[0].is_closed


def test_st05_20_redirect_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    """ST05-20 a redirect from the model server is not followed: one request, then an error."""
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        headers = {"location": "http://127.0.0.1:8000/v1/elsewhere"}
        return httpx2.Response(307, headers=headers, json={}, request=request)

    _serve(monkeypatch, handle)
    with pytest.raises(ModelUnavailable):
        asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request()))
    assert [str(r.url) for r in seen] == ["http://127.0.0.1:8000/v1/chat/completions"]


class _Streamed(httpx2.AsyncByteStream):
    """A streamed (not pre-read) body, so the loopback transport counts it while it is read."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aiter__(self) -> Any:
        yield self._body


def test_st05_20_gzip_body_counted_after_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """ST05-20 a gzip body is decoded by the loopback client and its decoded bytes are capped."""
    raw = json.dumps(_completion(content="z" * 2_000)).encode()
    packed = gzip.compress(raw)
    assert len(packed) < 500 < len(raw)

    def handle(request: httpx2.Request) -> httpx2.Response:
        headers = {"content-encoding": "gzip"}
        return httpx2.Response(200, headers=headers, stream=_Streamed(packed), request=request)

    _serve(monkeypatch, handle)
    assert len(asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request())).text) == 2_000
    monkeypatch.setattr(openai_compat, "MAX_RESPONSE_BYTES", 500)
    with pytest.raises(OutputValidationError, match=r"^response body exceeds limit$"):
        asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request()))


@pytest.mark.parametrize("reason", ["response_too_large", "unsupported_encoding"])
def test_st05_20_wrapped_body_refusal_is_output_validation_error(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """ST05-20 a body refusal the SDK wraps (APIConnectionError cause) still maps the same."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        msg = "wrapped"
        raise httpx2.ReadError(msg, request=request) from EgressBlocked("refused", reason=reason)

    _serve(monkeypatch, handle)
    with pytest.raises(OutputValidationError, match=r"^response body exceeds limit$") as info:
        asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request()))
    assert info.value.context["client"] == "local-30b"


@pytest.mark.parametrize("wrapped", [False, True])
def test_ut05_25_other_egress_block_propagates(
    monkeypatch: pytest.MonkeyPatch, *, wrapped: bool
) -> None:
    """UT05-25 an EgressBlocked with another reason surfaces unchanged, direct or wrapped."""
    blocked = EgressBlocked("refused", reason="not_loopback")

    def handle(request: httpx2.Request) -> httpx2.Response:
        if wrapped:
            msg = "wrapped"
            raise httpx2.ReadError(msg, request=request) from blocked
        raise blocked

    _serve(monkeypatch, handle)
    with pytest.raises(EgressBlocked) as info:
        asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request()))
    assert info.value is blocked


# --- UT05-29: astream (U05-26) ------------------------------------------------------------------

_USAGE = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}


def _chunk(delta: dict[str, Any] | None = None, *, finish: str | None = None, **top: Any) -> Any:
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "qwen3-30b",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    body.update(top)
    return body


def _frag(index: Any, arguments: Any, call_id: str | None = None, name: str | None = None) -> Any:
    frag: dict[str, Any] = {"index": index, "function": {"arguments": arguments}}
    if call_id is not None:
        frag["id"] = call_id
        frag["type"] = "function"
        frag["function"]["name"] = name
    return frag


def _sse(chunks: list[Any], *, done: bool = True) -> list[bytes]:
    events = [f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks]
    return [*events, b"data: [DONE]\n\n"] if done else events


class _SseBody(httpx2.AsyncByteStream):
    """A real streamed SSE body, one event per read; records reads and closing."""

    def __init__(self, events: list[bytes], delay_s: float = 0.0) -> None:
        self.events = events
        self.delay_s = delay_s
        self.sent = 0
        self.closed = False

    async def __aiter__(self) -> Any:
        for event in self.events:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            self.sent += 1
            yield event

    async def aclose(self) -> None:
        self.closed = True


class _SseServer:
    """Answers each request with the next queued SSE body or JSON completion."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.queue: list[_SseBody | tuple[int, dict[str, Any]]] = []
        self.streams: list[_SseBody] = []

    def stream(self, chunks: list[Any], *, done: bool = True, delay_s: float = 0.0) -> _SseBody:
        body = _SseBody(_sse(chunks, done=done), delay_s)
        self.queue.append(body)
        return body

    def respond(self, status: int, body: dict[str, Any]) -> None:
        self.queue.append((status, body))

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        self.bodies.append(json.loads(request.content))
        item = self.queue.pop(0)
        if isinstance(item, tuple):
            return httpx2.Response(item[0], json=item[1], request=request)
        self.streams.append(item)
        headers = {"content-type": "text/event-stream"}
        return httpx2.Response(200, headers=headers, stream=item, request=request)


@pytest.fixture
def sse(monkeypatch: pytest.MonkeyPatch) -> _SseServer:
    fake = _SseServer()
    _serve(monkeypatch, fake.handle)
    return fake


def _events(req: LLMRequest | None = None, cfg: ClientConfig | None = None) -> list[Any]:
    client = OpenAICompatClient(cfg or _cfg())

    async def collect() -> list[Any]:
        return [event async for event in client.astream(req or _request())]

    return asyncio.run(collect())


def _events_until_error(req: LLMRequest | None = None) -> tuple[list[Any], Any]:
    """The events a consumer saw before the stream raised, and the exception."""
    got: list[Any] = []
    client = OpenAICompatClient(_cfg())

    async def collect() -> None:
        async for event in client.astream(req or _request()):
            got.append(event)  # noqa: PERF401 - keeps the events seen before the stream raises

    with pytest.raises(Exception) as info:  # noqa: PT011 - each caller checks the type
        asyncio.run(collect())
    return got, info.value


def _stream_chunks() -> list[Any]:
    return [
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"reasoning_content": "think "}),
        _chunk({"reasoning_content": "more"}),
        _chunk({"content": "Hel"}),
        _chunk({"content": "lo"}),
        _chunk({"tool_calls": [_frag(0, '{"sql": ', "call_a", "run_sql")]}),
        _chunk({"tool_calls": [_frag(0, '"select 1"}')]}),
        _chunk({"tool_calls": [_frag(1, "", "call_b", "list_tables")]}),
        _chunk({}, finish="tool_calls"),
        _chunk(choices=[], usage=_USAGE),
    ]


def _same_completion() -> dict[str, Any]:
    return _completion(
        content="Hello",
        reasoning_content="think more",
        finish_reason="tool_calls",
        tool_calls=[
            _tool_call("call_a", "run_sql", '{"sql": "select 1"}'),
            _tool_call("call_b", "list_tables", ""),
        ],
    )


def _no_latency(resp: Any) -> Any:
    return resp.model_copy(update={"latency_ms": 0})


def test_ut05_29_astream_deltas_in_order_and_done_equals_acomplete(sse: _SseServer) -> None:
    """UT05-29 deltas in order; exactly one Done, last, equal to acomplete on the same payload."""
    sse.stream(_stream_chunks())
    sse.respond(200, _same_completion())
    events = _events()
    expected = asyncio.run(OpenAICompatClient(_cfg()).acomplete(_request()))
    assert events[:-1] == [
        base.TextDelta("Hel"),
        base.TextDelta("lo"),
        base.ToolCallDelta("call_a", "run_sql", '{"sql": '),
        base.ToolCallDelta("call_a", "run_sql", '"select 1"}'),
        base.ToolCallDelta("call_b", "list_tables", ""),
    ]
    done = events[-1]
    assert isinstance(done, base.Done)
    assert sum(isinstance(event, base.Done) for event in events) == 1
    assert _no_latency(done.response) == _no_latency(expected)
    assert done.response.reasoning == [ReasoningPart(provider="vllm", text="think more")]
    assert done.response.usage.input_tokens == 120
    assert done.response.stop_reason == "tool_use"
    stream_body, plain_body = sse.bodies
    assert stream_body.pop("stream") is True
    assert stream_body.pop("stream_options") == {"include_usage": True}
    assert stream_body == plain_body
    assert sse.streams[0].closed


def test_ut05_29_astream_reasoning_key_and_stream_without_done_marker(sse: _SseServer) -> None:
    """UT05-29 delta.reasoning is buffered (not yielded); a stream without [DONE] still ends."""
    sse.stream([_chunk({"reasoning": "r"}), _chunk({"content": "x"}, finish="stop")], done=False)
    events = _events()
    assert events[0] == base.TextDelta("x")
    resp = events[-1].response
    assert (resp.text, resp.reasoning[0].text, resp.stop_reason) == ("x", "r", "end_turn")
    assert resp.usage.input_tokens == 0


def test_ut05_29_astream_truncates_at_done_only(sse: _SseServer) -> None:
    """UT05-29 deltas stay raw; the Done text is cut to 200,000 with the marker and a WARNING."""
    limit = base.MAX_RESPONSE_PART_CHARS
    sse.stream([_chunk({"content": "a" * limit}), _chunk({"content": "b"}, finish="stop")])
    with capture_logs() as logs:
        events = _events()
    assert [e.text for e in events[:-1]] == ["a" * limit, "b"]
    text = events[-1].response.text
    assert len(text) == limit
    assert text.endswith(base.TRUNCATION_MARKER)
    assert any(e["event"] == "harness.llm.response_truncated" for e in logs)


@pytest.mark.parametrize("field", ["content", "reasoning_content"])
def test_ut05_29_astream_text_cap_while_consuming(
    sse: _SseServer, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """UT05-29 a buffer passing 1,000,000 chars raises at once: no later delta, no Done."""
    made = _spy_clients(monkeypatch)
    limit = base.MAX_RESPONSE_TEXT_CHARS
    sse.stream([_chunk({field: "a" * limit}), _chunk({field: "b"}), _chunk({"content": "late"})])
    got, exc = _events_until_error()
    assert isinstance(exc, OutputValidationError)
    assert str(exc) == "response text exceeds limit"
    assert exc.context["client"] == "local-30b"
    assert got == ([base.TextDelta("a" * limit)] if field == "content" else [])
    assert made[0].is_closed
    assert sse.streams[0].closed


def test_ut05_29_astream_tool_call_count_cap(sse: _SseServer) -> None:
    """UT05-29 64 tool-call indices map; the 65th index raises as soon as it appears."""
    calls = [_frag(i, "{}", f"call_{i}", "run_sql") for i in range(base.MAX_TOOL_CALLS)]
    sse.stream([_chunk({"tool_calls": [call]}) for call in calls])
    events = _events()
    assert len(events[-1].response.tool_calls) == 64
    extra = _frag(base.MAX_TOOL_CALLS, "{}", "call_x", "run_sql")
    chunks = [_chunk({"tool_calls": [call]}) for call in [*calls, extra]]
    sse.stream([*chunks, _chunk({"content": "late"})])
    got, exc = _events_until_error()
    assert isinstance(exc, OutputValidationError)
    assert str(exc) == "response has more than 64 tool calls"
    assert len(got) == 64


def test_ut05_29_astream_tool_arguments_cap(sse: _SseServer) -> None:
    """UT05-29 one call's arguments passing 1,000,000 chars raise while streaming."""
    limit = base.MAX_RESPONSE_TEXT_CHARS
    sse.stream(
        [
            _chunk({"tool_calls": [_frag(0, "x" * limit, "call_a", "run_sql")]}),
            _chunk({"tool_calls": [_frag(0, "y")]}),
        ]
    )
    got, exc = _events_until_error()
    assert isinstance(exc, OutputValidationError)
    assert str(exc) == "tool call call_a arguments exceed limit"
    assert len(got) == 1


_BAD_CHUNKS = {
    "index_negative": _chunk({"tool_calls": [_frag(-1, "{}", "call_a", "run_sql")]}),
    # The SDK coerces "0" and true to ints like any lax pydantic field; these it cannot coerce.
    "index_null": _chunk({"tool_calls": [_frag(None, "{}", "call_a", "run_sql")]}),
    "index_fraction": _chunk({"tool_calls": [_frag(0.5, "{}", "call_a", "run_sql")]}),
    "index_word": _chunk({"tool_calls": [_frag("first", "{}", "call_a", "run_sql")]}),
    "first_fragment_no_id": _chunk({"tool_calls": [_frag(0, "{}")]}),
    "arguments_int": _chunk({"tool_calls": [_frag(0, 5, "call_a", "run_sql")]}),
    "content_list": _chunk({"content": ["x"]}),
    "delta_null": _chunk(choices=[{"index": 0, "delta": None, "finish_reason": None}]),
}


@pytest.mark.parametrize("name", sorted(_BAD_CHUNKS))
def test_ut05_29_astream_malformed_chunk(sse: _SseServer, name: str) -> None:
    """UT05-29 a malformed chunk ends the stream with OutputValidationError("malformed ...")."""
    sse.stream([_chunk({"content": "ok"}), _BAD_CHUNKS[name]])
    got, exc = _events_until_error()
    assert isinstance(exc, OutputValidationError)
    assert str(exc) == "malformed response"
    assert got == [base.TextDelta("ok")]


def test_ut05_29_astream_empty_stream_is_malformed(sse: _SseServer) -> None:
    """UT05-29 a stream with no chunk at all yields no Done: the mapping finds no model."""
    sse.stream([])
    got, exc = _events_until_error()
    assert (got, str(exc)) == ([], "malformed response")


def test_ut05_29_astream_error_event_mid_stream(sse: _SseServer) -> None:
    """UT05-29 an error mid-stream propagates translated (U05-30); no Done is yielded."""
    sse.stream([_chunk({"content": "part"}), {"error": {"message": "boom"}}])
    got, exc = _events_until_error()
    assert isinstance(exc, ModelUnavailable)
    assert got == [base.TextDelta("part")]


def test_ut05_29_astream_status_error_translated(sse: _SseServer) -> None:
    """UT05-29 an HTTP error at create() is translated like acomplete; nothing is yielded."""
    sse.respond(401, {"error": {"message": "no"}})
    got, exc = _events_until_error()
    assert isinstance(exc, AuthError)
    assert got == []


def test_ut05_29_astream_transport_error_mid_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-29 a transport failure while reading the stream is ModelUnavailable, no Done."""

    class _Broken(httpx2.AsyncByteStream):
        async def __aiter__(self) -> Any:
            yield _sse([_chunk({"content": "part"})], done=False)[0]
            msg = "reset"
            raise httpx2.ReadError(msg)

    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=_Broken(), request=request)

    _serve(monkeypatch, handle)
    got, exc = _events_until_error()
    assert isinstance(exc, ModelUnavailable)
    assert got == [base.TextDelta("part")]


def test_ut05_29_astream_wrong_client_raises() -> None:
    """UT05-29 a request for another client raises ConfigError before any call."""
    with pytest.raises(ConfigError, match="sent to client local-30b"):
        _events(_request(client="other"))


def test_ut05_29_astream_deadline_per_chunk(sse: _SseServer) -> None:
    """UT05-29 a stream slower than timeout_s ends in ModelUnavailable like APITimeoutError."""
    sse.stream([_chunk({"content": "a"}), _chunk({"content": "b"})], delay_s=0.2)
    got, exc = _events_until_error(_request(timeout_s=0.3))
    assert isinstance(exc, ModelUnavailable)
    assert exc.message == "openai call failed: APITimeoutError"
    assert exc.__cause__ is None
    assert got == [base.TextDelta("a")]
    assert sse.streams[0].closed


def test_ut05_29_astream_deadline_counts_consumer_time_without_cancelling_it(
    sse: _SseServer,
) -> None:
    """UT05-29 consumer time counts toward the deadline but the consumer is never cancelled."""
    sse.stream([_chunk({"content": "a"}), _chunk({"content": "b"})])
    client = OpenAICompatClient(_cfg())
    slept: list[str] = []

    async def consume() -> None:
        async for _event in client.astream(_request(timeout_s=0.2)):
            await asyncio.sleep(0.3)  # would be cancelled if a timeout spanned the yield
            slept.append("done")

    with pytest.raises(ModelUnavailable, match="APITimeoutError"):
        asyncio.run(consume())
    assert slept == ["done"]


def test_ut05_29_astream_consumer_aclose_closes_everything(
    sse: _SseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-29 a consumer that stops early (aclose) closes the SDK stream and the HTTP client."""
    made = _spy_clients(monkeypatch)
    sse.stream([_chunk({"content": "a"}), _chunk({"content": "b"})])
    client = OpenAICompatClient(_cfg())

    async def first() -> Any:
        stream = client.astream(_request())
        event = await anext(stream)
        await stream.aclose()  # type: ignore[attr-defined]
        return event

    assert asyncio.run(first()) == base.TextDelta("a")
    assert made[0].is_closed
    assert sse.streams[0].closed


def test_ut05_29_astream_byte_cap_while_streaming(
    sse: _SseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-29 the loopback byte cap applies to the streamed body while it is read (m2)."""
    made = _spy_clients(monkeypatch)
    monkeypatch.setattr(openai_compat, "MAX_RESPONSE_BYTES", 400)
    sse.stream([_chunk({"content": "x" * 100}) for _ in range(10)])
    got, exc = _events_until_error()
    assert isinstance(exc, OutputValidationError)
    assert str(exc) == "response body exceeds limit"
    assert 0 < len(got) < 10
    assert sse.streams[0].sent < 10
    assert made[0].is_closed


def test_ut05_29_astream_is_stream_capable() -> None:
    """UT05-29 the adapter satisfies the StreamCapable protocol."""
    assert isinstance(OpenAICompatClient(_cfg()), base.StreamCapable)
