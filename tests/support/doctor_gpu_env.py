"""A scripted WSL/GPU host for the doctor GPU rows (impl 10 U10-91; T10-28).

``GpuHost`` extends the ``deploy pull`` host of ``tests.support.pull_host`` (pinned config,
``docker image inspect``, the ``verify_weights`` calls, the firewall read-back) with the rows
only the doctor runs: ``wsl.exe -l -q``, ``systemctl``, ``ss``, ``nvidia-smi``, ``git``, the
root ``stat`` of the env file and ``Get-NetFirewallRule``. Health GETs go through the real
``LoopbackHttp`` over an ``httpx2.MockTransport`` client installed as
``egress.loopback_http_client``. Tests change the public fields before running a check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import keyring
import pytest
from tests.support.doctor_host_env import FakeGpuState
from tests.support.fake_runner import FakeRunner
from tests.support.pull_host import PS, WSL, WSL_ROOT, PullHost, start_pull_host

from herness.admin import doctor_gpu
from herness.admin.wsl import CmdResult
from herness.core import egress

OPENJEV_KEY = "Oj-test-key-0123456789"  # pragma: allowlist secret - fake keyring value
SS_OK = (
    "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process\n"
    "LISTEN 0      4096   127.0.0.53%lo:53         0.0.0.0:*\n"
    "LISTEN 0      4096       127.0.0.1:8000       0.0.0.0:*\n"
    "LISTEN 0      4096            [::]:22            [::]:*\n"
)
NVIDIA_OK = "NVIDIA GeForce RTX 4090, 24564\n"
WSL_LIST = "﻿Ubuntu-24.04\r\nherness\r\n"


def inbound_rules(*ports: int, enabled: Any = 1, action: Any = 4) -> str:
    """``Get-NetFirewallRule | ConvertTo-Json`` of one inbound rule per port (enums as ints)."""
    rules = [
        {
            "DisplayName": f"herness-block-inbound-{port}",
            "Enabled": enabled,
            "Direction": 1,
            "Action": action,
        }
        for port in ports
    ]
    return json.dumps(rules[0] if len(rules) == 1 else rules)


@dataclass
class GpuHost:
    """The pull host plus the doctor-only outputs and the health endpoints."""

    pull: PullHost
    outputs: dict[str, CmdResult] = field(default_factory=dict)
    inbound: CmdResult = field(default_factory=lambda: CmdResult(0, "", "", 0.0))
    health: dict[str, int] = field(default_factory=dict)  # service URL -> status
    requests: list[tuple[str, str | None, float]] = field(default_factory=list)
    gpu: FakeGpuState = field(default_factory=lambda: FakeGpuState("reasoning"))

    @property
    def cfg(self) -> Any:
        return self.pull.cfg

    @property
    def runner(self) -> FakeRunner:
        return self.pull.runner

    def set(self, key: str, stdout: str, returncode: int = 0) -> None:
        """Answer the doctor program ``key`` (``wsl -l``, ``systemctl`` ...) with ``stdout``."""
        self.outputs[key] = CmdResult(returncode, stdout, "", 0.0)

    def _answer(self, key: str) -> Any:
        return lambda argv: self.outputs[key]

    def _ps(self, argv: list[str]) -> CmdResult:
        if argv[-1].startswith("Get-NetFirewallRule"):
            return self.inbound
        return self.pull._ps(argv)

    def _client(self, base_url: str, *, timeout_s: float, bearer: Any = None) -> httpx2.Client:
        def handle(request: httpx2.Request) -> httpx2.Response:
            auth = request.headers.get("authorization")
            self.requests.append((str(request.url), auth, timeout_s))
            return httpx2.Response(self.health.get(base_url, 503))

        headers = {"Authorization": f"Bearer {bearer.get_secret_value()}"} if bearer else {}
        return httpx2.Client(
            base_url=base_url, transport=httpx2.MockTransport(handle), headers=headers
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> GpuHost:
        rows = {
            "wsl -l": ["wsl.exe", "-l", "-q"],
            "systemctl": [*WSL, "systemctl"],
            "ss": [*WSL, "ss"],
            "nvidia-smi": [*WSL, "nvidia-smi"],
            "git": [*WSL, "git"],
            "stat root": [*WSL_ROOT, "stat"],
        }
        defaults = {
            "wsl -l": WSL_LIST,
            "systemctl": "active\n",
            "ss": SS_OK,
            "nvidia-smi": NVIDIA_OK,
            "git": "git version 2.43.0\n",
            "stat root": "600:root\n",
        }
        for key, prefix in rows.items():
            self.set(key, defaults[key])
            self.runner.add(prefix, self._answer(key))
        self.inbound = CmdResult(
            0, inbound_rules(8000, 8100, 8200, self.cfg.security.ui.port), "", 0.0
        )
        self.runner.add(PS, self._ps)
        classes = self.cfg.resilience.resilience.gpu.classes
        self.health = {s.url: 200 for c in classes.values() for s in c.services.values()}
        monkeypatch.setattr(egress, "loopback_http_client", self._client)
        monkeypatch.setattr(doctor_gpu, "gpu_state", lambda: self.gpu)
        return self


def start_gpu_host(tmp_path: Path, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> GpuHost:
    """A green host: every GPU row PASS. Needs ``fake_keyring`` (the OpenJev bearer is set)."""
    keyring.set_password("herness", "openjev_api_key", OPENJEV_KEY)  # names are lowercased
    return GpuHost(start_pull_host(tmp_path, runner, monkeypatch)).install(monkeypatch)
