"""ST10-36: a viewer cannot run admin commands (impl 10 TH10-01; R-46 exit 11).

``herness._cli.cmd_admin`` (T09-24) is not on this branch yet, so the test registers
test-local commands on the real root app ``herness.cli.app``: ``secrets set NAME`` is the
U09-98 wrapper around the real body ``cmd_secrets_set``; ``privacy delete`` and ``deploy up``
are recording probes (their bodies are T10-19 and T10-24). Each is ``guarded`` by its impl 09
command path, so ``check_command_role`` decides. T09-24 replaces the wrappers.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, cast

import pytest
import typer
from tests.support.cli_env import CliEnv
from tests.support.fake_keyring import MemoryKeyring

from herness import cli
from herness._cli import identity
from herness._cli.identity import guarded
from herness._cli.output import emit
from herness.admin.commands_secrets import cmd_secrets_set
from herness.reports.rules import Actor, UserInputError

pytestmark = pytest.mark.unit

VALUE = "St10-36-sentinel-value-x9"  # pragma: allowlist secret - test sentinel


def _secrets_set(
    ctx: typer.Context, actor: Actor | None, name: Annotated[str, typer.Argument()]
) -> int:
    """Stand-in for the T09-24 ``secrets set NAME`` command (U09-98)."""
    opts = cast("cli.GlobalOptions", ctx.find_root().obj)
    if opts.json:
        msg = "secrets set does not support --json"
        raise UserInputError(msg)
    assert actor is not None
    result = cmd_secrets_set(
        name, actor=actor.user_ref, prompt=lambda text: typer.prompt(text, hide_input=True)
    )
    return emit(opts, result.to_cli_result("secrets set"))


def _probe(path: str, ran: list[str]) -> typer.models.CommandFunctionType:
    def handler(actor: Actor | None) -> int:
        del actor
        ran.append(path)  # stands for the job the privacy body would enqueue
        return 0

    return handler


def _class_probe(path: str, ran: list[str]) -> typer.models.CommandFunctionType:
    # A plain required parameter: `guarded` keeps the string annotations of its signature, so
    # typer sees `Annotated[...]` metadata only through get_type_hints, which drops it.
    def handler(actor: Actor | None, gpu_class: str) -> int:
        del actor, gpu_class
        ran.append(path)  # stands for the deploy action the body would run
        return 0

    return handler


@pytest.fixture
def admin_app(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Register the three commands; the shell counts as elevated so only the role decides."""
    del cli_env
    monkeypatch.setattr(identity, "_is_elevated", lambda: True)
    ran: list[str] = []
    saved = list(cli.app.registered_groups)
    secrets_group, privacy, deploy = (typer.Typer(name=n) for n in ("secrets", "privacy", "deploy"))
    secrets_group.command("set")(guarded("secrets set")(_secrets_set))
    privacy.command("delete")(guarded("privacy delete")(_probe("privacy delete", ran)))
    deploy.command("up")(guarded("deploy up")(_class_probe("deploy up", ran)))
    for group in (secrets_group, privacy, deploy):
        cli.app.add_typer(group)
    try:
        yield ran
    finally:
        cli.app.registered_groups[:] = saved


def _run(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert isinstance(exc.value.code, int)
    return exc.value.code


def _audit_lines(root: Path) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    for path in sorted((root / "data" / "logs").glob("audit-*.jsonl")):
        lines += [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines()]
    return lines


def test_st10_36_viewer_refused_admin_commands(
    cli_env: CliEnv,
    admin_app: list[str],
    fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ST10-36 viewer runs secrets set, privacy delete, deploy up: exit 11 PermissionDenied;
    no keyring write, no job, no admin_action audit line."""
    prompts: list[str] = []
    monkeypatch.setattr(typer, "prompt", lambda text, **_kw: prompts.append(text) or VALUE)
    cfg_dir = str(cli_env.write_config(admins=["root-admin"], default_role="viewer"))
    before = dict(fake_keyring.store)
    for command in (
        ["secrets", "set", "snow.token"],
        ["privacy", "delete"],
        ["deploy", "up", "large"],
    ):
        assert _run(["--config-dir", cfg_dir, "--json", *command]) == 11
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert (payload["error"]["type"], payload["error"]["exit_code"]) == ("PermissionDenied", 11)
    assert fake_keyring.store == before
    assert (admin_app, prompts) == ([], [])
    lines = _audit_lines(cli_env.root)
    assert [line["event"] for line in lines] == ["auth"] * 3  # each refusal is audited
    assert all(line["fields"]["result"] == "denied" for line in lines)  # type: ignore[index]
    assert not any(line["event"] == "admin_action" for line in lines)


def test_st10_36_admin_control_sets_secret(
    cli_env: CliEnv,
    admin_app: list[str],
    fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ST10-36 control: the mapped admin's secrets set stores the value and audits by name."""
    del admin_app
    prompts: list[str] = []
    monkeypatch.setattr(typer, "prompt", lambda text, **_kw: prompts.append(text) or VALUE)
    cli_env.user = "root-admin"
    cfg_dir = str(cli_env.write_config(admins=["root-admin"], default_role="viewer"))
    assert _run(["--config-dir", cfg_dir, "secrets", "set", "snow.token"]) == 0
    assert prompts == ["Value for snow.token", "Repeat"]
    assert fake_keyring.store[("herness", "snow.token")] == VALUE
    actions = [line for line in _audit_lines(cli_env.root) if line["event"] == "admin_action"]
    assert [line["fields"] for line in actions] == [
        {"action": "secret_set", "target": "snow.token"}
    ]
    out, err = capsys.readouterr()
    assert VALUE not in out + err
    assert "snow.token" in out
