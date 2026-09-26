"""Security tests for the herness.yaml models and the root loader (impl 10 ST10-02/16/37)."""

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.support.config_tree import write_full_config

from herness.core import config as config_module
from herness.core import config_view
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
