"""WSL, Docker, GPU, image, weight, health and firewall rows for ``herness doctor`` (impl 10
U10-91; design 10 §5.6.3, §9.2).

Each check takes the config and returns one ``HostCheckResult``; ``doctor_host.doctor_checks``
runs them after ``HOST_CHECKS``, turns an exception into a FAIL row and answers the WSL and
PowerShell rows with WARN "not applicable" off Windows. Every program runs through
``herness.admin.wsl`` (no shell, allowlisted argv0, U10-77 argument patterns) with a 10 s
timeout, and every check runs under a 20 s deadline. Fail closed: a non-zero exit, a timeout,
empty or garbled output is FAIL (or the row's WARN), never PASS (TH10-29, TH10-30, TH10-33,
TH10-39). Config values reach a command only as positional data, never as a switch.
"""

from __future__ import annotations

import functools
import json
from typing import TYPE_CHECKING, Final, Literal

from herness.admin import _pull_verify as pv
from herness.admin import deploy_pull, wsl
from herness.admin.doctor_host import HostCheckResult
from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.jobs.gpu import gpu_state
from herness.core.jobs.gpu_services import LoopbackHttp

if TYPE_CHECKING:
    from collections.abc import Callable

    from herness.admin.deploy_pull import WeightClass
    from herness.core.config import HernessConfig
    from herness.core.resilience.ports import GpuStateReader
    from herness.core.settings import PinClass
    from herness.core.types import ServiceName

__all__ = ["GPU_CHECKS"]

type _Status = Literal["PASS", "WARN", "FAIL"]
type _Check = Callable[[HernessConfig], HostCheckResult]

_TIMEOUT_S: Final = 10.0  # per subprocess or health GET (U10-90 rule)
_CHECK_BUDGET_S: Final = 20.0  # per check (U10-91)
_MIN_VRAM_MIB: Final = 24_000
_DOCKER_API_PORTS: Final = frozenset({2375, 2376})
_LOCAL_COLUMN: Final = 3  # ``ss -ltn``: State Recv-Q Send-Q Local-Address:Port Peer ...
_TIMED_OUT: Final = 124  # ``CmdResult`` exit code of a timeout
_MODEL_PORTS: Final = (8000, 8100, 8200)  # R-51
_INBOUND_PREFIX: Final = "herness-block-inbound-"
_ENV_FILE_MODE: Final = "600:root"  # ``%a:%U``; the spec's ``%a %U`` fails the U10-77 pattern
# (doctor class, deploy section): ``decider`` is served by OpenJev (U10-83)
_CLASSES: Final[tuple[tuple[WeightClass, PinClass], ...]] = (
    ("reasoning", "reasoning"), ("decider", "openjev"), ("large", "large"),
)  # fmt: skip
_TRUE: Final = (True, 1, "True")  # PowerShell renders enums by name or by integer
_INBOUND: Final = ("Inbound", 1)
_BLOCK: Final = ("Block", 4)
_FIX: Final[dict[str, str]] = {
    "wsl_distro": "Runbook step 1",
    "wsl_systemd_docker": "Enable systemd; start docker",
    "docker_api_local": "Remove the TCP host from the Docker daemon config",
    "gpu_visible": "Install the Windows driver with WSL support",
    "gpu_vram": "A 24 GB+ GPU is required",
    "images_pinned": "herness deploy pull --allow-download",
    "wsl_git": "Install git in the WSL distro; weight verification fails closed without it",
    "weights_pinned": "herness deploy pull --allow-download",
    "model_health": "herness deploy up <class>",
    "firewall_outbound": (
        "Set-NetFirewallHyperVVMSetting -Name "
        f"'{deploy_pull.WSL_VM_CREATOR_ID}' -DefaultOutboundAction Block"
    ),
    "firewall_inbound": "Add the inbound block rules (runbook)",
    "env_file": "herness deploy render",
}


def _row(name: str, status: _Status, detail: str) -> HostCheckResult:
    return HostCheckResult(name, status, detail, "" if status == "PASS" else _FIX[name])


def _clean(text: str) -> str:
    """Printable characters only (GPU, distro and rule names are untrusted output)."""
    return "".join(c for c in text if c.isprintable())[:120]


def _positional(value: str, what: str) -> str:
    """A config value used as a command operand: never empty and never an option switch."""
    if not value or value.startswith("-") or not value.isprintable():
        msg = f"unsafe {what}"
        raise ConfigError(msg)
    return value


def _limited(check: _Check) -> _Check:
    """``check`` with every WSL call capped at 10 s, the whole check at 20 s, and an exception
    (``ConfigError`` for an unsafe value, a runner error) turned into this row's FAIL."""

    name = check.__name__.removeprefix("_check_")

    @functools.wraps(check)
    def run(cfg: HernessConfig) -> HostCheckResult:
        token = pv.LIMIT.set((_TIMEOUT_S, clock.monotonic() + _CHECK_BUDGET_S))
        try:
            return check(cfg)
        except Exception as exc:  # noqa: BLE001 - FAIL with this row's fix hint, never raise
            return _row(name, "FAIL", type(exc).__name__)
        finally:
            pv.LIMIT.reset(token)

    return run


def _wsl(cfg: HernessConfig, argv: list[str]) -> wsl.CmdResult:
    """One program in the deploy distro under the check's deadline (U10-77)."""
    return pv.run(cfg, argv, timeout_s=_TIMEOUT_S)


# --- WSL and Docker ---------------------------------------------------------------------------


def _check_wsl_distro(cfg: HernessConfig) -> HostCheckResult:
    distro = cfg.deploy.wsl_distro
    result = wsl.run_cmd(["wsl.exe", "-l", "-q"], timeout_s=_TIMEOUT_S, encoding="utf-16-le")
    if result.returncode != 0:
        return _row("wsl_distro", "FAIL", f"wsl.exe -l exit {result.returncode}")
    names = {line.strip().lstrip("﻿").strip("\0") for line in result.stdout.splitlines()}
    if distro in names:
        return _row("wsl_distro", "PASS", f"{distro} installed")
    return _row("wsl_distro", "FAIL", f"{_clean(distro)} not listed")


def _check_wsl_systemd_docker(cfg: HernessConfig) -> HostCheckResult:
    result = _wsl(cfg, ["systemctl", "is-active", "docker"])
    state = _clean(result.stdout.strip())
    if result.returncode == 0 and state == "active":
        return _row("wsl_systemd_docker", "PASS", "docker active")
    return _row("wsl_systemd_docker", "FAIL", f"docker {state or 'unknown'}")


def _docker_api_listeners(stdout: str) -> list[str] | None:
    """Local addresses listening on 2375/2376 from ``ss -ltn``; None when unreadable."""
    lines = [line.split() for line in stdout.splitlines() if line.strip()]
    if not lines or lines[0][:1] != ["State"]:
        return None
    found: list[str] = []
    for fields in lines[1:]:
        local = fields[3] if len(fields) > _LOCAL_COLUMN else ""
        _host, colon, port = local.rpartition(":")
        if not colon or not port.isdigit():
            return None
        if int(port) in _DOCKER_API_PORTS:
            found.append(_clean(local))
    return found


def _check_docker_api_local(cfg: HernessConfig) -> HostCheckResult:
    result = _wsl(cfg, ["ss", "-ltn"])
    found = _docker_api_listeners(result.stdout) if result.returncode == 0 else None
    if found is None:
        return _row("docker_api_local", "FAIL", f"ss -ltn unreadable (exit {result.returncode})")
    if found:
        return _row("docker_api_local", "FAIL", "Docker API listening: " + ", ".join(found))
    return _row("docker_api_local", "PASS", "no listener on 2375 or 2376")


# --- GPU --------------------------------------------------------------------------------------


def _gpus(cfg: HernessConfig) -> list[tuple[str, int]] | str:
    """``(name, memory.total MiB)`` per GPU from ``nvidia-smi``, or why it is unusable."""
    argv = ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]
    result = _wsl(cfg, argv)
    if result.returncode != 0:
        return f"nvidia-smi exit {result.returncode}"
    gpus: list[tuple[str, int]] = []
    for line in filter(str.strip, result.stdout.splitlines()):
        name, comma, total = line.rpartition(",")
        mib = total.strip().removesuffix("MiB").strip()
        if not comma or not name.strip() or not mib.isdigit():
            return "nvidia-smi output unreadable"
        gpus.append((_clean(name.strip()), int(mib)))
    return gpus or "nvidia-smi reported no GPU"


def _check_gpu_visible(cfg: HernessConfig) -> HostCheckResult:
    gpus = _gpus(cfg)
    if isinstance(gpus, str):
        return _row("gpu_visible", "FAIL", gpus)
    return _row("gpu_visible", "PASS", "; ".join(name for name, _ in gpus))


def _check_gpu_vram(cfg: HernessConfig) -> HostCheckResult:
    """GPU 0 (the device the single-GPU model servers run on) needs ≥ 24,000 MiB."""
    gpus = _gpus(cfg)
    if isinstance(gpus, str):
        return _row("gpu_vram", "FAIL", gpus)
    detail = "; ".join(f"{name} {mib} MiB" for name, mib in gpus)
    return _row("gpu_vram", "PASS" if gpus[0][1] >= _MIN_VRAM_MIB else "FAIL", detail)


# --- images and weights -----------------------------------------------------------------------


def _image_problem(cfg: HernessConfig, section: PinClass) -> str | None:
    cfg.deploy.require_pinned([section])
    ref = _positional(getattr(cfg.deploy, section).image, "image ref")
    result = _wsl(cfg, ["docker", "image", "inspect", ref])
    if result.returncode != 0:
        return "inspect timed out" if result.returncode == _TIMED_OUT else "not present"
    found = deploy_pull._repo_digests(result.stdout)  # the U10-82 parser, reused (T10-28)
    digest = ref.rpartition("@")[2]
    return None if any(d.endswith("@" + digest) for d in found) else "digest mismatch"


def _per_class(name: str, problems: dict[WeightClass, str | None]) -> HostCheckResult:
    bad = [f"{cls}: {problem}" for cls, problem in problems.items() if problem is not None]
    if bad:
        return _row(name, "FAIL", "; ".join(bad))
    return _row(name, "PASS", ", ".join(problems) + " ok")


def _safely(probe: Callable[[], str | None]) -> str | None:
    try:
        return probe()
    except ConfigError as exc:  # unpinned or unsafe value: this class fails, the others run
        return _clean(str(exc))


def _check_images_pinned(cfg: HernessConfig) -> HostCheckResult:
    problems = {
        cls: _safely(functools.partial(_image_problem, cfg, section)) for cls, section in _CLASSES
    }
    return _per_class("images_pinned", problems)


def _check_wsl_git(cfg: HernessConfig) -> HostCheckResult:
    result = _wsl(cfg, ["git", "--version"])
    version = _clean(result.stdout.strip())
    if result.returncode == 0 and version.startswith("git version "):
        return _row("wsl_git", "PASS", version)
    return _row("wsl_git", "FAIL", f"git not usable in the distro (exit {result.returncode})")


def _weights_problem(cfg: HernessConfig, gpu_class: WeightClass) -> str | None:
    status = deploy_pull.verify_weights(cfg, gpu_class, full=False).get("status")
    return None if status == "ok" else _clean(str(status or "no status"))


def _check_weights_pinned(cfg: HernessConfig) -> HostCheckResult:
    problems = {cls: _safely(functools.partial(_weights_problem, cfg, cls)) for cls, _ in _CLASSES}
    return _per_class("weights_pinned", problems)


# --- model health -----------------------------------------------------------------------------


def _answers(state: GpuStateReader, name: ServiceName) -> bool:
    try:
        return state.service_healthy(name)  # cached GET, 2 s timeout (U08-83)
    except Exception:  # noqa: BLE001 - an unreachable service counts as not answering
        return False


def _loaded_class(cfg: HernessConfig, state: GpuStateReader) -> str:
    """The worker's loaded class; without a worker, the first class whose service answers."""
    try:
        return str(state.loaded_class())
    except Exception:  # noqa: BLE001 - no worker to ask (backend unbound or unreachable)
        classes = cfg.resilience.resilience.gpu.classes
        found = (c for c, spec in classes.items() if any(_answers(state, n) for n in spec.services))
        return next(found, "none")


def _check_model_health(cfg: HernessConfig) -> HostCheckResult:
    loaded = _loaded_class(cfg, gpu_state())
    if loaded in ("none", "swapping"):
        return _row("model_health", "WARN", f"no class loaded ({loaded})")
    classes = cfg.resilience.resilience.gpu.classes
    spec = next((s for c, s in classes.items() if c == loaded), None)
    if spec is None or not spec.services:
        return _row("model_health", "FAIL", f"unknown class {_clean(loaded)}")
    http = LoopbackHttp()  # ``loopback_http_client``; the bearer from ``health.bearer_secret``
    down = [n for n, svc in spec.services.items() if not http.healthy(svc, timeout_s=_TIMEOUT_S)]
    if down:
        return _row("model_health", "FAIL", f"{loaded}: unhealthy " + ", ".join(down))
    return _row("model_health", "PASS", f"{loaded}: " + ", ".join(spec.services) + " healthy")


# --- firewall and env file --------------------------------------------------------------------


def _check_firewall_outbound(cfg: HernessConfig) -> HostCheckResult:
    del cfg
    if deploy_pull._outbound_blocked(timeout_s=_TIMEOUT_S):  # the U10-81 read-back, reused
        return _row("firewall_outbound", "PASS", "WSL VM DefaultOutboundAction Block")
    return _row("firewall_outbound", "FAIL", "WSL VM DefaultOutboundAction is not Block")


def _inbound_ports(stdout: str) -> set[int] | None:
    """Ports named by enabled inbound Block rules ``herness-block-inbound-<port>[-<port>...]``.

    ``Get-NetFirewallRule`` carries no port fields, so the rule names state the coverage
    (T10-28 ruling); unreadable JSON is None.
    """
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    ports: set[int] = set()
    for rule in data if isinstance(data, list) else [data]:
        if not isinstance(rule, dict):
            return None
        name = str(rule.get("DisplayName", ""))
        live = rule.get("Enabled") in _TRUE and rule.get("Direction") in _INBOUND
        if live and rule.get("Action") in _BLOCK and name.startswith(_INBOUND_PREFIX):
            parts = name.removeprefix(_INBOUND_PREFIX).split("-")
            ports |= {int(p) for p in parts if p.isdigit()}
    return ports


def _check_firewall_inbound(cfg: HernessConfig) -> HostCheckResult:
    params = {"DisplayName": _INBOUND_PREFIX + "*"}
    result = wsl.run_powershell("Get-NetFirewallRule", params, timeout_s=_TIMEOUT_S)
    ports = _inbound_ports(result.stdout) if result.returncode == 0 else None
    if ports is None:
        return _row("firewall_inbound", "WARN", f"rules unreadable (exit {result.returncode})")
    missing = [p for p in (*_MODEL_PORTS, cfg.security.ui.port) if p not in ports]
    if missing:
        return _row("firewall_inbound", "WARN", "not covered: " + ", ".join(map(str, missing)))
    return _row("firewall_inbound", "PASS", "inbound block rules cover the model and UI ports")


def _check_env_file(cfg: HernessConfig) -> HostCheckResult:
    path = _positional(cfg.deploy.env_file, "env_file")
    argv = ["stat", "-c", "%a:%U", path]
    result = wsl.run_wsl(cfg.deploy.wsl_distro, argv, user="root", timeout_s=_TIMEOUT_S)
    found = _clean(result.stdout.strip())
    if result.returncode == 0 and found == _ENV_FILE_MODE:
        return _row("env_file", "PASS", f"{path} 600 root")
    return _row("env_file", "FAIL", f"{path}: {found or 'unreadable'} (want 600 root)")


GPU_CHECKS: Final[tuple[_Check, ...]] = tuple(
    map(
        _limited,
        (
            _check_wsl_distro, _check_wsl_systemd_docker, _check_docker_api_local,
            _check_gpu_visible, _check_gpu_vram, _check_images_pinned, _check_wsl_git,
            _check_weights_pinned, _check_model_health, _check_firewall_outbound,
            _check_firewall_inbound, _check_env_file,
        ),
    )
)  # fmt: skip
