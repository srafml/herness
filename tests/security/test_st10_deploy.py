"""Security tests for the compose file, the env render, class up and rollback (impl 10 ST10-34,
ST10-43, ST10-44, ST10-45, ST10-52).

The compose lint below checks the U10-80 invariants (TH10-26, TH10-27, TH10-29) on the
repository file and on mutated copies. The doctor halves of ST10-34 (``env_file``) and ST10-44
(``firewall_outbound``) are U10-91 checks; they landed with T10-28 in
``tests/security/test_st10_doctor.py``.
"""

from __future__ import annotations

import datetime
import json
import re
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from tests.support.config_tree import REPO, write_checked_config
from tests.support.deploy_host import (
    DeployHost,
    FakeHealth,
    FakeJobs,
    FakeTime,
    render,
    start_deploy_host,
)
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_runner import FakeRunner
from tests.support.pull_host import ACTOR

from herness.admin import commands_deploy, deploy
from herness.core import config as c
from herness.core.errors import ConfigError
from herness.core.settings import DeployConfig

pytestmark = pytest.mark.unit

NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.UTC)
COMPOSE = REPO / "docker" / "compose.yaml"
SERVICES = {"vllm-reasoning", "openjev", "llamacpp-large"}
OFFLINE = ("HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "DO_NOT_TRACK")
_PORT = re.compile(r"127\.0\.0\.1:\$\{[A-Z_]+_PORT\}:[0-9]{1,5}")
_IMAGE = re.compile(r"\$\{[A-Z]+_IMAGE\}")


def _service_issues(name: str, svc: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    found += [f"{name}: port {p}" for p in svc.get("ports", []) if not _PORT.fullmatch(str(p))]
    if svc.get("privileged"):
        found.append(f"{name}: privileged")
    found += [f"{name}: {key} host" for key in ("network_mode", "pid") if svc.get(key) == "host"]
    if "cap_add" in svc:
        found.append(f"{name}: cap_add")
    found += [f"{name}: docker.sock" for v in svc.get("volumes", []) if "docker.sock" in str(v)]
    if svc.get("restart") != "no":
        found.append(f"{name}: restart")
    env = svc.get("environment") or {}
    found += [f"{name}: {flag}" for flag in OFFLINE if env.get(flag) != "1"]
    if name == "vllm-reasoning" and env.get("VLLM_NO_USAGE_STATS") != "1":
        found.append(f"{name}: VLLM_NO_USAGE_STATS")
    if not _IMAGE.fullmatch(str(svc.get("image"))):
        found.append(f"{name}: image")
    return found


def lint_compose(text: str) -> list[str]:
    """Every U10-80 invariant violated by the compose document ``text``."""
    services = yaml.safe_load(text)["services"]
    found = [] if set(services) == SERVICES else ["service set"]
    for name, svc in services.items():
        found += _service_issues(name, svc)
    return found


def _mutated(old: str, new: str) -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    assert old in text
    return text.replace(old, new, 1)


def test_st10_43_repository_compose_passes_lint() -> None:
    """ST10-43 the repository compose file satisfies every U10-80 invariant."""
    assert lint_compose(COMPOSE.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    ("old", "new", "issue"),
    [
        ('  ipc: host\n', '  ipc: host\n  privileged: true\n', "vllm-reasoning: privileged"),
        (
            '"127.0.0.1:${REASONING_PORT}:8000"',
            '"0.0.0.0:8000:8000"',
            "vllm-reasoning: port 0.0.0.0:8000:8000",
        ),
        (
            'volumes: ["${MODEL_ROOT}/gguf:/models:ro"]',
            'volumes: ["${MODEL_ROOT}/gguf:/models:ro", "/var/run/docker.sock:/sock"]',
            "llamacpp-large: docker.sock",
        ),
        ("  ipc: host\n", "  ipc: host\n  network_mode: host\n", "openjev: network_mode host"),
        ("  ipc: host\n", "  ipc: host\n  pid: host\n", "openjev: pid host"),
        ("  ipc: host\n", "  ipc: host\n  cap_add: [SYS_ADMIN]\n", "openjev: cap_add"),
        ('restart: "no"', "restart: always", "openjev: restart"),
        ("${OPENJEV_IMAGE}", "razorback16/openjev:latest", "openjev: image"),
    ],
    ids=["privileged", "lan-port", "docker-sock", "net-host", "pid-host", "cap-add", "restart",
         "image"],
)  # fmt: skip
def test_st10_43_mutated_compose_fails_lint(old: str, new: str, issue: str) -> None:
    """ST10-43 privileged, LAN-published, socket-mounting and similar copies fail the lint."""
    assert issue in lint_compose(_mutated(old, new))


def test_st10_44_missing_offline_flag_fails_lint() -> None:
    """ST10-44 a copy whose llamacpp-large lacks the offline env fails (HF_HUB_OFFLINE)."""
    text = _mutated("    environment: *offline ", "    x-dropped: *offline ")
    issues = lint_compose(text)
    assert "llamacpp-large: HF_HUB_OFFLINE" in issues
    assert all(i.startswith("llamacpp-large") for i in issues)


def test_st10_44_one_flag_and_usage_stats() -> None:
    """ST10-44 one service without HF_HUB_OFFLINE, or vLLM without VLLM_NO_USAGE_STATS, fails."""
    text = _mutated('      <<: *offline\n      VLLM_NO_USAGE_STATS: "1"\n', "      X: '1'\n")
    issues = lint_compose(text)
    assert "vllm-reasoning: HF_HUB_OFFLINE" in issues
    assert "vllm-reasoning: VLLM_NO_USAGE_STATS" in issues
    assert "openjev: HF_HUB_OFFLINE" not in issues


def test_st10_43_compose_variables_are_the_rendered_keys() -> None:
    """ST10-43 every ``${VAR}`` in compose is a key ``render_env`` writes, and every key is used."""
    used = set(re.findall(r"\$\{([A-Z_]+)\}", COMPOSE.read_text(encoding="utf-8")))
    assert used == set(deploy.ENV_KEYS)


def test_st10_34_env_file_under_mnt_is_config_error(tmp_path: Path) -> None:
    """ST10-34 ``deploy.env_file: /mnt/d/herness/docker.env`` is refused at load time."""
    cfg_dir = write_checked_config(tmp_path)
    herness = cfg_dir / "herness.yaml"
    text = herness.read_text("utf-8").replace(
        "deploy:\n", "deploy:\n  env_file: /mnt/d/herness/docker.env\n"
    )
    herness.write_text(text, "utf-8")
    with pytest.raises(ConfigError):
        c.load_config("local", config_dir=cfg_dir)
    base = c.load_config("local", config_dir=write_checked_config(tmp_path / "ok")).deploy
    data = base.model_dump() | {"env_file": "/mnt/d/herness/docker.env"}
    with pytest.raises(ValidationError):
        DeployConfig.model_validate(data)


# --- ST10-45, ST10-52 class up and rollback (T10-25) -----------------------------------------


@pytest.fixture
def deploy_env(
    tmp_path: Path,
    fake_keyring: MemoryKeyring,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DeployHost, FakeJobs, FakeHealth, FakeTime]:
    fake_keyring.store[("herness", "vllm.api_key")] = "vllm-key-st10-sentinel"
    fake_keyring.store[("herness", "openjev_api_key")] = "jev-key-st10-sentinel"
    return start_deploy_host(tmp_path, fake_runner, monkeypatch)


def _audit_fields(cfg: c.HernessConfig) -> list[dict[str, Any]]:
    files = sorted(Path(cfg.paths.logs).glob("audit-*.jsonl"))
    return [json.loads(x)["fields"] for p in files for x in p.read_text("utf-8").splitlines()]


def test_st10_45_live_worker_deploy_up_large_no_compose(
    deploy_env: tuple[DeployHost, FakeJobs, FakeHealth, FakeTime],
) -> None:
    """ST10-45 live worker, ``deploy up large``: no compose call, class requested, audited."""
    host, jobs, _health, _time = deploy_env
    render(host)
    host.runner.calls.clear()
    jobs.alive = True
    result = commands_deploy.cmd_deploy_up("large", cfg=host.cfg, actor=ACTOR)
    assert result.data["via"] == "worker"
    assert jobs.requested == ["large"]
    gpu = host.cfg.resilience.resilience.gpu
    assert not any(a[: len(gpu.compose_cmd)] == list(gpu.compose_cmd) for a in host.runner.argvs)
    assert host.runner.calls == []
    assert not (Path(host.cfg.paths.data) / "locks" / "gpu.lock").exists()
    target = f"large:{host.cfg.deploy.large.sha256}"
    assert {"action": "deploy_up", "target": target} in _audit_fields(host.cfg)


def test_st10_52_rollback_offline_issues_no_download(
    deploy_env: tuple[DeployHost, FakeJobs, FakeHealth, FakeTime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ST10-52 outbound blocked, previous pins on disk: rollback succeeds with no download."""
    host, _jobs, _health, _time = deploy_env

    def no_connect(_sock: socket.socket, address: object) -> None:
        pytest.fail(f"socket connect attempted: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", no_connect)  # socket guard: nothing leaves
    host.outbound = "Block"  # fake Hyper-V firewall: WSL outbound blocked
    for gpu_class in ("reasoning", "decider", "large"):
        result = deploy.rollback_class(host.cfg, gpu_class, actor=ACTOR, now=NOW)
        assert result["status"] == "loaded"
    argvs = host.runner.argvs
    joined = [" ".join(a) for a in argvs]
    assert not any(" pull " in f" {j} " or "download" in j or "huggingface" in j for j in joined)
    assert not any(a[0] == "powershell.exe" for a in argvs)  # the firewall is never opened
    assert host.outbound == "Block"
    rollbacks = [f for f in _audit_fields(host.cfg) if f["action"] == "deploy_rollback"]
    assert len(rollbacks) == 3


def test_st10_52_rollback_target_missing_never_downloads(
    deploy_env: tuple[DeployHost, FakeJobs, FakeHealth, FakeTime],
) -> None:
    """ST10-52 a target not on disk fails with "run deploy pull" and still downloads nothing."""
    host, _jobs, _health, _time = deploy_env
    host.repo_digests.clear()
    with pytest.raises(ConfigError, match="rollback target not on disk; run deploy pull"):
        deploy.rollback_class(host.cfg, "reasoning", actor=ACTOR, now=NOW)
    assert not any("pull" in a or "download" in a for argv in host.runner.argvs for a in argv)
