"""The ``herness eval`` command (impl 09 U09-98; impl 11 U11-68, U11-69; R-43, R-46).

Private sibling of ``cmd_admin`` (§2 module map, line budget). ``--compare-runs`` compares
stored runs in-process (``herness.eval.report.compare``) and enqueues nothing. Otherwise the
command binds the eval deps factory (``_eval_deps.bind``), builds the payload with
``build_eval_payload`` (spec 11 §3.2 options in snake_case, unset options omitted) and
submits one ``eval`` job with ``eval_gpu_class`` and ``eval_idem_key`` (priority None, the
impl 08 per-kind default), then follows it, runs it here (``--inline``, admin) or returns at
once (``--no-wait``). Exit codes come from ``exit_code``: 0 passed, 14 an eval gate failed,
3 ``ConfigError``, 1 any other failure.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final

import typer

from herness._cli._job_cmds import Inline, Wait, current_opts, job_error, start_job, wait_options
from herness._cli.identity import guarded
from herness._cli.output import CliResult, emit, emit_job_error, table_printer
from herness._cli.term import safe_terminal_text
from herness._cli.wait import submit_job
from herness.core.errors import ConfigError, HernessError
from herness.reports.rules import Actor, UserInputError  # Actor: Typer reads the hints

if TYPE_CHECKING:
    from herness._cli.wait import FollowOutcome
    from herness.cli import GlobalOptions
    from herness.core.jobs import JobRow

__all__ = ["eval_command"]

_Opt = typer.Option
_NAMED_SUITES: Final = ("golden", "classifier")
_DEPTHS: Final = ("fast", "standard", "deep")
_FAILED_FIX: Final = "See `herness jobs list` and the worker log for the job."


def _split(text: str | None) -> list[str] | None:
    """``"G01, F01"`` -> ``["G01", "F01"]``; None when the option was not given."""
    return None if text is None else [part.strip() for part in text.split(",") if part.strip()]


def _path(text: str | None) -> str | None:
    """A file option as an absolute path: the worker may run in another directory."""
    return None if text is None else str(Path(text).resolve())


def _compare_runs(opts: GlobalOptions, run_ids: list[str]) -> int:
    """U09-98: compare stored eval runs here; no job (``comparison.json`` in the newest)."""
    from herness.eval.report import compare  # noqa: PLC0415 - lazy (BT09-07)
    from herness.eval.runner import exit_code  # noqa: PLC0415

    table = compare(run_ids, data_root=Path(opts.config().paths.data))
    passed = not table.harness_defects
    data: dict[str, object] = {
        "run_id": table.newest_run_id,
        "run_ids": list(table.run_ids),
        "harness_defects": list(table.harness_defects),
        "passed": passed,
    }
    code = exit_code({"passed": passed})
    return emit(opts, CliResult(opts.command, data, [], table_printer(data), code))


def _job_code(job: JobRow) -> int:
    """U11-69 on the job row: a done job's result, else its error class (never stale)."""
    from herness.eval.runner import exit_code  # noqa: PLC0415 - lazy (BT09-07)

    if job.status == "done":
        return exit_code(job.result or {})
    return exit_code({"status": "failed", "last_error": job.last_error or {}})


def _finish(opts: GlobalOptions, outcome: FollowOutcome) -> int:
    job = outcome.job
    result = job.result or {}
    code = outcome.exit_code if outcome.detached else _job_code(job)
    data: dict[str, object] = {
        "job_id": job.job_id,
        "status": job.status,
        "run_id": result.get("run_id"),
        "passed": result.get("passed"),
    }
    if "compare_run_id" in result:
        data["compare_run_id"] = result["compare_run_id"]
    cli = CliResult(opts.command, data, list(outcome.warnings), table_printer(data), code)
    failed = job_error(opts, dataclasses.replace(outcome, exit_code=code), cli)
    return emit(opts, cli) if failed is None else failed


def _inline_failed(opts: GlobalOptions, job_id: str, exc: HernessError) -> int:
    """An inline handler error other than ``ConfigError`` (the job row is failed): exit 1."""
    from herness.reports.rules import user_message  # noqa: PLC0415 - lazy (BT09-07)

    what, fix = user_message(exc)
    if not opts.json:
        sys.stderr.write(f"Error: {safe_terminal_text(what)}\nFix: {safe_terminal_text(fix)}\n")
    return emit_job_error(opts, type(exc).__name__, 1, message=what, hint=fix,
                          details={"job_id": job_id})  # fmt: skip


@guarded("eval")
def eval_command(  # noqa: PLR0913 - one parameter per spec 11 §3.2 option
    *,
    suite: Annotated[str | None, _Opt("--suite", help="golden, classifier or PATH.yaml.")] = None,
    profile: Annotated[str | None, _Opt("--profile", help="Eval profile.")] = None,
    compare: Annotated[str | None, _Opt("--compare", help="Second profile to compare.")] = None,
    depth: Annotated[str | None, _Opt("--depth", help="fast, standard or deep.")] = None,
    ids: Annotated[str | None, _Opt("--ids", help="Question ids, e.g. G01,F01.")] = None,
    tags: Annotated[str | None, _Opt("--tags", help="Question tags (comma list).")] = None,
    repeat: Annotated[int | None, _Opt("--repeat", help="Runs per question.")] = None,
    mock_llm: Annotated[str | None, _Opt("--mock-llm", help="Script directory.")] = None,
    no_judge: Annotated[bool, _Opt("--no-judge", help="Skip rubric judging.")] = False,
    resume: Annotated[str | None, _Opt("--resume", help="Eval run id to resume.")] = None,
    baseline: Annotated[str | None, _Opt("--baseline", help="Baseline name.")] = None,
    set_baseline: Annotated[str | None, _Opt("--set-baseline", help="Save as baseline.")] = None,
    compare_runs: Annotated[str | None, _Opt("--compare-runs", help="RUN_ID,RUN_ID,...")] = None,
    wait: Wait = True,
    inline: Inline = False,
    actor: Actor,
) -> int:
    """Run the golden or classifier eval, or compare stored eval runs (spec 11)."""
    from herness._cli import _eval_deps  # noqa: PLC0415 - lazy (BT09-07)
    from herness.eval.runner import (  # noqa: PLC0415
        build_eval_payload,
        eval_gpu_class,
        eval_idem_key,
    )

    opts = current_opts()
    wait_options(wait=wait, inline=inline)
    if depth is not None and depth not in _DEPTHS:  # before the eval.yaml repeat lookup
        msg = "--depth takes fast, standard or deep"
        raise UserInputError(msg, hint="Run `herness eval --help`.")
    cfg = opts.config()
    if compare_runs is not None:
        return _compare_runs(opts, _split(compare_runs) or [])
    _eval_deps.bind(opts.config_dir)
    payload = build_eval_payload(
        suite=suite if suite is None or suite in _NAMED_SUITES else _path(suite),
        profile=profile or cfg.profile, compare_profile=compare, depth=depth, ids=_split(ids),
        tags=_split(tags), repeat=repeat, mock_llm_dir=_path(mock_llm), no_judge=no_judge or None,
        resume_run_id=resume, baseline=baseline, set_baseline=set_baseline, settings=cfg.eval,
    )  # fmt: skip
    job_id = submit_job(opts, kind="eval", payload=payload, gpu_class=eval_gpu_class(payload),
                        priority=None, idem_key=eval_idem_key(payload), inline=inline)  # fmt: skip
    try:
        outcome = start_job(opts, actor, job_id, wait=wait, inline=inline)
    except ConfigError:
        raise  # exit 3 (R-46)
    except HernessError as exc:
        return _inline_failed(opts, job_id, exc)
    if isinstance(outcome, CliResult):  # --no-wait
        return emit(opts, outcome)
    return _finish(opts, outcome)
