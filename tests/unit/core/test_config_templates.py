"""Tests for the repository config templates (impl 10 U10-93, U10-94, U10-95; UT10-76).

UT10-76 loads the repository's own `config/` tree (via `tests.support.config_tree.
write_repo_config`, a copy harness that stands in for the owner files that have not shipped
yet) for the `local`, `synth` and `hybrid` profiles, and checks the two non-config-loader
artifacts of U10-95 (`.env.example`, `.streamlit/config.toml`) directly.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.support.config_tree import register_checked_names, write_repo_config

from herness.core import config as c
from herness.core import config_validate as cv
from herness.core import registry
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
PROFILES = ("local", "hybrid", "premium", "synth")
NAMED_SECRETS = (
    "OPENJEV_API_KEY",
    "TYPESAFE_API_KEY",
    "VLLM_API_KEY",
    "ANTHROPIC_API_KEY",
    "REDACT_HMAC_KEY",
    "UI_USER_REF_KEY",
)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    register_checked_names()
    yield
    c.reset_config()
    registry.reset_registry()
    cv.reset_owner_validators()


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    return write_repo_config(tmp_path)


def _profile_yaml(name: str) -> dict[str, Any]:
    text = (REPO / "config" / "profiles" / f"{name}.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


# --- U10-93, U10-94: the templates load through the real loader ----------------------------


def test_ut10_76_local_loads_with_c13_placeholder_warnings_only(cfg_dir: Path) -> None:
    """UT10-76 `local` loads; unpinned `deploy.*` placeholders are C13 warnings, not errors."""
    cfg = c.load_config("local", config_dir=cfg_dir, env={})
    assert cfg.profile == "local"
    assert (cfg.security.egress.enabled, cfg.security.egress.destinations) == (False, ())
    issues = cv.run_cross_checks(cfg, offline=True, include_registry=False)
    assert issues, "expected the shipped deploy.* placeholders to raise C13 warnings"
    assert {i.severity for i in issues} == {"warn"}
    assert {i.message.split()[0] for i in issues} <= {"C13", "C08a"}


def test_ut10_76_synth_loads_with_c13_placeholder_warnings_only(cfg_dir: Path) -> None:
    """UT10-76 `synth` loads; same C13-only placeholder warnings as `local`."""
    cfg = c.load_config("synth", config_dir=cfg_dir, env={})
    assert cfg.profile == "synth"
    assert cfg.security.secrets.backend == "dotenv"
    assert (cfg.security.egress.enabled, cfg.security.egress.destinations) == (False, ())
    issues = cv.run_cross_checks(cfg, offline=True, include_registry=False)
    assert {i.severity for i in issues} == {"warn"}


def test_ut10_76_hybrid_fails_the_data_policy_gate(cfg_dir: Path) -> None:
    """UT10-76 `hybrid` fails to load: `herness.yaml` ships `hybrid_approved: false`."""
    with pytest.raises(ConfigError, match="profile hybrid requires recorded approval"):
        c.load_config("hybrid", config_dir=cfg_dir, env={})


def test_ut10_76_offline_validate_reports_no_errors_for_synth(cfg_dir: Path) -> None:
    """UT10-76 `config validate --profile synth --offline` (via `HernessConfig.validate`)."""
    issues = c.validate(cfg_dir, "synth", offline=True)
    assert [i for i in issues if i.severity == "error"] == []


# --- U10-94: overlay invariants (TH10-03) ---------------------------------------------------


def test_ut10_76_no_profile_overlay_sets_security_data_policy() -> None:
    """UT10-76 no `config/profiles/*.yaml` sets `security.data_policy` (U10-17, TH10-03)."""
    for name in PROFILES:
        security = _profile_yaml(name).get("security") or {}
        assert "data_policy" not in security, name


def test_ut10_76_synth_overlay_never_enables_egress() -> None:
    """UT10-76 `profiles/synth.yaml` sets no `security.egress` key at all."""
    security = _profile_yaml("synth").get("security") or {}
    assert "egress" not in security


def test_ut10_76_hybrid_and_premium_overlays_declare_egress() -> None:
    """UT10-76 `hybrid`/`premium` each declare a non-empty destination and purpose (C24)."""
    for name in ("hybrid", "premium"):
        egress = _profile_yaml(name)["security"]["egress"]
        assert egress["enabled"] is True
        assert egress["destinations"]
        assert egress["purposes"]


def test_ut10_76_stand_in_stems_still_have_no_repo_file() -> None:
    """UT10-76 the four stems `write_repo_config` stands in still have no repo file to use."""
    for stem in ("sources", "mappings", "pipelines", "resilience"):
        assert not (REPO / "config" / f"{stem}.yaml").exists()


# --- U10-95: .env.example and .streamlit/config.toml ----------------------------------------


def test_ut10_76_env_example_has_no_values_and_every_named_secret() -> None:
    """UT10-76 every `.env.example` value is empty; all six U10-95/§3.3 secrets are present."""
    lines = (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
    pairs = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
    for key, value in pairs.items():
        assert value == "", key
    for name in NAMED_SECRETS:
        assert f"HERNESS_SECRET__{name}" in pairs, name


def test_ut10_76_streamlit_config_has_the_four_hardening_keys() -> None:
    """UT10-76 `.streamlit/config.toml` sets the four design 10 §9.2 hardening keys."""
    data = tomllib.loads((REPO / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert data["server"]["address"] == "127.0.0.1"
    assert data["server"]["headless"] is True
    assert data["server"]["enableXsrfProtection"] is True
    assert data["browser"]["gatherUsageStats"] is False
