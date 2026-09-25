"""Security tests for the config layer sources (impl 10 ST10-01, ST10-03, ST10-05)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from tests.support.config_harness import load, write_config

from herness.core import config_sources as cs
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

BILLION_LAUGHS = """\
a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]
b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]
c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]
d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]
e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d]
f: &f [*e,*e,*e,*e,*e,*e,*e,*e,*e]
g: &g [*f,*f,*f,*f,*f,*f,*f,*f,*f]
h: &h [*g,*g,*g,*g,*g,*g,*g,*g,*g]
i: &i [*h,*h,*h,*h,*h,*h,*h,*h,*h]
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    return write_config(tmp_path)


def test_st10_01_env_cannot_enable_egress(cfg: Path) -> None:
    """ST10-01 HERNESS_SECURITY__EGRESS__ENABLED in env or dev .env: ConfigError."""
    with pytest.raises(ConfigError, match="file-only"):
        load(cfg, env={"HERNESS_SECURITY__EGRESS__ENABLED": "true"})
    (cfg.parent / ".env").write_text("HERNESS_SECURITY__EGRESS__ENABLED=true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="file-only") as info:
        load(cfg, env={"HERNESS_ENV": "dev"})
    assert info.value.message == "security.* is file-only; remove HERNESS_SECURITY__EGRESS__ENABLED"
    assert load(cfg).security.egress.enabled is False


def test_st10_03_profile_cannot_self_approve(cfg: Path) -> None:
    """ST10-03 profiles/hybrid.yaml setting data_policy.hybrid_approved: ConfigError."""
    (cfg / "profiles" / "hybrid.yaml").write_text(
        "version: 1\nsecurity:\n  data_policy:\n    hybrid_approved: true\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="data_policy"):
        load(cfg, "hybrid")
    with pytest.raises(ConfigError, match="data_policy"):
        cs.load_bootstrap("hybrid", cfg, {})


def _timed_reject(path: Path) -> float:
    start = time.perf_counter()
    with pytest.raises(ConfigError):
        cs.load_yaml_file(path)
    return time.perf_counter() - start


def test_st10_05_billion_laughs(tmp_path: Path) -> None:
    """ST10-05 a billion-laughs document is rejected within 1 s."""
    path = tmp_path / "metrics.yaml"
    path.write_text(BILLION_LAUGHS, encoding="utf-8")
    assert _timed_reject(path) < 1.0


def test_st10_05_six_mib_file(tmp_path: Path) -> None:
    """ST10-05 a 6 MiB file is rejected within 1 s."""
    path = tmp_path / "metrics.yaml"
    path.write_bytes(b"a: 1\n" + b"#" * (6 * 1024 * 1024))
    assert _timed_reject(path) < 1.0


def test_st10_05_duplicate_enabled_key(tmp_path: Path) -> None:
    """ST10-05 a duplicate enabled key (value shadowing) is rejected within 1 s."""
    path = tmp_path / "sources.yaml"
    path.write_text(
        "version: 1\nsources:\n  jira:\n    enabled: false\n    enabled: true\n",
        encoding="utf-8",
    )
    assert _timed_reject(path) < 1.0
