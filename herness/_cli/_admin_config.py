"""``config`` and ``secrets`` commands (impl 09 U09-98; impl 10 U10-65 to U10-71; TH09-20).

Private sibling of ``cmd_admin`` (§2 module map, line budget), which registers both groups.
``config validate`` adds the R-71 owner validators (U09-106) to impl 10's checks unless
``--offline`` or the load failed. Secret values come only from hidden prompts and reach the
operator only through a stderr write that bypasses logging; ``secrets set`` takes no value
argument, and ``--json`` is refused for the interactive commands. ``secrets init`` audits as
``system`` until it has created the user-ref key (impl 10 audit actors, U10-60).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Final, cast

import typer

from herness._cli._job_cmds import current_opts
from herness._cli.identity import guarded
from herness._cli.output import CommandResult, emit, markup
from herness.reports.rules import Actor, UserInputError  # Actor: Typer reads the hints

if TYPE_CHECKING:
    from rich.console import Console

    from herness.cli import GlobalOptions
    from herness.core.config_sources import ProfileName

__all__ = ["COMMANDS"]

_Opt = typer.Option
_Arg = typer.Argument
_UNKEYED: Final = "unkeyed"
Profile = Annotated[str | None, _Opt("--profile", help="Profile (spec 10).")]


def _emit(result: CommandResult) -> int:
    """U09-105: the owner's ``CommandResult`` as this command's ``CliResult``."""
    opts = current_opts()
    return emit(opts, result.to_cli_result(opts.command))


# --- config --------------------------------------------------------------------------------------


def _lines_printer(lines: list[str]) -> Callable[[Console], None]:
    def show(console: Console) -> None:
        for line in lines:
            console.print(markup(line))

    return show


def _validate_printer(data: dict[str, object]) -> Callable[[Console], None]:
    """``severity path file: message`` lines, then the resolved profile and config hash."""
    issues = cast("list[str]", data.get("issues") or [])
    tail = [f"profile: {data.get('profile')}", f"config_hash: {data.get('config_hash')}"]
    return _lines_printer([*issues, *tail])


def _with_startup_issues(opts: GlobalOptions, result: CommandResult, profile: str | None, *,
                         strict: bool) -> CommandResult:  # fmt: skip
    """U09-98: append the R-71 owner validators' issues (U09-106) to a loadable config's."""
    from herness import cli  # noqa: PLC0415 - lazy: the root module is loaded already

    if profile is not None:
        opts.profile = profile  # the loaded config is the one validated
    cfg = opts.config(startup_validation=False)
    issues = cli.run_startup_validation(cfg, raise_on_error=False, config_dir=opts.config_dir)
    severities = {issue.severity for issue in issues}
    code = 1 if "error" in severities or (strict and "warn" in severities) else result.exit_code
    data = dict(result.data or {})
    data["issues"] = [*cast("list[str]", data.get("issues") or []), *map(str, issues)]
    return CommandResult(ok=code == 0, data=data, warnings=result.warnings, exit_code=code)


@guarded("config validate")
def config_validate(
    *, profile: Profile = None,
    offline: Annotated[bool, _Opt("--offline", help="No network checks.")] = False,
    strict: Annotated[bool, _Opt("--strict", help="Warnings fail too.")] = False,
    actor: Actor | None,
) -> int:  # fmt: skip
    """Validate the config tree (spec 10); allowed for every user."""
    from herness.admin.commands_config import cmd_config_validate  # noqa: PLC0415 - lazy

    del actor
    opts = current_opts()
    result = cmd_config_validate(config_dir=opts.config_dir, offline=offline, strict=strict,
                                 profile=cast("ProfileName | None", profile))  # fmt: skip
    if not offline and (result.data or {}).get("config_hash") is not None:
        result = _with_startup_issues(opts, result, profile, strict=strict)
    cli_result = result.to_cli_result(opts.command)
    cli_result.human = _validate_printer(dict(result.data or {}))
    return emit(opts, cli_result)


@guarded("config show")
def config_show(*, profile: Profile = None, actor: Actor) -> int:
    """Print the effective config; secrets stay ``secret:<name>`` (spec 10)."""
    import yaml  # noqa: PLC0415 - lazy (BT09-07)

    from herness.admin.commands_config import cmd_config_show  # noqa: PLC0415

    del actor
    opts = current_opts()
    result = cmd_config_show(config_dir=opts.config_dir,
                             profile=cast("ProfileName | None", profile))  # fmt: skip
    cli_result = result.to_cli_result(opts.command)
    lines = yaml.safe_dump(result.data or {}, sort_keys=True, allow_unicode=True).splitlines()
    cli_result.human = _lines_printer(lines)  # per line: never cut at the 20,000-char cap
    return emit(opts, cli_result)


@guarded("config hash")
def config_hash(*, profile: Profile = None, actor: Actor) -> int:
    """Print the config hash (spec 10)."""
    from herness.admin.commands_config import cmd_config_hash  # noqa: PLC0415 - lazy

    del actor
    opts = current_opts()
    return _emit(cmd_config_hash(config_dir=opts.config_dir,
                                 profile=cast("ProfileName | None", profile)))  # fmt: skip


# --- secrets -------------------------------------------------------------------------------------


def _prompt(text: str) -> str:
    """Hidden console input (TH09-20): the value is never echoed, logged or an argument."""
    return str(typer.prompt(text, hide_input=True))


def _show(text: str) -> None:
    """Show a generated value once on stderr, bypassing logging (U10-68)."""
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _interactive(command: str) -> None:
    if current_opts().json:
        msg = f"secrets {command} is interactive and does not support --json"
        raise UserInputError(msg, hint=f"Run `herness secrets {command}` without --json.")


def _audit_actor(actor: Actor) -> str:
    """``system`` before ``secrets init`` created the user-ref key (audit actors, U10-60)."""
    return "system" if actor.user_ref == _UNKEYED else actor.user_ref


@guarded("secrets init")
def secrets_init(*, actor: Actor) -> int:
    """Create the redaction and user-ref keys, escrow-confirmed (spec 10)."""
    from herness.admin.commands_secrets import cmd_secrets_init  # noqa: PLC0415 - lazy

    _interactive("init")
    return _emit(cmd_secrets_init(actor=_audit_actor(actor), prompt=_prompt, show=_show))


@guarded("secrets set")
def secrets_set(name: Annotated[str, _Arg(help="Secret name.")], *, actor: Actor) -> int:
    """Store a secret; the value is read from two hidden prompts, never an argument."""
    from herness.admin.commands_secrets import cmd_secrets_set  # noqa: PLC0415 - lazy

    _interactive("set")
    return _emit(cmd_secrets_set(name, actor=actor.user_ref, prompt=_prompt))


@guarded("secrets status")
def secrets_status(*, actor: Actor) -> int:
    """Secret names, presence and last-set time; never values (spec 10)."""
    from herness.admin.commands_secrets import cmd_secrets_status  # noqa: PLC0415 - lazy

    del actor
    return _emit(cmd_secrets_status(cfg=current_opts().config()))


@guarded("secrets rekey")
def secrets_rekey(*, actor: Actor) -> int:
    """Stage a new redaction key and plan the rekey job (spec 10)."""
    from herness.admin.commands_secrets import cmd_secrets_rekey  # noqa: PLC0415 - lazy

    _interactive("rekey")
    return _emit(cmd_secrets_rekey(actor=actor.user_ref, prompt=_prompt, show=_show))


COMMANDS: Final[dict[str, tuple[tuple[str, Callable[..., int]], ...]]] = {
    "config": (("validate", config_validate), ("show", config_show), ("hash", config_hash)),
    "secrets": (("init", secrets_init), ("set", secrets_set), ("status", secrets_status),
                ("rekey", secrets_rekey)),
}  # fmt: skip
