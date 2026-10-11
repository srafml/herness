"""Admin commands: ``config``, ``secrets``, ``deploy``, ``eval``, ``distill``, ``laya``,
``privacy``, ``maintenance`` (impl 09 U09-98).

Thin wrappers (R-07): each command calls its owner's public function (impl 10
``herness.admin.commands_*``, impl 03 ``laya_admin``) with the parsed arguments and the CLI
actor after ``guarded`` checked the role and the elevated shell (U09-89), and renders the
returned ``CommandResult`` as a ``CliResult`` with the same data, warnings and exit code
(U09-105). Secret values come only from hidden prompts and reach the operator only through a
stderr write that bypasses logging; ``secrets set`` takes no value argument (TH09-20). The
``config`` and ``secrets`` commands live in the private sibling ``_admin_config``, ``eval`` in
``_eval_cmd``. Owner modules are imported inside the command bodies (BT09-07).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal, cast

import typer

from herness._cli import _admin_config, _eval_cmd
from herness._cli._job_cmds import Inline, Wait, current_opts, finish_job, start_job, wait_options
from herness._cli.identity import guarded
from herness._cli.output import CliResult, CommandResult, emit
from herness._cli.wait import run_job_inline, submit_job
from herness.reports.rules import Actor, UserInputError  # Actor: Typer reads the hints

if TYPE_CHECKING:
    from herness.admin.deploy import DeployClass

__all__ = ["register"]

_Opt = typer.Option
_Arg = typer.Argument
_CLASSES: Final = ("reasoning", "decider", "large")
_CLASS_HELP: Final = "reasoning, decider or large."
DryRun = Annotated[bool, _Opt("--dry-run", help="Report only; change nothing.")]


def _emit(result: CommandResult) -> int:
    """U09-105: the owner's ``CommandResult`` as this command's ``CliResult``."""
    opts = current_opts()
    return emit(opts, result.to_cli_result(opts.command))


# --- deploy --------------------------------------------------------------------------------------


def _deploy_class(text: str) -> DeployClass:
    if text not in _CLASSES:
        msg = "CLASS must be reasoning, decider or large"
        raise UserInputError(msg, hint="Run `herness deploy --help`.")
    return cast("DeployClass", text)


def _deploy(op: str, *args: object, actor: Actor, **kwargs: object) -> int:
    from herness.admin import commands_deploy  # noqa: PLC0415 - lazy (BT09-07)

    command = getattr(commands_deploy, f"cmd_deploy_{op}")
    return _emit(command(*args, cfg=current_opts().config(), actor=actor.user_ref, **kwargs))


ClassArg = Annotated[str, _Arg(help=_CLASS_HELP)]


@guarded("deploy render")
def deploy_render(*, actor: Actor) -> int:
    """Render the model-server compose environment (spec 10)."""
    return _deploy("render", actor=actor)


@guarded("deploy pull")
def deploy_pull(
    *, allow_download: Annotated[bool, _Opt("--allow-download", help="Download images.")] = False,
    actor: Actor,
) -> int:  # fmt: skip
    """Pull and verify the pinned model-server images (spec 10)."""
    return _deploy("pull", actor=actor, allow_download=allow_download)


@guarded("deploy up")
def deploy_up(gpu_class: ClassArg, *, actor: Actor) -> int:
    """Start one model-server class (spec 10)."""
    return _deploy("up", _deploy_class(gpu_class), actor=actor)


@guarded("deploy down")
def deploy_down(
    gpu_class: Annotated[str | None, _Arg(help=_CLASS_HELP + " Default: every class.")] = None,
    *, actor: Actor,
) -> int:  # fmt: skip
    """Stop one model-server class, or every class (spec 10)."""
    if gpu_class is not None:
        return _deploy("down", _deploy_class(gpu_class), actor=actor)
    from herness.admin.commands_deploy import cmd_deploy_down  # noqa: PLC0415 - lazy

    cfg = current_opts().config()
    data: dict[str, object] = {}
    warnings: list[str] = []
    for name in _CLASSES:
        result = cmd_deploy_down(_deploy_class(name), cfg=cfg, actor=actor.user_ref)
        data[name] = result.data
        warnings += result.warnings
    return _emit(CommandResult(ok=True, data={"classes": data}, warnings=warnings, exit_code=0))


@guarded("deploy rollback")
def deploy_rollback(gpu_class: ClassArg, *, actor: Actor) -> int:
    """Roll one model-server class back to its previous image (spec 10)."""
    return _deploy("rollback", _deploy_class(gpu_class), actor=actor)


@guarded("deploy prune")
def deploy_prune(*, actor: Actor) -> int:
    """Remove unpinned model-server images (spec 10)."""
    return _deploy("prune", actor=actor)


@guarded("deploy install")
def deploy_install(
    bundle_dir: Annotated[Path, _Arg(help="Release bundle directory (wheel, attestation, SBOM).",
                                     exists=True, file_okay=False, dir_okay=True)],
    *, actor: Actor,
) -> int:  # fmt: skip
    """Verify and install a release bundle (spec 10, R-58)."""
    return _deploy("install", bundle_dir, actor=actor)


# --- distill, laya -------------------------------------------------------------------------------


@guarded("distill")
def distill(
    *, active: Annotated[bool, _Opt("--active", help="Active-learning round.")] = False,
    wait: Wait = True, inline: Inline = False, actor: Actor,
) -> int:  # fmt: skip
    """Enqueue a distillation round (spec 03)."""
    opts = current_opts()
    wait_options(wait=wait, inline=inline)
    if inline:  # the job runs here: this process needs the owner's handler (U09-104)
        from herness._cli._worker_boot import cached_handlers  # noqa: PLC0415 - lazy
        from herness.core.jobs.handlers import register_handler  # noqa: PLC0415

        register_handler("distill", cached_handlers()["distill"])
    job_id = submit_job(opts, kind="distill", payload={"active": active}, gpu_class="decider",
                        inline=inline)  # fmt: skip
    return finish_job(opts, start_job(opts, actor, job_id, wait=wait, inline=inline))


def _laya(data: dict[str, object]) -> int:
    return _emit(CommandResult(ok=True, data=data, warnings=[], exit_code=0))


@guarded("laya status")
def laya_status(*, actor: Actor) -> int:
    """Every Laya version and the current one (spec 03)."""
    from herness.enrich import laya_admin  # noqa: PLC0415 - lazy (BT09-07)

    del actor
    current_opts().config()
    return _laya(laya_admin.laya_status())


VersionArg = Annotated[str, _Arg(help="Laya version (laya-YYYYMMDD-N).")]


@guarded("laya accept")
def laya_accept(version: VersionArg, *, actor: Actor) -> int:
    """Promote a Laya version after human review (spec 03)."""
    from herness.enrich import laya_admin  # noqa: PLC0415 - lazy (BT09-07)

    current_opts().config()
    laya_admin.accept_model(version, actor=actor.user_ref)
    return _laya({"version": version, "status": "accepted"})


@guarded("laya rollback")
def laya_rollback(version: VersionArg, *, actor: Actor) -> int:
    """Point CURRENT at an earlier accepted Laya version (spec 03)."""
    from herness.enrich import laya_admin  # noqa: PLC0415 - lazy (BT09-07)

    current_opts().config()
    laya_admin.rollback_model(version, actor=actor.user_ref)
    return _laya({"current": version})


# --- privacy, maintenance ------------------------------------------------------------------------


def _maintenance_jobs(actor: Actor, result: CommandResult, *, inline: bool) -> int:
    """Print the enqueued ``maintenance`` jobs; with ``--inline`` run each here, in order."""
    opts = current_opts()
    out: CliResult = result.to_cli_result(opts.command)
    data: dict[str, object] = out.data if out.data is not None else {}
    rows = cast("list[dict[str, object]]", data["jobs"]) if "jobs" in data else [data]
    for row in rows if inline else []:
        outcome = run_job_inline(opts, actor, str(row["job_id"]))
        row["status"] = outcome.job.status
        out.exit_code = out.exit_code or outcome.exit_code
    return emit(opts, out)


def _bind_maintenance(*, inline: bool) -> None:
    if inline:  # the job runs here: this process needs the owner's handler (U09-104)
        import herness.admin  # noqa: PLC0415 - lazy (BT09-07)
        from herness._cli._worker_boot import memory_purge  # noqa: PLC0415

        herness.admin.register_handlers(memory_purge=memory_purge)


@guarded("privacy delete")
def privacy_delete(
    *, record_id: Annotated[list[str], _Opt("--record-id", help="Record id (repeatable).")],
    reason_ref: Annotated[str, _Opt("--reason-ref", help="Privacy request reference.")],
    inline: Inline = False, actor: Actor,
) -> int:  # fmt: skip
    """Erase records everywhere: one maintenance job per record (spec 10)."""
    from herness.admin.commands_data import cmd_privacy_delete  # noqa: PLC0415 - lazy

    current_opts().config()
    _bind_maintenance(inline=inline)
    result = cmd_privacy_delete(record_id, reason_ref=reason_ref, actor=actor.user_ref)
    return _maintenance_jobs(actor, result, inline=inline)


def _maintenance(action: Literal["backup", "purge"], actor: Actor, *, dry_run: bool,
                 inline: bool) -> int:  # fmt: skip
    from herness.admin.commands_data import cmd_maintenance  # noqa: PLC0415 - lazy

    current_opts().config()
    _bind_maintenance(inline=inline)
    result = cmd_maintenance(action, dry_run=dry_run, actor=actor.user_ref)
    return _maintenance_jobs(actor, result, inline=inline)


@guarded("maintenance backup")
def maintenance_backup(*, dry_run: DryRun = False, inline: Inline = False, actor: Actor) -> int:
    """Back up the stores (spec 10)."""
    return _maintenance("backup", actor, dry_run=dry_run, inline=inline)


@guarded("maintenance purge")
def maintenance_purge(*, dry_run: DryRun = False, inline: Inline = False, actor: Actor) -> int:
    """Purge data past its retention (spec 10)."""
    return _maintenance("purge", actor, dry_run=dry_run, inline=inline)


def register(root: typer.Typer) -> None:
    """Register the admin groups and commands on the root app (U09-84 step 4)."""
    groups: tuple[tuple[str, str, tuple[tuple[str, Callable[..., int]], ...]], ...] = (
        ("config", "Configuration (spec 10).", _admin_config.COMMANDS["config"]),
        ("secrets", "Secrets (spec 10).", _admin_config.COMMANDS["secrets"]),
        ("deploy", "Model servers and releases (spec 10).",
         (("render", deploy_render), ("pull", deploy_pull), ("up", deploy_up),
          ("down", deploy_down), ("rollback", deploy_rollback), ("prune", deploy_prune),
          ("install", deploy_install))),
        ("laya", "Laya classifier versions (spec 03).",
         (("status", laya_status), ("accept", laya_accept), ("rollback", laya_rollback))),
        ("privacy", "Privacy requests (spec 10).", (("delete", privacy_delete),)),
        ("maintenance", "Backups and retention (spec 10).",
         (("backup", maintenance_backup), ("purge", maintenance_purge))),
    )  # fmt: skip
    for name, help_text, commands in groups:
        group = typer.Typer(name=name, no_args_is_help=True, help=help_text)
        for command, handler in commands:
            group.command(command)(handler)
        root.add_typer(group, name=name)
    root.command("eval")(_eval_cmd.eval_command)
    root.command("distill")(distill)
