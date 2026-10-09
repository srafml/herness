"""IT10-12: doctor host rows on a non-Windows host (impl 10 F10-09, U10-89).

The platform probe reports a Linux host: the Windows-only rows (`acl_data`, `bitlocker`,
`clock`, `services`) WARN "not applicable on this host" without running any Windows program
(`fake_runner` has no answers, so any call would fail the test), and every other row
evaluates against a real config tree, the real `validate`, a real audit chain and real disk.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.support.doctor_host_env import Host, listen, load_cfg
from tests.support.fake_runner import FakeRunner

from herness.admin import doctor_host as dh
from herness.core import audit
from herness.core.config import HernessConfig, init_config, reset_config
from herness.core.errors import EgressBlocked

pytestmark = pytest.mark.integration

WINDOWS_ONLY = {"acl_data", "bitlocker", "clock", "services"}


@pytest.fixture
def linux_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[HernessConfig]:
    cfg, cfg_dir = load_cfg(tmp_path)
    init_config(config_dir=cfg_dir, env={})  # audit() writes under the cached config's logs
    host = Host(monkeypatch, conns=[listen("127.0.0.1", 8000)])
    host.install(cfg_dir, windows=False)

    def blocked(address: Any, timeout: Any = None) -> Any:
        msg = "blocked"
        raise EgressBlocked(msg, reason="not_allowed")

    monkeypatch.setattr(socket, "create_connection", blocked)
    yield cfg
    reset_config()


def test_it10_12_windows_rows_not_applicable_others_evaluate(
    linux_host: HernessConfig, fake_runner: FakeRunner
) -> None:
    """IT10-12 non-Windows host: Windows rows WARN "not applicable"; others evaluate."""
    audit.audit("admin_action", "system", action="purge", target="t0", counts="rows=0")
    rows = {r.name: r for r in dh.doctor_checks(linux_host)}
    assert fake_runner.calls == []
    for name in WINDOWS_ONLY:
        assert (rows[name].status, rows[name].detail) == ("WARN", "not applicable on this host")
    evaluated = {name: row for name, row in rows.items() if name not in WINDOWS_ONLY}
    assert all(row.detail != "not applicable on this host" for row in evaluated.values())
    assert rows["audit_chain"].status == "PASS"
    assert rows["audit_chain"].detail == "1 files, 1 lines intact"
    assert rows["socket_guard"].status == "PASS"
    assert rows["ports_loopback"].status == "PASS"
    assert rows["config_valid"].status in ("PASS", "WARN", "FAIL")
    assert rows["disk_free"].detail.endswith("GiB free")
    assert len(rows) == len(dh.HOST_CHECKS)
