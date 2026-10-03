"""Tests for herness._cli.identity: command table, CLI actor, role and elevation checks (T09-20)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import typer
import typer.main
from structlog.testing import capture_logs
from tests.support.cli_env import CliEnv

from herness._cli import identity as ident
from herness.cli import GlobalOptions, app
from herness.core import errors as e
from herness.reports.rules import ROLE_RANK

pytestmark = pytest.mark.unit

TABLE = Path(__file__).resolve().parents[2] / "fixtures" / "cli" / "command_table.md"
_ROLE_WORD = {"admin": "admin", "reviewer": "reviewer", "viewer": "viewer", "any": "viewer"}


def _paths(cell: str) -> list[str]:
    """Command paths in one first-column cell: `a b` / `a c`, `x y\\|z` alternatives."""
    paths: list[str] = []
    for spec in re.findall(r"`([^`]+)`", cell):
        words: list[str] = []
        for word in spec.split():
            if word.isupper() or word.startswith("["):
                break
            words.append(word)
        *head, last = words
        paths += [" ".join([*head, alt]) for alt in last.split("\\|")]
    return paths


def _table() -> dict[str, str]:
    """The §3.12 fixture copy as path -> minimum role (`denied` for the any-also-denied rows)."""
    rows = [line for line in TABLE.read_text("utf-8").splitlines() if line.startswith("| `")]
    roles: dict[str, str] = {}
    for line in rows:
        cells = [c.strip() for c in line.strip("|").split(" | ")]
        word = cells[2].split()[0].rstrip(",")
        role = "denied" if "also `denied`" in cells[2] else _ROLE_WORD[word]
        roles |= dict.fromkeys(_paths(cells[0]), role)
    return roles


def _registered(command: Any, prefix: str = "") -> Iterator[str]:
    """Every leaf command path of the Typer app (a group name joins its subcommands)."""
    for name, sub in getattr(command, "commands", {}).items():
        path = f"{prefix} {name}".strip()
        if getattr(sub, "commands", None) is None:
            yield path
        else:
            yield from _registered(sub, path)


def test_ut09_66_command_roles_match_fixture_table() -> None:
    """UT09-66 COMMAND_ROLES equals the §3.12 table; R-47 rows present; sets are consistent."""
    table = _table()
    assert len(table) == 53
    expected = {p: ("viewer" if r == "denied" else r) for p, r in table.items()}
    assert dict(ident.COMMAND_ROLES) == expected
    assert {"deploy install", "secrets rekey", "gpu load"} <= set(ident.COMMAND_ROLES)
    assert "`large`" in TABLE.read_text("utf-8").split("`gpu load CLASS`")[1].splitlines()[0]
    assert {p for p, r in table.items() if r == "denied"} == ident.DENIED_ALLOWED
    assert {"doctor", "config validate"} == ident.DENIED_ALLOWED
    assert set(ident.COMMAND_ROLES) >= ident.WRITE_COMMANDS
    assert set(ident.COMMAND_ROLES) >= ident.ELEVATED_COMMANDS
    assert "secrets init" not in ident.WRITE_COMMANDS
    rows = TABLE.read_text("utf-8").splitlines()
    elevated = {p for line in rows if "elevated" in line for p in _paths(line.split(" | ")[0])}
    assert elevated == ident.ELEVATED_COMMANDS


def test_ut09_66_every_registered_command_has_a_role() -> None:
    """UT09-66 every registered Typer command path is in COMMAND_ROLES and in the table.

    No command group is registered before T09-22..T09-25, so this is vacuous for now.
    """
    table = _table()
    for path in _registered(typer.main.get_command(app)):
        assert path in ident.COMMAND_ROLES, path
        assert path in table, path


def test_ut09_66_paths_parser() -> None:
    """UT09-66 fixture parser: alternatives, arguments and optional arguments."""
    assert _paths("`report funding\\|org`") == ["report funding", "report org"]
    assert _paths("`decide REC_ID accepted\\|rejected\\|deferred`") == ["decide"]
    assert _paths("`deploy up CLASS` / `deploy down [CLASS]`") == ["deploy up", "deploy down"]


def _opts(env: CliEnv, default_role: str, **kw: Any) -> GlobalOptions:
    env.write_config(admins=["root-admin"], reviewers=["rev"], default_role=default_role)
    return GlobalOptions(config_dir=env.config_dir, **kw)


def _audit(env: CliEnv) -> list[dict[str, Any]]:
    logs = sorted((env.root / "data" / "logs").glob("audit-*.jsonl"))
    return [json.loads(x) for p in logs for x in p.read_text("utf-8").splitlines()]


def test_ut09_66_denied_user_refused_everywhere_but_two(cli_env: CliEnv) -> None:
    """UT09-66 denied user: refused for every path except doctor and config validate."""
    opts = _opts(cli_env, "denied")
    for path in ident.COMMAND_ROLES:
        if path in ident.DENIED_ALLOWED:
            assert ident.check_command_role(opts, path) is None
            continue
        with pytest.raises(e.PermissionDenied) as exc:
            ident.check_command_role(opts, path)
        assert exc.value.message == (
            f"You need the {ident.COMMAND_ROLES[path]} role to run herness {path}."
        )


def test_ut09_66_viewer_refused_for_admin_paths(cli_env: CliEnv) -> None:
    """UT09-66 viewer: admin and reviewer paths refused; viewer paths return the actor."""
    opts = _opts(cli_env, "viewer")
    for path, needed in ident.COMMAND_ROLES.items():
        if path in ident.DENIED_ALLOWED:
            continue
        if ROLE_RANK[needed] > ROLE_RANK["viewer"]:
            with pytest.raises(e.PermissionDenied):
                ident.check_command_role(opts, path)
        else:
            actor = ident.check_command_role(opts, path)
            assert actor is not None
            assert (actor.role, actor.channel, actor.display) == ("viewer", "cli", "alice")
            assert re.fullmatch(r"[0-9a-f]{32}", actor.user_ref)


def test_ut09_66_refusal_audited_and_logged(cli_env: CliEnv) -> None:
    """UT09-66 a refusal writes one `auth` audit line and logs `cli.auth.denied`."""
    opts = _opts(cli_env, "viewer")
    opts.config()  # configure_logging first, so capture_logs stays in place
    with capture_logs() as logs, pytest.raises(e.PermissionDenied):
        ident.check_command_role(opts, "build")
    (line,) = _audit(cli_env)
    actor = ident.cli_actor(opts.config(), need_ref=True)
    assert line["event"] == "auth"
    assert line["fields"] == {"user_ref": actor.user_ref, "role": "viewer", "result": "denied"}
    denied = [x for x in logs if x["event"] == "cli.auth.denied"]
    assert denied == [
        {
            "component": "cli",
            "event": "cli.auth.denied",
            "log_level": "warning",
            "user_ref": actor.user_ref,
            "role": "viewer",
            "action": "cli:build",
        }
    ]
    assert "alice" not in json.dumps(logs, default=str) + json.dumps(line)


def test_ut09_66_admin_inline_and_elevation(
    cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-66 --inline needs admin (R-45); ELEVATED_COMMANDS need an elevated process."""
    cli_env.user = "rev"
    opts = _opts(cli_env, "viewer")
    actor = ident.check_command_role(opts, "review-queue list")
    assert actor is not None
    assert actor.role == "reviewer"
    with pytest.raises(e.PermissionDenied, match="admin role"):
        ident.check_command_role(opts, "review-queue list", inline=True)
    cli_env.user = "root-admin"
    admin = GlobalOptions(config_dir=cli_env.config_dir)
    monkeypatch.setattr(ident, "_is_elevated", lambda: False)
    assert ident.check_command_role(admin, "build", inline=True) is not None
    for path in ident.ELEVATED_COMMANDS:
        with pytest.raises(
            e.PermissionDenied, match=r"^Run this command from an elevated shell\.$"
        ):
            ident.check_command_role(admin, path)
    monkeypatch.setattr(ident, "_is_elevated", lambda: True)
    for path in ident.ELEVATED_COMMANDS:
        assert ident.check_command_role(admin, path) is not None


def test_ut09_66_is_elevated_reports_a_bool() -> None:
    """UT09-66 the elevation probe runs on this host and returns a bool."""
    assert isinstance(ident._is_elevated(), bool)


def test_ut09_66_init_bootstrap_and_unknown_path(cli_env: CliEnv) -> None:
    """UT09-66 `init` without herness.yaml needs no config (OI-05); unknown paths need admin."""
    opts = GlobalOptions(config_dir=cli_env.config_dir)
    assert ident.check_command_role(opts, "init") is None
    opts = _opts(cli_env, "viewer")
    with pytest.raises(e.PermissionDenied, match="admin role"):
        ident.check_command_role(opts, "init")
    with pytest.raises(e.PermissionDenied, match="You need the admin role to run herness nope"):
        ident.check_command_role(opts, "nope")


def test_ut09_66_cli_actor_key_handling(cli_env: CliEnv, fake_keyring: Any) -> None:
    """UT09-66 missing user_ref key: `unkeyed` when not needed, ConfigError when needed."""
    cfg = _opts(cli_env, "viewer").config()
    del fake_keyring.store[("herness", "ui_user_ref_key")]
    assert ident.cli_actor(cfg, need_ref=False).user_ref == "unkeyed"
    with pytest.raises(e.ConfigError):
        ident.cli_actor(cfg, need_ref=True)


def test_ut09_66_guarded_passes_actor(cli_env: CliEnv) -> None:
    """UT09-66 `guarded` runs the role check, passes `actor`, hides it from the Typer signature."""
    opts = _opts(cli_env, "viewer")
    seen: list[object] = []
    test_app = typer.Typer()

    @test_app.command("status")
    @ident.guarded("status")
    def status(actor: object, inline: bool = False) -> int:
        seen.append(actor)
        return 0

    @test_app.command("build")
    @ident.guarded("build")
    def build(actor: object) -> int:
        seen.append(actor)
        return 0

    @test_app.callback()
    def root() -> None:
        """Test root."""

    assert test_app(args=["status"], standalone_mode=False, obj=opts) == 0
    assert seen[0] is not None
    assert opts.command == "status"
    with pytest.raises(e.PermissionDenied):
        test_app(args=["status", "--inline"], standalone_mode=False, obj=opts)
    with pytest.raises(e.PermissionDenied):
        test_app(args=["build"], standalone_mode=False, obj=opts)
    assert len(seen) == 1
