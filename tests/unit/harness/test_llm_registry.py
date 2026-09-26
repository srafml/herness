"""Tests for herness.harness.llm.registry (U05-31 LLMRegistry, U05-32 client_for)."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from herness.core import registry as core_registry
from herness.core.errors import ConfigError
from herness.harness.llm import registry as reg
from herness.harness.llm.base import LLMClient
from herness.harness.llm.registry import LLMRegistry, client_for
from herness.harness.llm.settings import ClientConfig, ModelsConfig

pytestmark = pytest.mark.unit

_ZERO = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}
_OPUS_PRICE = {"input": "4.00", "output": "20.00", "cache_read": "0.20", "cache_write": "5.00"}


def _config() -> dict[str, Any]:
    data: dict[str, Any] = {
        "models": {
            "clients": {
                "local-30b": {
                    "kind": "openai_compat",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "api_key": "secret:vllm.api_key",
                    "model": "local-30b",
                    "context_window": 32768,
                    "max_output_tokens": 4096,
                    "tokenizer": "vllm_endpoint",
                    "max_concurrency": 6,
                    "price_per_mtok": dict(_ZERO),
                },
                "local-large-offload": {
                    "kind": "openai_compat",
                    "base_url": "http://127.0.0.1:8200/v1",
                    "model": "local-large",
                    "context_window": 32768,
                    "max_output_tokens": 4096,
                    "tokenizer": "estimate",
                    "max_concurrency": 1,
                    "price_per_mtok": dict(_ZERO),
                },
                "claude-opus": {
                    "kind": "anthropic",
                    "api_key": "secret:anthropic.api_key",
                    "model": "claude-opus-5-5",
                    "context_window": 1_000_000,
                    "max_output_tokens": 16000,
                    "tokenizer": "anthropic",
                    "max_concurrency": 50,
                    "thinking_mode": "adaptive_always",
                    "off_network": True,
                    "price_per_mtok": dict(_OPUS_PRICE),
                },
            },
            "roles": {"planner": "local-30b", "writer": "local-30b", "chat": "local-30b"},
            "fallback": {
                "planner": ["local-30b", "claude-opus"],
                "writer": ["local-30b", "local-large-offload", "claude-opus"],
            },
            "depth_overrides": {"deep": {"roles": {"writer": "local-large-offload"}}},
            "role_params": {"writer": {"temperature": 0.4, "effort": "high", "thinking": "auto"}},
        },
        "harness": {},
    }
    return data


def _cfg() -> ModelsConfig:
    return ModelsConfig.model_validate(_config())


@pytest.fixture(autouse=True)
def _fake_llm_client() -> None:
    """A stand-in ``llm_client`` so ``client()``/``client_for`` never build a real SDK client."""

    class _FakeClient:
        def __init__(self, cfg: ClientConfig) -> None:
            self.name = cfg.name
            self.cfg = cfg

        def complete(self, req: object) -> object:  # pragma: no cover - not exercised
            raise NotImplementedError

        async def acomplete(self, req: object) -> object:  # pragma: no cover - not exercised
            raise NotImplementedError

    core_registry.register("llm_client", "openai_compat")(_FakeClient)
    core_registry.register("llm_client", "anthropic")(_FakeClient)


# --- UT05-18 -------------------------------------------------------------------------------


def test_ut05_18_off_network_in_fallback_egress_off_raises() -> None:
    """UT05-18 config with a Claude (off-network) role, egress off -> ConfigError."""
    with pytest.raises(
        ConfigError, match="claude-opus is off-network but egress is disabled in profile local"
    ):
        LLMRegistry(_cfg(), profile="local", egress_enabled=False)


def test_ut05_18_logs_config_invalid_error() -> None:
    """UT05-18 construction failure logs harness.llm.config_invalid at ERROR, no secrets."""
    with capture_logs() as logs, pytest.raises(ConfigError):
        LLMRegistry(_cfg(), profile="local", egress_enabled=False)
    events = [e for e in logs if e["event"] == "harness.llm.config_invalid"]
    assert len(events) == 1
    assert events[0]["log_level"] == "error"
    assert events[0]["client"] == "claude-opus"


def test_ut05_18_egress_enabled_constructs() -> None:
    """UT05-18 counterpart: the same config constructs fine when egress is enabled."""
    LLMRegistry(_cfg(), profile="local", egress_enabled=True)


def test_ut05_18_reads_get_config_when_egress_enabled_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT05-18 ``egress_enabled=None`` reads ``get_config().security.egress.enabled``."""
    stub = SimpleNamespace(security=SimpleNamespace(egress=SimpleNamespace(enabled=False)))
    monkeypatch.setattr(reg, "get_config", lambda: stub)
    with pytest.raises(ConfigError, match="egress is disabled"):
        LLMRegistry(_cfg(), profile="local")


# --- UT05-40 -------------------------------------------------------------------------------


def test_ut05_40_model_for_depth_override_and_base_role() -> None:
    """UT05-40 depth override wins; base-role fallback used when the role itself is absent."""
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    assert registry.model_for("writer", "standard") == "local-30b"
    assert registry.model_for("writer", "deep") == "local-large-offload"
    # "judge" has no direct role entry; BASE_ROLE["judge"] == "planner"
    assert registry.model_for("judge", "standard") == "local-30b"
    # unknown depth -> no override applied
    assert registry.model_for("writer", "unknown-depth") == "local-30b"


def test_ut05_40_model_for_unknown_role_raises() -> None:
    """UT05-40 a role with no entry and no base role -> ConfigError."""
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    with pytest.raises(ConfigError, match="no model for role nope"):
        registry.model_for("nope", "standard")


def test_ut05_40_chain_for_dedupes_deep_head() -> None:
    """UT05-40 chain_for: deep-override head, then its fallback chain, deduplicated."""
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    assert registry.chain_for("writer", "deep") == [
        "local-large-offload",
        "local-30b",
        "claude-opus",
    ]
    assert registry.chain_for("writer", "standard") == [
        "local-30b",
        "local-large-offload",
        "claude-opus",
    ]


def test_ut05_40_chain_for_base_role_fallback() -> None:
    """UT05-40 chain_for falls back to the base role's fallback chain."""
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    assert registry.chain_for("judge", "standard") == ["local-30b", "claude-opus"]


def test_ut05_40_chain_for_no_fallback_is_head_only() -> None:
    """UT05-40 a role with no fallback entry and no base role -> chain is just the head."""
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    assert registry.chain_for("chat", "standard") == ["local-30b"]


def test_ut05_40_role_params_inherits_base_role_and_defaults_none() -> None:
    """UT05-40 role_params: direct entry, base-role entry, else None."""
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    params = registry.role_params("writer")
    assert params is not None
    assert params.temperature == 0.4
    assert registry.role_params("chat") is None


# --- UT05-41 -------------------------------------------------------------------------------


def test_ut05_41_client_same_instance_across_threads() -> None:
    """UT05-41 client(name) returns the same cached instance, even under concurrent calls."""
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    results: list[LLMClient] = []

    def worker() -> None:
        results.append(registry.client("local-30b"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    first = registry.client("local-30b")
    assert len(results) == 8
    assert all(r is first for r in results)


def test_ut05_41_client_unknown_name_raises() -> None:
    """UT05-41 client() and config() raise ConfigError naming the client for an unknown key."""
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    with pytest.raises(ConfigError, match="unknown model client nope"):
        registry.client("nope")
    with pytest.raises(ConfigError, match="unknown model client nope"):
        registry.config("nope")


# --- UT05-42 -------------------------------------------------------------------------------


def _root_cfg(profile: str) -> SimpleNamespace:
    return SimpleNamespace(
        profile=profile,
        models=_cfg(),
        security=SimpleNamespace(egress=SimpleNamespace(enabled=True)),
    )


def test_ut05_42_client_for_matching_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-42 client_for with a matching profile returns (client, config) for the role head."""
    monkeypatch.setattr(reg, "get_config", lambda: _root_cfg("local"))
    client, cfg = client_for("writer", profile="local", depth="standard")
    assert cfg.name == "local-30b"
    assert client.name == "local-30b"


def test_ut05_42_client_for_mismatched_profile_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-42 client_for with a mismatching profile -> ConfigError, verbatim message."""
    root_cfg = SimpleNamespace(profile="hybrid", models=_cfg())
    monkeypatch.setattr(reg, "get_config", lambda: root_cfg)
    with pytest.raises(
        ConfigError, match=r"client_for profile local does not match loaded profile hybrid"
    ):
        client_for("writer", profile="local", depth="standard")


# --- UT05-123 (U05-31 half: local ok/down, R-52 reason, off-network ok/down) ----------------


class _FakeResponse:
    def __init__(self, status_code: int, body: object = None, *, bad_json: bool = False) -> None:
        self.status_code = status_code
        self._body = body
        self._bad_json = bad_json

    def json(self) -> object:
        if self._bad_json:
            msg = "invalid json"
            raise ValueError(msg)
        return self._body


class _FakeHttpClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.paths: list[str] = []

    def __enter__(self) -> _FakeHttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, path: str) -> _FakeResponse:
        self.paths.append(path)
        return self._response


def _stub_loopback(
    monkeypatch: pytest.MonkeyPatch, response: _FakeResponse
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def factory(
        root: str, *, timeout_s: float, bearer: SecretStr | None, max_response_bytes: int
    ) -> _FakeHttpClient:
        calls.append(
            {
                "root": root,
                "timeout_s": timeout_s,
                "bearer": bearer,
                "max_response_bytes": max_response_bytes,
            }
        )
        return _FakeHttpClient(response)

    monkeypatch.setattr(reg.egress, "loopback_http_client", factory)
    monkeypatch.setattr(reg.secrets, "resolve", lambda ref: SecretStr(f"resolved:{ref}"))
    return calls


def test_ut05_123_local_client_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-123 a non-vLLM local client returning 200 is ok; root and bearer are as documented."""
    calls = _stub_loopback(monkeypatch, _FakeResponse(200))
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    health = registry.health()
    assert health["local-large-offload"] == "ok"
    call = next(c for c in calls if c["root"] == "http://127.0.0.1:8200")
    assert call["timeout_s"] == 2.0
    assert call["max_response_bytes"] == 1_048_576
    assert call["bearer"] is None  # local-large-offload has no api_key


def test_ut05_123_local_client_down_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-123 a non-200 response -> "down" with no reason."""
    _stub_loopback(monkeypatch, _FakeResponse(503))
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    assert registry.health()["local-30b"] == "down"


def test_ut05_123_transport_exception_is_generic_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-123 any exception (transport, secret, JSON) -> "down: <ExceptionType>", no message."""

    def boom(*_a: object, **_kw: object) -> _FakeHttpClient:
        msg = "connection refused: 10.1.2.3 secret-token"
        raise RuntimeError(msg)

    monkeypatch.setattr(reg.egress, "loopback_http_client", boom)
    monkeypatch.setattr(reg.secrets, "resolve", lambda ref: SecretStr("k"))
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    result = registry.health()["local-30b"]
    assert result == "down: RuntimeError"
    assert "10.1.2.3" not in result
    assert "secret-token" not in result


def test_ut05_123_bad_json_is_generic_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-123 a vLLM client whose /v1/models body is not JSON -> "down: <ExceptionType>"."""
    _stub_loopback(monkeypatch, _FakeResponse(200, bad_json=True))
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    assert registry.health()["local-30b"] == "down: ValueError"


def test_ut05_123_r52_context_window_above_max_model_len(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-123 R-52: vLLM entry's max_model_len below context_window -> down with the reason."""
    body = {"data": [{"id": "local-30b", "max_model_len": 4096}]}
    _stub_loopback(monkeypatch, _FakeResponse(200, body))
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    assert registry.health()["local-30b"] == (
        "down: context_window 32768 above server max_model_len 4096"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"data": [{"id": "local-30b"}]},  # missing max_model_len
        {"data": [{"id": "local-30b", "max_model_len": "32"}]},  # non-int
        {"data": [{"id": "local-30b", "max_model_len": True}]},  # bool, not int
        {"data": [{"id": "other-model", "max_model_len": 10}]},  # entry missing
        {"data": []},
        {},
    ],
)
def test_ut05_123_r52_skipped_on_missing_or_non_int_field(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    """UT05-123 R-52: a missing field, non-int value or missing entry skips the check -> ok."""
    _stub_loopback(monkeypatch, _FakeResponse(200, body))
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    assert registry.health()["local-30b"] == "ok"


def test_ut05_123_r52_not_checked_for_non_vllm_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-123 the R-52 check only runs for an effective vLLM server."""
    body = {"data": [{"id": "local-large", "max_model_len": 1}]}
    _stub_loopback(monkeypatch, _FakeResponse(200, body))
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    assert registry.health()["local-large-offload"] == "ok"


def test_ut05_123_off_network_ok_without_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-123 an off-network client is ok without any HTTP call when egress is enabled."""
    calls = _stub_loopback(monkeypatch, _FakeResponse(200))
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    assert registry.health()["claude-opus"] == "ok"
    # Two local clients are in use (local-30b, local-large-offload); the off-network one made
    # no additional call of its own.
    assert len(calls) == 2


def test_ut05_123_off_network_down_when_egress_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-123 an off-network client is down: egress disabled, and makes no call either."""
    calls = _stub_loopback(monkeypatch, _FakeResponse(200))
    # Constructed with egress enabled (claude-opus is in use, so construction would otherwise
    # raise per U05-31 algorithm 1); the health() -> string mapping itself is what this test
    # exercises, decoupled from the constructor's off-network guard.
    registry = LLMRegistry(_cfg(), profile="local", egress_enabled=True)
    registry._egress_enabled = False
    assert registry.health()["claude-opus"] == "down: egress disabled"
    assert len(calls) == 2
