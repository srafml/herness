"""UT10-71 real-process case: the runner against Windows PowerShell (impl 10 U10-76)."""

from __future__ import annotations

import shutil
import sys

import pytest

from herness.admin.wsl import run_cmd

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell.exe") is None,
    reason="needs Windows PowerShell",
)
def test_ut10_71_real_powershell_roundtrip() -> None:
    res = run_cmd(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Write-Output hi"],
        timeout_s=60,
        check=True,
    )
    assert res.stdout.strip() == "hi"
    assert res.returncode == 0
