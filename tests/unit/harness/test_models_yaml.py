"""Tests for the repository config/models.yaml (U05-72), validated through ModelsConfig."""

from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from herness.harness.llm.settings import ModelsConfig

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
MODELS_YAML = ROOT / "config" / "models.yaml"

# design §5.1.3: context, effective context, max out, tokenizer, max concurrency,
# prices in / out / cache read / cache write; local-30b context per R-52.
TABLE: dict[str, tuple[int, int | None, int, str, int, tuple[str, str, str, str]]] = {
    "local-30b": (32768, None, 4096, "vllm_endpoint", 6, ("0", "0", "0", "0")),
    "local-lora-14b": (32768, None, 4096, "vllm_endpoint", 6, ("0", "0", "0", "0")),
    "local-large-offload": (32768, None, 4096, "estimate", 1, ("0", "0", "0", "0")),
    "local-small-cpu": (16384, None, 2048, "estimate", 2, ("0", "0", "0", "0")),
    "local-judge": (16384, None, 2048, "estimate", 1, ("0", "0", "0", "0")),
    "claude-opus": (1000000, 200000, 16000, "anthropic", 50, ("4.00", "20.00", "0.20", "5.00")),
    "claude-sonnet": (1000000, 200000, 16000, "anthropic", 50, ("2.00", "10.00", "0.20", "2.50")),
    "claude-haiku": (200000, None, 8000, "anthropic", 50, ("1.00", "5.00", "0.10", "1.25")),
}
MODEL_NAMES = {
    "local-30b": "local-30b",
    "local-lora-14b": "local-lora-14b",
    "local-large-offload": "local-large",
    "local-small-cpu": "small-cpu-model",
    "local-judge": "judge-model",
    "claude-opus": "claude-opus-5-5",
    "claude-sonnet": "claude-sonnet-5",
    "claude-haiku": "claude-haiku-4-5",
}
BLOCKED = [
    "core.incident.short_description",
    "core.incident.description",
    "core.incident.close_notes",
    "core.change.short_description",
    "core.change.description",
    "core.problem.root_cause_text",
    "core.work_item.description",
]


def _raw() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8"))
    assert data.pop("version") == 1  # root file key, stripped by the spec 10 loader (U10-16)
    return data


def _load() -> ModelsConfig:
    data = _raw()
    return ModelsConfig.model_validate({key: data[key] for key in ("models", "harness")})


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return {str(k) for k in node} | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def test_ut05_125_repository_config_loads() -> None:
    """UT05-125 config/models.yaml validates; top-level keys are models, harness (and deciders)."""
    assert set(_raw()) <= {"models", "harness", "deciders"}
    assert {"models", "harness"} <= set(_raw())
    assert len(MODELS_YAML.read_text(encoding="utf-8").splitlines()) <= 180
    assert _load().models.depth.default == "standard"


def test_ut05_125_clients_equal_design_table() -> None:
    """UT05-125 client values equal the design §5.1.3 table (R-52 for local-30b)."""
    clients = _load().models.clients
    assert set(clients) == set(TABLE)
    for key, (window, effective, max_out, tokenizer, conc, prices) in TABLE.items():
        client = clients[key]
        assert client.context_window == window, key
        assert client.max_effective_context == effective, key
        assert client.max_output_tokens == max_out, key
        assert client.tokenizer == tokenizer, key
        assert client.max_concurrency == conc, key
        price = client.price_per_mtok
        got = (price.input, price.output, price.cache_read, price.cache_write)
        assert got == tuple(Decimal(p) for p in prices), key
        assert client.model == MODEL_NAMES[key], key
    assert clients["local-30b"].context_window == 32768


# design §7: timeout_s, gpu_class, reasoning_parser, thinking_mode (Claude timeout 600 per impl §9)
CLIENT_EXTRAS: dict[str, tuple[float, str | None, str | None, str | None]] = {
    "local-30b": (300, "reasoning", "qwen3", None),
    "local-lora-14b": (300, "reasoning", None, None),
    "local-large-offload": (3600, "large", None, None),
    "local-small-cpu": (300, None, None, None),
    "local-judge": (300, None, None, None),
    "claude-opus": (600, None, None, "adaptive_always"),
    "claude-sonnet": (600, None, None, "adaptive_optional"),
    "claude-haiku": (600, None, None, "budget"),
}


def test_ut05_125_client_runtime_fields_equal_design() -> None:
    """UT05-125 timeout_s, gpu_class, reasoning_parser and thinking_mode equal design §7."""
    clients = _load().models.clients
    for key, (timeout, gpu, parser, thinking) in CLIENT_EXTRAS.items():
        client = clients[key]
        got = (client.timeout_s, client.gpu_class, client.reasoning_parser, client.thinking_mode)
        assert got == (timeout, gpu, parser, thinking), key


def test_ut05_125_ports_and_network() -> None:
    """UT05-125 local ports are 8000 (vLLM) and 8200 (llama.cpp) per R-51; Claude off-network."""
    clients = _load().models.clients
    ports = {k: urlsplit(c.base_url).port for k, c in clients.items() if c.base_url is not None}
    assert ports == {
        "local-30b": 8000,
        "local-lora-14b": 8000,
        "local-large-offload": 8200,
        "local-small-cpu": 11434,
        "local-judge": 11434,
    }
    assert {k for k, c in clients.items() if c.off_network} == {
        "claude-opus",
        "claude-sonnet",
        "claude-haiku",
    }
    assert clients["local-large-offload"].server == "llamacpp"
    assert clients["local-judge"].server == "ollama"
    assert clients["local-30b"].server == "vllm"


def test_ut05_125_anthropic_supports_blocks() -> None:
    """UT05-125 Anthropic supports follow design §7."""
    clients = _load().models.clients
    for key in ("claude-opus", "claude-sonnet"):
        assert clients[key].supports.sampling_params is False, key
        assert clients[key].supports.effort is True, key
    for key in ("claude-opus", "claude-sonnet", "claude-haiku"):
        assert clients[key].supports.batch is True, key
    assert clients["claude-haiku"].thinking_mode == "budget"


def test_ut05_125_no_verifier_claim_and_secret_refs_only() -> None:
    """UT05-125 no verifier_claim or claim_check key (R-37); api keys are secret: references."""
    raw = _raw()
    keys = _keys(raw["models"]) | _keys(raw["harness"])
    assert not keys & {"verifier_claim", "claim_check", "claim_checker", "deciders"}
    cfg = _load()
    assert "verifier_claim" not in str(cfg.models.fallback)
    for client in cfg.models.clients.values():
        assert client.api_key is None or client.api_key.startswith("secret:")


def test_ut05_125_routing_and_harness_values() -> None:
    """UT05-125 routing and harness values equal design §7 with the U05-72 completions."""
    cfg = _load()
    models = cfg.models
    assert set(models.roles.values()) == {"local-30b", "local-small-cpu"}
    assert models.fallback["writer"] == ["local-30b", "local-large-offload", "claude-opus"]
    assert models.depth_overrides["deep"].roles == {
        "skeptic_final": "local-large-offload",
        "writer": "local-large-offload",
    }
    assert models.role_params["enrich_decider"].temperature == 0.7
    assert models.anthropic.server_side_fallback is False
    sql = cfg.harness.sql
    assert sql.blocked_columns == BLOCKED
    assert sql.timeout_s == {"fast": 15, "standard": 30, "deep": 120}
    assert (sql.return_rows, sql.scan_rows, sql.threads, sql.memory_limit) == (
        200,
        1_000_000,
        4,
        "8GB",
    )
    assert cfg.harness.trace.payload_sample_rate == {"eval": 1.0, "chat": 0.0, "review": 0.1}
    assert cfg.harness.verifier.rerun_timeout_s == 60
