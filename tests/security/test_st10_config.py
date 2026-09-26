"""Security tests for the herness.yaml models and the root loader (impl 10 ST10-02/16/37)."""

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.support.config_tree import register_checked_names, write_full_config, write_repo_config

from herness.core import config as config_module
from herness.core import config_view, registry
from herness.core.config import config_hash, effective_dict, load_config, reset_config
from herness.core.errors import ConfigError
from herness.core.settings import RedactionConfig

pytestmark = pytest.mark.unit

REDOS = "(a+)+$"
LONG = "a" * 600


@pytest.mark.parametrize(
    ("data", "loc"),
    [
        ({"custom_patterns": {"bad": REDOS}}, ("custom_patterns", "bad")),
        ({"custom_patterns": {"bad": LONG}}, ("custom_patterns", "bad")),
        ({"custom_patterns": {"bad": r"(\w*)*x"}}, ("custom_patterns", "bad")),
        ({"custom_patterns": {"bad": r"(a|b+){3,}"}}, ("custom_patterns", "bad")),
        ({"id_patterns": {"USER_ID": [REDOS]}}, ("id_patterns", "USER_ID", 0)),
        ({"national_id_patterns": [LONG]}, ("national_id_patterns", 0)),
    ],
)
def test_st10_37_redos_and_long_patterns_rejected(
    data: dict[str, Any], loc: tuple[Any, ...]
) -> None:
    """ST10-37 nested-quantifier and over-long redaction patterns fail validation by key."""
    started = time.perf_counter()
    with pytest.raises(ValidationError) as info:
        RedactionConfig.model_validate(data)
    # Generous bound: catastrophic backtracking would take far longer; CI jitter will not.
    assert time.perf_counter() - started < 10.0
    errors = info.value.errors(include_input=False)
    assert [tuple(e["loc"]) for e in errors] == [loc]
    assert REDOS not in str(errors)
    assert LONG not in str(errors)


# --- end to end through load_config (T10-03) ---------------------------------------------------


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Iterator[Path]:
    yield write_full_config(tmp_path)
    reset_config()  # local reset until T11-40 wires reset_config into tests/conftest.py (R3)


def _replace(cfg_dir: Path, old: str, new: str) -> None:
    path = cfg_dir / "herness.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


@pytest.mark.parametrize(
    ("body", "path"),
    [
        ('{directory_file: null, custom_patterns: {bad: "(a+)+$"}}', "custom_patterns.bad"),
        (f"{{directory_file: null, national_id_patterns: [{LONG}]}}", "national_id_patterns.0"),
        ("{directory_file: null, key: plain-hmac-key}", "key"),
    ],
)
def test_st10_37_invalid_security_value_via_load_config(
    cfg_dir: Path, body: str, path: str
) -> None:
    """ST10-37 a ReDoS, over-long or plain-text redaction value fails load_config by key path."""
    _replace(cfg_dir, "redaction: {directory_file: null}", f"redaction: {body}")
    with pytest.raises(ConfigError) as info:
        load_config(config_dir=cfg_dir, env={})
    assert [i.path for i in info.value.issues] == [f"security.redaction.{path}"]  # type: ignore[attr-defined]
    for value in (REDOS, LONG, "plain-hmac-key"):
        assert value not in str(info.value)
        assert value not in repr(info.value.issues)


def test_st10_37_invalid_paths_value_via_load_config(cfg_dir: Path) -> None:
    """ST10-37 an invalid paths value (NUL character) fails load_config without echoing it."""
    _replace(cfg_dir, "data: data,", 'data: "da\\0ta",')
    with pytest.raises(ConfigError) as info:
        load_config(config_dir=cfg_dir, env={})
    assert [i.path for i in info.value.issues] == ["paths.data"]  # type: ignore[attr-defined]
    assert "da\0ta" not in str(info.value)


@pytest.mark.parametrize(
    "override",
    ["security.egress.destinations=[evil.com]", "security.egress.enabled=true", "profile=premium"],
)
def test_st10_02_set_cannot_change_security(cfg_dir: Path, override: str) -> None:
    """ST10-02 --set security.egress.destinations=[evil.com] (and friends): ConfigError."""
    with pytest.raises(ConfigError, match="file-only"):
        load_config(overrides=[override], config_dir=cfg_dir, env={})


def test_st10_16_effective_dict_and_hash_hold_no_resolved_secret(
    cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-16 with sentinel secrets resolved, effective_dict and the hash input hold none."""
    sentinel = "SENTINEL-SECRET-9f3a"
    keyring = {"redact.hmac_key": sentinel}
    captured: list[str] = []
    real = config_view.canonical_json

    def capture(value: object) -> str:
        captured.append(real(value))
        return captured[-1]

    monkeypatch.setattr(config_view, "canonical_json", capture)
    monkeypatch.setattr(
        config_module, "_KEY_ID_PROVIDER", lambda cfg: "kid_" + keyring["redact.hmac_key"][-4:]
    )
    cfg = load_config(config_dir=cfg_dir, env={})
    shown = json.dumps(effective_dict(cfg))
    config_hash(cfg)
    assert sentinel not in shown
    assert captured
    assert all(sentinel not in text for text in captured)
    assert "secret:redact.hmac_key" in shown


# --- end to end on the repository config tree (T10-03b) ---------------------------------------

_OWNER_STEMS = (
    "sources",
    "mappings",
    "decisions",
    "metrics",
    "weights",
    "models",
    "pipelines",
    "memory",
    "resilience",
    "app",
    "eval",
)


@pytest.fixture
def repo_dir(tmp_path: Path) -> Iterator[Path]:
    register_checked_names()
    yield write_repo_config(tmp_path)
    reset_config()
    registry.reset_registry()


def _set_version(path: Path, line: str | None) -> None:
    kept = [
        x for x in path.read_text(encoding="utf-8").splitlines() if not x.startswith("version:")
    ]
    path.write_text("\n".join(([line] if line else []) + kept) + "\n", encoding="utf-8")


@pytest.mark.parametrize("stem", _OWNER_STEMS)
@pytest.mark.parametrize("line", ["version: 2", "version: '1'", "version: true", None])
def test_st10_37_owner_file_version_must_be_1_end_to_end(
    repo_dir: Path, stem: str, line: str | None
) -> None:
    """ST10-37 a wrong or missing top-level `version` in any owner file: ConfigError naming it."""
    _set_version(repo_dir / f"{stem}.yaml", line)
    with pytest.raises(ConfigError, match=rf"^{stem}\.yaml: version must be 1$"):
        load_config("local", config_dir=repo_dir, env={})


def test_st10_37_redos_pattern_in_repo_herness_yaml_fails_load(repo_dir: Path) -> None:
    """ST10-37 a ReDoS custom pattern added to the shipped herness.yaml fails load_config."""
    path = repo_dir / "herness.yaml"
    text = path.read_text(encoding="utf-8")
    assert "    custom_patterns: {}\n" in text
    bad = f'    custom_patterns: {{bad: "{REDOS}"}}\n'
    path.write_text(text.replace("    custom_patterns: {}\n", bad), "utf-8")
    with pytest.raises(ConfigError) as info:
        load_config("synth", config_dir=repo_dir, env={})
    assert [i.path for i in info.value.issues] == ["security.redaction.custom_patterns.bad"]  # type: ignore[attr-defined]
    assert REDOS not in str(info.value)


def test_st10_37_long_injection_pattern_fails_load_without_echo(repo_dir: Path) -> None:
    """ST10-37 a 600-char memory injection pattern fails load_config naming its line only."""
    path = repo_dir / "injection_patterns.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([*lines, LONG]) + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=rf"^injection_patterns line {len(lines) + 1}: "):
        load_config("local", config_dir=repo_dir, env={})
