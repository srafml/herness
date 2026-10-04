"""ST10-06: config values cannot be injected into `wsl.exe` or PowerShell (impl 10 TH10-05)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from herness.admin import wsl
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def boom(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        msg = "a subprocess was started"
        raise AssertionError(msg)

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(shutil, "which", lambda name: name)
    return calls


@pytest.mark.parametrize("value", ["a/b;rm -rf /", "$(id)", "abc$(id)", "a b", "`id`", "a&&b"])
def test_st10_06_model_and_revision_values(started: list[object], value: str) -> None:
    with pytest.raises(ConfigError):
        wsl.run_wsl("herness", ["serve", value], timeout_s=5)
    assert started == []


@pytest.mark.parametrize("distro", ["my distro", "x;rm", "$(id)", "-u", "a\nb"])
def test_st10_06_distro_values(started: list[object], distro: str) -> None:
    with pytest.raises(ConfigError):
        wsl.run_wsl(distro, ["ls"], timeout_s=5)
    assert started == []


@pytest.mark.parametrize(
    "argv", [["cmd.exe", "/c", "calc"], ["bash", "-c", "id"], ["wsl", "-e", "id"], ["curl", "x"]]
)
def test_st10_06_unlisted_executable(started: list[object], argv: list[str]) -> None:
    with pytest.raises(ConfigError, match="command not allowed"):
        wsl.run_cmd(argv, timeout_s=5)
    assert started == []


def test_st10_06_powershell_injection(started: list[object]) -> None:
    for params in ({"Name": "x'; calc; '"}, {"Name": "$(id)"}, {"Name;calc": "x"}):
        with pytest.raises(ConfigError):
            wsl.run_powershell("Get-NetFirewallRule", params, timeout_s=5)
    with pytest.raises(ConfigError):
        wsl.run_powershell("Invoke-Expression", {}, timeout_s=5)
    assert started == []


def test_st10_06_fixed_script_exemption_cannot_carry_user_values(started: list[object]) -> None:
    script = "cat > $1"
    bad_script = ["sh", "-c", "x;$(id)", "sh", "f"]
    bad_arg = ["sh", "-c", script, "sh", "f;id"]
    for argv in (bad_script, bad_arg):
        with pytest.raises(ConfigError):
            wsl.run_wsl("herness", argv, timeout_s=5, fixed_script=script)
    assert started == []
