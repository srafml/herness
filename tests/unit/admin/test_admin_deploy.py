"""Tests for ``herness deploy render`` and the deploy history (impl 10 U10-78, U10-79).

UT10-60 (render: keys, stdin-only secrets, ``rendered.json``, audit), the U10-78 half of
UT10-65 (``DeployHistory``), UT10-75 on the repository compose file, and the ``fake_runner``
plugin the admin deploy cards share.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.config_tree import REPO, write_checked_config, write_full_config
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_runner import FakeRunner

from herness.admin import deploy, wsl
from herness.core import config as c
from herness.core import config_validate as cv
from herness.core import time as clock
from herness.core.errors import ConfigError, FatalError, StoreBusy

pytestmark = pytest.mark.unit

USER = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret - audit actor id
SVC = "herness"
VLLM_KEY = "vllm-key-7f3a-sentinel"  # pragma: allowlist secret - test sentinel
JEV_KEY = "jev-key-91c2-sentinel"  # pragma: allowlist secret - test sentinel
NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.UTC)
ENV_FILE = "/opt/herness/docker.env"
EXPECTED_KEYS = [
    "LARGE_CTX", "LARGE_GGUF", "LARGE_GPU_LAYERS", "LARGE_IMAGE", "LARGE_PORT", "MODEL_ROOT",
    "OPENJEV_API_KEY", "OPENJEV_CANVAS", "OPENJEV_GPU_UTIL", "OPENJEV_IMAGE",
    "OPENJEV_MAX_NUM_SEQS", "OPENJEV_MODEL", "OPENJEV_PORT", "REASONING_GPU_UTIL",
    "REASONING_IMAGE", "REASONING_MAX_MODEL_LEN", "REASONING_MODEL", "REASONING_PARSER",
    "REASONING_PORT", "REASONING_REVISION", "REASONING_SERVED_NAME", "REASONING_TOOL_PARSER",
    "VLLM_API_KEY",
]  # fmt: skip
WSL_PREFIX = ["wsl.exe", "-d", "herness", "-u", "root", "--exec", "sh", "-c"]


@pytest.fixture
def keys(fake_keyring: MemoryKeyring) -> MemoryKeyring:
    fake_keyring.store[(SVC, "vllm.api_key")] = VLLM_KEY
    fake_keyring.store[(SVC, "openjev_api_key")] = JEV_KEY
    return fake_keyring


@pytest.fixture
def cfg(
    tmp_path: Path, keys: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> Iterator[c.HernessConfig]:
    """A cached, pinned config; ``clock.now`` fixed at ``NOW``."""
    del keys
    monkeypatch.setattr(clock, "now", lambda: NOW)
    yield c.init_config(config_dir=write_checked_config(tmp_path), env={})
    c.reset_config()


def _audit_lines(cfg: c.HernessConfig) -> list[dict[str, object]]:
    files = sorted(Path(cfg.paths.logs).glob("audit-*.jsonl"))
    return [json.loads(line) for p in files for line in p.read_text("utf-8").splitlines()]


def _rendered(cfg: c.HernessConfig) -> Path:
    return Path(cfg.paths.data) / "deploy" / "rendered.json"


# --- UT10-60 render_env ----------------------------------------------------------------------


def test_ut10_60_render_writes_sorted_keys_via_stdin(
    cfg: c.HernessConfig, fake_runner: FakeRunner
) -> None:
    """UT10-60 stdin holds exactly the listed keys, sorted; secrets never reach argv."""
    fake_runner.add(["wsl.exe"])
    result = deploy.render_env(cfg, actor=USER)
    assert result == dict.fromkeys(EXPECTED_KEYS, "set")
    [call] = fake_runner.calls
    assert list(call.argv) == [*WSL_PREFIX, deploy.SCRIPT, "sh", ENV_FILE]
    assert call.timeout_s == 30
    assert call.stdin is not None
    lines = call.stdin.decode("utf-8").splitlines()
    assert [line.split("=", 1)[0] for line in lines] == EXPECTED_KEYS
    env = dict(line.split("=", 1) for line in lines)
    assert env["VLLM_API_KEY"] == VLLM_KEY
    assert env["OPENJEV_API_KEY"] == JEV_KEY
    assert env["REASONING_IMAGE"] == cfg.deploy.reasoning.image
    assert env["REASONING_GPU_UTIL"] == "0.9"
    assert env["REASONING_PORT"] == "8000"
    assert env["OPENJEV_PORT"] == "8100"
    assert env["LARGE_GGUF"] == "qwen3-large.gguf"
    assert env["MODEL_ROOT"] == "/opt/herness/hf"
    assert call.stdin.endswith(b"\n")
    assert not any(VLLM_KEY in a or JEV_KEY in a for a in call.argv)
    assert VLLM_KEY not in json.dumps(result)


def test_ut10_60_script_is_the_fixed_constant() -> None:
    """UT10-60 the shell script is the spec's constant; env_file enters only as ``$1``."""
    assert deploy.SCRIPT == (
        'umask 077; cat > "$1.tmp" && chown root:root "$1.tmp" && chmod 600 "$1.tmp" '
        '&& mv "$1.tmp" "$1"'
    )


def test_ut10_60_rendered_json_and_audit(cfg: c.HernessConfig, fake_runner: FakeRunner) -> None:
    """UT10-60 ``rendered.json`` carries the stdin SHA-256; one audit line, no secret in it."""
    fake_runner.add(["wsl.exe"])
    deploy.render_env(cfg, actor=USER)
    stdin = fake_runner.calls[0].stdin
    assert stdin is not None
    data = json.loads(_rendered(cfg).read_text("utf-8"))
    assert data == {
        "config_hash": c.config_hash(cfg),
        "env_sha256": hashlib.sha256(stdin).hexdigest(),
        "ts": "2026-09-24T12:00:00.000000Z",
    }
    [line] = _audit_lines(cfg)
    assert line["event"] == "admin_action"
    assert line["actor"] == USER
    assert line["fields"] == {"action": "deploy_render", "target": ENV_FILE}
    logs = "".join(p.read_text("utf-8") for p in Path(cfg.paths.logs).glob("*"))
    assert VLLM_KEY not in logs
    assert JEV_KEY not in logs
    assert not list(_rendered(cfg).parent.glob("*.tmp"))


@pytest.mark.parametrize("bad", ["a\nB=1", "a\rb", "a\0b"], ids=["lf", "cr", "nul"])
def test_ut10_60_value_with_newline_is_config_error(
    cfg: c.HernessConfig, fake_runner: FakeRunner, keys: MemoryKeyring, bad: str
) -> None:
    """UT10-60 a value with ``\\n``, ``\\r`` or NUL is ``ConfigError`` naming the key only."""
    keys.store[(SVC, "vllm.api_key")] = bad
    with pytest.raises(ConfigError) as info:
        deploy.render_env(cfg, actor=USER)
    assert info.value.message == "env value for VLLM_API_KEY invalid"
    assert fake_runner.calls == []
    assert not _rendered(cfg).exists()
    assert _audit_lines(cfg) == []


def test_ut10_60_missing_secret_is_config_error(
    cfg: c.HernessConfig, fake_runner: FakeRunner, keys: MemoryKeyring
) -> None:
    """UT10-60 an absent ``OPENJEV_API_KEY`` stops the render before any command."""
    del keys.store[(SVC, "openjev_api_key")]
    with pytest.raises(ConfigError, match="secret not found: openjev_api_key"):
        deploy.render_env(cfg, actor=USER)
    assert fake_runner.calls == []


def test_ut10_60_unpinned_config_is_config_error(
    tmp_path: Path, keys: MemoryKeyring, fake_runner: FakeRunner
) -> None:
    """UT10-60 the U10-07 deploy-time pin rules are a precondition (placeholders refused)."""
    del keys
    cfg = c.init_config(config_dir=write_full_config(tmp_path), env={})
    try:
        with pytest.raises(ConfigError, match=r"deploy\.reasoning\.image is not pinned"):
            deploy.render_env(cfg, actor=USER)
    finally:
        c.reset_config()
    assert fake_runner.calls == []


def test_ut10_60_nonzero_exit_is_fatal(cfg: c.HernessConfig, fake_runner: FakeRunner) -> None:
    """UT10-60 a failing WSL write is ``FatalError``; nothing is recorded or audited."""
    fake_runner.add(["wsl.exe"], returncode=1, stderr="chown: Operation not permitted")
    with pytest.raises(FatalError, match="deploy render failed: exit 1"):
        deploy.render_env(cfg, actor=USER)
    assert not _rendered(cfg).exists()
    assert _audit_lines(cfg) == []


# --- UT10-65 (U10-78 half) DeployHistory -----------------------------------------------------


def _pins(tag: str) -> dict[str, str]:
    return {"image": f"img-{tag}", "model": "Qwen/Qwen3", "revision": f"rev-{tag}"}


def test_ut10_65_history_records_distinct_pin_sets(tmp_path: Path) -> None:
    """UT10-65 a repeated newest pin set is not appended; newest distinct sets come first."""
    history = deploy.DeployHistory(tmp_path / "deploy" / "history.jsonl")
    for tag in ("a", "a", "b", "c", "b", "b"):
        history.record("reasoning", _pins(tag), "cfg_1", NOW)
    history.record("large", {"image": "i", "gguf": "m.gguf", "sha256": "s"}, "cfg_1", NOW)
    lines = (tmp_path / "deploy" / "history.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 5
    first = json.loads(lines[0])
    assert first == {
        "ts": "2026-09-24T12:00:00.000000Z",
        "gpu_class": "reasoning",
        "pins": _pins("a"),
        "config_hash": "cfg_1",
    }
    assert lines[0] == json.dumps(first, sort_keys=True, separators=(",", ":"))
    assert history.last_promoted("reasoning") == [_pins("b"), _pins("c")]
    assert history.last_promoted("reasoning", n=5) == [_pins("b"), _pins("c"), _pins("a")]
    assert history.last_promoted("large") == [{"image": "i", "gguf": "m.gguf", "sha256": "s"}]
    assert history.last_promoted("decider") == []
    assert (tmp_path / "deploy" / ".deploy.lock").exists()


def test_ut10_65_history_skips_unreadable_lines(tmp_path: Path) -> None:
    """UT10-65 a torn or foreign line is ignored; an absent file has no history."""
    path = tmp_path / "history.jsonl"
    history = deploy.DeployHistory(path)
    assert history.last_promoted("reasoning") == []
    path.write_text('{"torn\n[1]\n{"gpu_class": "reasoning", "pins": 3}\n', encoding="utf-8")
    assert history.last_promoted("reasoning") == []
    history.record("reasoning", _pins("a"), "cfg_1", NOW)
    assert history.last_promoted("reasoning") == [_pins("a")]


def test_ut10_65_history_default_path(cfg: c.HernessConfig) -> None:
    """UT10-65 the default file is ``<paths.data>/deploy/history.jsonl``."""
    history = deploy.DeployHistory()
    history.record("decider", _pins("a"), "cfg_1", NOW)
    assert (Path(cfg.paths.data) / "deploy" / "history.jsonl").is_file()


def test_ut10_65_history_lock_busy_is_store_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-65 a held deploy lock surfaces as ``StoreBusy``; nothing is appended."""
    history = deploy.DeployHistory(tmp_path / "history.jsonl")

    def busy(*_a: object, **_k: object) -> None:
        msg = "log lock timeout: .deploy.lock"
        raise StoreBusy(msg)

    monkeypatch.setattr(deploy, "log_lock", busy)
    with pytest.raises(StoreBusy):
        history.record("reasoning", _pins("a"), "cfg_1", NOW)
    assert not (tmp_path / "history.jsonl").exists()


# --- UT10-75 repository compose file ---------------------------------------------------------


def test_ut10_75_repository_compose_cross_check(tmp_path: Path) -> None:
    """UT10-75 repository compose + impl 08 resilience.yaml: no C08a; a moved port is C08a."""
    compose = REPO / "docker" / "compose.yaml"
    cfg_dir = write_checked_config(tmp_path)
    cfg = c.load_config("local", config_dir=cfg_dir)
    issues = cv.run_cross_checks(cfg, offline=True, include_registry=False, compose_path=compose)
    assert [i for i in issues if i.message.startswith("C08a")] == []
    herness = cfg_dir / "herness.yaml"
    text = herness.read_text("utf-8").replace("large: {", "large: {port: 8201, ", 1)
    herness.write_text(text, "utf-8")
    with pytest.raises(ConfigError) as info:
        c.load_config("local", config_dir=cfg_dir)
    assert any(i.message.startswith("C08a") for i in info.value.issues)


# --- fake_runner plugin ----------------------------------------------------------------------


def test_ut10_60_fake_runner_longest_prefix_and_check(fake_runner: FakeRunner) -> None:
    """UT10-60 support: the longest prefix wins; ``check=True`` turns non-zero into Fatal."""
    fake_runner.add(["docker"], stdout="any")
    fake_runner.add(["docker", "image", "inspect"], returncode=1)
    assert wsl.run_cmd(["docker", "ps"], timeout_s=1).stdout == "any"
    assert wsl.run_cmd(["docker", "image", "inspect", "x"], timeout_s=1).returncode == 1
    with pytest.raises(FatalError, match="docker failed: exit 1"):
        wsl.run_cmd(["docker", "image", "inspect", "x"], timeout_s=1, check=True)
    assert [call.argv[1] for call in fake_runner.calls] == ["ps", "image", "image"]


def test_ut10_60_fake_runner_keeps_wsl_checks(fake_runner: FakeRunner) -> None:
    """UT10-60 support: ``run_wsl`` validation still runs; unknown programs are refused."""
    fake_runner.add(["wsl.exe"])
    with pytest.raises(ConfigError, match="unsafe argument for wsl"):
        wsl.run_wsl("herness", ["echo", "a;b"], timeout_s=1)
    with pytest.raises(ConfigError, match="command not allowed: bash"):
        wsl.run_cmd(["bash"], timeout_s=1)
    assert fake_runner.calls == []


def test_ut10_60_fake_runner_unmatched_fails(fake_runner: FakeRunner) -> None:
    """UT10-60 support: an argv no row matches fails the test instead of passing silently."""
    with pytest.raises(pytest.fail.Exception, match="no row matches"):
        wsl.run_cmd(["docker", "ps"], timeout_s=1)
