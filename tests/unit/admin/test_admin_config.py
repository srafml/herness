"""Tests for ``config validate|show|hash`` (impl 10 U10-65 to U10-67; UT10-20, UT10-15, ST10-16).

The bodies return a ``CommandResult`` and never print; a ``ConfigError`` raised at load by
``config show`` or ``config hash`` propagates for impl 09 to map to exit 3 (R-46).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from tests.support.config_tree import register_checked_names, write_checked_config
from tests.support.fake_keyring import MemoryKeyring

from herness._cli.output import CommandResult
from herness.admin.commands_config import (
    OFFLINE_HASH_WARNING,
    cmd_config_hash,
    cmd_config_show,
    cmd_config_validate,
)
from herness.core import config as c
from herness.core import config_validate as cv
from herness.core import redact
from herness.core import secrets as s
from herness.core.config_view import ConfigIssue
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

HASH_RE = re.compile(r"^cfg_[0-9a-f]{16}$")
LINE_RE = re.compile(r"^(error|warn) \S+ \S+: .+$")
SENTINEL = "SENTINEL-SECRET-adm-4c1e"  # pragma: allowlist secret - test sentinel
HMAC_KEY = "ab" * 32  # pragma: allowlist secret - 64-hex test key
HASH_LINE = f"warn config_hash -: {OFFLINE_HASH_WARNING}"


@pytest.fixture
def cfg_dir(tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The pinned test tree (every offline cross-check passes) with its registry names."""
    del fake_keyring
    monkeypatch.delenv("HERNESS_PROFILE", raising=False)
    register_checked_names()
    return write_checked_config(tmp_path)


def _replace(cfg_dir: Path, old: str, new: str, name: str = "herness.yaml") -> None:
    path = cfg_dir / name
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _validate(cfg_dir: Path, *, strict: bool, offline: bool = True) -> CommandResult:
    return cmd_config_validate(config_dir=cfg_dir, profile=None, offline=offline, strict=strict)


def _owner_error(cfg: object, *, offline: bool) -> list[ConfigIssue]:
    del cfg, offline
    return [ConfigIssue("error", "decisions.primary_decider", "owner check failed", "x.yaml")]


# --- UT10-20 cmd_config_validate ------------------------------------------------------------------


@pytest.mark.parametrize("strict", [False, True])
def test_ut10_20_clean_config_exits_0(cfg_dir: Path, strict: bool) -> None:
    """UT10-20 clean config: exit 0 with and without strict; offline hash note is a warn issue."""
    result = _validate(cfg_dir, strict=strict)
    assert (result.ok, result.exit_code) == (True, 0)
    assert result.data is not None
    assert result.data["profile"] == "local"
    assert HASH_RE.fullmatch(str(result.data["config_hash"]))
    assert result.data["issues"] == [HASH_LINE]
    assert result.warnings == [HASH_LINE]


def test_ut10_20_warning_only_config(cfg_dir: Path) -> None:
    """UT10-20 warning-only config: exit 0 without strict, 1 with strict (R-46)."""
    _replace(cfg_dir, "d" * 64, '"<sha256>"')
    loose, strict = _validate(cfg_dir, strict=False), _validate(cfg_dir, strict=True)
    assert (loose.ok, loose.exit_code) == (True, 0)
    assert (strict.ok, strict.exit_code) == (False, 1)
    assert loose.data is not None
    issues = loose.data["issues"]
    assert isinstance(issues, list)
    assert issues[0].startswith("warn deploy.large.sha256 herness.yaml: ")
    assert all(LINE_RE.fullmatch(line) for line in issues)
    assert HASH_RE.fullmatch(str(loose.data["config_hash"]))


@pytest.mark.parametrize("strict", [False, True])
def test_ut10_20_error_config_exits_1(cfg_dir: Path, strict: bool) -> None:
    """UT10-20 an error issue (owner validator) exits 1 with and without strict."""
    cv.register_owner_validator("admin.test", _owner_error)
    result = _validate(cfg_dir, strict=strict)
    assert (result.ok, result.exit_code) == (False, 1)
    assert result.data is not None
    assert "error decisions.primary_decider x.yaml: owner check failed" in result.data["issues"]
    assert HASH_RE.fullmatch(str(result.data["config_hash"]))


def test_ut10_20_load_error_is_an_issue_not_raised(cfg_dir: Path) -> None:
    """UT10-20 a config that fails to load is reported as issues (exit 1); config_hash null."""
    _replace(cfg_dir, "logging: {level: INFO}", "logging: {level: LOUD}")
    result = _validate(cfg_dir, strict=False)
    assert (result.ok, result.exit_code) == (False, 1)
    assert result.data is not None
    assert result.data["config_hash"] is None
    assert result.data["profile"] == "local"
    issues = result.data["issues"]
    assert isinstance(issues, list)
    assert issues
    assert all(LINE_RE.fullmatch(line) for line in issues)
    assert issues[0].startswith("error logging.level herness.yaml: ")
    assert result.warnings == []


def test_ut10_20_unknown_profile_is_an_issue(cfg_dir: Path) -> None:
    """UT10-20 an unknown profile name is an error issue (exit 1), not an exception."""
    result = cmd_config_validate(config_dir=cfg_dir, profile="nope", offline=True, strict=False)  # type: ignore[arg-type]
    assert (result.ok, result.exit_code) == (False, 1)
    assert result.data == {
        "profile": "nope",
        "config_hash": None,
        "issues": ["error profile -: unknown profile: nope"],
    }


def test_ut10_20_unknown_profile_from_environment_is_named(
    cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-20 an unknown HERNESS_PROFILE (no --profile) is reported by the name tried."""
    monkeypatch.setenv("HERNESS_PROFILE", "nope")
    result = _validate(cfg_dir, strict=False)
    assert (result.ok, result.exit_code) == (False, 1)
    assert result.data == {
        "profile": "nope",
        "config_hash": None,
        "issues": ["error profile -: unknown profile: nope"],
    }


def test_ut10_20_second_load_failure_is_an_error(
    cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-20 the hash load failing alone (config changed between loads) is an error issue."""
    real = c.load_config
    calls: list[int] = []

    def flaky(*args: object, **kwargs: object) -> c.HernessConfig:
        calls.append(1)
        if len(calls) > 1:
            msg = "config dir not found: config"
            raise ConfigError(msg)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(c, "load_config", flaky)
    monkeypatch.setattr("herness.admin.commands_config.load_config", flaky)
    result = _validate(cfg_dir, strict=False)
    assert len(calls) == 2
    assert (result.ok, result.exit_code) == (False, 1)
    assert result.data is not None
    assert result.data["config_hash"] is None
    assert result.data["issues"] == ["error config -: config dir not found: config"]


def test_ut10_20_offline_hash_uses_unresolved_key_id(
    cfg_dir: Path, fake_keyring: MemoryKeyring
) -> None:
    """UT10-20 step 3: offline hashes with key_id "unresolved" even when the key exists."""
    assert redact is not None  # registers the key-id provider of config_hash (U10-11)
    fake_keyring.store[("herness", "redact.hmac_key")] = HMAC_KEY
    cfg = c.load_config(config_dir=cfg_dir, env={})
    offline = _validate(cfg_dir, strict=False).data
    online = _validate(cfg_dir, strict=False, offline=False).data
    assert offline is not None
    assert online is not None
    assert offline["config_hash"] == c.config_hash(cfg, key_id="unresolved")
    assert online["config_hash"] == c.config_hash(cfg)
    assert offline["config_hash"] != online["config_hash"]


def test_ut10_20_profile_resolved_from_environment(
    cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-20 step 1: no --profile -> HERNESS_PROFILE (U10-09 step 1)."""
    monkeypatch.setenv("HERNESS_PROFILE", "synth")
    result = _validate(cfg_dir, strict=True)
    assert result.data is not None
    assert result.data["profile"] == "synth"
    assert result.exit_code == 0


def test_ut10_20_online_hash_has_no_offline_note(cfg_dir: Path) -> None:
    """UT10-20 without --offline the hash uses the key id and no offline note is added."""
    result = _validate(cfg_dir, strict=False, offline=False)
    assert result.data is not None
    assert HASH_RE.fullmatch(str(result.data["config_hash"]))
    assert HASH_LINE not in result.data["issues"]  # type: ignore[operator]
    assert result.warnings == []


# --- UT10-15 cmd_config_hash ----------------------------------------------------------------------


def test_ut10_15_cmd_config_hash_stable(cfg_dir: Path) -> None:
    """UT10-15 reordered keys and a different logging.level hash equal; ^cfg_[0-9a-f]{16}$."""
    first = cmd_config_hash(config_dir=cfg_dir, profile=None)
    assert (first.ok, first.exit_code, first.warnings) == (True, 0, [])
    _replace(cfg_dir, "logging: {level: INFO}", "logging: {level: DEBUG}")
    text = (cfg_dir / "herness.yaml").read_text(encoding="utf-8").splitlines(keepends=True)
    head, rest = text[0], text[1:]
    (cfg_dir / "herness.yaml").write_text(head + "".join(reversed(_blocks(rest))), "utf-8")
    second = cmd_config_hash(config_dir=cfg_dir, profile="local")
    assert first.data == second.data
    assert first.data is not None
    assert HASH_RE.fullmatch(str(first.data["config_hash"]))


def _blocks(lines: list[str]) -> list[str]:
    """Top-level YAML blocks (a key line plus its indented continuation lines)."""
    blocks: list[str] = []
    for line in lines:
        if line[:1] in {" ", "\t"} and blocks:
            blocks[-1] += line
        else:
            blocks.append(line)
    return blocks


def test_ut10_15_cmd_config_hash_load_error_propagates(cfg_dir: Path) -> None:
    """UT10-15 a ConfigError raised at load propagates (impl 09 maps it to exit 3)."""
    _replace(cfg_dir, "logging: {level: INFO}", "logging: {level: LOUD}")
    with pytest.raises(ConfigError):
        cmd_config_hash(config_dir=cfg_dir, profile=None)


# --- ST10-16 cmd_config_show ----------------------------------------------------------------------


def test_st10_16_cmd_config_show_keeps_secret_refs(
    cfg_dir: Path, fake_keyring: MemoryKeyring
) -> None:
    """ST10-16 config show with sentinel secrets resolved: no sentinel, refs stay secret:<name>."""
    for name in ("vllm.api_key", "anthropic.api_key"):
        fake_keyring.store[("herness", name)] = SENTINEL
    fake_keyring.store[("herness", "redact.hmac_key")] = HMAC_KEY  # must be 64 hex (U10-35)
    assert s.resolve("vllm.api_key").get_secret_value() == SENTINEL
    result = cmd_config_show(config_dir=cfg_dir, profile=None)
    assert (result.ok, result.exit_code, result.warnings) == (True, 0, [])
    shown = json.dumps(result.data)
    assert SENTINEL not in shown
    assert HMAC_KEY not in shown
    assert "secret:vllm.api_key" in shown
    assert "secret:anthropic.api_key" in shown


def test_st10_16_cmd_config_show_load_error_propagates(tmp_path: Path) -> None:
    """ST10-16 a missing config dir raises ConfigError (exit 3) without echoing values."""
    with pytest.raises(ConfigError):
        cmd_config_show(config_dir=tmp_path / "absent", profile=None)
