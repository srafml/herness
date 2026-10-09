"""Recorded host outputs and fakes for the doctor host checks (impl 10 U10-89, U10-90; T10-27).

Nothing here touches the real host: commands go through `fake_runner`, and `psutil`,
`socket`, `shutil`, the platform probe and the GPU state reader are monkeypatched.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest
from tests.support.config_tree import write_full_config

from herness.admin import doctor_host
from herness.core import time as clock
from herness.core.config import HernessConfig, load_config

NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.UTC)

ICACLS_DIR_OK = (
    "D:\\herness\\data BUILTIN\\Administrators:(OI)(CI)(F)\r\n"
    "                HERNESS-PC\\svc-herness:(OI)(CI)(M)\r\n"
    "                NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\r\n\r\n"
    "Successfully processed 1 files; Failed processing 0 files\r\n"
)
ICACLS_FILE_OK = (
    "D:\\herness\\.env BUILTIN\\Administrators:(F)\r\n"
    "                 HERNESS-PC\\svc-herness:(M)\r\n\r\n"
    "Successfully processed 1 files; Failed processing 0 files\r\n"
)
# ST10-38: a default-inherited tree — Users can read, and the ACEs are inherited (I).
ICACLS_USERS_INHERITED = (
    "D:\\herness\\data BUILTIN\\Administrators:(OI)(CI)(F)\r\n"
    "                HERNESS-PC\\svc-herness:(OI)(CI)(M)\r\n"
    "                BUILTIN\\Users:(RX)\r\n"
    "                NT AUTHORITY\\SYSTEM:(I)(OI)(CI)(F)\r\n\r\n"
    "Successfully processed 1 files; Failed processing 0 files\r\n"
)
_BDE_HEAD = (
    "BitLocker Drive Encryption: Configuration Tool version 10.0.26100\r\n"
    "Copyright (C) 2013 Microsoft Corporation. All rights reserved.\r\n\r\n"
)


def manage_bde(drive: str, protection: str) -> str:
    """Recorded `manage-bde -status <drive>` output with `Protection On` or `Protection Off`."""
    return (
        f"{_BDE_HEAD}Volume {drive} [Data]\r\n[Data Volume]\r\n\r\n"
        "    Size:                 931.39 GB\r\n"
        "    BitLocker Version:    2.0\r\n"
        "    Conversion Status:    Used Space Only Encrypted\r\n"
        "    Percentage Encrypted: 100.0%\r\n"
        "    Encryption Method:    XTS-AES 128\r\n"
        f"    Protection Status:    {protection}\r\n"
        "    Lock Status:          Unlocked\r\n"
    )


BDE_DENIED = f"{_BDE_HEAD}ERROR: An attempt to access a required resource was denied.\r\n"


def w32tm(offset: str) -> str:
    """Recorded `w32tm /query /status` output with the given `Phase Offset` (e.g. `0.0012s`)."""
    return (
        "Leap Indicator: 0(no warning)\r\n"
        "Stratum: 4 (secondary reference - syncd by (S)NTP)\r\n"
        "Precision: -23 (119.209ns per tick)\r\n"
        "Root Delay: 0.0312500s\r\n"
        "Source: time.windows.com,0x9\r\n"
        "Poll Interval: 10 (1024s)\r\n\r\n"
        f"Phase Offset: {offset}\r\n"
        "ClockRate: 0.0156250s\r\n"
    )


SC_WORKER_OK = (
    "\r\nSERVICE_NAME: herness-worker \r\n        TYPE               : 10  WIN32_OWN_PROCESS  \r\n"
    "        STATE              : 4  RUNNING \r\n"
)
SC_MISSING = "[SC] EnumQueryServicesStatus:OpenService FAILED 1060:\r\n\r\n"
SCHTASKS_WSL_OK = (
    "\r\nFolder: \\\r\nTaskName                                 Next Run Time          Status\r\n"
    "======================================== ====================== ===============\r\n"
    "herness-wsl                              N/A                    Ready\r\n"
)
SCHTASKS_MISSING = "ERROR: The system cannot find the file specified.\r\n"


def load_cfg(tmp_path: Path) -> tuple[HernessConfig, Path]:
    """A loaded config over a `write_full_config` tree, with `data\\` and `logs\\` created."""
    cfg_dir = write_full_config(tmp_path)
    cfg = load_config(config_dir=cfg_dir, env={})
    cfg.paths.logs.mkdir(parents=True, exist_ok=True)
    return cfg, cfg_dir


def with_paths(cfg: HernessConfig, **paths: Path) -> HernessConfig:
    """`cfg` with some `paths.*` replaced."""
    return cfg.model_copy(update={"paths": cfg.paths.model_copy(update=paths)})


def with_directory_file(cfg: HernessConfig, path: Path) -> HernessConfig:
    """`cfg` with `security.redaction.directory_file` set."""
    redaction = cfg.security.redaction.model_copy(update={"directory_file": path})
    security = cfg.security.model_copy(update={"redaction": redaction})
    return cfg.model_copy(update={"security": security})


def with_manager(cfg: HernessConfig, manager: str) -> HernessConfig:
    """`cfg` with `deploy.service.manager` set."""
    service = cfg.deploy.service.model_copy(update={"manager": manager})
    deploy = cfg.deploy.model_copy(update={"service": service})
    return cfg.model_copy(update={"deploy": deploy})


def listen(ip: str, port: int, pid: int | None = 4242, status: str = psutil.CONN_LISTEN) -> Any:
    """One fake `psutil.net_connections` entry."""
    return SimpleNamespace(laddr=SimpleNamespace(ip=ip, port=port), status=status, pid=pid)


@dataclass
class FakeGpuState:
    """`GpuStateReader` stand-in: `loaded` is a class, or an exception to raise."""

    loaded: str | BaseException = "none"
    healthy: set[str] = field(default_factory=set)

    def loaded_class(self) -> str:
        if isinstance(self.loaded, BaseException):
            raise self.loaded
        return self.loaded

    def service_healthy(self, name: str) -> bool:
        return name in self.healthy


@dataclass
class FakeProcess:
    """`psutil.Process` stand-in naming processes from `names` (pid → name)."""

    names: dict[int, str]

    def __call__(self, pid: int) -> Any:
        if pid not in self.names:
            raise psutil.NoSuchProcess(pid)
        return SimpleNamespace(name=lambda: self.names[pid])


@dataclass
class Host:
    """The monkeypatched host: listeners, GPU state, process names and the platform probe."""

    monkeypatch: pytest.MonkeyPatch
    conns: list[Any] = field(default_factory=list)
    gpu: FakeGpuState = field(default_factory=FakeGpuState)
    processes: FakeProcess = field(default_factory=lambda: FakeProcess({}))

    def install(self, cfg_dir: Path, *, windows: bool = True) -> None:
        mp = self.monkeypatch
        mp.setattr(doctor_host, "_on_windows", lambda: windows)
        mp.setattr(doctor_host, "_config_dir", lambda: cfg_dir)
        mp.setattr(psutil, "net_connections", lambda kind="inet": list(self.conns))
        mp.setattr(psutil, "Process", self.processes)
        mp.setattr(doctor_host, "gpu_state", lambda: self.gpu)
        mp.setattr(clock, "now", lambda: NOW)
        mp.setenv("SYSTEMDRIVE", "C:")
