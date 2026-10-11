"""Tests for the egress-aware fallback tails of LLMRegistry (T07-25, DD R-81; UT05-132..134)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs
from tests.support.config_tree import register_checked_names, write_repo_config
from tests.unit.harness.test_llm_registry import _config

from herness.core import config as c
from herness.core import egress as eg
from herness.core import registry
from herness.core.errors import ConfigError
from herness.harness.llm.registry import LLMRegistry
from herness.harness.llm.settings import ModelsConfig

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
PROFILES = sorted(p.stem for p in (REPO / "config" / "profiles").glob("*.yaml"))
DEPTHS = ("fast", "standard", "deep")


def _models(mutate: Any = None) -> ModelsConfig:
    data = _config()
    if mutate is not None:
        mutate(data["models"])
    return ModelsConfig.model_validate(data)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    register_checked_names()
    yield
    c.reset_config()
    registry.reset_registry()


def test_ut05_132_off_network_tail_dropped_egress_off_no_outbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT05-132 egress off: cloud tails are dropped with a WARNING; no cloud client is touched."""
    roots: list[str] = []
    monkeypatch.setattr(eg, "loopback_http_client", lambda root, **_kw: roots.append(root))
    guard_calls: list[str] = []
    monkeypatch.setattr(eg, "get_guard", lambda: guard_calls.append("get_guard"))

    with capture_logs() as logs:
        reg = LLMRegistry(_models(), profile="local", egress_enabled=False)

    assert reg.chain_for("planner", "standard") == ["local-30b"]
    assert reg.chain_for("writer", "standard") == ["local-30b", "local-large-offload"]
    assert reg.chain_for("writer", "deep") == ["local-large-offload", "local-30b"]
    dropped = [e for e in logs if e["event"] == "harness.llm.fallback_tail_dropped"]
    assert {(e["role"], e["client"]) for e in dropped} == {
        ("planner", "claude-opus"),
        ("writer", "claude-opus"),
    }
    assert {e["log_level"] for e in dropped} == {"warning"}
    health = reg.health()
    assert "claude-opus" not in health
    assert all("anthropic" not in r for r in roots)
    assert guard_calls == []
    assert reg._clients == {}


def test_ut05_132_off_network_tail_kept_when_egress_enabled() -> None:
    """UT05-132 counterpart: with egress enabled the chain keeps the cloud tail, no warning."""
    with capture_logs() as logs:
        reg = LLMRegistry(_models(), profile="hybrid", egress_enabled=True)
    assert reg.chain_for("planner", "standard") == ["local-30b", "claude-opus"]
    assert not [e for e in logs if e["event"] == "harness.llm.fallback_tail_dropped"]


def _role_head(models: dict[str, Any]) -> None:
    models["roles"]["planner"] = "claude-opus"
    models["fallback"]["planner"][0] = "claude-opus"  # settings: chain head == role


def _depth_head(models: dict[str, Any]) -> None:
    models["depth_overrides"]["deep"]["roles"]["writer"] = "claude-opus"


@pytest.mark.parametrize("mutate", [_role_head, _depth_head])
def test_ut05_133_off_network_heads_still_fail_closed_egress_off(mutate: Any) -> None:
    """UT05-133 role and depth-override heads that are off-network still raise."""
    with capture_logs() as logs, pytest.raises(ConfigError, match="claude-opus is off-network"):
        LLMRegistry(_models(mutate), profile="local", egress_enabled=False)
    assert [e["log_level"] for e in logs if e["event"] == "harness.llm.config_invalid"] == ["error"]
    LLMRegistry(_models(mutate), profile="hybrid", egress_enabled=True)


def _approve(cfg_dir: Path) -> None:
    path = cfg_dir / "herness.yaml"
    text = path.read_text(encoding="utf-8")
    for old, new in (
        ("hybrid_approved: false", "hybrid_approved: true"),
        ("premium_approved: false", "premium_approved: true"),
        ("approved_by: null", "approved_by: ops-lead"),
        ("approved_on: null", "approved_on: 2026-09-01"),
    ):
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("profile", PROFILES)
def test_ut05_134_every_shipped_profile_chains_validate(tmp_path: Path, profile: str) -> None:
    """UT05-134 LLMRegistry validates every shipped profile; egress-off chains are all local."""
    assert {"local", "hybrid", "premium", "synth"} <= set(PROFILES)
    cfg_dir = write_repo_config(tmp_path)
    _approve(cfg_dir)
    cfg = c.load_config(profile, config_dir=cfg_dir, env={})  # type: ignore[arg-type]
    egress_on = cfg.security.egress.enabled
    reg = LLMRegistry(cfg.models, profile=profile, egress_enabled=egress_on)
    roles = set(cfg.models.models.roles) | set(cfg.models.models.fallback)
    assert roles
    for role in sorted(roles):
        for depth in DEPTHS:
            chain = reg.chain_for(role, depth)
            assert chain
            assert all(reg.config(name) for name in chain)
            if not egress_on:
                assert not [n for n in chain if reg.config(n).off_network], (role, depth)
