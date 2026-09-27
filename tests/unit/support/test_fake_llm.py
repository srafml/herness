"""Tests for tests.support.fake_llm (T11-23): FakeLLMClient, respx_router, FakeLLMServer.

UT11-68 (U11-42 half; the U11-40 half is in tests/unit/eval/test_scripted_client.py), UT11-71
(including the acceptance check that the spec 05 adapters run against ``respx_router``),
UT11-73 and UT11-74.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from pydantic import SecretStr
from tests.support.egress_harness import load, make_guard
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_llm import FakeLLMClient, FakeLLMServer, respx_router

from herness.core import config as c
from herness.core import egress, registry, secrets
from herness.core.errors import ConfigError, RateLimited
from herness.core.types import (
    LLMRequest,
    Message,
    RequestMeta,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
)
from herness.eval.scripted import LLMScript, ScriptBook
from herness.eval.scripted_client import ScriptedLLMClient
from herness.harness.llm.anthropic_client import AnthropicClient
from herness.harness.llm.base import Done, TextDelta
from herness.harness.llm.openai_compat import OpenAICompatClient
from herness.harness.llm.settings import ClientConfig

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "tests" / "fixtures" / "llm_scripts" / "t11_23_two_turn.yaml"
OPENAI = "http://127.0.0.1:8000/v1/chat/completions"
ANTHROPIC = "https://api.anthropic.com/v1/messages"
_TABLE = (
    "query_id=q_00000000000000aa rows=1 shown=1 truncated=no ordered=yes\n"
    "team_name | incidents\n"
    "VARCHAR | BIGINT\n"
    "Payments | 42"
)
_ANSWER = "Payments had 42 incidents"
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_ZERO = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}


def _book(*scripts: dict[str, Any]) -> ScriptBook:
    return ScriptBook(
        [LLMScript.model_validate({**s, "source": f"t#{i}"}) for i, s in enumerate(scripts)]
    )


def _faulty(kind: str, turn: dict[str, Any]) -> ScriptBook:
    return _book({"turns": [turn], "faults": [{"at": 0, "kind": kind}]})


_TOOL_TURN: dict[str, Any] = {"tool_calls": [{"name": "run_sql", "arguments": {"sql": "x"}}]}
_TEXT_TURN: dict[str, Any] = {"final": {"text": "plain words for the reader"}}


def _meta(**over: Any) -> RequestMeta:
    values: dict[str, Any] = {
        "run_id": "run_1",
        "task_id": None,
        "role": "analyst",
        "model_role": "writer",
        "step": 0,
        "request_key": "run_1:0:call",
    }
    return RequestMeta.model_validate(values | over)


def _request(client: str, *extra: Message) -> LLMRequest:
    return LLMRequest(
        client=client,
        messages=[Message(role="user", parts=[TextPart(text="How is Payments?")]), *extra],
        max_output_tokens=512,
        timeout_s=30,
        metadata=_meta(),
    )


def _after_tool(call_id: str) -> list[Message]:
    call = ToolCall(id=call_id, name="run_sql", arguments={"sql": "select 1"})
    return [
        Message(role="assistant", parts=[ToolCallPart(call=call)]),
        Message(role="tool", parts=[ToolResultPart(tool_call_id=call_id, content=_TABLE)]),
    ]


def _openai_msgs(with_result: bool) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "How is Payments?"}]
    if with_result:
        msgs.append({"role": "tool", "tool_call_id": "call_0_0", "content": _TABLE})
    return msgs


def _anthropic_msgs(with_result: bool) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "How is Payments?"}]
    if with_result:
        block = {"type": "tool_result", "tool_use_id": "call_0_0", "content": _TABLE}
        msgs.append({"role": "user", "content": [block]})
    return msgs


# ---------------------------------------------------------------- UT11-68 FakeLLMClient


def test_ut11_68_fake_client_loads_path_and_serves_turns() -> None:
    """UT11-68 FakeLLMClient loads a script path; tool turn then a text turn over the result."""
    client = FakeLLMClient(SCRIPTS)
    assert isinstance(client, ScriptedLLMClient)
    assert client.name == "fake"
    first = client.complete(_request("local-30b"))
    assert first.stop_reason == "tool_use"
    (call,) = first.tool_calls
    assert (call.id, call.name) == ("call_0_0", "run_sql")
    second = client.complete(_request("local-30b", *_after_tool(call.id)))
    assert (second.text, second.stop_reason) == (_ANSWER, "end_turn")
    assert client.book.calls("analyst", "*") == 2


def test_ut11_68_fake_client_keeps_a_given_book_and_resolver() -> None:
    """UT11-68 a ScriptBook is used as is (`book`), and the dedup resolver is honoured."""
    book = _book({"match": {"dedup_key": "dk_7"}, "turns": [{"final": {"text": "keyed"}}]})
    client = FakeLLMClient(book, dedup_key_resolver=lambda task_id: "dk_7" if task_id else None)
    assert client.book is book
    req = _request("x").model_copy(update={"metadata": _meta(task_id="task_1")})
    assert asyncio.run(client.acomplete(req)).text == "keyed"


def test_ut11_68_fake_llm_registered_fixture(fake_llm_registered: type[FakeLLMClient]) -> None:
    """UT11-68 `fake_llm_registered` registers llm_client:fake; the registry reset drops it."""
    assert fake_llm_registered is FakeLLMClient
    assert registry.get("llm_client", "fake") is FakeLLMClient
    registry.reset_registry()  # what the autouse reset does after the test
    with pytest.raises(ConfigError):
        registry.get("llm_client", "fake")


# ---------------------------------------------------------------- UT11-71 respx_router


def _post(client: httpx2.Client, url: str, body: dict[str, Any], **headers: str) -> httpx2.Response:
    return client.post(url, json=body, headers=headers)


def test_ut11_71_openai_shape_over_two_turns() -> None:
    """UT11-71 OpenAI JSON: tool_calls with JSON-text arguments, then content; usage keys."""
    router = respx_router(SCRIPTS)
    with httpx2.Client(transport=router.transport()) as client:
        first = _post(client, OPENAI, {"model": "m1", "messages": _openai_msgs(False)}).json()
        second = _post(client, OPENAI, {"model": "m1", "messages": _openai_msgs(True)}).json()
    choice = first["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    (call,) = choice["message"]["tool_calls"]
    assert call["function"]["name"] == "run_sql"
    assert json.loads(call["function"]["arguments"]) == {
        "sql": "select team_name, incidents from t"
    }
    assert first["model"] == "m1"
    assert set(first["usage"]) >= {"prompt_tokens", "completion_tokens"}
    done = second["choices"][0]
    assert (done["message"]["content"], done["finish_reason"]) == (_ANSWER, "stop")
    assert "tool_calls" not in done["message"]


def test_ut11_71_anthropic_shape_over_two_turns() -> None:
    """UT11-71 Anthropic JSON: tool_use block and stop_reason, then a text block; usage keys."""
    router = respx_router(SCRIPTS)
    with httpx2.Client(transport=router.transport()) as client:
        first = _post(client, ANTHROPIC, {"model": "c", "messages": _anthropic_msgs(False)}).json()
        second = _post(client, ANTHROPIC, {"model": "c", "messages": _anthropic_msgs(True)}).json()
    assert first["stop_reason"] == "tool_use"
    (block,) = first["content"]
    assert (block["type"], block["name"], block["id"]) == ("tool_use", "run_sql", "call_0_0")
    assert block["input"] == {"sql": "select team_name, incidents from t"}
    assert set(first["usage"]) == {"input_tokens", "output_tokens"}
    assert second["stop_reason"] == "end_turn"
    assert second["content"] == [{"type": "text", "text": _ANSWER}]


def test_ut11_71_mismatch_is_500_with_prompt_hash() -> None:
    """UT11-71 no matching script (or an exhausted one) → 500 {"error": no script, hash}."""
    router = respx_router(_book({"match": {"role": "analyst"}, "turns": [_TEXT_TURN]}))
    with httpx2.Client(transport=router.transport()) as client:
        body = {"messages": _openai_msgs(False)}
        miss = _post(client, OPENAI, body)  # no X-Herness-Role header: role "*"
        hit = _post(client, OPENAI, body, **{"X-Herness-Role": "analyst"})
        spent = _post(client, ANTHROPIC, body, **{"X-Herness-Role": "analyst"})
    assert hit.status_code == 200
    for response in (miss, spent):
        assert response.status_code == 500
        error = response.json()["error"]
        assert error["message"] == "no script"
        assert _HEX16.match(error["prompt_hash"])


def test_ut11_71_headers_select_the_script() -> None:
    """UT11-71 X-Herness-Role / Model-Role / Dedup-Key key the call (DD11-02 delta)."""
    book = _book(
        {"match": {"role": "a", "model_role": "w", "dedup_key": "k1"}, "turns": [_TEXT_TURN]},
        {"turns": [{"final": {"text": "fallback"}}]},
    )
    heads = {"X-Herness-Role": "a", "X-Herness-Model-Role": "w", "X-Herness-Dedup-Key": "k1"}
    with httpx2.Client(transport=respx_router(book).transport()) as client:
        keyed = _post(client, OPENAI, {"messages": []}, **heads).json()
        other = _post(client, OPENAI, {"messages": []}).json()
    assert keyed["choices"][0]["message"]["content"] == "plain words for the reader"
    assert other["choices"][0]["message"]["content"] == "fallback"
    assert book.calls("a", "k1") == 1


@pytest.mark.parametrize("url", [OPENAI, ANTHROPIC])
def test_ut11_71_http_faults(url: str) -> None:
    """UT11-71 http_500 → 500; http_429 → 429 with Retry-After: 1."""
    for kind, status in (("http_500", 500), ("http_429", 429)):
        with httpx2.Client(transport=respx_router(_faulty(kind, _TEXT_TURN)).transport()) as cl:
            response = _post(cl, url, {"messages": []})
        assert response.status_code == status
    assert response.headers["retry-after"] == "1"


def test_ut11_71_malformed_json_faults() -> None:
    """UT11-71 malformed_json → 200 with tool arguments `{"a": ` or content `{"truncated": `."""
    results: dict[str, Any] = {}
    for name, turn in (("tool", _TOOL_TURN), ("text", _TEXT_TURN)):
        for url in (OPENAI, ANTHROPIC):
            router = respx_router(_faulty("malformed_json", turn))
            with httpx2.Client(transport=router.transport()) as client:
                response = _post(client, url, {"messages": []})
            assert response.status_code == 200
            results[f"{name}:{url}"] = response.json()
    assert results[f"tool:{OPENAI}"]["choices"][0]["message"]["tool_calls"][0]["function"] == {
        "name": "run_sql",
        "arguments": '{"a": ',
    }
    assert results[f"text:{OPENAI}"]["choices"][0]["message"]["content"] == '{"truncated": '
    assert results[f"tool:{ANTHROPIC}"]["content"][0]["input"] == '{"a": '
    assert results[f"text:{ANTHROPIC}"]["content"] == [{"type": "text", "text": '{"truncated": '}]


@pytest.mark.parametrize(
    ("kind", "error"), [("hang", httpx2.ReadTimeout), ("disconnect", httpx2.RemoteProtocolError)]
)
def test_ut11_71_transport_faults(kind: str, error: type[Exception]) -> None:
    """UT11-71 hang → httpx ReadTimeout; disconnect → httpx RemoteProtocolError."""
    router = respx_router(_faulty(kind, _TEXT_TURN))
    with httpx2.Client(transport=router.transport()) as client, pytest.raises(error):
        _post(client, OPENAI, {"messages": []})


def test_ut11_71_other_hosts_are_unrouted() -> None:
    """UT11-71 a request to neither endpoint gets the MockNet ConnectError."""
    with (
        httpx2.Client(transport=respx_router(SCRIPTS).transport()) as client,
        pytest.raises(httpx2.ConnectError),
    ):
        client.get("http://127.0.0.1:8000/v1/models")


# ------------------------------------------- UT11-71 acceptance: spec 05 adapters over the router


def _openai_cfg() -> ClientConfig:
    return ClientConfig.model_validate(
        {
            "name": "local-30b", "kind": "openai_compat", "base_url": "http://127.0.0.1:8000/v1",
            "model": "qwen3-30b", "context_window": 32768, "max_output_tokens": 4096,
            "tokenizer": "estimate", "max_concurrency": 4, "price_per_mtok": _ZERO,
            "server": "vllm",
        }
    )  # fmt: skip


def test_ut11_71_openai_adapter_runs_against_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT11-71 OpenAICompatClient (loopback client, real) drives a scripted 2-turn loop."""
    router = respx_router(SCRIPTS).install(monkeypatch)
    adapter = OpenAICompatClient(_openai_cfg())
    first = asyncio.run(adapter.acomplete(_request("local-30b")))
    (call,) = first.tool_calls
    assert (call.name, call.arguments, first.stop_reason) == (
        "run_sql",
        {"sql": "select team_name, incidents from t"},
        "tool_use",
    )
    second = asyncio.run(adapter.acomplete(_request("local-30b", *_after_tool(call.id))))
    assert (second.text, second.stop_reason) == (_ANSWER, "end_turn")
    assert [str(r.url) for r in router.requests] == [OPENAI, OPENAI]


def test_ut11_71_openai_adapter_translates_router_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT11-71 a scripted http_429 reaches the adapter as RateLimited(retry_after=1)."""
    with respx_router(_faulty("http_429", _TEXT_TURN)):
        adapter = OpenAICompatClient(_openai_cfg())
        with pytest.raises(RateLimited) as info:
            adapter.complete(_request("local-30b"))
    assert info.value.retry_after == 1.0


@pytest.fixture
def hybrid(
    tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Profile hybrid (egress on, api.anthropic.com allowed), a real guard with the fixed test
    redactor (``egress_harness.make_guard``) and a fake API key."""
    del fake_keyring
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    c.reset_config()
    guard = make_guard(load(tmp_path, "hybrid"))
    monkeypatch.setattr(egress, "get_guard", lambda: guard)
    monkeypatch.setattr(secrets, "resolve", lambda _ref: SecretStr("sk-ant-test-fake"))
    yield
    c.reset_config()


def test_ut11_71_anthropic_adapter_runs_against_router(
    hybrid: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-71 AnthropicClient through the real egress guard drives a scripted 2-turn loop."""
    del hybrid
    clients = yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    cfg = ClientConfig.model_validate(
        {"name": "claude-opus", **clients["models"]["clients"]["claude-opus"]}
    )
    router = respx_router(SCRIPTS).install(monkeypatch)
    adapter = AnthropicClient(cfg)
    first = asyncio.run(adapter.acomplete(_request("claude-opus")))
    (call,) = first.tool_calls
    assert (call.name, first.stop_reason, first.provider) == ("run_sql", "tool_use", "anthropic")
    second = asyncio.run(adapter.acomplete(_request("claude-opus", *_after_tool(call.id))))
    assert (second.text, second.stop_reason) == (_ANSWER, "end_turn")
    assert [str(r.url) for r in router.requests] == [ANTHROPIC, ANTHROPIC]


# ---------------------------------------------------------------- UT11-73 / UT11-74 FakeLLMServer


def test_ut11_73_server_tool_turn_non_stream() -> None:
    """UT11-73 POST non-stream: an OpenAI completion whose tool arguments are JSON text."""
    with (
        FakeLLMServer(_book({"turns": [_TOOL_TURN]}), service_name=None) as server,
        egress.loopback_http_client(server.base_url, timeout_s=10) as client,
    ):
        models = client.get("/v1/models").json()
        body = client.post("/v1/chat/completions", json={"model": "m", "messages": []}).json()
    assert models == {"object": "list", "data": [{"id": "scripted", "object": "model"}]}
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    arguments = choice["message"]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(arguments, str)
    assert json.loads(arguments) == {"sql": "x"}
    assert server.completed == 2


def _events(lines: list[str]) -> list[Any]:
    data = [line.removeprefix("data: ") for line in lines if line.startswith("data: ")]
    return [d if d == "[DONE]" else json.loads(d) for d in data]


def test_ut11_74_server_streams_text_turn() -> None:
    """UT11-74 40-char text, stream: 3 content chunks, finish chunk, usage chunk, [DONE]."""
    text = "x" * 16 + "y" * 16 + "z" * 8
    book = _book({"turns": [{"final": {"text": text}}]})
    body = {"model": "m", "messages": [], "stream": True}
    with (
        FakeLLMServer(book, service_name=None) as server,
        egress.loopback_http_client(server.base_url, timeout_s=10) as client,
        client.stream("POST", "/v1/chat/completions", json=body) as response,
    ):
        assert response.headers["content-type"] == "text/event-stream"
        events = _events(list(response.iter_lines()))
    content = [e["choices"][0]["delta"]["content"] for e in events[:3]]
    assert content == ["x" * 16, "y" * 16, "z" * 8]
    assert events[3]["choices"][0]["finish_reason"] == "stop"
    assert events[4]["choices"] == []
    assert set(events[4]["usage"]) >= {"prompt_tokens", "completion_tokens"}
    assert events[5:] == ["[DONE]"]


def test_ut11_74_openai_adapter_streams_from_server() -> None:
    """UT11-74 the SSE stream is what OpenAICompatClient.astream consumes: deltas then Done."""
    text = "x" * 16 + "y" * 16 + "z" * 8
    with FakeLLMServer(_book({"turns": [{"final": {"text": text}}]}), service_name=None) as srv:
        cfg = _openai_cfg().model_copy(update={"base_url": f"{srv.base_url}/v1"})

        async def collect() -> list[Any]:
            return [e async for e in OpenAICompatClient(cfg).astream(_request("local-30b"))]

        events = asyncio.run(collect())
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["x" * 16, "y" * 16, "z" * 8]
    assert isinstance(events[-1], Done)
    assert events[-1].response.text == text


def test_ut11_73_server_faults() -> None:
    """UT11-73 hang waits `hang_s` then errors; disconnect closes with no response."""
    book = _book(
        {
            "turns": [_TEXT_TURN],
            "faults": [{"at": 0, "kind": "hang"}, {"at": 1, "kind": "disconnect"}],
        }
    )
    with (
        FakeLLMServer(book, service_name=None, hang_s=0.05) as server,
        egress.loopback_http_client(server.base_url, timeout_s=10) as client,
    ):
        hung = client.post("/v1/chat/completions", json={"messages": []})
        with pytest.raises(httpx2.RemoteProtocolError):
            client.post("/v1/chat/completions", json={"messages": []})
        served = client.post("/v1/chat/completions", json={"messages": []})
    assert hung.status_code == 500
    assert served.json()["choices"][0]["message"]["content"] == "plain words for the reader"
