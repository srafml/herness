"""Security tests for the compose file and the env render (impl 10 ST10-34, ST10-43, ST10-44).

The compose lint below checks the U10-80 invariants (TH10-26, TH10-27, TH10-29) on the
repository file and on mutated copies. The doctor halves of ST10-34 (``env_file``) and ST10-44
(``firewall_outbound``) are U10-91 checks and land with T10-28.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from tests.support.config_tree import REPO, write_checked_config

from herness.admin import deploy
from herness.core import config as c
from herness.core.errors import ConfigError
from herness.core.settings import DeployConfig

pytestmark = pytest.mark.unit

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
