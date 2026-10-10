"""Review and report commands: ``report funding|org|render``, ``review``, ``resume``,
``decide`` (impl 09 U09-96).

Every write goes through ``herness.reports.actions`` with the CLI actor (resume, render,
decide); review jobs are enqueued through ``submit_job`` (U09-90). ``RUN_ID`` and ``REC_ID``
are checked before any path or store use (TH09-18). The renderer never calls the optimizer:
custom budgets reach spec 06 as ``RunRequest.scenarios`` (design 09 §5.2 row 4).
"""

from __future__ import annotations

import datetime
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal, cast

import typer
from pydantic import ValidationError

from herness._cli import _job_cmds as jc
from herness._cli.identity import guarded
from herness._cli.output import CliResult, emit, markup
from herness._cli.wait import submit_job
from herness.core.errors import NotFound, ReportContractError
from herness.reports.rules import REC_ID_RE, Actor, UserInputError, check_id

if TYPE_CHECKING:
    from rich.console import Console

    from herness._cli.wait import FollowOutcome
    from herness.cli import GlobalOptions
    from herness.core.types import ReportManifest
    from herness.reports.settings import ReportsSection

__all__ = ["register"]

_Opt = typer.Option
_Arg = typer.Argument
_Kind = Literal["funding_review", "org_review"]
_FORMATS: Final = ("html", "md", "pdf")
_DECISIONS: Final = ("accepted", "rejected", "deferred")
_KINDS: Final[dict[str, tuple[_Kind, ...]]] = {
    "funding": ("funding_review",), "org": ("org_review",),
    "both": ("funding_review", "org_review"),
}  # fmt: skip
_RENDER_ONLY: Final = "Render options apply only with --wait; use `herness report render <run_id>`."
_PARTIAL: Final = "Run `{run_id}` is partial; the report carries the partial banner."
DepthOpt = Annotated[str, _Opt("--depth", help="Review depth: fast, standard or deep.")]
Fmt = Annotated[str | None, _Opt("--format", help="Comma list of html, md, pdf.")]
Out = Annotated[Path | None, _Opt("--out", help="Output directory.")]
NoStrict = Annotated[bool, _Opt("--no-strict", help="Mark uncited numbers, do not refuse.")]


def _run_id_checked(run_id: str) -> str:
    from herness.reports.contract import RUN_ID_RE  # noqa: PLC0415 - lazy (BT09-07)

    return check_id(RUN_ID_RE, run_id, "run")


def _formats(text: str | None) -> list[str] | None:
    """``--format html,md`` -> ``["html", "md"]``; unknown or repeated names are usage errors."""
    if text is None:
        return None
    names = [name.strip() for name in text.split(",")]
    if not names or any(n not in _FORMATS for n in names) or len(set(names)) != len(names):
        msg = "--format takes a comma list of html, md and pdf"
        raise UserInputError(msg, hint="For example `--format html,md`.")
    return names


@dataclass(frozen=True)
class _Render:
    """The render options of ``report funding|org`` (U09-96 step 4)."""

    top_n: int | None
    formats: list[str] | None
    out: Path | None
    no_strict: bool
    open_: bool

    @property
    def given(self) -> bool:
        return any((self.top_n is not None, self.formats is not None, self.out is not None,
                    self.no_strict, self.open_))  # fmt: skip

    def differs(self, settings: ReportsSection) -> bool:
        """True when ``--top-n``, ``--format``, ``--out`` or ``--no-strict`` leave a default."""
        return any((self.top_n not in (None, settings.top_n),
                    self.formats not in (None, list(settings.formats)),
                    self.out is not None, self.no_strict and settings.strict_numbers))  # fmt: skip


def _paths_printer(run_id: str, paths: Sequence[Path]) -> Callable[[Console], None]:
    def show(console: Console) -> None:
        console.print(f"Report for run {markup(run_id)}:")
        for path in paths:
            console.print(f"  {markup(str(path))}")

    return show


def _report_result(opts: GlobalOptions, manifest: ReportManifest, out: Path | None,
                   extra: dict[str, object], *, open_: bool = False) -> CliResult:  # fmt: skip
    """Data = manifest fields plus ``extra``; prints the file paths; ``--open`` the HTML."""
    folder = Path(opts.config().paths.data) / "reports" / manifest.run_id if out is None else out
    paths = [folder / name for name in sorted(manifest.files)]
    if open_ and "report.html" in manifest.files:
        webbrowser.open((folder / "report.html").resolve().as_uri())
    data = cast("dict[str, object]", manifest.model_dump(mode="json")) | extra
    return CliResult(opts.command, data, [], _paths_printer(manifest.run_id, paths))


def _stored_manifest(opts: GlobalOptions, run_id: str) -> ReportManifest:
    from herness.core.types import ReportManifest  # noqa: PLC0415 - lazy (BT09-07)

    path = Path(opts.config().paths.data) / "reports" / run_id / "manifest.json"
    try:
        return ReportManifest.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        msg = f"report {run_id} not found"
        raise NotFound(msg, details={"code": "report_not_found", "run_id": run_id}) from None
    except ValidationError:
        msg = "report manifest is invalid"
        raise ReportContractError(msg, details={"code": "manifest_invalid"}) from None


def _after_review(opts: GlobalOptions, actor: Actor, outcome: FollowOutcome,
                  render: _Render) -> int:  # fmt: skip
    """U09-96 steps 4-6: the run's manifest (re-rendered when an option differs)."""
    from herness.reports.actions import rerender_report  # noqa: PLC0415 - lazy (BT09-07)

    run_id = outcome.run_id
    if outcome.job.status != "done" or outcome.detached or run_id is None:
        return jc.finish_job(opts, outcome)
    _run_id_checked(run_id)
    if render.differs(opts.config().app.reports):
        manifest = rerender_report(actor, run_id=run_id, formats=render.formats,
                                   out_dir=render.out, strict=not render.no_strict,
                                   top_n=render.top_n)  # fmt: skip
    else:
        manifest = _stored_manifest(opts, run_id)
    extra: dict[str, object] = {"job_id": outcome.job.job_id, "partial": outcome.partial}
    result = _report_result(opts, manifest, render.out, extra, open_=render.open_)
    result.warnings, result.exit_code = list(outcome.warnings), outcome.exit_code
    return emit(opts, result)


def _review_report(  # noqa: PLR0913 - the U09-96 option set
    kind: _Kind, actor: Actor, depth: str, budget: list[str] | None, render: _Render, *,
    wait: bool, inline: bool,
) -> int:  # fmt: skip
    from herness._cli.payloads import parse_budget_usd, review_request  # noqa: PLC0415

    opts = jc.current_opts()
    jc.wait_options(wait=wait, inline=inline)
    payload = review_request(kind, depth, [parse_budget_usd(b) for b in budget or ()])
    job_id = submit_job(opts, kind="review", payload=payload, gpu_class="reasoning",
                        inline=inline)  # fmt: skip
    outcome = jc.start_job(opts, actor, job_id, wait=wait, inline=inline)
    if isinstance(outcome, CliResult):  # --no-wait
        outcome.warnings += [_RENDER_ONLY] if render.given else []
        return emit(opts, outcome)
    return _after_review(opts, actor, outcome, render)


def _report_command(kind: _Kind, path: str) -> Callable[..., int]:
    @guarded(path)
    def command(  # noqa: PLR0913 - one parameter per U09-96 option
        *, depth: DepthOpt = "standard",
        budget: Annotated[list[str] | None, _Opt("--budget", help="USD budget (repeatable).")]
        = None,
        top_n: Annotated[int | None, _Opt("--top-n", min=1, max=100, help="Top N rows.")] = None,
        fmt: Fmt = None, out: Out = None, no_strict: NoStrict = False,
        open_: Annotated[bool, _Opt("--open", help="Open the HTML report.")] = False,
        wait: jc.Wait = True, inline: jc.Inline = False, actor: Actor,
    ) -> int:  # fmt: skip
        render = _Render(top_n, _formats(fmt), out, no_strict, open_)
        return _review_report(kind, actor, depth, budget, render, wait=wait, inline=inline)

    command.__doc__ = f"Run a {kind.replace('_', ' ')} and write its report (spec 06, 09)."
    return command


@guarded("report render")
def report_render(
    run_id: Annotated[str, _Arg(help="Run id.")], *, fmt: Fmt = None, out: Out = None,
    no_strict: NoStrict = False, actor: Actor,
) -> int:  # fmt: skip
    """Render (again) the report of a finished run."""
    from herness.reports.actions import rerender_report  # noqa: PLC0415 - lazy (BT09-07)
    from herness.store import ops  # noqa: PLC0415

    opts = jc.current_opts()
    _run_id_checked(run_id)
    manifest = rerender_report(actor, run_id=run_id, formats=_formats(fmt), out_dir=out,
                               strict=not no_strict)  # fmt: skip
    result = _report_result(opts, manifest, out, {})
    run = ops.get_run(run_id)
    if run is not None and run.status == "partial":
        result.warnings, result.exit_code = [_PARTIAL.format(run_id=run_id)], 6
    return emit(opts, result)


def _at(text: str | None) -> datetime.datetime | None:
    if text is None:
        return None
    try:
        at = datetime.datetime.fromisoformat(text)
    except ValueError:
        at = None
    if at is None or at.utcoffset() is None:
        msg = "--at must be an ISO-8601 time with an offset"
        raise UserInputError(msg, hint="For example `2026-10-01T06:00:00+00:00`.")
    return at


@guarded("review")
def review(
    *, kind: Annotated[str, _Opt("--kind", help="funding, org or both.")] = "both",
    depth: DepthOpt = "standard",
    at: Annotated[str | None, _Opt("--at", help="Start time, ISO-8601 with offset.")] = None,
    wait: jc.Wait = True, inline: jc.Inline = False, actor: Actor,
) -> int:  # fmt: skip
    """Enqueue review jobs (spec 06, 08); waits for them in order unless --no-wait."""
    from herness._cli.payloads import review_request  # noqa: PLC0415 - lazy (BT09-07)

    opts = jc.current_opts()
    if kind not in _KINDS:
        msg = "--kind takes funding, org or both"
        raise UserInputError(msg, hint="Run `herness review --help`.")
    jc.wait_options(wait=wait, inline=inline)
    if at is not None and inline:
        msg = "--at and --inline cannot be used together"
        raise UserInputError(msg, hint="Drop one of the two options.")
    when = _at(at)
    ids = [submit_job(opts, kind="review", payload=review_request(k, depth, []),
                      gpu_class="reasoning", scheduled_for=when, inline=inline)
           for k in _KINDS[kind]]  # fmt: skip
    rows: list[dict[str, object]] = []
    warnings: list[str] = []
    first: FollowOutcome | None = None  # the first job that did not end plain done
    for job_id in ids:
        outcome = jc.start_job(opts, actor, job_id, wait=wait, inline=inline)
        if isinstance(outcome, CliResult):
            rows.append(outcome.data or {})
            continue
        job = outcome.job
        rows.append({"job_id": job.job_id, "status": job.status, "run_id": outcome.run_id,
                     "partial": outcome.partial})  # fmt: skip
        warnings += outcome.warnings
        first = first or (outcome if outcome.exit_code else None)
    code = 0 if first is None else first.exit_code
    result = CliResult(opts.command, {"jobs": rows}, warnings, _jobs_printer(rows), code)
    failed = None if first is None else jc.job_error(opts, first, result)
    return emit(opts, result) if failed is None else failed


def _jobs_printer(rows: list[dict[str, object]]) -> Callable[[Console], None]:
    def show(console: Console) -> None:
        for row in rows:
            console.print(" ".join(f"{k}={markup(str(v))}" for k, v in row.items()))

    return show


@guarded("resume")
def resume(
    run_id: Annotated[str, _Arg(help="Run id.")], *,
    retry_dead: Annotated[bool, _Opt("--retry-dead", help="Retry dead tasks.")] = False,
    force: Annotated[bool, _Opt("--force", help="Resume a finished run.")] = False,
    wait: jc.Wait = True, inline: jc.Inline = False, actor: Actor,
) -> int:  # fmt: skip
    """Resume an unfinished run (spec 08)."""
    from herness.reports.actions import resume_run  # noqa: PLC0415 - lazy (BT09-07)

    opts = jc.current_opts()
    jc.wait_options(wait=wait, inline=inline)
    _run_id_checked(run_id)
    job_id = resume_run(actor, run_id=run_id, retry_dead=retry_dead, force=force)
    return jc.finish_job(opts, jc.start_job(opts, actor, job_id, wait=wait, inline=inline))


def _effective(text: str | None) -> datetime.date | None:
    try:
        return None if text is None else datetime.date.fromisoformat(text)
    except ValueError:
        msg = "--effective-at must be a date YYYY-MM-DD"
        raise UserInputError(msg, hint="For example `2026-10-01`.") from None


@guarded("decide")
def decide(
    rec_id: Annotated[str, _Arg(help="Recommendation id.")],
    decision: Annotated[str, _Arg(help="accepted, rejected or deferred.")], *,
    reason: Annotated[str, _Opt("--reason", help="Why (10 to 1000 characters).")],
    effective_at: Annotated[str | None, _Opt("--effective-at", help="YYYY-MM-DD.")] = None,
    actor: Actor,
) -> int:  # fmt: skip
    """Record a decision on a recommendation (spec 07)."""
    from herness.harness import memory  # noqa: PLC0415 - lazy (BT09-07)
    from herness.reports.actions import decide_recommendation  # noqa: PLC0415

    opts = jc.current_opts()
    check_id(REC_ID_RE, rec_id, "recommendation")
    if decision not in _DECISIONS:
        msg = "decision must be accepted, rejected or deferred"
        raise UserInputError(msg, hint="Run `herness decide --help`.")
    when = _effective(effective_at)
    choice = cast("Literal['accepted', 'rejected', 'deferred']", decision)
    decide_recommendation(actor, rec_id=rec_id, decision=choice, reason=reason,
                          effective_at=when, memory=memory.get_memory_store())  # fmt: skip
    data: dict[str, object] = {"rec_id": rec_id, "decision": decision, "effective_at": when}
    return emit(opts, CliResult(opts.command, data, [], _jobs_printer([data])))


def register(root: typer.Typer) -> None:
    """Register the review and report commands on the root app (U09-84 step 4)."""
    report = typer.Typer(name="report", no_args_is_help=True, help="Review reports.")
    report.command("funding")(_report_command("funding_review", "report funding"))
    report.command("org")(_report_command("org_review", "report org"))
    report.command("render")(report_render)
    root.add_typer(report, name="report")
    for name, handler in (("review", review), ("resume", resume), ("decide", decide)):
        root.command(name)(handler)
