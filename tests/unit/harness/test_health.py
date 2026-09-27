"""Tests for herness.harness.health (U05-73 harness_health)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from tests.support.tools_standin import BUILD_ID, make_build

from herness.harness import health as hh
from herness.harness.llm import registry as reg
from herness.harness.llm.registry import LLMRegistry
from herness.harness.llm.settings import ModelsConfig, SqlSettings

pytestmark = pytest.mark.unit

_ZERO = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}


def _client(
    base_url: str, *, tokenizer: str = "estimate", context_window: int = 32768
) -> dict[str, Any]:
    return {
        "kind": "openai_compat",
        "base_url": base_url,
        "model": base_url,
        "context_window": context_window,
        "max_output_tokens": 4096,
        "tokenizer": tokenizer,
        "max_concurrency": 4,
        "price_per_mtok": dict(_ZERO),
    }


def _config() -> dict[str, Any]:
    """Three clients named after their routed role: `analyst`, `chat`; `extra` routes nothing."""
    return {
        "models": {
            "clients": {
                "analyst": _client("http://127.0.0.1:8101/v1"),
                "chat": _client("http://127.0.0.1:8102/v1"),
                "extra": _client("http://127.0.0.1:8103/v1"),
            },
            "roles": {"analyst": "analyst", "chat": "chat", "planner": "extra"},
            "fallback": {},
            "depth_overrides": {},
            "role_params": {},
        },
        "harness": {},
    }


def _config_with_fallback() -> dict[str, Any]:
    """`_config()` plus a fallback chain off `analyst` and a `deep`-depth override for `chat`."""
    config = _config()
    config["models"]["clients"]["analyst_fallback"] = _client("http://127.0.0.1:8104/v1")
    config["models"]["clients"]["chat_deep"] = _client("http://127.0.0.1:8105/v1")
    config["models"]["fallback"] = {"analyst": ["analyst", "analyst_fallback"]}
    config["models"]["depth_overrides"] = {"deep": {"roles": {"chat": "chat_deep"}}}
    return config


def _registry(config: dict[str, Any] | None = None) -> LLMRegistry:
    return LLMRegistry(
        ModelsConfig.model_validate(config or _config()), profile="local", egress_enabled=True
    )


class _FakeResponse:
    def __init__(self, status_code: int, body: object = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


class _FakeHttpClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeHttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, path: str) -> _FakeResponse:
        return self._response


def _stub_loopback(monkeypatch: pytest.MonkeyPatch, by_root: dict[str, _FakeResponse]) -> None:
    """Route each loopback call to `by_root[root]`, default 200 for any unlisted root."""

    def factory(
        root: str, *, timeout_s: float, bearer: SecretStr | None, max_response_bytes: int
    ) -> _FakeHttpClient:
        return _FakeHttpClient(by_root.get(root, _FakeResponse(200)))

    monkeypatch.setattr(reg.egress, "loopback_http_client", factory)
    monkeypatch.setattr(reg.secrets, "resolve", lambda ref: SecretStr(f"resolved:{ref}"))


@pytest.fixture(autouse=True)
def _stub_get_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_config().models.harness.sql`: the same source the warehouse tools use (no config file
    is loaded in unit tests).
    """
    stub = SimpleNamespace(models=SimpleNamespace(harness=SimpleNamespace(sql=SqlSettings())))
    monkeypatch.setattr(hh, "get_config", lambda: stub)


def _traces_dir(tmp_path: Path) -> Path:
    traces_dir = tmp_path / "traces"
    traces_dir.mkdir()
    return traces_dir


def _warehouse_ok(tmp_path: Path) -> Path:
    warehouse_dir = tmp_path / "warehouse"
    make_build(warehouse_dir)
    (warehouse_dir / "CURRENT").write_text(BUILD_ID)
    return warehouse_dir


def test_ut05_123_ok_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """UT05-123 every client ok, traces writable, current warehouse opens -> ok, empty reason."""
    _stub_loopback(monkeypatch, {})
    registry = _registry()
    result = hh.harness_health(
        registry, traces_dir=_traces_dir(tmp_path), warehouse_dir=_warehouse_ok(tmp_path)
    )
    assert result == {
        "status": "ok",
        "reason": "",
        "clients": {"analyst": "ok", "chat": "ok", "extra": "ok"},
    }


def test_ut05_123_non_route_client_down_is_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT05-123 a down client outside the analyst/chat routes -> degraded, named by key."""
    _stub_loopback(monkeypatch, {"http://127.0.0.1:8103": _FakeResponse(503)})
    registry = _registry()
    result = hh.harness_health(
        registry, traces_dir=_traces_dir(tmp_path), warehouse_dir=_warehouse_ok(tmp_path)
    )
    assert result["status"] == "degraded"
    assert result["reason"] == "client extra down"
    assert result["clients"]["extra"] == "down"


def test_ut05_123_all_analyst_chat_route_clients_down_is_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT05-123 every analyst/chat route client down -> down, named by client key."""
    _stub_loopback(
        monkeypatch,
        {
            "http://127.0.0.1:8101": _FakeResponse(503),
            "http://127.0.0.1:8102": _FakeResponse(503),
        },
    )
    registry = _registry()
    result = hh.harness_health(
        registry, traces_dir=_traces_dir(tmp_path), warehouse_dir=_warehouse_ok(tmp_path)
    )
    assert result["status"] == "down"
    assert result["reason"] == "analyst/chat clients down"


def test_ut05_123_r52_vllm_entry_below_context_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT05-123 a vLLM client's max_model_len below context_window -> down with the R-52 reason."""
    config = _config()
    config["models"]["clients"]["analyst"]["tokenizer"] = "vllm_endpoint"
    body = {"data": [{"id": "http://127.0.0.1:8101/v1", "max_model_len": 4096}]}
    _stub_loopback(monkeypatch, {"http://127.0.0.1:8101": _FakeResponse(200, body)})
    registry = LLMRegistry(
        ModelsConfig.model_validate(config), profile="local", egress_enabled=True
    )
    result = hh.harness_health(
        registry, traces_dir=_traces_dir(tmp_path), warehouse_dir=_warehouse_ok(tmp_path)
    )
    assert result["clients"]["analyst"].startswith("down")
    assert "context_window 32768 above server max_model_len 4096" in result["clients"]["analyst"]
    # chat is still ok, so this is not an all-route-down "down": it is a "degraded".
    assert result["status"] == "degraded"
    assert result["reason"] == "client analyst down"


def test_ut05_123_traces_dir_unwritable_is_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT05-123 traces_dir pointing at an existing file (not a directory) -> down."""
    _stub_loopback(monkeypatch, {})
    registry = _registry()
    not_a_dir = tmp_path / "traces_is_a_file"
    not_a_dir.write_text("not a directory")
    result = hh.harness_health(
        registry, traces_dir=not_a_dir, warehouse_dir=_warehouse_ok(tmp_path)
    )
    assert result["status"] == "down"
    assert result["reason"] == "traces dir not writable"


def test_ut05_123_missing_current_is_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT05-123 a missing CURRENT file -> degraded."""
    _stub_loopback(monkeypatch, {})
    registry = _registry()
    warehouse_dir = tmp_path / "warehouse"
    warehouse_dir.mkdir()
    result = hh.harness_health(
        registry, traces_dir=_traces_dir(tmp_path), warehouse_dir=warehouse_dir
    )
    assert result["status"] == "degraded"
    assert result["reason"] == "current warehouse unavailable"


def test_ut05_123_garbage_current_is_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT05-123 a CURRENT file that is not a valid build id -> degraded."""
    _stub_loopback(monkeypatch, {})
    registry = _registry()
    warehouse_dir = tmp_path / "warehouse"
    warehouse_dir.mkdir()
    (warehouse_dir / "CURRENT").write_text("not-a-valid-build-id\nwith extra junk after it too")
    result = hh.harness_health(
        registry, traces_dir=_traces_dir(tmp_path), warehouse_dir=warehouse_dir
    )
    assert result["status"] == "degraded"
    assert result["reason"] == "current warehouse unavailable"


def test_ut05_123_registry_health_raising_is_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT05-123 registry.health() raising -> degraded, no exception escapes, clients {}."""
    registry = _registry()

    def boom() -> dict[str, str]:
        msg = "connection refused: secret-token sk-ant-abc123 at /etc/herness/secret.key"
        raise RuntimeError(msg)

    monkeypatch.setattr(registry, "health", boom)
    result = hh.harness_health(
        registry, traces_dir=_traces_dir(tmp_path), warehouse_dir=_warehouse_ok(tmp_path)
    )
    assert result["status"] == "degraded"
    assert result["reason"] == "client health unavailable"
    assert result["clients"] == {}
    assert "secret-token" not in str(result)
    assert "sk-ant-abc123" not in str(result)
    assert "/etc/herness/secret.key" not in str(result)


def test_ut05_123_warehouse_failure_never_leaks_secrets_or_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT05-123 a warehouse-open failure carrying a secret/path in its message never leaks it."""
    _stub_loopback(monkeypatch, {})
    registry = _registry()

    def boom(build_id: str, *, warehouse_dir: Path, sql: SqlSettings) -> None:
        msg = f"failed for api_key=secret:vllm.api_key at {warehouse_dir / 'wh-x.duckdb'}"
        raise RuntimeError(msg)

    monkeypatch.setattr(hh, "open_warehouse", boom)
    warehouse_dir = tmp_path / "warehouse"
    warehouse_dir.mkdir()
    (warehouse_dir / "CURRENT").write_text(BUILD_ID)
    result = hh.harness_health(
        registry, traces_dir=_traces_dir(tmp_path), warehouse_dir=warehouse_dir
    )
    assert result["status"] == "degraded"
    assert result["reason"] == "current warehouse unavailable"
    assert "secret:vllm.api_key" not in str(result)
    assert str(warehouse_dir) not in str(result)


def test_ut05_123_chain_for_raising_non_config_error_is_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT05-123 (I1) chain_for raising something other than ConfigError -> degraded, no leak."""
    _stub_loopback(monkeypatch, {})
    registry = _registry()

    def boom(role: str, depth: str) -> list[str]:
        msg = "boom sk-secret"
        raise RuntimeError(msg)

    monkeypatch.setattr(registry, "chain_for", boom)
    result = hh.harness_health(
        registry, traces_dir=_traces_dir(tmp_path), warehouse_dir=_warehouse_ok(tmp_path)
    )
    assert result["status"] == "degraded"
    assert result["reason"] == "client health unavailable"
    assert result["clients"] == {}
    assert "sk-secret" not in str(result)


def test_ut05_123_health_with_non_str_value_is_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT05-123 (I1) a non-str health() value (e.g. None) -> degraded, no exception escapes."""
    registry = _registry()
    monkeypatch.setattr(registry, "health", lambda: {"a": None})
    result = hh.harness_health(
        registry, traces_dir=_traces_dir(tmp_path), warehouse_dir=_warehouse_ok(tmp_path)
    )
    assert result["status"] == "degraded"
    assert result["reason"] == "client health unavailable"
    assert result["clients"] == {}


def test_ut05_123_route_union_spans_fallback_and_depth_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT05-123 (M3) the route union spans fallback chains and depth overrides, not just the
    standard-depth heads: analyst/chat down but their fallback/override members still up ->
    degraded, not down.
    """
    _stub_loopback(
        monkeypatch,
        {
            "http://127.0.0.1:8101": _FakeResponse(503),  # analyst
            "http://127.0.0.1:8102": _FakeResponse(503),  # chat
        },
    )
    registry = _registry(_config_with_fallback())
    result = hh.harness_health(
        registry, traces_dir=_traces_dir(tmp_path), warehouse_dir=_warehouse_ok(tmp_path)
    )
    assert result["status"] == "degraded"
    assert result["reason"] == "client analyst down; client chat down"


def test_ut05_123_route_union_all_members_down_is_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT05-123 (M3) down only once every route-union member - including a fallback-chain and a
    depth-override member - is down.
    """
    _stub_loopback(
        monkeypatch,
        {
            "http://127.0.0.1:8101": _FakeResponse(503),  # analyst
            "http://127.0.0.1:8102": _FakeResponse(503),  # chat
            "http://127.0.0.1:8104": _FakeResponse(503),  # analyst_fallback
            "http://127.0.0.1:8105": _FakeResponse(503),  # chat_deep
        },
    )
    registry = _registry(_config_with_fallback())
    result = hh.harness_health(
        registry, traces_dir=_traces_dir(tmp_path), warehouse_dir=_warehouse_ok(tmp_path)
    )
    assert result["status"] == "down"
    assert result["reason"] == "analyst/analyst_fallback/chat/chat_deep clients down"


def test_ut05_123_all_route_down_still_lists_other_down_clients(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT05-123 (M3, builder deviation) all route clients down still names a non-route down
    client too; status stays down.
    """
    _stub_loopback(
        monkeypatch,
        {
            "http://127.0.0.1:8101": _FakeResponse(503),  # analyst (route)
            "http://127.0.0.1:8102": _FakeResponse(503),  # chat (route)
            "http://127.0.0.1:8103": _FakeResponse(503),  # extra (not routed)
        },
    )
    registry = _registry()
    result = hh.harness_health(
        registry, traces_dir=_traces_dir(tmp_path), warehouse_dir=_warehouse_ok(tmp_path)
    )
    assert result["status"] == "down"
    assert result["reason"] == "analyst/chat clients down; client extra down"
