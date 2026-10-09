"""Security tests for the doctor host rows (impl 10 ST10-17 doctor part, ST10-28, ST10-38,
ST10-42, ST10-49; TH10-08, TH10-09, TH10-13, TH10-25, TH10-26, TH10-41, TH10-43).

Commands answer from recorded outputs through `fake_runner`; `psutil` and the GPU state are
monkeypatched. The audit attack runs against a real chain written by `herness.core.audit`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.doctor_host_env import (
    ICACLS_USERS_INHERITED,
    Host,
    listen,
    load_cfg,
    manage_bde,
    with_paths,
)
from tests.support.fake_runner import FakeRunner

from herness.admin import doctor_host as dh
from herness.core import audit
from herness.core.config import HernessConfig, init_config, reset_config

pytestmark = pytest.mark.unit

OLD = (1_700_000_000, 1_700_000_000)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[HernessConfig, Host]]:
    cfg, cfg_dir = load_cfg(tmp_path)
    init_config(config_dir=cfg_dir, env={})
    host = Host(monkeypatch)
    host.install(cfg_dir)
    yield cfg, host
    reset_config()


def test_st10_17_doctor_audit_chain_fails_naming_the_line(env: tuple[HernessConfig, Host]) -> None:
    """ST10-17 doctor audit_chain FAIL naming file:line after edit, delete, reorder, truncate."""
    cfg, _ = env
    for n in range(5):
        audit.audit("admin_action", "system", action="purge", target=f"t{n}", counts=f"rows={n}")
    (path,) = cfg.paths.logs.glob("audit-*.jsonl")
    orig = path.read_bytes().splitlines()
    assert dh._check_audit_chain(cfg).status == "PASS"
    attacks = [
        ([orig[0], orig[1].replace(b'"t1"', b'"tX"'), *orig[2:]], b"\n", 3),
        ([orig[0], *orig[2:]], b"\n", 2),
        ([orig[0], orig[2], orig[1], *orig[3:]], b"\n", 2),
        ([*orig[:4], orig[4][:20]], b"", 5),
    ]
    for lines, tail, line_no in attacks:
        path.write_bytes(b"\n".join(lines) + tail)
        os.utime(path, OLD)
        row = dh._check_audit_chain(cfg)
        assert (row.name, row.status) == ("audit_chain", "FAIL")
        assert row.detail == f"chain broken at {path.name}:{line_no}"
        assert row.fix == "Investigate; restore from backup"


def test_st10_28_vllm_on_all_interfaces_fails_ports_loopback(
    env: tuple[HernessConfig, Host],
) -> None:
    """ST10-28 vLLM listening on 0.0.0.0:8000 → ports_loopback FAIL."""
    cfg, host = env
    host.conns = [listen("0.0.0.0", 8000, pid=31)]  # noqa: S104 - the attack under test
    row = dh._check_ports_loopback(cfg)
    assert (row.status, row.detail) == ("FAIL", "not loopback: 8000 on 0.0.0.0")


def test_st10_38_users_and_inherited_ace_fail_acl_data(
    env: tuple[HernessConfig, Host], fake_runner: FakeRunner
) -> None:
    """ST10-38 icacls output with BUILTIN\\Users:(RX) and an (I) ACE → acl_data FAIL."""
    cfg, _ = env
    fake_runner.add(["icacls.exe"], ICACLS_USERS_INHERITED)
    row = dh._check_acl_data(cfg)
    assert row.status == "FAIL"
    assert f"{cfg.paths.data}: BUILTIN\\Users has access" in row.detail
    inherited_only = ICACLS_USERS_INHERITED.replace("                BUILTIN\\Users:(RX)\r\n", "")
    fake_runner.add(["icacls.exe"], inherited_only)
    row = dh._check_acl_data(cfg)
    assert row.status == "FAIL"
    assert f"{cfg.paths.data}: inherited ACE (I)" in row.detail
    assert "BUILTIN\\Users" not in row.detail


def test_st10_42_unknown_listener_without_worker_warns_with_name(
    env: tuple[HernessConfig, Host],
) -> None:
    """ST10-42 no worker; an unknown process listening on 8100 → ports_expected WARN + name."""
    cfg, host = env
    host.gpu.loaded = "none"
    host.conns = [listen("127.0.0.1", 8100, pid=5150)]
    host.processes.names[5150] = "squatter.exe"
    row = dh._check_ports_expected(cfg)
    assert (row.status, row.detail) == ("WARN", "unexpected listener: 8100 by squatter.exe")
    assert row.fix == "Stop the unexpected listener"


def test_st10_49_backup_drive_protection_off_fails_bitlocker(
    env: tuple[HernessConfig, Host], fake_runner: FakeRunner
) -> None:
    """ST10-49 manage-bde reports Protection Off for the backup drive → bitlocker FAIL."""
    cfg, _ = env
    cfg = with_paths(cfg, data=Path("D:/herness/data"), backup_target=Path("E:/herness-backup"))
    fake_runner.add(["manage-bde.exe", "-status", "C:"], manage_bde("C:", "Protection On"))
    fake_runner.add(["manage-bde.exe", "-status", "D:"], manage_bde("D:", "Protection On"))
    fake_runner.add(["manage-bde.exe", "-status", "E:"], manage_bde("E:", "Protection Off"))
    row = dh._check_bitlocker(cfg)
    assert row.status == "FAIL"
    assert row.detail == "C: Protection On; D: Protection On; E: Protection Off"
    assert row.fix == "Enable BitLocker"
