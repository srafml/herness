"""Tests for ``secrets init|set|status`` (impl 10 U10-68 to U10-70; UT10-72, UT10-33).

Values reach the operator only through the injected ``show``; they are never placed in
``data``, ``warnings`` or the audit line (TH10-07). A backend ``ConfigError`` propagates for
impl 09 to map to exit 3 (R-46).
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness._cli.output import CommandResult
from herness.admin.commands_secrets import (
    INIT_NAMES,
    cmd_secrets_init,
    cmd_secrets_set,
    cmd_secrets_status,
)
from herness.core import config as c
from herness.core import secrets as s
from herness.core import time as clock
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

USER = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret - audit actor id
SVC = "herness"
VALUE = "Val-adm-9f3a-sentinel-77"  # pragma: allowlist secret - test sentinel
START = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)


class SetClock:
    """Settable ``clock.now``, as the U10-64 audit tests use. The freezegun ``fake_clock``
    fixture spun at 100 % CPU in a lazy ``herness.enrich`` import with a config loaded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = START
        monkeypatch.setattr(clock, "now", lambda: self.now)

    def advance(self, delta: datetime.timedelta) -> None:
        self.now += delta


class Prompt:
    """Scripted ``prompt``: records each question and returns the queued answers in order."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []

    def __call__(self, question: str) -> str:
        self.questions.append(question)
        return self.answers.pop(0)


@pytest.fixture
def cfg(tmp_path: Path, fake_keyring: MemoryKeyring) -> Iterator[c.HernessConfig]:
    """A cached config (keyring backend; audit lines under ``paths.logs``)."""
    del fake_keyring
    yield c.init_config(config_dir=write_full_config(tmp_path), env={})
    c.reset_config()


def _audit_lines(cfg: c.HernessConfig) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    for path in sorted(Path(cfg.paths.logs).glob("audit-*.jsonl")):
        lines += [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines()]
    return lines


def _audit_text(cfg: c.HernessConfig) -> str:
    return "".join(p.read_text("utf-8") for p in Path(cfg.paths.logs).glob("audit-*.jsonl"))


def _targets(cfg: c.HernessConfig) -> list[object]:
    return [line["fields"]["target"] for line in _audit_lines(cfg)]  # type: ignore[index]


def _leaks(result: CommandResult, *values: str) -> bool:
    text = json.dumps(result.data) + json.dumps(result.warnings)
    return any(value in text for value in values)


# --- UT10-72 cmd_secrets_init ---------------------------------------------------------------------


def test_ut10_72_init_creates_escrowed_keys(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring
) -> None:
    """UT10-72 both keys absent and escrowed: created, 64 hex, shown once, audited by name."""
    shown: list[str] = []
    prompt = Prompt("ESCROWED", "ESCROWED")
    result = cmd_secrets_init(actor=USER, prompt=prompt, show=shown.append)
    assert (result.ok, result.exit_code, result.warnings) == (True, 0, [])
    assert result.data == {
        "secrets": [
            {"name": "redact.hmac_key", "status": "created"},
            {"name": "ui_user_ref_key", "status": "created"},
        ]
    }
    assert INIT_NAMES == ("redact.hmac_key", "ui_user_ref_key")
    values = [fake_keyring.store[(SVC, name)] for name in INIT_NAMES]
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in values)
    assert values[0] != values[1]
    assert len(shown) == 2
    for name, value, text in zip(INIT_NAMES, values, shown, strict=True):
        assert name in text
        assert value in text
        assert "password vault" in text
    assert prompt.questions == [f"Type ESCROWED after storing {n} in the vault" for n in INIT_NAMES]
    assert not _leaks(result, *values)
    assert _targets(cfg) == list(INIT_NAMES)
    assert not any(value in _audit_text(cfg) for value in values)


def test_ut10_72_init_unconfirmed_key_is_not_created(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring
) -> None:
    """UT10-72 an answer other than ESCROWED stores nothing for that name; exit 1."""
    shown: list[str] = []
    result = cmd_secrets_init(actor=USER, prompt=Prompt("no", "ESCROWED"), show=shown.append)
    assert (result.ok, result.exit_code) == (False, 1)
    assert result.data == {
        "secrets": [
            {"name": "redact.hmac_key", "status": "not created"},
            {"name": "ui_user_ref_key", "status": "created"},
        ]
    }
    assert (SVC, "redact.hmac_key") not in fake_keyring.store
    assert (SVC, "ui_user_ref_key") in fake_keyring.store
    assert result.warnings == ["redact.hmac_key not created: escrow not confirmed"]
    assert _targets(cfg) == ["ui_user_ref_key"]
    assert not _leaks(result, *(text.split()[1] for text in shown))


def test_ut10_72_init_present_keys_are_kept(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring
) -> None:
    """UT10-72 existing keys are reported present; nothing shown, prompted or written."""
    del cfg
    fake_keyring.store[(SVC, "redact.hmac_key")] = "ab" * 32
    fake_keyring.store[(SVC, "ui_user_ref_key")] = VALUE
    before = dict(fake_keyring.store)
    shown: list[str] = []
    prompt = Prompt()
    result = cmd_secrets_init(actor=USER, prompt=prompt, show=shown.append)
    assert (result.ok, result.exit_code) == (True, 0)
    assert result.data == {
        "secrets": [
            {"name": "redact.hmac_key", "status": "present"},
            {"name": "ui_user_ref_key", "status": "present"},
        ]
    }
    assert (shown, prompt.questions, fake_keyring.store) == ([], [], before)


def test_ut10_72_init_backend_error_propagates(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring
) -> None:
    """UT10-72 a keyring failure is a ConfigError (exit 3) that names no value."""
    del cfg
    fake_keyring.error = RuntimeError("backend down")
    with pytest.raises(ConfigError, match="secret backend unavailable"):
        cmd_secrets_init(actor=USER, prompt=Prompt(), show=lambda _text: None)


# --- UT10-72 cmd_secrets_status -------------------------------------------------------------------


def test_ut10_72_status_lists_names_presence_and_last_set(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-72 status lists names, presence and last-set time from audit; missing -> exit 1."""
    SetClock(monkeypatch)
    s.set_secret("redact.hmac_key", "ab" * 32, actor=USER)
    fake_keyring.store[(SVC, "ui_user_ref_key")] = VALUE  # present, never audited
    result = cmd_secrets_status(cfg=cfg)
    assert (result.ok, result.exit_code) == (False, 1)
    assert result.data is not None
    rows = result.data["secrets"]
    assert isinstance(rows, list)
    by_name = {row["name"]: row for row in rows}
    assert [row["name"] for row in rows] == s.referenced_secret_names(cfg)
    assert by_name["redact.hmac_key"] == {
        "name": "redact.hmac_key",
        "present": True,
        "last_set": "2026-09-01T00:00:00.000000Z",
    }
    assert by_name["ui_user_ref_key"] == {
        "name": "ui_user_ref_key",
        "present": True,
        "last_set": None,
    }
    missing = [row["name"] for row in rows if not row["present"]]
    assert "vllm.api_key" in missing
    assert result.warnings == [f"secret missing: {name}" for name in missing]
    assert not _leaks(result, VALUE, "ab" * 32)


def test_ut10_72_status_all_present_and_rotation_warning(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-72 all present -> exit 0; last_set older than 90 days -> "rotate per policy"."""
    clk = SetClock(monkeypatch)
    names = s.referenced_secret_names(cfg)
    for name in names:
        fake_keyring.store[(SVC, name)] = "ab" * 32
    s.set_secret("vllm.api_key", VALUE, actor=USER)
    clk.advance(datetime.timedelta(days=90))
    s.set_secret("anthropic.api_key", VALUE, actor=USER)
    result = cmd_secrets_status(cfg=cfg)
    assert (result.ok, result.exit_code, result.warnings) == (True, 0, [])
    clk.advance(datetime.timedelta(seconds=1))
    result = cmd_secrets_status(cfg=cfg)
    assert (result.ok, result.exit_code) == (True, 0)
    assert result.warnings == ["vllm.api_key: last set 90 days ago; rotate per policy"]


# --- UT10-33 cmd_secrets_set ----------------------------------------------------------------------


def test_ut10_33_cmd_secrets_set_stores_matching_value(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring
) -> None:
    """UT10-33 two matching prompts: audit secret_set by name only, then stored; no echo."""
    prompt = Prompt(VALUE, VALUE)
    result = cmd_secrets_set("secret:Snow.Token", actor=USER, prompt=prompt)
    assert (result.ok, result.exit_code, result.warnings) == (True, 0, [])
    assert result.data == {"name": "snow.token", "stored": True}
    assert prompt.questions == ["Value for snow.token", "Repeat"]
    assert fake_keyring.store[(SVC, "snow.token")] == VALUE
    assert _targets(cfg) == ["snow.token"]
    assert VALUE not in _audit_text(cfg)
    assert not _leaks(result, VALUE)


@pytest.mark.parametrize(
    ("answers", "reason"),
    [
        ((VALUE, VALUE + "x"), "values differ"),
        (("{not json", "{not json"), "a value starting with { must be a JSON object of strings"),
        (('{"a": 1}', '{"a": 1}'), "a value starting with { must be a JSON object of strings"),
        (('{"a": ["b"]}',) * 2, "a value starting with { must be a JSON object of strings"),
        (("x" * 16_385,) * 2, "value exceeds 16 KiB"),
        (("é" * 8_193,) * 2, "value exceeds 16 KiB"),
    ],
)
def test_ut10_33_cmd_secrets_set_rejects(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring, answers: tuple[str, str], reason: str
) -> None:
    """UT10-33 mismatch, a non-object JSON value or more than 16 KiB: exit 1, nothing stored."""
    result = cmd_secrets_set("snow.token", actor=USER, prompt=Prompt(*answers))
    assert (result.ok, result.exit_code) == (False, 1)
    assert result.data == {"name": "snow.token", "stored": False}
    assert result.warnings == [reason]
    assert fake_keyring.store == {}
    assert _audit_lines(cfg) == []
    assert not _leaks(result, *answers)


def test_ut10_33_cmd_secrets_set_json_object_of_strings(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring
) -> None:
    """UT10-33 a value starting with { that is a JSON object of strings is stored as typed."""
    del cfg
    value = '{"user": "svc-reader", "password": "pw-0123456789"}'  # pragma: allowlist secret
    result = cmd_secrets_set("snow.creds", actor=USER, prompt=Prompt(value, value))
    assert result.exit_code == 0
    assert fake_keyring.store[(SVC, "snow.creds")] == value
    assert s.resolve_json("snow.creds")["user"].get_secret_value() == "svc-reader"


def test_ut10_33_cmd_secrets_set_short_value_raises(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring
) -> None:
    """UT10-33 a value of 7 chars: ConfigError from set_secret, nothing audited or stored."""
    with pytest.raises(ConfigError) as info:
        cmd_secrets_set("snow.token", actor=USER, prompt=Prompt("7-chars", "7-chars"))
    assert "7-chars" not in str(info.value)
    assert (fake_keyring.store, _audit_lines(cfg)) == ({}, [])


def test_ut10_33_cmd_secrets_set_bad_name_raises(cfg: c.HernessConfig) -> None:
    """UT10-33 step 1: an invalid name is a ConfigError before any prompt."""
    del cfg
    prompt = Prompt()
    with pytest.raises(ConfigError, match="invalid secret name"):
        cmd_secrets_set("bad name!", actor=USER, prompt=prompt)
    assert prompt.questions == []


def test_ut10_33_cmd_secrets_set_backend_error_propagates(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring
) -> None:
    """UT10-33 F10-05 step 4: backend down -> ConfigError (exit 3); the attempt is audited."""
    fake_keyring.error = RuntimeError("backend down")
    with pytest.raises(ConfigError, match="secret backend unavailable") as info:
        cmd_secrets_set("snow.token", actor=USER, prompt=Prompt(VALUE, VALUE))
    assert VALUE not in str(info.value)
    assert _targets(cfg) == ["snow.token"]


def test_ut10_33_cmd_secrets_set_dotenv_is_read_only(tmp_path: Path) -> None:
    """UT10-33 the dotenv backend refuses writes with ConfigError (exit 3)."""
    cfg_dir = write_full_config(tmp_path)
    herness = cfg_dir / "herness.yaml"
    text = herness.read_text("utf-8").replace(
        "security:\n", "security:\n  secrets: {backend: dotenv}\n"
    )
    herness.write_text(text, "utf-8")
    c.init_config("synth", config_dir=cfg_dir, env={})
    with pytest.raises(ConfigError, match="read-only"):
        cmd_secrets_set("snow.token", actor=USER, prompt=Prompt(VALUE, VALUE))
