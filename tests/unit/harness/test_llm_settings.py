"""Tests for herness.harness.llm.settings (U05-19): ModelsConfig validation rules."""

import copy
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from herness.core.errors import ConfigError
from herness.harness.llm import settings as s

pytestmark = pytest.mark.unit

_ZERO = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}


def _base() -> dict[str, Any]:
    return {
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
                    "chat_reserved_slots": 2,
                    "price_per_mtok": dict(_ZERO),
                },
                "local-large-offload": {
                    "kind": "openai_compat",
                    "base_url": "http://localhost:8200/v1",
                    "model": "local-large",
                    "context_window": 32768,
                    "max_output_tokens": 4096,
                    "tokenizer": "estimate",
                    "max_concurrency": 1,
                    "price_per_mtok": dict(_ZERO),
                },
                "local-small-cpu": {
                    "kind": "openai_compat",
                    "base_url": "http://[::1]:11434/v1",
                    "model": "small-cpu-model",
                    "context_window": 16384,
                    "max_output_tokens": 2048,
                    "tokenizer": "estimate",
                    "max_concurrency": 2,
                    "price_per_mtok": dict(_ZERO),
                },
                "claude-opus": {
                    "kind": "anthropic",
                    "api_key": "secret:anthropic.api_key",
                    "model": "claude-opus-5-5",
                    "context_window": 1000000,
                    "max_effective_context": 200000,
                    "max_output_tokens": 16000,
                    "tokenizer": "anthropic",
                    "max_concurrency": 50,
                    "thinking_mode": "adaptive_always",
                    "off_network": True,
                    "price_per_mtok": {
                        "input": "4.00",
                        "output": "20.00",
                        "cache_read": "0.20",
                        "cache_write": "5.00",
                    },
                },
            },
            "roles": {"writer": "local-30b", "chat": "local-30b"},
            "fallback": {
                "writer": ["local-30b", "local-large-offload", "claude-opus"],
                "chat": ["local-30b", "claude-opus"],
            },
            "depth_overrides": {
                "fast": {},
                "standard": {},
                "deep": {"roles": {"writer": "local-large-offload"}},
            },
            "role_params": {
                "writer": {"temperature": 0.4, "effort": "high", "thinking": "auto"},
            },
            "anthropic": {"server_side_fallback": False, "cache_ttl": "5m"},
            "depth": {"default": "standard"},
        },
        "harness": {
            "tools": {"max_parallel": 4},
            "sql": {
                "return_rows": 200,
                "scan_rows": 1000000,
                "timeout_s": {"fast": 15, "standard": 30, "deep": 120},
                "threads": 4,
                "memory_limit": "8GB",
                "blocked_columns": ["core.incident.description"],
            },
            "loop": {"wrap_up_ratio": 0.9, "no_progress_steps": 4, "error_streak": 3},
            "verifier": {"float_rel_tol": 0.005, "rerun_timeout_s": 60},
            "trace": {
                "payload_sample_rate": {"eval": 1.0, "chat": 0.0, "review": 0.1},
                "max_payload_chars": 20000,
            },
        },
    }


def _client(cfg: dict[str, Any], key: str) -> dict[str, Any]:
    clients: dict[str, dict[str, Any]] = cfg["models"]["clients"]
    return clients[key]


L30 = "models/clients/local-30b"
OPUS = "models/clients/claude-opus"
_DROP = object()
_Case = tuple[str, str, object, str]


def _case(name: str, path: str, value: object = _DROP, key: str | None = None) -> _Case:
    """One violation: set (or drop) ``path``; ``key`` defaults to ``path`` in dot form."""
    return name, path, value, key if key is not None else path.replace("/", ".")


VIOLATIONS: list[_Case] = [
    _case("r1_plain_api_key", f"{L30}/api_key", "sk-live-abc123"),
    _case("r2_no_base_url", f"{L30}/base_url"),
    _case("r2_non_loopback", f"{L30}/base_url", "http://10.0.0.5:8000/v1"),
    _case("r2_off_network_http", f"{L30}/off_network", True, f"{L30}/base_url".replace("/", ".")),
    _case("r2_bad_scheme", f"{L30}/base_url", "ftp://127.0.0.1/v1"),
    _case("r2_bad_port", f"{L30}/base_url", "http://127.0.0.1:99999/v1"),
    _case("r3_anthropic_base_url", f"{OPUS}/base_url", "https://api.example.com"),
    _case("r3_anthropic_no_thinking", f"{OPUS}/thinking_mode"),
    _case("r3_anthropic_tokenizer", f"{OPUS}/tokenizer", "estimate"),
    _case("r3_anthropic_no_price", f"{OPUS}/price_per_mtok", dict(_ZERO)),
    _case("r4_tokenizer_anthropic_local", f"{L30}/tokenizer", "anthropic"),
    _case("r5_context", f"{L30}/max_output_tokens", 16384),
    _case(
        "r5_effective_context",
        f"{OPUS}/max_effective_context",
        32000,
        "models.clients.claude-opus.max_output_tokens",
    ),
    _case("r6_reserved_slots", f"{L30}/chat_reserved_slots", 6),
    _case("r7_anthropic_server", f"{OPUS}/server", "openai"),
    _case("r8_role_client", "models/roles/writer", "nope"),
    _case(
        "r8_fallback_client",
        "models/fallback/chat",
        ["local-30b", "nope"],
        "models.fallback.chat[1]",
    ),
    _case("r8_depth_client", "models/depth_overrides/deep/roles/writer", "nope"),
    _case(
        "r9_fallback_head", "models/fallback/writer", ["claude-opus"], "models.fallback.writer[0]"
    ),
    _case("r10_server_side_fallback", "models/anthropic/server_side_fallback", True),
    _case(
        "r11_blocked_column",
        "harness/sql/blocked_columns",
        ["raw.x.y"],
        "harness.sql.blocked_columns[0]",
    ),
    _case("range_concurrency", f"{L30}/max_concurrency", 201),
    _case("range_price_negative", f"{L30}/price_per_mtok/input", "-1"),
    _case("range_price_float", f"{L30}/price_per_mtok/input", 0.1),
    _case("range_price_text", f"{L30}/price_per_mtok/input", "abc"),
    _case("range_max_parallel", "harness/tools/max_parallel", 17),
    _case("range_memory_limit", "harness/sql/memory_limit", "8 GB"),
    _case("range_sql_timeout", "harness/sql/timeout_s/deep", 601),
    _case("range_sql_timeout_keys", "harness/sql/timeout_s/deep", _DROP, "harness.sql.timeout_s"),
    _case("range_wrap_up", "harness/loop/wrap_up_ratio", 0.4),
    _case("range_rel_tol", "harness/verifier/float_rel_tol", 0),
    _case("range_sample_rate", "harness/trace/payload_sample_rate/chat", 1.5),
    _case("range_temperature", "models/role_params/writer/temperature", 3.0),
    _case("r37_claim_check", "harness/verifier/claim_check", {"deep": True}),
    _case("r37_claim_checker", "harness/verifier/claim_checker", "openjev"),
    _case("extra_top_level", "deciders", {}),
    _case("missing_section", "harness"),
    _case("name_mismatch", f"{L30}/name", "other"),
]


def _mutate(cfg: dict[str, Any], path: str, value: object) -> None:
    keys = path.split("/")
    node = cfg
    for key in keys[:-1]:
        node = node[key]
    if value is _DROP:
        del node[keys[-1]]
    else:
        node[keys[-1]] = value


def test_ut05_17_base_config_is_valid() -> None:
    """UT05-17 the baseline config validates into a frozen ModelsConfig."""
    cfg = s.ModelsConfig.model_validate(_base())
    assert cfg.models.clients["local-30b"].name == "local-30b"
    assert cfg.models.clients["claude-opus"].price_per_mtok.input == Decimal("4.00")
    assert cfg.harness.sql.timeout_s == {"fast": 15.0, "standard": 30.0, "deep": 120.0}
    with pytest.raises(ValidationError):
        cfg.models.depth.default = "deep"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("path", "value", "key_path"), [c[1:] for c in VIOLATIONS], ids=[c[0] for c in VIOLATIONS]
)
def test_ut05_17_rule_violation_raises_config_error_with_key_path(
    path: str, value: object, key_path: str
) -> None:
    """UT05-17 each design §7 rule violation raises ConfigError naming the key path."""
    cfg = _base()
    _mutate(cfg, path, value)
    with pytest.raises(ConfigError) as info:
        s.ModelsConfig.model_validate(cfg)
    assert info.value.message.startswith(key_path + ":"), info.value.message


def test_ut05_17_plain_key_is_not_echoed() -> None:
    """UT05-17 a plain-text api_key never appears in the error (TH05-15)."""
    cfg = _base()
    _client(cfg, "local-30b")["api_key"] = "sk-live-abc123"
    with pytest.raises(ConfigError) as info:
        s.ModelsConfig.model_validate(cfg)
    assert "sk-live-abc123" not in str(info.value)
    assert "sk-live-abc123" not in repr(info.value.__dict__)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_ut05_17_deciders_given_to_models_section_directly() -> None:
    """UT05-17 a deciders key given to ModelsSection directly raises ConfigError (R-76)."""
    section = copy.deepcopy(_base()["models"])
    section["deciders"] = {"enrich": {}}
    with pytest.raises(ConfigError) as info:
        s.ModelsSection.model_validate(section)
    assert info.value.message.startswith("models.deciders:")


def test_ut05_17_harness_settings_directly() -> None:
    """UT05-17 HarnessSettings converts its own violations to ConfigError."""
    with pytest.raises(ConfigError) as info:
        s.HarnessSettings.model_validate({"verifier": {"claim_check": {}}})
    assert info.value.message.startswith("harness.verifier.claim_check:")
    defaults = s.HarnessSettings()
    assert defaults.sql.blocked_columns[0] == "core.incident.short_description"
    assert defaults.trace.payload_sample_rate == {"eval": 1.0, "chat": 0.0, "review": 0.1}


def test_ut05_17_server_is_inferred_once() -> None:
    """UT05-17 openai_compat server is inferred per D05-08; an explicit server is kept."""
    cfg = _base()
    _client(cfg, "local-30b")["server"] = None
    _client(cfg, "local-large-offload")["base_url"] = "http://127.0.0.1:8200/v1"
    clients = s.ModelsConfig.model_validate(cfg).models.clients
    assert clients["local-30b"].server == "vllm"
    assert clients["local-small-cpu"].server == "ollama"
    assert clients["local-large-offload"].server == "llamacpp"
    assert clients["claude-opus"].server is None
    _client(cfg, "local-small-cpu")["server"] = "openai"
    assert s.ModelsConfig.model_validate(cfg).models.clients["local-small-cpu"].server == "openai"


def test_ut05_17_off_network_https_client_is_allowed() -> None:
    """UT05-17 a non-loopback openai_compat client is valid only off-network over https."""
    cfg = _base()
    _client(cfg, "local-30b").update(base_url="https://llm.example.com/v1", off_network=True)
    client = s.ModelsConfig.model_validate(cfg).models.clients["local-30b"]
    assert client.off_network is True


def test_ut05_17_client_instances_and_defaults() -> None:
    """UT05-17 ModelsSection accepts ClientConfig instances; defaults follow U05-19."""
    base = _base()["models"]
    opus = s.ClientConfig.model_validate({**base["clients"]["claude-opus"], "name": "claude-opus"})
    assert opus.supports == s.ClientSupports()
    assert opus.timeout_s == 300
    section = s.ModelsSection.model_validate(
        {"clients": {"claude-opus": opus}, "roles": {"chat": "claude-opus"}}
    )
    assert section.anthropic == s.AnthropicSettings()
    assert section.depth.default == "standard"
    with pytest.raises(ConfigError) as info:
        s.ModelsSection.model_validate({"clients": {"other": opus}, "roles": {}})
    assert info.value.message.startswith("models.clients.other.name:")
    with pytest.raises(ConfigError) as info:
        s.ModelsSection.model_validate("not a mapping")
    assert info.value.message.startswith("models:")


def test_ut05_17_role_params_optional_values() -> None:
    """UT05-17 RoleParams accepts null temperature and effort."""
    params = s.RoleParams.model_validate({"temperature": None, "effort": None, "thinking": "off"})
    assert params.temperature is None
    assert params.effort is None
