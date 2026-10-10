"""UT10-71 GPU rows: each doctor GPU check PASS/WARN/FAIL per recorded fixture (U10-91).

Every program answers through ``fake_runner`` (``tests.support.doctor_gpu_env``); health GETs
go through the real ``LoopbackHttp`` over an ``httpx2.MockTransport`` client, and the GPU
state reader is a fake, so neither the real host nor a socket is used. Fail closed: empty,
garbled, non-zero, timed-out or denied output never PASSes; config values never become switches.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from tests.support.doctor_gpu_env import (
    OPENJEV_KEY,
    PS,
    WSL,
    GpuHost,
    inbound_rules,
    start_gpu_host,
)
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_runner import FakeRunner

from herness.admin import _pull_verify as pv
from herness.admin import deploy_pull, doctor_gpu, wsl
from herness.admin.doctor_host import HostCheckResult
from herness.admin.wsl import CmdResult
from herness.core import time as clock
from herness.core.config import HernessConfig
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

GPU_NAMES = [
    "wsl_distro", "wsl_systemd_docker", "docker_api_local", "gpu_visible", "gpu_vram",
    "images_pinned", "wsl_git", "weights_pinned", "model_health", "firewall_outbound",
    "firewall_inbound", "env_file",
]  # fmt: skip
CHECKS = {c.__name__.removeprefix("_check_"): c for c in doctor_gpu.GPU_CHECKS}


@pytest.fixture
def host(
    tmp_path: Path,
    fake_keyring: MemoryKeyring,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> GpuHost:
    del fake_keyring
    return start_gpu_host(tmp_path, fake_runner, monkeypatch)


def run(name: str, cfg: HernessConfig) -> HostCheckResult:
    row = CHECKS[name](cfg)
    assert row.name == name
    assert (row.fix == "") is (row.status == "PASS")
    return row


def _with_deploy(cfg: HernessConfig, **update: Any) -> HernessConfig:
    return cfg.model_copy(update={"deploy": cfg.deploy.model_copy(update=update)})


# --- table, order, argv and timeouts --------------------------------------------------------


def test_ut10_71_gpu_checks_all_pass_in_table_order(host: GpuHost) -> None:
    """UT10-71 GPU rows on a green host: all PASS, in the U10-91 order (+ wsl_git, M12)."""
    assert list(CHECKS) == GPU_NAMES
    rows = [run(name, host.cfg) for name in GPU_NAMES]
    assert {r.name: (r.status, r.detail) for r in rows if r.status != "PASS"} == {}
    by_name = {r.name: r.detail for r in rows}
    assert by_name["gpu_vram"] == "NVIDIA GeForce RTX 4090 24564 MiB"
    assert by_name["model_health"] == "reasoning: vllm-reasoning healthy"
    assert by_name["images_pinned"] == "reasoning, decider, large ok"


def test_ut10_71_gpu_argv_forms_and_timeouts(host: GpuHost) -> None:
    """UT10-71 the spec's argv forms; every call ≤ 10 s; ``wsl -l -q`` decoded as UTF-16LE."""
    for name in GPU_NAMES:
        run(name, host.cfg)
    calls = host.runner.calls
    assert all(0 < c.timeout_s <= 10.0 for c in calls)
    first = calls[0]
    assert (first.argv, first.encoding) == (["wsl.exe", "-l", "-q"], "utf-16-le")
    argvs = host.runner.argvs
    smi = [*WSL, "nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]
    stat = ["wsl.exe", "-d", "herness", "-u", "root", "--exec", "stat", "-c", "%a:%U"]
    for expected in (
        [*WSL, "systemctl", "is-active", "docker"],
        [*WSL, "ss", "-ltn"],
        smi,
        [*WSL, "docker", "image", "inspect", host.cfg.deploy.openjev.image],
        [*WSL, "git", "--version"],
        [*stat, host.cfg.deploy.env_file],
    ):
        assert expected in argvs
    ps = [a[-1] for a in argvs if a[:4] == PS]
    assert ps == [
        f"Get-NetFirewallHyperVVMSetting -Name '{deploy_pull.WSL_VM_CREATOR_ID}'"
        " | ConvertTo-Json -Depth 3",
        "Get-NetFirewallRule -DisplayName 'herness-block-inbound-*' | ConvertTo-Json -Depth 3",
    ]


# --- fail closed: every runner-backed row ---------------------------------------------------

_KEYS = {
    "wsl_distro": "wsl -l",
    "wsl_systemd_docker": "systemctl",
    "docker_api_local": "ss",
    "gpu_visible": "nvidia-smi",
    "gpu_vram": "nvidia-smi",
    "wsl_git": "git",
    "env_file": "stat root",
}
_BAD = [("", 0), ("", 1), ("", 124), ("garbled \x00\x1b[31m output", 0), ("Access is denied.", 5)]


@pytest.mark.parametrize(("name", "bad"), list(itertools.product(_KEYS, _BAD)))
def test_ut10_71_runner_rows_fail_closed(host: GpuHost, name: str, bad: tuple[str, int]) -> None:
    """UT10-71 empty, garbled, non-zero, timed-out or denied output: FAIL, never PASS."""
    host.set(_KEYS[name], *bad)
    row = run(name, host.cfg)
    assert row.status == "FAIL"
    assert row.detail.isprintable()


@pytest.mark.parametrize("bad", _BAD)
def test_ut10_71_firewall_rows_fail_closed(host: GpuHost, bad: tuple[str, int]) -> None:
    """UT10-71 unreadable PowerShell output: outbound FAIL, inbound WARN (never PASS)."""
    host.pull.readback = lambda outbound: bad[0]
    host.inbound = CmdResult(bad[1], bad[0], "", 0.0)
    assert run("firewall_outbound", host.cfg).status == "FAIL"
    assert run("firewall_inbound", host.cfg).status == "WARN"


def test_ut10_71_runner_exception_is_fail_with_hint(host: GpuHost) -> None:
    """UT10-71 a runner error inside a check is that row's FAIL with its fix hint."""

    def boom(argv: list[str]) -> CmdResult:
        msg = "runner broke"
        raise RuntimeError(msg)

    host.runner.add([*WSL, "systemctl"], boom)
    row = run("wsl_systemd_docker", host.cfg)
    assert (row.status, row.detail, row.fix) == (
        "FAIL",
        "RuntimeError",
        "Enable systemd; start docker",
    )


# --- WSL and Docker ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("listing", "status"),
    [
        ("﻿herness\r\n", "PASS"),
        ("Ubuntu\r\ndocker-desktop\r\n", "FAIL"),
        ("Herness\r\n", "FAIL"),
        ("herness-old\r\n", "FAIL"),
    ],
)
def test_ut10_71_wsl_distro(host: GpuHost, listing: str, status: str) -> None:
    """UT10-71 wsl_distro lists ``deploy.wsl_distro`` exactly (UTF-16LE listing, BOM)."""
    host.set("wsl -l", listing)
    row = run("wsl_distro", host.cfg)
    assert row.status == status
    if status == "FAIL":
        assert (row.detail, row.fix) == ("herness not listed", "Runbook step 1")


@pytest.mark.parametrize(
    ("stdout", "code", "status"),
    [
        ("active\n", 0, "PASS"),
        ("inactive\n", 3, "FAIL"),
        ("active\n", 1, "FAIL"),
        ("act\tive\n", 0, "FAIL"),
        ("active\x00\n", 0, "FAIL"),
    ],
)
def test_ut10_71_wsl_systemd_docker(host: GpuHost, stdout: str, code: int, status: str) -> None:
    """UT10-71 ``systemctl is-active docker`` must print ``active`` with exit 0."""
    host.set("systemctl", stdout, code)
    assert run("wsl_systemd_docker", host.cfg).status == status


_SS_HEAD = "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process\n"


@pytest.mark.parametrize(
    ("line", "detail"),
    [
        ("LISTEN 0 4096 0.0.0.0:2375 0.0.0.0:*\n", "Docker API listening: 0.0.0.0:2375"),
        ("LISTEN 0 4096 [::]:2376 [::]:*\n", "Docker API listening: [::]:2376"),
        ("LISTEN 0 4096 127.0.0.1:2375 0.0.0.0:*\n", "Docker API listening: 127.0.0.1:2375"),
        ("LISTEN 0 4096\n", "ss -ltn unreadable (exit 0)"),
        ("LISTEN 0 4096 0.0.0.0:http 0.0.0.0:*\n", "ss -ltn unreadable (exit 0)"),
    ],
)
def test_ut10_71_docker_api_local_fails(host: GpuHost, line: str, detail: str) -> None:
    """UT10-71 any listener on 2375/2376 FAILs; an unparsable ``ss`` line FAILs too."""
    host.set("ss", _SS_HEAD + line)
    row = run("docker_api_local", host.cfg)
    assert (row.status, row.detail) == ("FAIL", detail)


def test_ut10_71_docker_api_local_header_only_passes(host: GpuHost) -> None:
    """UT10-71 ``ss`` with its header and no listener at all: PASS."""
    host.set("ss", _SS_HEAD)
    assert run("docker_api_local", host.cfg).status == "PASS"


# --- GPU --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "visible", "vram"),
    [
        ("NVIDIA RTX 6000 Ada Generation, 49140\n", "PASS", "PASS"),
        ("NVIDIA GeForce RTX 4090, 24000\n", "PASS", "PASS"),
        ("NVIDIA GeForce RTX 4080, 23999\n", "PASS", "FAIL"),
        ("NVIDIA GeForce RTX 4090, 24564 MiB\n", "PASS", "PASS"),
        ("NVIDIA GeForce RTX 4090, 24564\nNVIDIA T400, 2048\n", "PASS", "PASS"),
        ("NVIDIA T400, 2048\nNVIDIA GeForce RTX 4090, 24564\n", "PASS", "FAIL"),
        ("NVIDIA GeForce RTX 4090, [N/A]\n", "FAIL", "FAIL"),
        ("NVIDIA GeForce RTX 4090, 24564\nNo devices were found\n", "FAIL", "FAIL"),
        ("NVIDIA GeForce RTX 4090, -24564\n", "FAIL", "FAIL"),
        (", 24564\n", "FAIL", "FAIL"),
        ("\n\n", "FAIL", "FAIL"),
    ],
)
def test_ut10_71_gpu_visible_and_vram(host: GpuHost, stdout: str, visible: str, vram: str) -> None:
    """UT10-71 nvidia-smi parsed defensively: multi-GPU (GPU 0 counts), units, non-numeric."""
    host.set("nvidia-smi", stdout)
    assert run("gpu_visible", host.cfg).status == visible
    row = run("gpu_vram", host.cfg)
    assert row.status == vram
    if vram == "FAIL":
        assert row.fix == "A 24 GB+ GPU is required"


# --- images and weights -----------------------------------------------------------------------


def test_ut10_71_images_pinned_per_class(host: GpuHost) -> None:
    """UT10-71 per class: inspect failure, digest mismatch and timeout each FAIL that class."""
    deploy = host.cfg.deploy
    host.pull.repo_digests[deploy.openjev.image] = ["razorback16/openjev@sha256:" + "e" * 64]
    row = run("images_pinned", host.cfg)
    assert (row.status, row.detail) == ("FAIL", "decider: digest mismatch")
    assert row.fix == "herness deploy pull --allow-download"
    host.pull.fail_prefix = ("docker", "image", "inspect")
    detail = "reasoning: not present; decider: not present; large: not present"
    assert run("images_pinned", host.cfg).detail == detail
    host.runner.add([*WSL, "docker", "image", "inspect", deploy.large.image], "", returncode=124)
    assert run("images_pinned", host.cfg).detail.endswith("large: inspect timed out")


@pytest.mark.parametrize("stdout", ["not json", "[]", '[{"RepoDigests": 7}]', "", "null"])
def test_ut10_71_images_pinned_garbled_inspect(host: GpuHost, stdout: str) -> None:
    """UT10-71 unreadable ``docker image inspect`` JSON is a mismatch (fail closed)."""
    host.runner.add([*WSL, "docker", "image", "inspect"], stdout)
    row = run("images_pinned", host.cfg)
    assert row.status == "FAIL"
    assert row.detail.count("digest mismatch") == 3


def test_ut10_71_images_pinned_unpinned_or_unsafe_ref(host: GpuHost) -> None:
    """UT10-71 an unpinned ref FAILs its class without running docker; others still run."""
    large = host.cfg.deploy.large.model_copy(update={"image": "--privileged"})
    cfg = _with_deploy(host.cfg, large=large)
    row = run("images_pinned", cfg)
    assert (row.status, row.detail) == ("FAIL", "large: deploy.large.image is not pinned")
    inspected = [a[-1] for a in host.runner.argvs if a[4:7] == ["docker", "image", "inspect"]]
    assert "--privileged" not in inspected
    assert len(inspected) == 2


@pytest.mark.parametrize(
    ("stdout", "code"), [("git version 2.43.0\n", 127), ("bash: git: not found\n", 0)]
)
def test_ut10_71_wsl_git(host: GpuHost, stdout: str, code: int) -> None:
    """UT10-71 wsl_git (T10-24 M12): git missing in the distro FAILs with the install hint."""
    host.set("git", stdout, code)
    row = run("wsl_git", host.cfg)
    assert row.status == "FAIL"
    assert row.fix.startswith("Install git in the WSL distro")


def test_ut10_71_weights_pinned_per_class(host: GpuHost) -> None:
    """UT10-71 ``verify_weights(full=False)`` not ok for a class: FAIL naming class and status."""
    blob = host.pull.blob(host.cfg.deploy.reasoning.model, "model.safetensors")
    del host.pull.sizes[blob]
    host.pull.sizes.pop(f"{host.cfg.deploy.model_root}/gguf/{host.cfg.deploy.large.gguf}")
    row = run("weights_pinned", host.cfg)
    assert row.status == "FAIL"
    assert row.detail == "reasoning: missing:model.safetensors; large: missing:qwen3-large.gguf"
    assert not any(a[4:5] == ["sha256sum"] for a in host.runner.argvs)  # full=False


def test_ut10_71_weights_pinned_runner_failures(host: GpuHost) -> None:
    """UT10-71 a failing ``find``/``test`` is a non-ok status, never PASS."""
    host.pull.fail_prefix = ("find",)
    row = run("weights_pinned", host.cfg)
    assert row.status == "FAIL"
    assert "reasoning: list_failed" in row.detail
    host.pull.fail_prefix = ("test",)
    assert "snapshot_missing" in run("weights_pinned", host.cfg).detail


def test_ut10_71_time_limit_caps_calls_and_deadline(
    host: GpuHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-71 inside a check every WSL call ≤ 10 s; past the 20 s deadline nothing starts."""
    run("weights_pinned", host.cfg)
    assert host.runner.calls
    assert max(c.timeout_s for c in host.runner.calls) <= 10.0  # verify_weights asks 600 s
    host.runner.calls.clear()
    ticks = iter([0.0, *(100.0 for _ in range(100))])
    monkeypatch.setattr(clock, "monotonic", lambda: next(ticks))
    row = run("weights_pinned", host.cfg)
    assert row.status == "FAIL"
    assert host.runner.calls == []
    assert pv.LIMIT.get() is None  # reset after the check


# --- model health -----------------------------------------------------------------------------


def test_ut10_71_model_health_loaded_class(host: GpuHost) -> None:
    """UT10-71 the loaded class's services answer 2xx through ``loopback_http_client``."""
    row = run("model_health", host.cfg)
    assert row.status == "PASS"
    assert host.requests == [("http://127.0.0.1:8000/health", None, 10.0)]
    host.health["http://127.0.0.1:8000"] = 503
    row = run("model_health", host.cfg)
    assert (row.status, row.detail) == ("FAIL", "reasoning: unhealthy vllm-reasoning")
    assert row.fix == "herness deploy up <class>"


def test_ut10_71_model_health_openjev_bearer(host: GpuHost) -> None:
    """UT10-71 decider: OpenJev ``/v1/models`` with the ``secret:OPENJEV_API_KEY`` bearer."""
    host.gpu.loaded = "decider"
    row = run("model_health", host.cfg)
    assert (row.status, row.detail) == ("PASS", "decider: openjev healthy")
    assert host.requests == [("http://127.0.0.1:8100/v1/models", f"Bearer {OPENJEV_KEY}", 10.0)]
    assert OPENJEV_KEY not in row.detail


@pytest.mark.parametrize("loaded", ["none", "swapping"])
def test_ut10_71_model_health_no_class_warns(host: GpuHost, loaded: str) -> None:
    """UT10-71 no class loaded (or a swap in progress): WARN, no health GET."""
    host.gpu.loaded = loaded
    row = run("model_health", host.cfg)
    assert (row.status, row.detail) == ("WARN", f"no class loaded ({loaded})")
    assert host.requests == []


def test_ut10_71_model_health_without_worker(host: GpuHost) -> None:
    """UT10-71 no worker: the class whose service answers is checked; none answering WARNs."""
    host.gpu.loaded = RuntimeError("no jobs backend")
    assert run("model_health", host.cfg).status == "WARN"
    host.gpu.healthy = {"llamacpp-large"}
    row = run("model_health", host.cfg)
    assert (row.status, row.detail) == ("PASS", "large: llamacpp-large healthy")
    host.health["http://127.0.0.1:8200"] = 500
    assert run("model_health", host.cfg).status == "FAIL"


def test_ut10_71_model_health_unknown_class_fails(host: GpuHost) -> None:
    """UT10-71 a loaded-class value outside the catalogue FAILs."""
    host.gpu.loaded = "giant\x1b"
    row = run("model_health", host.cfg)
    assert (row.status, row.detail) == ("FAIL", "unknown class giant")


# --- firewall ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ('{"DefaultOutboundAction": "Block"}', "PASS"),
        ('{"DefaultOutboundAction": 4}', "PASS"),
        ('{"DefaultOutboundAction": 2}', "FAIL"),
        ('[{"DefaultOutboundAction": "Block"}, {"DefaultOutboundAction": "Allow"}]', "FAIL"),
        ("[]", "FAIL"),
    ],
)
def test_ut10_71_firewall_outbound(host: GpuHost, body: str, status: str) -> None:
    """UT10-71 only a parsed ``Block`` (name or enum 4) on every entry PASSes."""
    host.pull.readback = lambda outbound: body
    row = run("firewall_outbound", host.cfg)
    assert row.status == status
    if status == "FAIL":
        assert row.fix.startswith("Set-NetFirewallHyperVVMSetting -Name '{40E0AC32")
        assert row.fix.endswith("-DefaultOutboundAction Block")


def test_ut10_71_firewall_inbound(host: GpuHost) -> None:
    """UT10-71 enabled inbound Block rules must cover 8000, 8100, 8200 and the UI port."""
    ui = host.cfg.security.ui.port
    name = f"herness-block-inbound-8000-8100-8200-{ui}"
    rule = {"DisplayName": name, "Enabled": "True", "Direction": "Inbound", "Action": "Block"}
    cases = [
        (inbound_rules(8000, 8100, 8200), "WARN", f"not covered: {ui}"),
        (inbound_rules(8000, 8100, 8200, ui, enabled=2), "WARN", None),
        (inbound_rules(8000, 8100, 8200, ui, action=2), "WARN", None),
        (json.dumps([rule]), "PASS", None),
        ('["herness-block-inbound-8000"]', "WARN", None),
    ]  # fmt: skip
    for body, status, detail in cases:
        host.inbound = CmdResult(0, body, "", 0.0)
        row = run("firewall_inbound", host.cfg)
        assert row.status == status, body
        if detail is not None:
            assert row.detail == detail


# --- env file and unsafe values ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "status"),
    [
        ("600:root\n", "PASS"),
        ("644:root\n", "FAIL"),
        ("600:herness\n", "FAIL"),
        ("600 root", "FAIL"),
        ("600:\troot\n", "FAIL"),
        ("600:root x\n", "FAIL"),
        ("600:root\n600:root\n", "FAIL"),
        ("600:root\x1b\n", "FAIL"),
    ],
)
def test_ut10_71_env_file(host: GpuHost, stdout: str, status: str) -> None:
    """UT10-71 env_file: root ``stat`` must print mode 600 owned by root."""
    host.set("stat root", stdout)
    row = run("env_file", host.cfg)
    assert row.status == status
    if status == "FAIL":
        assert row.fix == "herness deploy render"


@pytest.mark.parametrize(
    ("name", "update"),
    [
        ("env_file", {"env_file": "-rf"}),
        ("env_file", {"env_file": ""}),
        ("env_file", {"wsl_distro": "-u"}),
        ("wsl_systemd_docker", {"wsl_distro": "--exec"}),
        ("weights_pinned", {"wsl_distro": "a b"}),
    ],
)
def test_ut10_71_unsafe_config_values_rejected(
    host: GpuHost, name: str, update: dict[str, str]
) -> None:
    """UT10-71 a config value that would be a switch or unsafe: FAIL, nothing is run."""
    row = run(name, _with_deploy(host.cfg, **update))
    assert row.status == "FAIL"
    assert host.runner.calls == []


def test_ut10_71_unsafe_argv_rejected_by_runner(fake_runner: FakeRunner) -> None:
    """UT10-71 the U10-77 runner refuses an unsafe distro or argument before any call."""
    with pytest.raises(ConfigError, match="unsafe"):
        wsl.run_wsl("-d", ["true"], timeout_s=1)
    with pytest.raises(ConfigError, match="unsafe"):
        wsl.run_wsl("herness", ["stat", "-c", "%a %U", "/x"], timeout_s=1)
    assert fake_runner.calls == []


def test_ut10_71_firewall_outbound_nonzero_exit_with_block_body(host: GpuHost) -> None:
    """UT10-71 the read-back exits non-zero although its body says ``Block``: FAIL."""
    body = '{"DefaultOutboundAction": "Block"}'
    host.runner.add([*PS, f"Get-NetFirewallHyperVVMSetting -Name '{deploy_pull.WSL_VM_CREATOR_ID}'"
                     " | ConvertTo-Json -Depth 3"], body, returncode=1)  # fmt: skip
    assert run("firewall_outbound", host.cfg).status == "FAIL"


@pytest.mark.parametrize(
    "rule",
    [
        {"Enabled": 1, "Direction": 1, "Action": 4},
        {"DisplayName": "herness-block-inbound-8000x"},
        {"DisplayName": "herness-block-inbound-08000"},
        {"DisplayName": "herness-block-inbound-\uff18\uff10\uff10\uff10"},
        {"DisplayName": "herness-block-inbound-80000"},
    ],
)
def test_ut10_71_firewall_inbound_bad_rule_names_do_not_count(
    host: GpuHost, rule: dict[str, Any]
) -> None:
    """UT10-71 a rule without a name, a garbled, zero-padded, full-width or out-of-range port
    does not cover 8000."""
    ui = host.cfg.security.ui.port
    good = [
        {"DisplayName": f"herness-block-inbound-{p}", "Enabled": 1, "Direction": 1, "Action": 4}
        for p in (8100, 8200, ui)
    ]
    bad = {"Enabled": 1, "Direction": 1, "Action": 4, **rule}
    host.inbound = CmdResult(0, json.dumps([*good, bad]), "", 0.0)
    row = run("firewall_inbound", host.cfg)
    assert (row.status, row.detail) == ("WARN", "not covered: 8000")


@pytest.mark.parametrize("stdout", ["git version 2.43.0\x1b[0m\n", "git version\n", "x\n"])
def test_ut10_71_wsl_git_garbled_version_fails(host: GpuHost, stdout: str) -> None:
    """UT10-71 wsl_git compares the raw ``git --version`` line: control bytes or no version FAIL."""
    host.set("git", stdout)
    assert run("wsl_git", host.cfg).status == "FAIL"
