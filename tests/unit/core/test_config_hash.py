"""Tests for config_hash, effective_dict, the cache and ConfigIssue (impl 10 U10-10 to U10-14)."""

from __future__ import annotations

import functools
import hashlib
import json
import re
import tempfile
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.config_tree import write_full_config

from herness.core import config as c
from herness.core import config_view
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

HASH_RE = re.compile(r"^cfg_[0-9a-f]{16}$")
SENTINEL = "SENTINEL-SECRET-9f3a"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    c.reset_config()  # local reset until T11-40 wires reset_config into tests/conftest.py (R3)


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    return write_full_config(tmp_path)


def _load(cfg_dir: Path, **kw: Any) -> c.HernessConfig:
    return c.load_config(config_dir=cfg_dir, env=kw.pop("env", {}), **kw)


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# --- U10-11 config_hash ----------------------------------------------------------------------


def test_ut10_15_hash_ignores_key_order_and_excluded_sections(cfg_dir: Path) -> None:
    """UT10-15 reordered keys and a different logging.level give the same cfg_ hash."""
    first = c.config_hash(_load(cfg_dir))
    assert HASH_RE.fullmatch(first)
    weights = cfg_dir / "weights.yaml"
    lines = weights.read_text(encoding="utf-8").splitlines(keepends=True)
    tz = next(i for i, line in enumerate(lines) if line.startswith("business_timezone:"))
    lines.append(lines.pop(tz))  # move a top-level key to the end of the file
    weights.write_text("".join(lines), encoding="utf-8")
    _edit(cfg_dir / "herness.yaml", "logging: {level: INFO}", "logging: {level: DEBUG}")
    _edit(cfg_dir / "herness.yaml", "backup_target: backup", "backup_target: other")
    second = _load(cfg_dir, overrides=["backup.keep_daily=3"])
    assert second.logging.level == "DEBUG"
    assert c.config_hash(second) == first


def test_ut10_15_hash_excludes_ui_and_deploy_service(cfg_dir: Path) -> None:
    """UT10-15 security.ui and deploy.service are outside the hash; profile is inside."""
    base = c.config_hash(_load(cfg_dir))
    _edit(cfg_dir / "herness.yaml", "security:\n", "security:\n  ui: {port: 9000}\n")
    _edit(cfg_dir / "herness.yaml", "deploy:\n", "deploy:\n  service: {manager: task_scheduler}\n")
    cfg = _load(cfg_dir)
    assert (cfg.security.ui.port, cfg.deploy.service.manager) == (9000, "task_scheduler")
    assert c.config_hash(cfg) == base
    assert c.config_hash(_load(cfg_dir, profile="premium")) != base


def test_ut10_15_path_values_hash_as_posix_text(cfg_dir: Path) -> None:
    """UT10-15 Path values enter the effective dict as POSIX text, the same on every OS."""
    _edit(cfg_dir / "herness.yaml", "{directory_file: null}", "{directory_file: 'D:/x/dir.csv'}")
    _edit(
        cfg_dir / "sources.yaml",
        "version: 1\n",
        "version: 1\nsources:\n  files:\n"
        "    inbox: data/inbox\n    entities: {roster: {pattern: '*.csv', key_field: [id]}}\n",
    )
    shown = c.effective_dict(_load(cfg_dir))
    assert shown["security"]["redaction"]["directory_file"] == "D:/x/dir.csv"
    assert shown["sources"]["sources"]["files"]["inbox"] == "data/inbox"
    assert "\\" not in json.dumps(shown["paths"])


def test_ut10_16_one_weight_changes_hash(cfg_dir: Path) -> None:
    """UT10-16 changing one weight in weights.yaml changes the hash."""
    before = c.config_hash(_load(cfg_dir))
    _edit(
        cfg_dir / "weights.yaml",
        "cost_per_engineer_hour: {unconfirmed: true, value: 95}",
        "cost_per_engineer_hour: {unconfirmed: true, value: 96}",
    )
    assert c.config_hash(_load(cfg_dir)) != before


def test_ut10_16_key_id_and_directory_file_are_inputs(cfg_dir: Path, tmp_path: Path) -> None:
    """UT10-16 key_id and the directory file bytes feed the hash; content is not stored."""
    cfg = _load(cfg_dir)
    assert c.config_hash(cfg, key_id="k1") != c.config_hash(cfg, key_id="k2")
    assert c.config_hash(cfg) == c.config_hash(cfg, key_id="unresolved")
    directory = tmp_path / "directory.csv"
    directory.write_bytes(b"name\nAnn Lee\n")
    _edit(
        cfg_dir / "herness.yaml",
        "{directory_file: null}",
        f"{{directory_file: '{directory.as_posix()}'}}",
    )
    with_dir = _load(cfg_dir)
    first = c.config_hash(with_dir)
    assert first != c.config_hash(cfg)
    directory.write_bytes(b"name\nBo Chan\n")
    changed = c.config_hash(with_dir)
    directory.unlink()
    assert len({first, changed, c.config_hash(with_dir)}) == 3  # absent file hashes as null


def test_ut10_16_unreadable_directory_file(cfg_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-16 a directory file that exists but cannot be read is a ConfigError."""
    cfg = _load(cfg_dir)
    target = cfg_dir / "herness.yaml"
    redaction = cfg.security.redaction.model_copy(update={"directory_file": target})
    security = cfg.security.model_copy(update={"redaction": redaction})
    broken = cfg.model_copy(update={"security": security})

    def deny(self: Path, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Path, "open", deny)
    with pytest.raises(ConfigError, match=r"^cannot read directory_file$"):
        c.config_hash(broken)


def test_ut10_17_hash_input_holds_references_only(
    cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-17 with a fake keyring of sentinels the canonical input holds only secret: refs."""
    keyring = {"redact.hmac_key": SENTINEL, "OPENJEV_API_KEY": SENTINEL}
    captured: list[str] = []
    real = config_view.canonical_json

    def capture(value: object) -> str:
        captured.append(real(value))
        return captured[-1]

    def provider(cfg: c.HernessConfig) -> str:
        secret = keyring[cfg.security.redaction.key.removeprefix("secret:")]
        return "kid_" + hashlib.sha256(secret.encode()).hexdigest()[:8]

    monkeypatch.setattr(config_view, "canonical_json", capture)
    monkeypatch.setattr(c, "_KEY_ID_PROVIDER", provider)
    c.config_hash(_load(cfg_dir))
    data = json.loads(captured[-1])
    assert "secret:" in captured[-1]
    assert SENTINEL not in captured[-1]
    assert data["_inputs"]["redact_key_id"].startswith("kid_")
    assert data["security"]["redaction"]["key"] == "secret:redact.hmac_key"


def test_ut10_17_missing_key_secret_is_unresolved(
    cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-17 a provider that cannot resolve the key secret gives key id 'unresolved'."""
    cfg = _load(cfg_dir)

    def missing(_cfg: c.HernessConfig) -> str:
        msg = "secret not found: redact.hmac_key"
        raise ConfigError(msg)

    monkeypatch.setattr(c, "_KEY_ID_PROVIDER", missing)
    assert c.config_hash(cfg) == c.config_hash(cfg, key_id="unresolved")


def _permuted(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _permuted(node[key]) for key in reversed(list(node))}
    if isinstance(node, list):
        return [_permuted(item) for item in node]
    return node


@functools.cache
def _base_dict() -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="pt10_01_"))
    return c.effective_dict(c.load_config(config_dir=write_full_config(root), env={}))


def _fake(data: dict[str, Any]) -> Any:
    redaction = types.SimpleNamespace(directory_file=None)
    return types.SimpleNamespace(
        model_dump=lambda mode="python": json.loads(json.dumps(data)),
        security=types.SimpleNamespace(redaction=redaction),
    )


_KEYS = st.text(alphabet="abcdefghij_", min_size=1, max_size=6)
_LEAVES = st.none() | st.booleans() | st.integers(-1000, 1000) | st.text(max_size=8)
_TREES = st.recursive(
    _LEAVES,
    lambda kids: st.lists(kids, max_size=3) | st.dictionaries(_KEYS, kids, max_size=4),
    max_leaves=20,
)


@settings(max_examples=60, deadline=None)
@given(section=st.dictionaries(_KEYS, _TREES, min_size=1, max_size=5))
def test_pt10_01_hash_is_order_independent(section: dict[str, Any]) -> None:
    """PT10-01 a random nested section in any key order gives the same config hash."""
    data = dict(_base_dict(), random_section=section)
    first = c.config_hash(_fake(data))
    assert HASH_RE.fullmatch(first)
    assert c.config_hash(_fake(_permuted(data))) == first


# --- U10-12 effective_dict -----------------------------------------------------------------


def test_ut10_18_plain_secret_values_masked(cfg_dir: Path) -> None:
    """UT10-18 a raw dict with password: plain (bypassing validation): *** ; refs unchanged."""
    raw = {
        "db": {"password": "plain", "PWD": "x", "token": "secret:db.token", "port": 5432},
        "list": [{"api_key": "abc", "client_secret": None}, "password"],
        "secret": "secret:other",
        "private_key": "-----BEGIN-----",
    }
    fake = types.SimpleNamespace(model_dump=lambda mode="python": json.loads(json.dumps(raw)))
    shown = c.effective_dict(fake)  # type: ignore[arg-type]
    assert shown["db"] == {
        "password": "***",
        "PWD": "***",
        "token": "secret:db.token",
        "port": 5432,
    }
    assert shown["list"] == [{"api_key": "***", "client_secret": None}, "password"]
    assert (shown["secret"], shown["private_key"]) == ("secret:other", "***")
    assert c.effective_dict(fake, redact_secrets=False) == raw  # type: ignore[arg-type]


def test_ut10_18_loaded_config_keeps_references(cfg_dir: Path) -> None:
    """UT10-18 effective_dict of a loaded config is JSON-shaped and keeps secret: refs."""
    shown = c.effective_dict(_load(cfg_dir))
    assert json.loads(json.dumps(shown)) == shown
    assert shown["security"]["redaction"]["key"] == "secret:redact.hmac_key"
    assert shown["models"]["deciders"]["openjev"]["api_key"].startswith("secret:")
    assert shown["paths"]["data"].endswith("data")


# --- U10-10 cache ----------------------------------------------------------------------------


def test_ut10_22_cache_and_reset_hooks(cfg_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-22 get_config twice gives the same object; reset_config calls hooks, then new object."""
    monkeypatch.chdir(cfg_dir.parent)
    monkeypatch.delenv("HERNESS_PROFILE", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(c, "_RESET_HOOKS", [lambda: calls.append("a"), lambda: calls.append("b")])
    first = c.get_config()
    assert c.get_config() is first
    c.reset_config()
    assert calls == ["a", "b"]
    second = c.get_config()
    assert second is not first
    assert second == first


def test_ut10_22_init_config_replaces_and_logs(cfg_dir: Path) -> None:
    """UT10-22 init_config caches its result; replacing it logs config.cache.replaced."""
    with structlog.testing.capture_logs() as logs:
        first = c.init_config(config_dir=cfg_dir, env={})
        assert c.get_config() is first
        second = c.init_config("premium", config_dir=cfg_dir, env={})
    assert c.get_config() is second
    events = [(e["event"], e["log_level"]) for e in logs]
    assert events.count(("config.load.completed", "info")) == 2
    assert ("config.cache.replaced", "warning") in events
    done = next(e for e in logs if e["event"] == "config.load.completed")
    assert HASH_RE.fullmatch(done["config_hash"])
    assert done["profile"] == "local"
    assert isinstance(done["duration_ms"], int)


# --- U10-14 ConfigIssue ----------------------------------------------------------------------


def test_rf_config_issue_renders_design_format() -> None:
    """RF ConfigIssue renders 'severity path file: message'; file None shows '-'."""
    issue = c.ConfigIssue("error", "security.egress", "bad host", "herness.yaml")
    assert str(issue) == "error security.egress herness.yaml: bad host"
    assert str(c.ConfigIssue("warn", "<file>", "x", None)) == "warn <file> -: x"
    with pytest.raises(AttributeError):
        issue.path = "y"  # type: ignore[misc]
