"""Argument-list runners for `wsl.exe`, `powershell.exe` and Windows tools (impl 10 U10-76, U10-77).

The only subprocess runner in `herness/admin` (TH10-05): `shell=False`, an executable allowlist,
and strict patterns for everything that reaches `wsl.exe` or a PowerShell command line.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import TypeAdapter, ValidationError

from herness.core.errors import ConfigError, FatalError
from herness.core.logging import get_logger
from herness.core.settings import DeployConfig

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_log = get_logger("admin.cmd")

ALLOWED_COMMANDS: Final = frozenset(
    {
        "wsl.exe",
        "powershell.exe",
        "icacls.exe",
        "manage-bde.exe",
        "w32tm.exe",
        "sc.exe",
        "schtasks.exe",
        "gh",
        "uv",
        "nvidia-smi",
        "docker",
    }
)
_VENV_PY: Final = os.path.normcase(sys.executable)  # U10-112: the running Herness venv only
MAX_TIMEOUT_S: Final = 7200
MAX_OUTPUT_BYTES: Final = 16 * 1024 * 1024
_WSL_ARG: Final = re.compile(r"[A-Za-z0-9._:/@=+,%{}-]{1,512}")
_PS_CMDLETS: Final = frozenset(
    {"Get-NetFirewallHyperVVMSetting", "Set-NetFirewallHyperVVMSetting", "Get-NetFirewallRule"}
)
_PS_NAME: Final = re.compile(r"[A-Za-z]{1,40}")
_PS_VALUE: Final = re.compile(r"[A-Za-z0-9{}\-:.* ]{1,100}")
# The U10-07 distro pattern, reused from the deploy settings model rather than repeated here.
_DISTRO: Final[TypeAdapter[str]] = TypeAdapter(
    Annotated[str, *DeployConfig.model_fields["wsl_distro"].metadata]
)


@dataclass(frozen=True)
class CmdResult:
    """Outcome of one command: exit code (124 timeout, 127 not found), decoded output, seconds."""

    returncode: int
    stdout: str
    stderr: str
    duration_s: float


def _decode(data: bytes | None, encoding: str) -> str:
    return (data or b"")[:MAX_OUTPUT_BYTES].decode(encoding, errors="replace")


def run_cmd(
    argv: Sequence[str],
    *,
    timeout_s: float,
    stdin: bytes | None = None,
    encoding: str = "utf-8",
    check: bool = False,
) -> CmdResult:
    """Run one allowlisted program with no shell (U10-76); output is capped at 16 MiB."""
    if not argv or (argv[0] not in ALLOWED_COMMANDS and os.path.normcase(str(argv[0])) != _VENV_PY):
        msg = f"command not allowed: {argv[0] if argv else ''}"
        raise ConfigError(msg)
    elements: Sequence[object] = argv
    if not all(isinstance(a, str) and "\0" not in a for a in elements):
        msg = "command element is not a string without NUL"
        raise ConfigError(msg)
    if not 0 < timeout_s <= MAX_TIMEOUT_S:
        msg = f"timeout_s must be in (0, {MAX_TIMEOUT_S}]"
        raise ConfigError(msg)
    argv0 = argv[0]
    exe = shutil.which(argv0)
    if exe is None:
        result = CmdResult(127, "", "not found", 0)
    else:
        result = _execute([exe, *argv[1:]], timeout_s, stdin, encoding)
    _log.debug(
        "admin.cmd.completed",
        argv0=argv0,
        returncode=result.returncode,
        duration_s=result.duration_s,
    )
    if check and result.returncode != 0:
        msg = f"{argv0} failed: exit {result.returncode}"
        raise FatalError(msg)
    return result


def _execute(argv: list[str], timeout_s: float, stdin: bytes | None, encoding: str) -> CmdResult:
    start = time.monotonic()
    try:
        done = subprocess.run(  # noqa: S603 - allowlisted argv0, validated elements, shell=False
            argv, input=stdin, capture_output=True, timeout=timeout_s, shell=False, check=False
        )
    except subprocess.TimeoutExpired:  # subprocess.run has already killed the child
        return CmdResult(124, "", "timeout", time.monotonic() - start)
    return CmdResult(
        done.returncode,
        _decode(done.stdout, encoding),
        _decode(done.stderr, encoding),
        time.monotonic() - start,
    )


def run_wsl(
    distro: str,
    argv: Sequence[str],
    *,
    user: Literal["default", "root"] = "default",
    timeout_s: float,
    stdin: bytes | None = None,
    fixed_script: str | None = None,
) -> CmdResult:
    """Run a Linux program in `distro` via `--exec`, without the Linux shell (U10-77).

    `fixed_script` is the one exemption (ruling for U10-79 and U10-83): a module-level constant
    shell script. It is accepted only as `argv[2]` of an `sh -c <script> sh ...` call; every
    other element is still pattern-checked, so a value from config or input never reaches it.
    """
    try:
        _DISTRO.validate_python(distro, strict=True)
    except ValidationError:
        msg = "unsafe distro name for wsl"
        raise ConfigError(msg) from None
    exempt = _fixed_index(argv, fixed_script)
    if any(i != exempt and _WSL_ARG.fullmatch(a) is None for i, a in enumerate(argv)):
        msg = "unsafe argument for wsl"
        raise ConfigError(msg)
    prefix = ["wsl.exe", "-d", distro, *(["-u", "root"] if user == "root" else []), "--exec"]
    return run_cmd([*prefix, *argv], timeout_s=timeout_s, stdin=stdin)


def _fixed_index(argv: Sequence[str], fixed_script: str | None) -> int | None:
    if fixed_script is None:
        return None
    if len(argv) < 3 or list(argv[:2]) != ["sh", "-c"] or argv[2] != fixed_script:  # noqa: PLR2004
        msg = "unsafe argument for wsl"
        raise ConfigError(msg)
    return 2


def run_powershell(cmdlet: str, params: Mapping[str, str], *, timeout_s: float) -> CmdResult:
    """Run one allowlisted firewall cmdlet; values are single-quoted after the check (U10-77)."""
    if cmdlet not in _PS_CMDLETS:
        msg = "cmdlet not allowed"
        raise ConfigError(msg)
    parts = [cmdlet]
    for name, value in params.items():
        if _PS_NAME.fullmatch(name) is None or _PS_VALUE.fullmatch(value) is None:
            msg = "unsafe powershell parameter"
            raise ConfigError(msg)
        parts.append(f"-{name} '{value}'")
    command = " ".join(parts)
    if cmdlet.startswith("Get-"):
        command += " | ConvertTo-Json -Depth 3"
    return run_cmd(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        timeout_s=timeout_s,
    )
