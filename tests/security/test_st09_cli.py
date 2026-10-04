"""Security tests for the CLI core: role refusal and stdout purity (T09-20; TH09-22, TH09-27)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
import typer
from tests.support.cli_env import CliEnv

from herness import cli
from herness._cli import identity
from herness._cli.identity import guarded
from herness._cli.output import CliResult, emit
from herness.core.logging import get_logger
from herness.reports.rules import Actor

pytestmark = pytest.mark.unit

_REAL_CREDENTIAL_USER = identity._credential_user  # before `cli_env` patches it


def _probe(path: str, ran: list[str]) -> typer.models.CommandFunctionType:
    def handler(actor: Actor | None) -> int:
        del actor
        ran.append(path)
        return 0

    return handler


@pytest.fixture
def guarded_app(cli_env: CliEnv) -> Iterator[list[str]]:
    """Register guarded probes for an admin, a viewer and a denied-allowed path."""
    ran: list[str] = []
    saved = list(cli.app.registered_commands)
    for path in ("build", "sync", "status", "doctor"):
        cli.app.command(path)(guarded(path)(_probe(path, ran)))
    try:
        yield ran
    finally:
        cli.app.registered_commands[:] = saved


def _run(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert isinstance(exc.value.code, int)
    return exc.value.code


@pytest.mark.parametrize("role", ["viewer", "denied"])
def test_st09_21_admin_commands_refused(
    cli_env: CliEnv, guarded_app: list[str], role: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST09-21 viewer and denied users: admin commands exit 11 with PermissionDenied."""
    cfg_dir = str(cli_env.write_config(admins=["root-admin"], default_role=role))
    for path in ("build", "sync"):
        assert _run(["--config-dir", cfg_dir, "--json", path]) == 11
        error = json.loads(capsys.readouterr().out)["error"]
        assert (error["type"], error["exit_code"]) == ("PermissionDenied", 11)
    assert guarded_app == []
    assert _run(["--config-dir", cfg_dir, "--json", "status"]) == (0 if role == "viewer" else 11)
    assert _run(["--config-dir", cfg_dir, "doctor"]) == 0
    assert guarded_app == (["status", "doctor"] if role == "viewer" else ["doctor"])


def test_st09_21_admin_allowed(cli_env: CliEnv, guarded_app: list[str]) -> None:
    """ST09-21 control: the mapped admin runs the admin command."""
    cli_env.user = "root-admin"
    cfg_dir = str(cli_env.write_config(admins=["root-admin"], default_role="denied"))
    assert _run(["--config-dir", cfg_dir, "build"]) == 0
    assert guarded_app == ["build"]


def test_st09_24_json_stdout_is_one_object(
    cli_env: CliEnv, capfd: pytest.CaptureFixture[str]
) -> None:
    """ST09-24 a --json command that logs warnings writes exactly one JSON object to stdout."""
    saved = list(cli.app.registered_commands)
    log = get_logger("cli")

    def noisy(ctx: typer.Context) -> int:
        log.warning("cli.worker.absent", detail="no worker")
        opts = ctx.obj
        opts.config()
        result = CliResult(command="status", data={"n": 1}, warnings=["slow"], human=None)
        return emit(opts, result)

    cli.app.command("noisy")(noisy)
    try:
        cfg_dir = str(cli_env.write_config())
        assert _run(["--config-dir", cfg_dir, "--json", "--verbose", "noisy"]) == 0
    finally:
        cli.app.registered_commands[:] = saved
    out, err = capfd.readouterr()
    lines = out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["data"] == {"schema": "cli/1", "n": 1}
    assert payload["warnings"] == ["slow"]
    assert "cli.worker.absent" in err
    assert "cli.command.completed" in err


@pytest.mark.parametrize("variable", ["USERNAME", "LOGNAME", "USER", "LNAME"])
def test_st09_21_env_spoof_gains_no_role(
    cli_env: CliEnv,
    guarded_app: list[str],
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    """ST09-21 env spoof (TH09-27): a denied user exporting an admin's name stays denied."""
    monkeypatch.setattr(identity, "_credential_user", _REAL_CREDENTIAL_USER)
    monkeypatch.setattr(identity, "_windows_user", lambda: "real-user")
    monkeypatch.setattr(identity, "_posix_user", lambda: "real-user")
    monkeypatch.setenv(variable, "root-admin")
    cfg_dir = str(cli_env.write_config(admins=["root-admin"], default_role="denied"))
    assert _run(["--config-dir", cfg_dir, "build"]) == 11
    assert guarded_app == []
