"""Data commands: ``sync``, ``build``, ``enrich``, ``score``, ``metrics list``, ``pipeline``
(impl 09 U09-95); jobs are enqueued through ``submit_job`` and followed through ``_job_cmds``.

Bad option combinations raise UserInputError (exit 2) before anything is enqueued. L2-L4
modules are imported inside the command bodies so ``herness --help`` stays fast (BT09-07).
"""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer

from herness._cli._job_cmds import (
    Inline,
    Wait,
    current_opts,
    finish_job,
    start_job,
    wait_options,
)
from herness._cli.identity import guarded
from herness._cli.output import CliResult, emit, table_printer
from herness._cli.wait import submit_job
from herness.core import time as clock
from herness.core.errors import NotFound
from herness.reports.rules import Actor, UserInputError  # Actor: Typer reads the hints

if TYPE_CHECKING:
    from herness.cli import GlobalOptions

__all__ = ["register"]

_Opt = typer.Option
BuildId = Annotated[str | None, _Opt("--build-id", help="Build id.")]


def _pipeline(actor: Actor, stages: list[str], build_id: str | None, *, wait: bool,
              inline: bool, **extra: object) -> int:  # fmt: skip
    from herness._cli.payloads import pipeline_payload  # noqa: PLC0415 - lazy (BT09-07)

    opts = current_opts()
    wait_options(wait=wait, inline=inline)
    payload = pipeline_payload(stages, build_id, **extra)
    job_id = submit_job(opts, kind="build_pipeline", payload=payload, gpu_class="none",
                        inline=inline)  # fmt: skip
    return finish_job(opts, start_job(opts, actor, job_id, wait=wait, inline=inline))


# --- sync ----------------------------------------------------------------------------------------


def _sync_rules(source: str | None, entity: list[str], flags: dict[str, bool],
                dates: tuple[datetime.date | None, datetime.date | None]) -> None:  # fmt: skip
    """Impl 01 U01-54 option rules as usage errors (U09-95), checked before any call."""
    from_, to = dates
    rules = (
        (bool(entity) and source is None, "--entity requires SOURCE"),
        (flags["reconcile"] and source is None, "--reconcile requires SOURCE"),
        (flags["reconcile"] and (flags["full"] or flags["backfill"] or from_ or to),
         "--reconcile cannot be combined with --full, --backfill, --from or --to"),
        (flags["full"] and flags["backfill"], "--full and --backfill are exclusive"),
        (flags["backfill"] and from_ is None, "--backfill requires --from"),
        (not flags["backfill"] and (from_ or to), "--from and --to require --backfill"),
        (flags["check"] and source is None, "--check-mapping requires SOURCE"),
        (flags["discover"] and source != "jira", "--discover-fields requires SOURCE jira"),
    )  # fmt: skip
    for broken, msg in rules:
        if broken:
            raise UserInputError(msg, hint="Run `herness sync --help`.")


def _inspect(opts: GlobalOptions, source: str, *, check: bool) -> int:
    """``--check-mapping`` / ``--discover-fields``: in-process spec 01 calls, no job."""
    cfg = opts.config()
    if check:
        from herness.connectors import mapping_check  # noqa: PLC0415 - lazy (BT09-07)

        rows = mapping_check.check_mapping(source, cfg)
        key = "issues"
    else:
        from herness.connectors import factory  # noqa: PLC0415 - lazy (BT09-07)

        rows = factory.build_connector(source, cfg).discover_fields()  # type: ignore[attr-defined]
        key = "fields"
    data: dict[str, object] = {key: [dataclasses.asdict(row) for row in rows]}
    return emit(opts, CliResult(opts.command, data, [], table_printer(data)))


@guarded("sync")
def sync(  # noqa: PLR0913 - one parameter per U09-95 option
    source: Annotated[str | None, typer.Argument(help="Source name.")] = None,
    *,
    entity: Annotated[list[str] | None, _Opt("--entity", help="Entity (repeatable).")] = None,
    full: Annotated[bool, _Opt("--full", help="Backfill from backfill.start.")] = False,
    backfill: Annotated[bool, _Opt("--backfill", help="Backfill a date range.")] = False,
    from_: Annotated[datetime.datetime | None, _Opt("--from", formats=["%Y-%m-%d"])] = None,
    to: Annotated[datetime.datetime | None, _Opt("--to", formats=["%Y-%m-%d"])] = None,
    reconcile: Annotated[bool, _Opt("--reconcile", help="Reconcile deletions.")] = False,
    check_mapping: Annotated[bool, _Opt("--check-mapping", help="Check mappings.")] = False,
    discover_fields: Annotated[bool, _Opt("--discover-fields", help="Jira fields.")] = False,
    wait: Wait = True,
    inline: Inline = False,
    actor: Actor,
) -> int:
    """Sync one source or all sources (spec 01)."""
    from herness.connectors import jobs as connector_jobs  # noqa: PLC0415 - lazy (BT09-07)

    opts = current_opts()
    entities = entity or []
    flags = {"full": full, "backfill": backfill, "reconcile": reconcile,
             "check": check_mapping, "discover": discover_fields}  # fmt: skip
    start, end = (None if d is None else d.date() for d in (from_, to))
    _sync_rules(source, entities, flags, (start, end))
    wait_options(wait=wait, inline=inline)
    if (check_mapping or discover_fields) and source is not None:
        return _inspect(opts, source, check=check_mapping)
    kind, payload, idem_key = connector_jobs.build_sync_payload(
        source, entities=entities, full=full, backfill=backfill, from_=start, to=end,
        reconcile=reconcile, today=clock.now().date())  # fmt: skip
    job_id = submit_job(opts, kind=kind, payload=dict(payload), gpu_class="none",
                        idem_key=idem_key, inline=inline)  # fmt: skip
    return finish_job(opts, start_job(opts, actor, job_id, wait=wait, inline=inline))


# --- build, enrich, score, pipeline, metrics list ---------------------------------------------


@guarded("build")
def build(*, wait: Wait = True, inline: Inline = False, actor: Actor) -> int:
    """Build a new warehouse (spec 02)."""
    return _pipeline(actor, ["build"], None, wait=wait, inline=inline)


@guarded("enrich")
def enrich(
    *, build_id: BuildId = None,
    stage: Annotated[str | None, _Opt("--stage", help="Enrichment stage.")] = None,
    depth: Annotated[str | None, _Opt("--depth", help="Enrichment depth.")] = None,
    wait: Wait = True, inline: Inline = False, actor: Actor,
) -> int:  # fmt: skip
    """Enrich a build (spec 03, R-48)."""
    return _pipeline(actor, ["enrich"], build_id, wait=wait, inline=inline,
                     enrich_stage=stage, depth=depth)  # fmt: skip


def _scenario(opts: GlobalOptions, text: str) -> int:
    """``score --scenario``: the optimizer on the CURRENT build, never persisted; no job."""
    from herness._cli.payloads import parse_budget_usd  # noqa: PLC0415 - lazy (BT09-07)
    from herness.metrics import portfolio  # noqa: PLC0415
    from herness.store.warehouse import read_current  # noqa: PLC0415

    opts.config()
    scenario: portfolio.Scenario | str = text
    if text[:1].isdigit():
        usd = parse_budget_usd(text)
        scenario = portfolio.Scenario(name=f"custom_{int(usd)}", budget_usd=usd)
    if read_current() is None:
        msg = "no current build"
        raise NotFound(msg, details={"code": "no_current"})
    result = portfolio.optimize_portfolio(scenario, persist=False)
    data = cast("dict[str, object]", result.model_dump(mode="json"))
    shown = {k: data[k] for k in ("scenario", "solver_status", "selected", "total_effort_usd",
                                  "total_expected_impact_usd")}  # fmt: skip
    return emit(opts, CliResult(opts.command, data, [], table_printer(shown)))


@guarded("score")
def score(
    *, build_id: BuildId = None,
    step: Annotated[str | None, _Opt("--step", help="Scoring step.")] = None,
    scenario: Annotated[str | None, _Opt("--scenario", help="Scenario name or USD.")] = None,
    wait: Wait = True, inline: Inline = False, actor: Actor,
) -> int:  # fmt: skip
    """Score a build (spec 04), or optimise one scenario on the current build."""
    if scenario is None:
        steps = None if step is None else [step]
        return _pipeline(actor, ["score"], build_id, wait=wait, inline=inline, score_steps=steps)
    if step is not None or build_id is not None or inline or not wait:
        msg = "--scenario cannot be combined with --step, --build-id, --inline or --no-wait"
        raise UserInputError(msg, hint="Run `herness score --help`.")
    return _scenario(current_opts(), scenario)


@guarded("pipeline")
def pipeline(
    *, from_stage: Annotated[str, _Opt("--from-stage", help="First stage.")] = "build",
    build_id: BuildId = None, wait: Wait = True, inline: Inline = False, actor: Actor,
) -> int:  # fmt: skip
    """Run the pipeline from one stage (specs 02, 08)."""
    from herness._cli.payloads import stages_from  # noqa: PLC0415 - lazy (BT09-07)

    stages = stages_from(from_stage)
    if from_stage != "build" and build_id is None:
        msg = "--from-stage other than build requires --build-id"
        raise UserInputError(msg, hint="Pass the build id to continue.")
    return _pipeline(actor, stages, build_id, wait=wait, inline=inline)


@guarded("metrics list")
def metrics_list(*, actor: Actor) -> int:
    """List the metric catalog (spec 04)."""
    from herness.metrics.catalog import load_catalog  # noqa: PLC0415 - lazy (BT09-07)

    del actor
    opts = current_opts()
    opts.config()
    rows = load_catalog(Path(opts.config_dir) / "metrics.yaml").describe()
    data: dict[str, object] = {"metrics": rows}
    return emit(opts, CliResult(opts.command, data, [], table_printer(data)))


def register(root: typer.Typer) -> None:
    """Register the data commands on the root app (U09-84 step 4)."""
    for name, handler in (("sync", sync), ("build", build), ("enrich", enrich),
                          ("score", score), ("pipeline", pipeline)):  # fmt: skip
        root.command(name)(handler)
    metrics = typer.Typer(name="metrics", no_args_is_help=True, help="Metric catalog.")
    metrics.command("list")(metrics_list)
    root.add_typer(metrics)
