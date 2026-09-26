"""Tests for herness.harness.llm.tokens count_tokens and estimate_tokens (U05-23).

The vLLM path runs the real ``egress.loopback_http_client`` with its pool transport swapped for
an ``httpx2.MockTransport`` (respx cannot patch ``httpx2``), so the loopback host check, the
byte cap and the bearer header all run for real. The Anthropic path uses a stub guard whose
``http_client`` hands out a mock-transport client, as in the adapter tests.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from pydantic import SecretStr
from structlog.testing import capture_logs
from tests.support.egress_harness import load
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import egress, egress_clients, secrets
from herness.core.errors import EgressBlocked
from herness.core.ids import canonical_json
from herness.core.types import (
    Message,
    ReasoningPart,
    SystemBlock,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
)
from herness.harness.llm import tokens
from herness.harness.llm.settings import ClientConfig, PricePerMTok

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
_ZERO = Decimal("0")
_PRICE = PricePerMTok(input=_ZERO, output=_ZERO, cache_read=_ZERO, cache_write=_ZERO)
_KEY = "sk-local-tokenize-0123456789"  # pragma: allowlist secret
_ANT_KEY = "sk-ant-test-" + "t" * 24  # pragma: allowlist secret
_ABSENT_REF = "secret:absent_key"  # pragma: allowlist secret
_KEY_REF = "secret:vllm_key"  # pragma: allowlist secret
_Handler = Callable[[httpx2.Request], httpx2.Response]


def _vllm_cfg(**updates: Any) -> ClientConfig:
    data: dict[str, Any] = {
        "name": "local-30b",
        "kind": "openai_compat",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "qwen3-30b",
        "context_window": 32768,
        "max_output_tokens": 4096,
        "tokenizer": "vllm_endpoint",
        "max_concurrency": 4,
        "price_per_mtok": _PRICE,
    }
    data.update(updates)
    return ClientConfig.model_validate(data)


def _anthropic_cfg() -> ClientConfig:
    clients = yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    return ClientConfig.model_validate(
        {"name": "claude-opus", **clients["models"]["clients"]["claude-opus"]}
    )


def _messages() -> list[Message]:
    call = ToolCall(id="call_1", name="run_sql", arguments={"sql": "select 1"})
    return [
        Message(role="user", parts=[TextPart(text="How many incidents?")]),
        Message(
            role="assistant",
            parts=[
                ReasoningPart(provider="vllm", text="private"),
                TextPart(text="Checking."),
                ToolCallPart(call=call),
            ],
        ),
        Message(role="tool", parts=[ToolResultPart(tool_call_id="call_1", content="[[1]]")]),
    ]


def _tools() -> list[ToolSpec]:
    return [ToolSpec(name="run_sql", description="Run SQL.", input_schema={"type": "object"})]


def _system() -> list[SystemBlock]:
    return [SystemBlock(text="You are the planner."), SystemBlock(text="Be brief.")]


def _serve(monkeypatch: pytest.MonkeyPatch, handler: _Handler) -> list[httpx2.Request]:
    """Answer the loopback client's requests with ``handler``; returns the requests seen."""
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        request.read()
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        egress_clients.httpx2, "HTTPTransport", lambda **_kw: httpx2.MockTransport(record)
    )
    return seen


def _spy_loopback(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any], Any]]:
    made: list[tuple[str, dict[str, Any], Any]] = []
    real = egress.loopback_http_client

    def spy(base_url: str, **kwargs: Any) -> Any:
        client = real(base_url, **kwargs)
        made.append((base_url, kwargs, client))
        return client

    monkeypatch.setattr(egress, "loopback_http_client", spy)
    return made


def _count(cfg: ClientConfig, *, tools: list[ToolSpec] | None = None) -> tuple[int, bool]:
    return tokens.count_tokens(cfg, _messages(), _tools() if tools is None else tools, _system())


def _estimate() -> int:
    return tokens.estimate_tokens(_messages(), _tools(), _system())


# --- estimate_tokens ----------------------------------------------------------------------------


def test_ut05_22_estimate_is_ceil_chars_over_3_5() -> None:
    """UT05-22 estimate = ceil(chars / 3.5) over system texts, message parts and tool specs."""
    chars = sum(len(b.text) for b in _system())
    chars += sum(
        len(canonical_json([p.model_dump(mode="json") for p in m.parts])) for m in _messages()
    )
    chars += sum(len(canonical_json(t.model_dump(mode="json"))) for t in _tools())
    assert _estimate() == math.ceil(chars / 3.5)


@pytest.mark.parametrize(("n", "expected"), [(0, 0), (1, 1), (7, 2), (8, 3), (35, 10), (36, 11)])
def test_ut05_22_estimate_rounds_up_exactly(n: int, expected: int) -> None:
    """UT05-22 the rounding is exact at the 3.5 boundaries (integer arithmetic, no float drift)."""
    assert tokens.estimate_tokens([], system=[SystemBlock(text="x" * n)] if n else []) == expected


def test_ut05_22_estimate_defaults_and_is_pure() -> None:
    """UT05-22 tools and system default to empty; the same input gives the same count."""
    messages = [Message(role="user", parts=[TextPart(text="hi")])]
    first = tokens.estimate_tokens(messages)
    assert first == tokens.estimate_tokens(messages, (), ()) > 0


def test_ut05_22_estimate_tokenizer_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-22 tokenizer estimate returns (estimate, False) without any HTTP call."""
    seen = _serve(monkeypatch, lambda r: httpx2.Response(500, request=r))
    assert _count(_vllm_cfg(tokenizer="estimate")) == (_estimate(), False)
    assert seen == []


# --- vllm_endpoint -------------------------------------------------------------------------------


def test_ut05_22_vllm_exact_count(
    monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> None:
    """UT05-22 POST <root>/tokenize through the loopback client; {"count": n} -> (n, True)."""
    fake_keyring.store[("herness", "vllm_key")] = _KEY
    made = _spy_loopback(monkeypatch)
    seen = _serve(monkeypatch, lambda r: httpx2.Response(200, json={"count": 321}, request=r))
    assert _count(_vllm_cfg(api_key=_KEY_REF)) == (321, True)
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "http://127.0.0.1:8000/tokenize"
    assert request.headers["authorization"] == f"Bearer {_KEY}"
    assert request.headers["accept-encoding"] == "identity"
    body = json.loads(request.content)
    assert set(body) == {"model", "messages", "tools", "add_generation_prompt"}
    assert body["model"] == "qwen3-30b"
    assert body["add_generation_prompt"] is True
    assert body["messages"][0] == {"role": "system", "content": "You are the planner.\n\nBe brief."}
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool"]
    assert "private" not in request.content.decode()
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "run_sql",
                "description": "Run SQL.",
                "parameters": {"type": "object"},
            },
        }
    ]
    ((root, kwargs, client),) = made
    assert root == "http://127.0.0.1:8000"
    assert kwargs["timeout_s"] == 5
    assert kwargs["max_response_bytes"] == 1_048_576
    assert kwargs["bearer"].get_secret_value() == _KEY
    assert client.is_closed


@pytest.mark.parametrize(
    ("base_url", "root"),
    [
        ("http://127.0.0.1:8000/v1/", "http://127.0.0.1:8000"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://127.0.0.1:8000/proxy/v1", "http://127.0.0.1:8000/proxy"),
    ],
)
def test_ut05_22_vllm_root_and_no_tools(
    monkeypatch: pytest.MonkeyPatch, base_url: str, root: str
) -> None:
    """UT05-22 root drops a trailing /v1 and slash; no key means no bearer; no tools key."""
    made = _spy_loopback(monkeypatch)
    seen = _serve(monkeypatch, lambda r: httpx2.Response(200, json={"count": 0}, request=r))
    assert _count(_vllm_cfg(base_url=base_url), tools=[]) == (0, True)
    assert made[0][0] == root
    assert made[0][1]["bearer"] is None
    assert str(seen[0].url) == f"{root}/tokenize"
    assert "authorization" not in seen[0].headers
    assert "tools" not in json.loads(seen[0].content)


def _body(payload: object) -> _Handler:
    return lambda r: httpx2.Response(200, json=payload, request=r)


def _raise(exc: Exception) -> _Handler:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise exc

    return handler


class _Big(httpx2.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        for _ in range(20):
            yield b" " * 100_000


_FAILURES: dict[str, tuple[_Handler, str]] = {
    "status_500": (lambda r: httpx2.Response(500, json={}, request=r), "HTTPStatusError"),
    "missing_count": (_body({"tokens": [1, 2]}), "OutputValidationError"),
    "count_bool": (_body({"count": True}), "OutputValidationError"),
    "count_negative": (_body({"count": -1}), "OutputValidationError"),
    "count_text": (_body({"count": "12"}), "OutputValidationError"),
    "count_float": (_body({"count": 12.0}), "OutputValidationError"),
    "not_an_object": (_body([1, 2]), "OutputValidationError"),
    "not_json": (lambda r: httpx2.Response(200, text="nope", request=r), "JSONDecodeError"),
    "connect_error": (_raise(httpx2.ConnectError("refused")), "ConnectError"),
    "over_one_mib": (lambda r: httpx2.Response(200, stream=_Big(), request=r), "EgressBlocked"),
}


@pytest.mark.parametrize("name", sorted(_FAILURES))
def test_ut05_22_vllm_failure_falls_back_to_estimate(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """UT05-22 a failing endpoint gives (estimate, False) and one WARNING without the message."""
    handler, error_type = _FAILURES[name]
    made = _spy_loopback(monkeypatch)
    _serve(monkeypatch, handler)
    with capture_logs() as logs:
        assert _count(_vllm_cfg()) == (_estimate(), False)
    events = [e for e in logs if e["event"] == "harness.llm.token_count_fallback"]
    assert events == [
        {
            "event": "harness.llm.token_count_fallback",
            "component": "harness.llm",
            "client": "local-30b",
            "error_type": error_type,
            "log_level": "warning",
        }
    ]
    assert made[0][2].is_closed


def test_ut05_22_vllm_non_loopback_url_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-22 TH05-13: a non-loopback root is refused by the loopback client -> estimate."""
    seen = _serve(monkeypatch, _body({"count": 5}))
    cfg = _vllm_cfg().model_copy(update={"base_url": "http://10.0.0.5:8000/v1"})
    with capture_logs() as logs:
        assert _count(cfg) == (_estimate(), False)
    assert seen == []
    assert logs[-1]["event"] == "harness.llm.token_count_fallback"


def test_ut05_22_missing_secret_falls_back(fake_keyring: MemoryKeyring) -> None:
    """UT05-22 an unresolvable api_key reference is a counting failure, not an exception."""
    del fake_keyring
    with capture_logs() as logs:
        assert _count(_vllm_cfg(api_key=_ABSENT_REF)) == (_estimate(), False)
    assert logs[-1]["event"] == "harness.llm.token_count_fallback"


# --- anthropic -----------------------------------------------------------------------------------


class _StubGuard:
    """Hands out sync httpx2 clients over a mock transport, like the T10-16 guard's."""

    def __init__(self, handler: _Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[Any, ...]] = []
        self.requests: list[httpx2.Request] = []
        self.clients: list[httpx2.Client] = []

    def _record(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        self.requests.append(request)
        return self.handler(request)

    def http_client(self, purpose: str, payload_class: str, **kwargs: Any) -> httpx2.Client:
        self.calls.append((purpose, payload_class, kwargs))
        self.clients.append(httpx2.Client(transport=httpx2.MockTransport(self._record)))
        return self.clients[-1]


@pytest.fixture
def hybrid(
    tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    del fake_keyring
    c.reset_config()
    load(tmp_path, "hybrid")
    monkeypatch.setattr(secrets, "resolve", lambda _ref: SecretStr(_ANT_KEY))
    yield
    c.reset_config()


def _guard(monkeypatch: pytest.MonkeyPatch, handler: _Handler) -> _StubGuard:
    guard = _StubGuard(handler)
    monkeypatch.setattr(egress, "get_guard", lambda: guard)
    return guard


def test_ut05_22_anthropic_exact_count(hybrid: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-22 messages.count_tokens through the guard's client with the U05-27 mapping."""
    del hybrid
    guard = _guard(monkeypatch, _body({"input_tokens": 77}))
    assert _count(_anthropic_cfg()) == (77, True)
    assert guard.calls == [("reasoning_final", "aggregated_evidence", {})]
    (request,) = guard.requests
    assert request.url.path == "/v1/messages/count_tokens"
    assert request.headers["x-api-key"] == _ANT_KEY
    body = json.loads(request.content)
    assert set(body) == {"model", "system", "messages", "tools"}
    assert body["model"] == _anthropic_cfg().model
    assert [b["text"] for b in body["system"]] == ["You are the planner.", "Be brief."]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][1]["content"][1]["type"] == "tool_use"
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    assert body["tools"][0]["name"] == "run_sql"
    assert "private" not in request.content.decode()
    assert guard.clients[0].is_closed


def test_ut05_22_anthropic_minimal_request_omits_optional_keys(
    hybrid: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-22 without system blocks or tools those keys are not sent."""
    del hybrid
    guard = _guard(monkeypatch, _body({"input_tokens": 3}))
    messages = [Message(role="user", parts=[TextPart(text="hi")])]
    assert tokens.count_tokens(_anthropic_cfg(), messages, [], []) == (3, True)
    assert set(json.loads(guard.requests[0].content)) == {"model", "messages"}


@pytest.mark.parametrize(
    ("handler", "error_type"),
    [
        (lambda r: httpx2.Response(500, json={"error": {}}, request=r), "InternalServerError"),
        (_body({"input_tokens": -4}), "OutputValidationError"),
        (_raise(EgressBlocked("refused", reason="profile")), "EgressBlocked"),
    ],
)
def test_ut05_22_anthropic_failure_falls_back(
    hybrid: None, monkeypatch: pytest.MonkeyPatch, handler: _Handler, error_type: str
) -> None:
    """UT05-22 an SDK error, a bad count or an egress refusal gives the estimate and a WARNING."""
    del hybrid
    guard = _guard(monkeypatch, handler)
    with capture_logs() as logs:
        assert _count(_anthropic_cfg()) == (_estimate(), False)
    event = logs[-1]
    assert (event["event"], event["client"]) == ("harness.llm.token_count_fallback", "claude-opus")
    assert event["error_type"] == error_type
    assert guard.clients[0].is_closed


def test_ut05_22_anthropic_without_egress_falls_back(
    tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-22 in a profile with egress off the adapter refuses construction -> estimate."""
    del fake_keyring
    c.reset_config()
    try:
        load(tmp_path, "local")
        guard = _guard(monkeypatch, _body({"input_tokens": 1}))
        with capture_logs() as logs:
            assert _count(_anthropic_cfg()) == (_estimate(), False)
        assert logs[-1]["error_type"] == "ConfigError"
        assert guard.calls == []
    finally:
        c.reset_config()
