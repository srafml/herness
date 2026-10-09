"""Host-side rows for ``herness doctor`` (impl 10 U10-89, U10-90; design 10 §5.6.3, §5.5).

Each check takes the config and returns one ``HostCheckResult``; ``doctor_checks`` runs them
in a thread pool and never raises. External programs run only through ``herness.admin.wsl`` (no
shell, allowlisted argv0, 10 s timeouts). Details name paths, ports, drives, secret names and
process names, never a secret value or file contents. T09-22 (``run_doctor``) renders the rows.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Final, Literal, get_args

import psutil

from herness.admin import wsl
from herness.core import secrets
from herness.core import time as clock
from herness.core.audit import verify_chain
from herness.core.config import validate
from herness.core.errors import EgressBlocked, HernessError
from herness.core.jobs.gpu import gpu_state
from herness.core.types import ServiceName

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from herness.core.config import HernessConfig
    from herness.core.resilience.ports import GpuStateReader

__all__ = ["HOST_CHECKS", "HostCheckResult", "doctor_checks"]

type _Status = Literal["PASS", "WARN", "FAIL"]

_TIMEOUT_S: Final = 10.0
_MAX_WORKERS: Final = 8
_MIN_FREE_BYTES: Final = 50 * 1024**3
_MAX_SKEW_S: Final = 5.0
_MODEL_PORTS: Final = (8000, 8100, 8200)  # R-51
_NOT_APPLICABLE: Final = "not applicable on this host"
_FORBIDDEN_ACES: Final = ("BUILTIN\\Users", "Everyone", "Authenticated Users")
_PHASE_OFFSET: Final = re.compile(r"Phase Offset:\s*([-+]?\d+(?:\.\d+)?)s")
_FIX: Final[dict[str, str]] = {
    "python_uv": "Install Python 3.12 and uv",
    "config_valid": "herness config validate",
    "secrets_present": "herness secrets set <name>",  # pragma: allowlist secret - a fix hint
    "acl_data": (
        'icacls <path> /inheritance:r /grant:r "Administrators:(OI)(CI)F" "svc-herness:(OI)(CI)M"'
    ),
    "bitlocker": "Enable BitLocker",
    "audit_chain": "Investigate; restore from backup",
    "disk_free": "Free space",
    "clock": "w32tm /resync; wsl --shutdown",
    "ports_loopback": "Fix the bind or compose ports",
    "ports_expected": "Stop the unexpected listener",
    "socket_guard": "Defect: the guard is not installed",
    "plugins": "Remove unapproved plugins",
    "services": "Register services (design 10 §5.6.4)",
}
# Checks that only mean something on the Windows host (IT10-12); T10-28 may extend this.
_WINDOWS_ONLY: frozenset[str] = frozenset({"acl_data", "bitlocker", "clock", "services"})


# The spec's ``CheckResult``, renamed: impl 06 owns ``CheckResult`` in core.types (OWN040).
@dataclass(frozen=True)
class HostCheckResult:
    """One doctor row; impl 09 JSON ``{"name", "status", "detail", "fix"}`` (U10-89)."""

    name: str
    status: _Status
    detail: str
    fix: str


def _row(name: str, status: _Status, detail: str) -> HostCheckResult:
    return HostCheckResult(name, status, detail, "" if status == "PASS" else _FIX.get(name, ""))


def _on_windows() -> bool:
    """The platform probe (IT10-12 replaces it)."""
    return sys.platform == "win32"


def _config_dir() -> Path:
    """The config directory ``load_config`` and the CLI default to (``config`` under the cwd)."""
    return Path("config")


def _worst(statuses: Sequence[_Status]) -> _Status:
    return "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"


# --- U10-90 checks ----------------------------------------------------------------------------


def _check_python_uv(cfg: HernessConfig) -> HostCheckResult:
    del cfg
    major, minor = sys.version_info[:2]
    problems = [] if (major, minor) == (3, 12) else [f"python {major}.{minor} (need 3.12)"]
    if shutil.which("uv") is None:
        problems.append("uv not on PATH")
    return _row("python_uv", "FAIL" if problems else "PASS", "; ".join(problems) or "3.12, uv")


def _check_config_valid(cfg: HernessConfig) -> HostCheckResult:
    issues = validate(_config_dir(), cfg.profile)
    errors = sum(issue.severity == "error" for issue in issues)
    warns = sum(issue.severity == "warn" for issue in issues)
    status: _Status = "FAIL" if errors else "WARN" if warns else "PASS"
    return _row("config_valid", status, f"{errors} errors, {warns} warnings")


def _check_secrets_present(cfg: HernessConfig) -> HostCheckResult:
    missing = [name for name in secrets.referenced_secret_names(cfg) if not secrets.exists(name)]
    if missing:
        return _row("secrets_present", "FAIL", "missing: " + ", ".join(missing))
    return _row("secrets_present", "PASS", "all referenced secrets exist")


def _acl_targets(cfg: HernessConfig) -> list[tuple[Path, bool]]:
    """``(path, required)``: ``data\\`` must exist; the others are checked when present."""
    config_dir = _config_dir().resolve()
    optional = [config_dir / "profiles", config_dir.parent / ".env"]
    directory_file = cfg.security.redaction.directory_file
    if directory_file is not None:
        optional.insert(1, directory_file)
    return [(cfg.paths.data, True), *((p, False) for p in optional)]


def _acl_problem(path: Path, account: str) -> str | None:
    """Why the ACL of ``path`` is unsafe, or None (design 10 §5.5)."""
    result = wsl.run_cmd(["icacls.exe", str(path)], timeout_s=_TIMEOUT_S)
    if result.returncode != 0:
        return f"icacls exit {result.returncode}"
    lines = result.stdout.splitlines()
    flags = "(OI)(CI)" if path.is_dir() else ""
    svc = re.compile(rf"(?:^|\s)[^\s\\]+\\{re.escape(account)}:{re.escape(flags)}\(M\)\s*$")
    for line in lines:
        bad = next((ace for ace in _FORBIDDEN_ACES if ace in line), None)
        if bad is not None:
            return f"{bad} has access"
        if "(I)" in line:
            return "inherited ACE (I)"
    if not any(f"BUILTIN\\Administrators:{flags}(F)" in line for line in lines):
        return f"no BUILTIN\\Administrators:{flags}(F)"
    if not any(svc.search(line) for line in lines):
        return f"no {account}:{flags}(M)"
    return None


def _check_acl_data(cfg: HernessConfig) -> HostCheckResult:
    account = cfg.deploy.service.account
    problems: list[str] = []
    for path, required in _acl_targets(cfg):
        if not path.exists():
            if required:
                problems.append(f"{path}: missing")
            continue
        problem = _acl_problem(path, account)
        if problem is not None:
            problems.append(f"{path}: {problem}")
    if problems:
        return _row("acl_data", "FAIL", "; ".join(problems))
    return _row("acl_data", "PASS", "ACLs restricted")


def _drives(cfg: HernessConfig) -> list[str]:
    """System drive, then the drives of ``paths.data`` and ``paths.backup_target``, deduplicated."""
    paths = (os.environ.get("SYSTEMDRIVE", "C:"), cfg.paths.data, cfg.paths.backup_target)
    return list(dict.fromkeys(d for d in (PureWindowsPath(p).drive for p in paths) if d))


def _bitlocker_status(drive: str) -> _Status:
    result = wsl.run_cmd(["manage-bde.exe", "-status", drive], timeout_s=_TIMEOUT_S)
    if "Protection On" in result.stdout and result.returncode == 0:
        return "PASS"
    return "FAIL" if "Protection Off" in result.stdout else "WARN"


def _check_bitlocker(cfg: HernessConfig) -> HostCheckResult:
    found = {drive: _bitlocker_status(drive) for drive in _drives(cfg)}
    status = _worst(list(found.values()))
    words = {"PASS": "Protection On", "FAIL": "Protection Off", "WARN": "access denied or unknown"}
    detail = "; ".join(f"{drive} {words[s]}" for drive, s in found.items())
    return _row("bitlocker", status, detail)


def _check_audit_chain(cfg: HernessConfig) -> HostCheckResult:
    report = verify_chain(cfg.paths.logs)
    if report.ok:
        return _row("audit_chain", "PASS", f"{report.files} files, {report.lines} lines intact")
    return _row("audit_chain", "FAIL", f"chain broken at {report.first_break}")


def _check_disk_free(cfg: HernessConfig) -> HostCheckResult:
    free = shutil.disk_usage(cfg.paths.data).free
    gib = f"{free / 1024**3:.1f} GiB free"
    return _row("disk_free", "PASS" if free >= _MIN_FREE_BYTES else "FAIL", gib)


def _host_offset() -> float | None:
    result = wsl.run_cmd(["w32tm.exe", "/query", "/status"], timeout_s=_TIMEOUT_S)
    match = _PHASE_OFFSET.search(result.stdout)
    return float(match.group(1)) if result.returncode == 0 and match else None


def _wsl_offset(cfg: HernessConfig) -> float | None:
    result = wsl.run_wsl(cfg.deploy.wsl_distro, ["date", "+%s.%N"], timeout_s=_TIMEOUT_S)
    try:
        wsl_time = float(result.stdout.strip())
    except ValueError:
        return None
    return wsl_time - clock.now().timestamp() if result.returncode == 0 else None


def _check_clock(cfg: HernessConfig) -> HostCheckResult:
    offsets = {"w32tm": _host_offset(), "wsl": _wsl_offset(cfg)}
    statuses: list[_Status] = []
    parts: list[str] = []
    for source, offset in offsets.items():
        if offset is None:
            statuses.append("WARN")
            parts.append(f"{source} unavailable")
        else:
            statuses.append("FAIL" if abs(offset) >= _MAX_SKEW_S else "PASS")
            parts.append(f"{source} offset {offset:+.3f} s")
    return _row("clock", _worst(statuses), "; ".join(parts))


def _listeners(ports: Sequence[int]) -> list[tuple[int, str, int | None]]:
    """``(port, address, pid)`` of every TCP ``LISTEN`` socket on one of ``ports``."""
    return [
        (conn.laddr.port, conn.laddr.ip, conn.pid)
        for conn in psutil.net_connections(kind="tcp")
        if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port in ports
    ]


def _loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _check_ports_loopback(cfg: HernessConfig) -> HostCheckResult:
    ports = (*_MODEL_PORTS, cfg.security.ui.port)
    exposed = sorted({f"{port} on {ip}" for port, ip, _ in _listeners(ports) if not _loopback(ip)})
    if exposed:
        return _row("ports_loopback", "FAIL", "not loopback: " + ", ".join(exposed))
    return _row("ports_loopback", "PASS", "listeners on loopback only")


def _healthy(state: GpuStateReader, name: ServiceName) -> bool:
    try:
        return state.service_healthy(name)
    except HernessError:
        return False


def _no_class_loaded() -> bool:
    """``loaded_class() == "none"``, or no worker known and no healthy service (U10-90)."""
    state = gpu_state()
    try:
        return state.loaded_class() == "none"
    except HernessError:  # no worker to ask (jobs backend unbound or unreachable)
        return not any(_healthy(state, name) for name in get_args(ServiceName.__value__))


def _process_name(pid: int | None) -> str:
    if pid is None:
        return "unknown process"
    try:
        return psutil.Process(pid).name()
    except psutil.Error:
        return f"pid {pid}"


def _check_ports_expected(cfg: HernessConfig) -> HostCheckResult:
    del cfg
    if not _no_class_loaded():
        return _row("ports_expected", "PASS", "a model class is loaded")
    found = sorted({f"{port} by {_process_name(pid)}" for port, _, pid in _listeners(_MODEL_PORTS)})
    if found:
        return _row("ports_expected", "WARN", "unexpected listener: " + ", ".join(found))
    return _row("ports_expected", "PASS", "no listener while no class is loaded")


def _check_socket_guard(cfg: HernessConfig) -> HostCheckResult:
    del cfg
    try:
        conn = socket.create_connection(("1.1.1.1", 443), timeout=2)
    except EgressBlocked:
        return _row("socket_guard", "PASS", "connect to 1.1.1.1:443 raised EgressBlocked")
    except Exception as exc:  # noqa: BLE001 - any other outcome is the defect being reported
        return _row("socket_guard", "FAIL", f"connect raised {type(exc).__name__}")
    conn.close()
    return _row("socket_guard", "FAIL", "connect to 1.1.1.1:443 succeeded")


def _check_plugins(cfg: HernessConfig) -> HostCheckResult:
    """Every ``herness.plugins`` entry point is loaded by the registry on first lookup."""
    del cfg
    found = sorted(
        {
            f"{ep.dist.name if ep.dist else ep.name} {ep.dist.version if ep.dist else '?'}"
            for ep in metadata.entry_points(group="herness.plugins")
        }
    )
    if found:
        return _row("plugins", "WARN", "plugins: " + ", ".join(found))
    return _row("plugins", "PASS", "no herness.plugins entry points")


def _check_services(cfg: HernessConfig) -> HostCheckResult:
    manager = cfg.deploy.service.manager
    task = ["schtasks.exe", "/Query", "/TN"]
    worker = ["sc.exe", "query"] if manager == "nssm" else task
    wanted = {"herness-worker": worker, "herness-wsl": task}
    missing = [
        name
        for name, argv in wanted.items()
        if wsl.run_cmd([*argv, name], timeout_s=_TIMEOUT_S).returncode != 0
    ]
    if missing:
        return _row("services", "WARN", "not registered: " + ", ".join(missing))
    return _row("services", "PASS", f"registered ({manager})")


type Check = Callable[[HernessConfig], HostCheckResult]

HOST_CHECKS: Final[tuple[Check, ...]] = (
    _check_python_uv,
    _check_config_valid,
    _check_secrets_present,
    _check_acl_data,
    _check_bitlocker,
    _check_audit_chain,
    _check_disk_free,
    _check_clock,
    _check_ports_loopback,
    _check_ports_expected,
    _check_socket_guard,
    _check_plugins,
    _check_services,
)
# Extension point for U10-91 GPU_CHECKS (T10-28); empty until that card lands.
_GPU_CHECKS: tuple[Check, ...] = ()


# --- U10-89 ---------------------------------------------------------------------------------


def _check_name(check: Check) -> str:
    return getattr(check, "__name__", "check").removeprefix("_check_")


def _run_one(check: Check, cfg: HernessConfig) -> HostCheckResult:
    name = _check_name(check)
    if name in _WINDOWS_ONLY and not _on_windows():
        return _row(name, "WARN", _NOT_APPLICABLE)
    try:
        return check(cfg)
    except HernessError as exc:
        return _row(name, "FAIL", type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - doctor_checks never raises (U10-89 Errors)
        return _row(name, "FAIL", type(exc).__name__)


def doctor_checks(cfg: HernessConfig) -> list[HostCheckResult]:
    """Run ``HOST_CHECKS`` and the GPU checks concurrently; results in table order (U10-89)."""
    checks = (*HOST_CHECKS, *_GPU_CHECKS)
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="doctor") as pool:
        futures = [pool.submit(_run_one, check, cfg) for check in checks]
        return [future.result() for future in futures]
