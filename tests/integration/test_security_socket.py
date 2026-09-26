"""Integration test for the process socket guard in a real subprocess (impl 10 IT10-15).

Card T10-18. Runs in a fresh Python process (a real ``sys.addaudithook`` cannot be uninstalled,
so the guard is exercised outside the test process itself) that installs the guard for profile
``local`` and attempts a real IP connect and a real hostname fetch; both must be blocked.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_SCRIPT = """
import socket
import urllib.request

from herness.core.config_sources import BootstrapConfig
from herness.core.egress_socket import install_socket_guard
from herness.core.errors import EgressBlocked
from herness.core.settings import SecurityConfig

install_socket_guard(BootstrapConfig("local", SecurityConfig(), ()))

try:
    socket.create_connection(("1.1.1.1", 443), timeout=2)
    print("connect: NOT BLOCKED")
except EgressBlocked:
    print("connect: blocked")

try:
    urllib.request.urlopen("https://example.org", timeout=2)
    print("urlopen: NOT BLOCKED")
except EgressBlocked:
    print("urlopen: blocked")
"""


def test_it10_15_subprocess_blocks_real_ip_and_hostname() -> None:
    """IT10-15 a fresh process: a raw-IP connect and a urlopen host both raise EgressBlocked."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, test-controlled script
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "connect: blocked" in proc.stdout
    assert "urlopen: blocked" in proc.stdout
