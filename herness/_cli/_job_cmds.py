"""Enqueue-and-follow plumbing shared by the job commands of ``cmd_data`` and ``cmd_review``
(impl 09 U09-95, U09-96; R-45): the ``--wait/--no-wait`` and ``--inline`` options, the
``--inline``/``--no-wait`` usage check and the job result printing (U09-86, U09-91).

Private sibling of ``cmd_data`` (budget, T09-23).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final, cast

import typer
from typer._click.globals import get_current_context

from herness._cli.output import CliResult, emit, emit_job_error, table_printer
from herness._cli.wait import follow_job, run_job_inline
from herness.core import jobs
from herness.reports.rules import UserInputError

if TYPE_CHECKING:
    from herness._cli.wait import FollowOutcome
    from herness.cli import GlobalOptions
    from herness.reports.rules import Actor

__all__ = [
    "Inline", "Wait", "current_opts", "finish_job", "job_error", "start_job", "wait_options",
]  # fmt: skip

_Opt = typer.Option
Wait = Annotated[bool, _Opt("--wait/--no-wait", help="Follow the job until it ends.")]
Inline = Annotated[bool, _Opt("--inline", help="Run the job in this process (admin).")]
_FAILED_FIX: Final = "See `herness jobs list` and the worker log for the job."
_CANCELED: Final = "Job `{job_id}` was canceled."


def current_opts() -> GlobalOptions:
    """The ``GlobalOptions`` of the running command (``ctx.obj`` of the root context)."""
    return cast("GlobalOptions", get_current_context().find_root().obj)


def wait_options(*, wait: bool, inline: bool) -> None:
    """``--inline`` runs the job here, so it cannot be combined with ``--no-wait`` (R-45)."""
    if inline and not wait:
        msg = "--inline and --no-wait cannot be used together"
        raise UserInputError(msg, hint="Drop one of the two options.")


def _queued(opts: GlobalOptions, job_id: str) -> CliResult:
    data: dict[str, object] = {"job_id": job_id, "status": jobs.get(job_id).status}
    return CliResult(opts.command, data, [], table_printer(data))


def start_job(
    opts: GlobalOptions, actor: Actor, job_id: str, *, wait: bool, inline: bool
) -> FollowOutcome | CliResult:
    """``--no-wait`` -> the queued result; ``--inline`` -> run here; else follow (U09-95)."""
    if inline:
        return run_job_inline(opts, actor, job_id)
    if not wait:
        return _queued(opts, job_id)
    return follow_job(opts, job_id, poll_s=opts.config().app.cli.poll_interval_s)


def job_error(opts: GlobalOptions, outcome: FollowOutcome, result: CliResult) -> int | None:
    """The error of a failed (``last_error``) or canceled job (U09-91), printed with
    ``result``'s data; None when the job did not fail or get canceled."""
    job = outcome.job
    if job.status == "failed":
        error = job.last_error or {}
        kind, text = error.get("class"), error.get("message")
    elif job.status == "canceled":
        kind, text = "JobStateError", _CANCELED.format(job_id=job.job_id)
    else:
        return None
    kind, text = kind if isinstance(kind, str) else "UnknownError", text or ""
    return emit_job_error(opts, kind, outcome.exit_code, message=str(text), hint=_FAILED_FIX,
                          details={"job_id": job.job_id}, result=result)  # fmt: skip


def finish_job(opts: GlobalOptions, outcome: FollowOutcome | CliResult) -> int:
    """Print a job command's result; a failed or canceled job carries its error (U09-91)."""
    if isinstance(outcome, CliResult):
        return emit(opts, outcome)
    job = outcome.job
    data: dict[str, object] = {"job_id": job.job_id, "status": job.status,
                               "run_id": outcome.run_id, "partial": outcome.partial}  # fmt: skip
    result = CliResult(opts.command, data, list(outcome.warnings), table_printer(data),
                       outcome.exit_code)  # fmt: skip
    code = job_error(opts, outcome, result)
    return emit(opts, result) if code is None else code
