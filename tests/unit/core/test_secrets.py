"""Tests for secret references, backends and resolution (impl 10 U10-27 to U10-34, T10-06)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, TypeAdapter, ValidationError
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import audit as a
from herness.core import config as c
from herness.core import config_sources as cs
from herness.core import secrets as s
from herness.core.errors import ConfigError, FatalError
from herness.core.logging import configure_logging, get_logger, reset_logging

pytestmark = pytest.mark.unit

USER = "0123456789abcdef0123456789abcdef"
VALUE = "Val-9f3a-sentinel-77"
SVC = "herness"


@pytest.fixture(autouse=True)
def _isolate(fake_keyring: MemoryKeyring) -> Iterator[None]:
    """Every test runs on the in-memory keyring and leaves no config or known values behind."""
    c.reset_config()
    yield
    c.reset_config()


def _dotenv_config(tmp_path: Path, profile: str = "local") -> c.HernessConfig:
    cfg_dir = write_full_config(tmp_path)
    herness = cfg_dir / "herness.yaml"
    text = herness.read_text("utf-8").replace(
        "security:\n", "security:\n  secrets: {backend: dotenv}\n"
    )
    herness.write_text(text, "utf-8")
    return c.init_config(profile, config_dir=cfg_dir, env={})  # type: ignore[arg-type]


# --- UT10-28 resolve, exists, referenced_secret_names --------------------------------------------


def test_ut10_28_resolve_present_and_missing(fake_keyring: MemoryKeyring) -> None:
    """UT10-28 present -> SecretStr and known; missing -> ConfigError naming only the name."""
    fake_keyring.store[(SVC, "vllm.api_key")] = VALUE
    got = s.resolve("secret:VLLM.api_key")
    assert isinstance(got, SecretStr)
    assert got.get_secret_value() == VALUE
    assert VALUE in s.known_values()
    assert s.resolve(s.SecretRef.parse("vllm.api_key")).get_secret_value() == VALUE
    with pytest.raises(ConfigError, match=r"^secret not found: missing\.key$") as exc:
        s.resolve("missing.key")
    assert VALUE not in str(exc.value)
    assert exc.value.message.startswith("secret not found: ")


def test_ut10_28_exists_does_not_remember(fake_keyring: MemoryKeyring) -> None:
    """UT10-28 exists returns a bool and never adds the value to known_values."""
    fake_keyring.store[(SVC, "snow.token")] = VALUE
    assert s.exists("secret:snow.token") is True
    assert s.exists("Snow.Token") is True
    assert s.exists("other.token") is False
    assert VALUE not in s.known_values()


def test_ut10_28_backend_is_keyring_before_config(fake_keyring: MemoryKeyring) -> None:
    """UT10-28 with no cached config resolve uses keyring and does not load a config."""
    fake_keyring.store[(SVC, "k1")] = VALUE
    assert s.resolve("k1").get_secret_value() == VALUE
    assert c._Cache.config is None


def test_ut10_28_known_values_cleared_by_reset(fake_keyring: MemoryKeyring) -> None:
    """UT10-28 reset_config clears the known-values set (X-1 reset hook)."""
    fake_keyring.store[(SVC, "k1")] = VALUE
    s.resolve("k1")
    assert a._known_values() == frozenset({VALUE})
    c.reset_config()
    assert s.known_values() == frozenset()


def test_ut10_28_referenced_names_from_config(tmp_path: Path) -> None:
    """UT10-28 referenced_secret_names on a real config: refs collected, disabled skipped."""
    cfg = c.init_config(config_dir=write_full_config(tmp_path), env={})
    names = s.referenced_secret_names(cfg)
    assert {"redact.hmac_key", "ui_user_ref_key"} <= set(names)
    assert {"vllm.api_key", "anthropic.api_key"} <= set(names)  # models.yaml client references
    assert "typesafe_api_key" not in names  # the disabled ``jev`` decider's reference
    assert names == sorted(set(names))
    assert all(n == n.lower() for n in names)


def test_ut10_28_referenced_names_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-28 walk collects secret: strings, skips enabled: false subtrees, de-duplicates."""
    tree: dict[str, Any] = {
        "sources": {
            "snow": {"enabled": True, "token": "secret:Snow.Token", "hosts": ["a"]},
            "jira": {"enabled": False, "token": "secret:jira.token"},
        },
        "models": {
            "clients": [{"api_key": "secret:VLLM.api_key"}, {"api_key": "secret:snow.token"}]
        },
        "note": "not secret:ish but plain",
        "n": 3,
    }
    monkeypatch.setattr(s, "effective_dict", lambda cfg: tree)
    assert s.referenced_secret_names(object()) == [  # type: ignore[arg-type]
        "redact.hmac_key",
        "snow.token",
        "ui_user_ref_key",
        "vllm.api_key",
    ]


# --- UT10-29 resolve_json ------------------------------------------------------------------------


def test_ut10_29_resolve_json(fake_keyring: MemoryKeyring) -> None:
    """UT10-29 object -> dict of SecretStr, members known; list and invalid JSON -> ConfigError."""
    member = "Member-Secret-88x"
    fake_keyring.store[(SVC, "sf.creds")] = json.dumps({"user": "svc", "password": member})
    got = s.resolve_json("secret:sf.creds")
    assert {k: v.get_secret_value() for k, v in got.items()} == {"user": "svc", "password": member}
    assert all(isinstance(v, SecretStr) for v in got.values())
    assert {"svc", member} <= s.known_values()
    fake_keyring.store[(SVC, "bad.list")] = json.dumps([member])
    fake_keyring.store[(SVC, "bad.json")] = "{" + member
    fake_keyring.store[(SVC, "bad.member")] = json.dumps({"n": 1})
    for name in ("bad.list", "bad.json", "bad.member"):
        with pytest.raises(
            ConfigError, match=rf"^secret {name} is not a JSON object of strings$"
        ) as exc:
            s.resolve_json(name)
        assert member not in str(exc.value)
        assert exc.value.__cause__ is None


# --- UT10-30 references --------------------------------------------------------------------------


def test_ut10_30_parse_forms() -> None:
    """UT10-30 secret:Foo.Bar and a bare name parse lower-cased; x and 'bad name' are rejected."""
    ref = s.SecretRef.parse("secret:Foo.Bar")
    assert ref.name == "foo.bar"
    assert ref == "foo.bar"
    assert s.SecretRef.parse(ref) == ref
    assert s.SecretRef.parse(s.SecretRef("Mixed.Case")).name == "mixed.case"
    with pytest.raises(ConfigError, match=r"^invalid secret name$"):
        s.SecretRef.parse(s.SecretRef("Bad Name"))
    assert s.SecretRef.parse("OPENJEV_API_KEY").name == "openjev_api_key"
    for bad in ("x", "secret:bad name", "secret:", "-lead", "a" * 65, "ok\n"):
        with pytest.raises(ConfigError, match=r"^invalid secret name$") as exc:
            s.SecretRef.parse(bad)
        assert "bad name" not in str(exc.value)


def test_ut10_30_pydantic_types() -> None:
    """UT10-30 SecretRefStr needs secret:<name>; SecretNameStr needs a bare name."""
    refs, names = TypeAdapter(s.SecretRefStr), TypeAdapter(s.SecretNameStr)
    assert refs.validate_python("secret:Foo.Bar") == "secret:Foo.Bar"
    assert names.validate_python("OPENJEV_API_KEY") == "OPENJEV_API_KEY"
    for bad in ("OPENJEV_API_KEY", "secret:x", "secret:bad name", "hunter2 plain"):
        with pytest.raises(ValidationError):
            refs.validate_python(bad)
    for bad in ("secret:foo.bar", "x", "bad name"):
        with pytest.raises(ValidationError):
            names.validate_python(bad)
    assert s.SECRET_NAME.pattern == r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$"


# --- UT10-31 dotenv backend ----------------------------------------------------------------------


def _write_env(root: Path) -> None:
    (root / ".env").write_text(
        "# dev secrets\n\nHERNESS_SECRET__VLLM_API_KEY=" + VALUE + "\n"
        'herness_secret__SF_CREDS="quoted value"\nHERNESS_SECRET__EMPTY_KEY=\n',
        encoding="utf-8",
    )


def test_ut10_31_dotenv_backend(tmp_path: Path) -> None:
    """UT10-31 HERNESS_ENV=dev resolves vllm.api_key from .env; read-only; refused without dev."""
    _write_env(tmp_path)
    backend = s._DotenvBackend("local", env={"HERNESS_ENV": "dev"}, root=tmp_path)
    assert backend.get("vllm.api_key") == VALUE
    assert backend.get("vllm-api-key") == VALUE
    assert backend.get("sf.creds") == "quoted value"
    assert backend.get("empty_key") is None
    assert backend.get("absent") is None
    for call in (lambda: backend.set("a1", "x" * 9), lambda: backend.delete("a1")):
        with pytest.raises(ConfigError, match=r"^dotenv backend is read-only$"):
            call()
    with pytest.raises(
        ConfigError, match=r"^dotenv secrets backend is refused outside dev and synth$"
    ):
        s._DotenvBackend("local", env={}, root=tmp_path)
    assert s._DotenvBackend("synth", env={}, root=tmp_path).get("vllm.api_key") == VALUE
    assert s._DotenvBackend("synth", env={}, root=tmp_path / "none").get("vllm.api_key") is None


def test_ut10_31_dotenv_malformed(tmp_path: Path) -> None:
    """UT10-31 a malformed .env line is a ConfigError naming the line, not its content."""
    (tmp_path / ".env").write_text(f"1BAD={VALUE}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"^\.env line 1: malformed$") as exc:
        s._DotenvBackend("synth", env={}, root=tmp_path)
    assert VALUE not in str(exc.value)


def test_ut10_31_resolve_through_dotenv_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-31 backend: dotenv in config with HERNESS_ENV=dev: resolve reads .env; set refused."""
    _dotenv_config(tmp_path)
    _write_env(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERNESS_ENV", "dev")
    assert s.resolve("secret:vllm.api_key").get_secret_value() == VALUE
    with pytest.raises(ConfigError, match=r"^dotenv backend is read-only$"):
        s.set_secret("vllm.api_key", "long-enough", actor=USER)
    with pytest.raises(ConfigError, match=r"^dotenv backend is read-only$"):
        s.delete_secret("vllm.api_key", actor=USER)
    assert not list(Path(c.get_config().paths.logs).glob("audit-*.jsonl"))


# --- UT10-32 keyring chunking --------------------------------------------------------------------


def test_ut10_32_chunked_value(fake_keyring: MemoryKeyring) -> None:
    """UT10-32 a 3,000-char value: 3 chunks written, reassembled equal, all removed on delete."""
    value = "".join(chr(65 + i % 26) for i in range(3000))
    backend = s._KeyringBackend()
    backend.set("Big.Key", value)
    assert set(fake_keyring.store) == {(SVC, "big.key")} | {
        (SVC, f"big.key#{i}") for i in (1, 2, 3)
    }
    assert fake_keyring.store[(SVC, "big.key")] == "chunked:v1:3"
    assert [len(fake_keyring.store[(SVC, f"big.key#{i}")]) for i in (1, 2, 3)] == [1200, 1200, 600]
    assert backend.get("big.key") == value
    backend.delete("big.key")
    assert fake_keyring.store == {}
    backend.delete("big.key")  # missing: no-op


def test_ut10_32_overwrite_and_edge_values(fake_keyring: MemoryKeyring) -> None:
    """UT10-32 shorter overwrite drops stale chunks; header-like values are chunked; lost chunk."""
    backend = s._KeyringBackend()
    backend.set("k1", "a" * 2500)
    backend.set("k1", "b" * 1300)
    assert set(fake_keyring.store) == {(SVC, "k1"), (SVC, "k1#1"), (SVC, "k1#2")}
    backend.set("k1", "c" * 1200)
    assert fake_keyring.store == {(SVC, "k1"): "c" * 1200}
    backend.set("k2", "chunked:v1:9")
    assert fake_keyring.store[(SVC, "k2")] == "chunked:v1:1"
    assert backend.get("k2") == "chunked:v1:9"
    del fake_keyring.store[(SVC, "k2#1")]
    assert backend.get("k2") is None


# --- UT10-33 set_secret, delete_secret -----------------------------------------------------------


def _audit_lines(cfg: c.HernessConfig) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for p in sorted(Path(cfg.paths.logs).glob("audit-*.jsonl"))
        for line in p.read_text("utf-8").splitlines()
    ]


def test_ut10_33_set_secret_validates_then_audits(
    tmp_path: Path, fake_keyring: MemoryKeyring
) -> None:
    """UT10-33 7-char value -> ConfigError, no audit; valid -> audit secret_set by name, stored."""
    cfg = c.init_config(config_dir=write_full_config(tmp_path), env={})
    for bad in (
        "7-chars",
        "x" * 16_385,
        "line\nbreak-long",
        "nul\x00-long-value",
        "cr\r-long-value",
    ):
        with pytest.raises(
            ConfigError, match=r"^secret value rejected: length or characters$"
        ) as exc:
            s.set_secret("snow.token", bad, actor=USER)
        assert bad not in str(exc.value)
    assert _audit_lines(cfg) == []
    s.set_secret("Snow.Token", VALUE, actor=USER)
    (line,) = _audit_lines(cfg)
    assert line["event"] == "admin_action"
    assert line["actor"] == USER
    assert line["fields"] == {"action": "secret_set", "target": "snow.token"}
    assert VALUE not in json.dumps(line)
    assert fake_keyring.store == {(SVC, "snow.token"): VALUE}
    s.set_secret("long.key", "y" * 16_384, actor="system")
    assert s.resolve("long.key").get_secret_value() == "y" * 16_384


def test_ut10_33_audit_failure_blocks_set(
    tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-33 when the audit line cannot be written the secret is not stored or deleted."""
    c.init_config(config_dir=write_full_config(tmp_path), env={})
    fake_keyring.store[(SVC, "keep.me")] = VALUE

    def boom(*args: object, **kwargs: object) -> None:
        msg = "audit write failed: admin_action"
        raise FatalError(msg)

    monkeypatch.setattr(s, "audit", boom)
    with pytest.raises(FatalError):
        s.set_secret("snow.token", VALUE, actor=USER)
    with pytest.raises(FatalError):
        s.delete_secret("keep.me", actor=USER)
    assert fake_keyring.store == {(SVC, "keep.me"): VALUE}


def test_ut10_33_delete_secret(tmp_path: Path, fake_keyring: MemoryKeyring) -> None:
    """UT10-33 delete_secret audits secret_rotate by name, removes the value; missing is a no-op."""
    cfg = c.init_config(config_dir=write_full_config(tmp_path), env={})
    s.set_secret("big.key", "z" * 3000, actor=USER)
    s.delete_secret("secret:big.key", actor=USER)
    s.delete_secret("never.set", actor=USER)
    assert fake_keyring.store == {}
    actions = [(r["fields"]["action"], r["fields"]["target"]) for r in _audit_lines(cfg)]
    assert actions == [
        ("secret_set", "big.key"),
        ("secret_rotate", "big.key"),
        ("secret_rotate", "never.set"),
    ]
    times = a.last_secret_set_times(Path(cfg.paths.logs), ["big.key"])
    assert times["big.key"] is not None


def test_ut10_30_unvalidated_ref_is_rechecked(fake_keyring: MemoryKeyring) -> None:
    """UT10-30 a directly built SecretRef is re-validated and lower-cased before any backend use."""
    fake_keyring.store[(SVC, "mixed.key")] = VALUE
    assert s.resolve(s.SecretRef("MIXED.Key")).get_secret_value() == VALUE
    assert s.exists(s.SecretRef("Mixed.KEY")) is True
    with pytest.raises(ConfigError, match=r"^invalid secret name$"):
        s.resolve(s.SecretRef("bad name"))


def test_ut10_31_dotenv_root_follows_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-31 synth on a temp config tree reads <config dir parent>/.env, never the cwd's .env."""
    tree, cwd = tmp_path / "tree", tmp_path / "dev"
    cwd.mkdir()
    (cwd / ".env").write_text("HERNESS_SECRET__VLLM_API_KEY=Developer-Real-Value\n", "utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("HERNESS_ENV", raising=False)
    _dotenv_config(tree, "synth")
    assert s._dotenv_root() == tree.resolve()
    assert not s.exists("vllm.api_key")  # tree has no .env: the developer's file is not read
    _write_env(tree)
    c.reset_config()  # also drops the cached (empty) .env read
    c.init_config("synth", config_dir=tree / "config", env={})
    assert s.resolve("vllm.api_key").get_secret_value() == VALUE


def test_ut10_31_dotenv_root_fallbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-31 no cached config: the load context's config dir parent, else the working dir."""
    monkeypatch.chdir(tmp_path)
    assert s._dotenv_root() == Path.cwd()
    with cs.load_context("synth", tmp_path / "other" / "config", {}):
        assert s._dotenv_root() == (tmp_path / "other").resolve()


# --- U10-32 scrub_secrets --------------------------------------------------------------------


def test_ut10_34_nested_known_value_masked(fake_keyring: MemoryKeyring) -> None:
    """UT10-34 a resolved sentinel is replaced by *** wherever it sits, nested or not."""
    fake_keyring.store[(SVC, "svc.key")] = VALUE
    s.resolve("svc.key")
    event: dict[str, Any] = {
        "event": "core.test.value",
        "nested": {"list": [{"inner": f"prefix {VALUE} suffix"}, (VALUE, "kept")]},
        "top": VALUE,
    }
    out = s.scrub_secrets(None, "info", event)
    dumped = json.dumps(out)
    assert VALUE not in dumped
    assert "***" in dumped
    assert out["top"] == "***"


def test_ut10_35_credential_and_url_token_masked() -> None:
    """UT10-35 a password= field and a SAS URL query value both become [SECRET]."""
    event = {
        "event": "core.test.creds",
        "line": "auth failed: password=abc123xyz please retry",
        "url": "https://acct.blob.core.windows.net/c/f?sv=2021&se=2026&sig=AbCdEfGh12345%3D",
    }
    out = s.scrub_secrets(None, "info", event)
    assert "[SECRET]" in out["line"]
    assert "abc123xyz" not in out["line"]
    assert "[SECRET]" in out["url"]
    assert "AbCdEfGh12345" not in out["url"]


def test_ut10_35_scrub_secrets_is_idempotent() -> None:
    """UT10-35 running scrub_secrets again on its own output changes nothing further."""
    event = {"event": "core.test.creds", "line": "password=abc123xyz"}
    once = s.scrub_secrets(None, "info", event)
    twice = s.scrub_secrets(None, "info", dict(once))
    assert once == twice


def test_st10_15_exception_with_secret_is_scrubbed(
    fake_keyring: MemoryKeyring, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST10-15 a resolved secret inside a logged exception's message never reaches stderr."""
    fake_keyring.store[(SVC, "exc.secret")] = VALUE
    s.resolve("exc.secret")
    configure_logging("INFO", scrubber=s.scrub_secrets)
    try:
        log = get_logger("core.test")
        try:
            msg = f"boom: {VALUE}"
            raise ValueError(msg)  # noqa: TRY301 - needs a real traceback frame
        except ValueError:
            log.exception("core.test.exc")
        err = capsys.readouterr().err
        assert VALUE not in err
        assert "***" in err
    finally:
        reset_logging()
