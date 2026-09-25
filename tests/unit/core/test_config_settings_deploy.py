"""Tests for the deploy models of herness.core.settings (impl 10 U10-07; UT10-19, UT10-61)."""

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from herness.core import settings as st
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "a" * 64
REV = "0123456789abcdef0123456789abcdef01234567"
PLACEHOLDER_DEPLOY: dict[str, Any] = {
    "reasoning": {
        "image": "<reasoning-image>",
        "model": "<reasoning-model>",
        "revision": "<revision>",
        "tool_call_parser": "<parser>",
    },
    "openjev": {"image": "<openjev-image>", "revision": "<revision>"},
    "large": {"image": "<large-image>", "gguf": "<gguf>", "sha256": "<sha256>"},
}
PINNED_DEPLOY: dict[str, Any] = {
    "reasoning": {
        "image": f"vllm/vllm-openai@{DIGEST}",
        "model": "Qwen/Qwen3-30B-A3B",
        "revision": REV,
        "tool_call_parser": "hermes",
    },
    "openjev": {"image": f"ghcr.io/org/openjev@{DIGEST}", "revision": REV},
    "large": {
        "image": f"ghcr.io/ggml-org/llama.cpp@{DIGEST}",
        "gguf": "model-Q4_K_M.gguf",
        "sha256": "b" * 64,
    },
}


def _errors(model: type[BaseModel], data: dict[str, Any]) -> list[tuple[tuple[Any, ...], str]]:
    with pytest.raises(ValidationError) as info:
        model.model_validate(data)
    return [(tuple(e["loc"]), e["type"]) for e in info.value.errors()]


def _deploy(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {k: dict(v) for k, v in PINNED_DEPLOY.items()}
    for path, value in changes.items():
        section, _, key = path.partition("__")
        if key:
            data[section][key] = value
        else:
            data[section] = value
    return data


@pytest.mark.parametrize(
    ("changes", "loc", "kind"),
    [
        ({"reasoning__model": "a/b;rm -rf /"}, ("reasoning", "model"), "string_pattern_mismatch"),
        ({"reasoning__revision": "$(id)"}, ("reasoning", "revision"), "string_pattern_mismatch"),
        ({"reasoning__image": "img`x`"}, ("reasoning", "image"), "string_pattern_mismatch"),
        ({"large__gguf": "a b.gguf"}, ("large", "gguf"), "string_pattern_mismatch"),
        ({"large__sha256": "x" * 257}, ("large", "sha256"), "string_pattern_mismatch"),
        ({"openjev__model": '"quoted"'}, ("openjev", "model"), "string_pattern_mismatch"),
        ({"openjev__image": "<has space>"}, ("openjev", "image"), "string_pattern_mismatch"),
        ({"openjev__image": "a|b"}, ("openjev", "image"), "string_pattern_mismatch"),
        (
            {"reasoning__served_name": "x\n"},
            ("reasoning", "served_name"),
            "string_pattern_mismatch",
        ),
        ({"wsl_distro": "her ness"}, ("wsl_distro",), "string_pattern_mismatch"),
        ({"wsl_distro": "-x"}, ("wsl_distro",), "string_pattern_mismatch"),
        ({"model_root": "opt/hf"}, ("model_root",), "value_error"),
        ({"model_root": "/opt/../etc"}, ("model_root",), "value_error"),
        ({"model_root": "//"}, ("model_root",), "value_error"),
        ({"env_file": "/"}, ("env_file",), "value_error"),
        ({"env_file": "/mnt/c/docker.env"}, ("env_file",), "value_error"),
        ({"env_file": "/opt/docker env"}, ("env_file",), "value_error"),
        ({"reasoning__port": 80}, ("reasoning", "port"), "greater_than_equal"),
        ({"large__port": 65536}, ("large", "port"), "less_than_equal"),
        (
            {"reasoning__gpu_memory_utilization": 0.99},
            ("reasoning", "gpu_memory_utilization"),
            "less_than_equal",
        ),
        (
            {"reasoning__gpu_memory_utilization": 0.05},
            ("reasoning", "gpu_memory_utilization"),
            "greater_than_equal",
        ),
        ({"reasoning__max_model_len": 1023}, ("reasoning", "max_model_len"), "greater_than_equal"),
        (
            {"reasoning__max_model_len": 1_048_577},
            ("reasoning", "max_model_len"),
            "less_than_equal",
        ),
        ({"openjev__max_num_seqs": 0}, ("openjev", "max_num_seqs"), "greater_than_equal"),
        ({"openjev__canvas": 1025}, ("openjev", "canvas"), "less_than_equal"),
        ({"large__gpu_layers": 1000}, ("large", "gpu_layers"), "less_than_equal"),
        ({"service": {"manager": "systemd"}}, ("service", "manager"), "literal_error"),
        (
            {"service": {"account": "svc herness"}},
            ("service", "account"),
            "string_pattern_mismatch",
        ),
        ({"release": {"repo": "org"}}, ("release", "repo"), "string_pattern_mismatch"),
        (
            {"release": {"signer_workflow": "org/repo/.github/workflows/rel.sh"}},
            ("release", "signer_workflow"),
            "string_pattern_mismatch",
        ),
        (
            {"release": {"licence_exceptions": ["a;b"]}},
            ("release", "licence_exceptions", 0),
            "string_pattern_mismatch",
        ),
        (
            {"release": {"duckdb_extensions": {"excel": "A" * 64}}},
            ("release", "duckdb_extensions", "excel"),
            "string_pattern_mismatch",
        ),
        (
            {"release": {"duckdb_extensions": {"json": "a" * 64}}},
            ("release", "duckdb_extensions", "json", "[key]"),
            "literal_error",
        ),
        ({"reasoning__extra": "x"}, ("reasoning", "extra"), "extra_forbidden"),
    ],
)
def test_ut10_19_deploy_load_time(changes: dict[str, Any], loc: tuple[Any, ...], kind: str) -> None:
    """UT10-19 deploy values are argv-safe at load time (patterns, ranges, paths)."""
    assert _errors(st.DeployConfig, _deploy(**changes)) == [(loc, kind)]


def test_ut10_19_deploy_ports_distinct() -> None:
    """UT10-19 deploy ports must be pairwise distinct."""
    errors = _errors(st.DeployConfig, _deploy(openjev__port=8000))
    assert errors == [((), "value_error")]


def test_ut10_19_deploy_release_values() -> None:
    """UT10-19 a complete release block is accepted."""
    cfg = st.DeployConfig.model_validate(
        _deploy(
            release={
                "repo": "acme/herness",
                "signer_workflow": "acme/herness/.github/workflows/release.yml",
                "licence_exceptions": ["some-pkg"],
                "duckdb_extensions": {"excel": "c" * 64},
            },
            service={"manager": "task_scheduler", "account": "svc-herness"},
        )
    )
    assert cfg.release.licence_exceptions == ("some-pkg",)
    assert cfg.release.duckdb_extensions == {"excel": "c" * 64}
    assert cfg.service.manager == "task_scheduler"


def test_ut10_61_pinned_config_passes() -> None:
    """UT10-61 fully pinned deploy values pass the deploy-time rules."""
    cfg = st.DeployConfig.model_validate(PINNED_DEPLOY)
    assert cfg.unpinned_keys() == ()
    cfg.require_pinned()


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        ({"reasoning__image": "<reasoning-image>"}, "reasoning.image"),
        ({"reasoning__image": "vllm/vllm-openai:v0.10.1"}, "reasoning.image"),
        ({"reasoning__revision": REV[:39]}, "reasoning.revision"),
        ({"reasoning__model": "Qwen3-30B"}, "reasoning.model"),
        ({"reasoning__served_name": "-bad"}, "reasoning.served_name"),
        ({"reasoning__tool_call_parser": "Hermes"}, "reasoning.tool_call_parser"),
        ({"reasoning__reasoning_parser": "<parser>"}, "reasoning.reasoning_parser"),
        ({"openjev__image": "ghcr.io/org/openjev:latest"}, "openjev.image"),
        ({"openjev__revision": "main"}, "openjev.revision"),
        ({"large__gguf": "model.bin"}, "large.gguf"),
        ({"large__sha256": "<sha256>"}, "large.sha256"),
    ],
)
def test_ut10_61_unpinned_values_raise(changes: dict[str, Any], key: str) -> None:
    """UT10-61 placeholder, tag instead of digest, 39-hex revision → not pinned."""
    cfg = st.DeployConfig.model_validate(_deploy(**changes))
    assert cfg.unpinned_keys() == (key,)
    with pytest.raises(ConfigError) as info:
        cfg.require_pinned()
    assert info.value.message == f"deploy.{key} is not pinned"
    assert info.value.context["key"] == f"deploy.{key}"


def test_ut10_61_placeholder_template_and_class_filter() -> None:
    """UT10-61 the shipped placeholders are all unpinned; classes can be checked one by one."""
    cfg = st.DeployConfig.model_validate(PLACEHOLDER_DEPLOY)
    assert cfg.unpinned_keys(("large",)) == ("large.image", "large.gguf", "large.sha256")
    assert "reasoning.served_name" not in cfg.unpinned_keys()
    assert len(cfg.unpinned_keys()) == 9
    with pytest.raises(ConfigError, match=r"^deploy\.reasoning\.image is not pinned$"):
        cfg.require_pinned()
    pinned_large = st.DeployConfig.model_validate(
        {**PLACEHOLDER_DEPLOY, "large": PINNED_DEPLOY["large"]}
    )
    pinned_large.require_pinned(("large",))
