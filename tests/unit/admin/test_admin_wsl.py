"""Tests for the command runners (impl 10 U10-76, U10-77; UT10-71 runner cases)."""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import pytest

from herness.admin import wsl
from herness.admin.wsl import CmdResult, run_cmd, run_powershell, run_wsl
from herness.core.errors import ConfigError, FatalError

pytestmark = pytest.mark.unit


class Spy:
    """Replaces `subprocess.run` and `shutil.which` inside `herness.admin.wsl`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.result: subprocess.CompletedProcess[bytes] = subprocess.CompletedProcess(
            [], 0, b"", b""
        )
        self.raises: BaseException | None = None
        monkeypatch.setattr(shutil, "which", lambda name: f"C:\\bin\\{name}")
        monkeypatch.setattr(subprocess, "run", self._run)

    def _run(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, kw))
        if self.raises is not None:
            raise self.raises
        return self.result

    def set_out(self, rc: int, out: bytes, err: bytes = b"") -> None:
        self.result = subprocess.CompletedProcess([], rc, out, err)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Spy:
    return Spy(monkeypatch)


def test_ut10_71_run_cmd_decodes_utf16le(spy: Spy) -> None:
    spy.set_out(0, "herness\r\nUbuntu\r\n".encode("utf-16-le"))
    res = run_cmd(["wsl.exe", "-l", "-q"], timeout_s=5, encoding="utf-16-le")
    assert res.stdout == "herness\r\nUbuntu\r\n"
    argv, kw = spy.calls[0]
    assert argv[1:] == ["-l", "-q"]
    assert kw["shell"] is False
    assert kw["capture_output"] is True
    assert kw["timeout"] == 5


def test_ut10_71_run_cmd_replaces_bad_bytes(spy: Spy) -> None:
    spy.set_out(0, b"ok\xff")
    assert run_cmd(["uv", "x"], timeout_s=1).stdout == "ok\ufffd"


def test_ut10_71_run_cmd_passes_stdin(spy: Spy) -> None:
    run_cmd(["docker", "ps"], timeout_s=1, stdin=b"data")
    assert spy.calls[0][1]["input"] == b"data"


def test_ut10_71_run_cmd_truncates_output(spy: Spy) -> None:
    big = b"a" * (wsl.MAX_OUTPUT_BYTES + 100)
    spy.set_out(0, big, big)
    res = run_cmd(["docker", "ps"], timeout_s=1)
    assert len(res.stdout) == wsl.MAX_OUTPUT_BYTES
    assert len(res.stderr) == wsl.MAX_OUTPUT_BYTES


def test_ut10_71_run_cmd_timeout(spy: Spy) -> None:
    spy.raises = subprocess.TimeoutExpired(["docker"], 1)
    res = run_cmd(["docker", "ps"], timeout_s=1)
    assert (res.returncode, res.stdout, res.stderr) == (124, "", "timeout")


def test_ut10_71_run_cmd_timeout_with_check_is_fatal(spy: Spy) -> None:
    spy.raises = subprocess.TimeoutExpired(["docker"], 1)
    with pytest.raises(FatalError, match="docker failed: exit 124"):
        run_cmd(["docker", "ps"], timeout_s=1, check=True)


def test_ut10_71_run_cmd_check_nonzero_is_fatal(spy: Spy) -> None:
    spy.set_out(3, b"", b"boom")
    assert run_cmd(["gh", "x"], timeout_s=1).returncode == 3
    with pytest.raises(FatalError, match="gh failed: exit 3") as exc:
        run_cmd(["gh", "x"], timeout_s=1, check=True)
    assert "boom" not in str(exc.value)


def test_ut10_71_run_cmd_not_found(monkeypatch: pytest.MonkeyPatch, spy: Spy) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert run_cmd(["nvidia-smi"], timeout_s=1) == CmdResult(127, "", "not found", 0)
    assert spy.calls == []
    with pytest.raises(FatalError):
        run_cmd(["nvidia-smi"], timeout_s=1, check=True)


@pytest.mark.parametrize("timeout_s", [7200.5, 10**6, 0, -1])
def test_ut10_71_run_cmd_timeout_bound(spy: Spy, timeout_s: float) -> None:
    with pytest.raises(ConfigError):
        run_cmd(["docker", "ps"], timeout_s=timeout_s)
    assert spy.calls == []
    run_cmd(["docker", "ps"], timeout_s=7200)
    assert len(spy.calls) == 1


@pytest.mark.parametrize("argv", [[], ["cmd.exe", "/c", "dir"], ["wsl"], ["/usr/bin/docker"]])
def test_ut10_71_run_cmd_allowlist(spy: Spy, argv: list[str]) -> None:
    with pytest.raises(ConfigError, match="command not allowed"):
        run_cmd(argv, timeout_s=1)
    assert spy.calls == []


def test_ut10_71_run_cmd_nul_rejected(spy: Spy) -> None:
    with pytest.raises(ConfigError):
        run_cmd(["docker", "a\0b"], timeout_s=1)
    assert spy.calls == []


def test_ut10_71_run_wsl_argv_shape(spy: Spy) -> None:
    run_wsl("herness", ["ls", "-la", "/opt/x"], timeout_s=5)
    argv = spy.calls[0][0]
    assert argv[1:] == ["-d", "herness", "--exec", "ls", "-la", "/opt/x"]


def test_ut10_71_run_wsl_root_user_before_exec(spy: Spy) -> None:
    run_wsl("herness", ["id"], user="root", timeout_s=5, stdin=b"in")
    argv, kw = spy.calls[0]
    assert argv[1:] == ["-d", "herness", "-u", "root", "--exec", "id"]
    assert kw["input"] == b"in"


@pytest.mark.parametrize(
    "bad", ["a b", "x;y", "$(id)", "`id`", "a|b", "a'b", "", "x" * 513, "a\nb"]
)
def test_ut10_71_run_wsl_unsafe_argument(spy: Spy, bad: str) -> None:
    with pytest.raises(ConfigError, match="unsafe argument for wsl"):
        run_wsl("herness", ["ls", bad], timeout_s=5)
    assert spy.calls == []


@pytest.mark.parametrize("bad", ["my distro", "-x", "a;b", "", "a" * 65, "d\n"])
def test_ut10_71_run_wsl_unsafe_distro(spy: Spy, bad: str) -> None:
    with pytest.raises(ConfigError):
        run_wsl(bad, ["ls"], timeout_s=5)
    assert spy.calls == []


SCRIPT = 'umask 077; cat > "$1.tmp" && mv "$1.tmp" "$1"'


def test_ut10_71_run_wsl_fixed_script(spy: Spy) -> None:
    argv = ["sh", "-c", SCRIPT, "sh", "/opt/herness/docker.env"]
    run_wsl("herness", argv, user="root", timeout_s=5, stdin=b"K=v", fixed_script=SCRIPT)
    assert spy.calls[0][0][-5:] == argv


@pytest.mark.parametrize(
    "argv",
    [
        ["sh", "-c", SCRIPT, "sh", "bad;arg"],  # other elements are still checked
        ["sh", "-c", "other;script", "sh", "x"],  # not the declared constant
        ["bash", "-c", SCRIPT],  # not `sh -c`
        ["ls", "x", SCRIPT],  # wrong position
    ],
)
def test_ut10_71_run_wsl_fixed_script_is_tight(spy: Spy, argv: list[str]) -> None:
    with pytest.raises(ConfigError):
        run_wsl("herness", argv, timeout_s=5, fixed_script=SCRIPT)
    assert spy.calls == []


def test_ut10_71_run_wsl_script_without_declaration_rejected(spy: Spy) -> None:
    with pytest.raises(ConfigError):
        run_wsl("herness", ["sh", "-c", SCRIPT, "sh", "x"], timeout_s=5)
    assert spy.calls == []


def test_ut10_71_run_powershell_get_command(spy: Spy) -> None:
    params = {"DisplayName": "herness block*", "Enabled": "True"}
    run_powershell("Get-NetFirewallRule", params, timeout_s=9)
    argv = spy.calls[0][0]
    assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert argv[4] == (
        "Get-NetFirewallRule -DisplayName 'herness block*' -Enabled 'True'"
        " | ConvertTo-Json -Depth 3"
    )


def test_ut10_71_run_powershell_set_has_no_json_pipe(spy: Spy) -> None:
    params = {"Name": "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}", "DefaultOutboundAction": "Allow"}
    run_powershell("Set-NetFirewallHyperVVMSetting", params, timeout_s=9)
    assert spy.calls[0][0][4] == (
        "Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'"
        " -DefaultOutboundAction 'Allow'"
    )


def test_ut10_71_run_powershell_no_params(spy: Spy) -> None:
    run_powershell("Get-NetFirewallHyperVVMSetting", {}, timeout_s=9)
    assert spy.calls[0][0][4] == "Get-NetFirewallHyperVVMSetting | ConvertTo-Json -Depth 3"


@pytest.mark.parametrize(
    ("cmdlet", "params"),
    [
        ("Remove-NetFirewallRule", {}),
        ("Get-NetFirewallRule; calc", {}),
        ("Get-NetFirewallRule", {"Name": "a'b"}),
        ("Get-NetFirewallRule", {"Name": "a;b"}),
        ("Get-NetFirewallRule", {"Name": ""}),
        ("Get-NetFirewallRule", {"Name": "x" * 101}),
        ("Get-NetFirewallRule", {"Na-me": "x"}),
        ("Get-NetFirewallRule", {"Name; calc": "x"}),
        ("Get-NetFirewallRule", {"N" * 41: "x"}),
        ("Get-NetFirewallRule", {"Name": "x\n"}),
    ],
)
def test_ut10_71_run_powershell_unsafe(spy: Spy, cmdlet: str, params: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        run_powershell(cmdlet, params, timeout_s=9)
    assert spy.calls == []
