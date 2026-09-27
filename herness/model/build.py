"""The ``build_pipeline`` job body and its stages (impl 02 U02-95 … U02-99; design 02 §4.1, §7).

Stages run in ``STAGE_ORDER`` on one writable build connection. No transaction is held across
stages or around ``run_sql_range``: every statement autocommits, so later stage hooks (spec 04
``materialize_facts``) start with no open transaction. Only repository SQL runs; nothing from
the job payload reaches SQL text (TH02-10). ``CURRENT`` changes only inside promotion.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Final, Literal, Self

import duckdb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)

from herness.core import time as clock
from herness.core.config import HernessConfig, config_hash, get_config
from herness.core.errors import ConfigError, HernessError, SchemaViolation
from herness.core.jobs.ports import JobContext
from herness.core.logging import get_logger
from herness.core.resilience import fault_point
from herness.core.types import JobOutcome
from herness.model import _build_support as support
from herness.model import meta
from herness.model._build_support import STAGE_ORDER
from herness.model.errors import BuildSqlError
from herness.model.lakeinfo import LakeInventory, scan_lake
from herness.model.refdata import register_reference_tables
from herness.model.render_context import RenderContext, build_render_context
from herness.model.sqlfiles import SqlFile, discover_sql_files, render_sql
from herness.store import ops, warehouse
from herness.store._warehouse_rw import open_for_build
from herness.store.layout import DataLayout, data_layout

type Stage = Literal["build", "enrich", "score", "dq", "promote"]
type StageStatus = Literal["done", "yield"]
type _StageUnit = Callable[[_BuildRun], StageStatus]

_FACTS: Final = (400, 499)  # facts run through the spec 04 hook, never as a SQL range
_REPO_DIR: Final = Path(__file__).resolve().parents[2]
_ERRORS_SHOWN: Final = 200

_log = get_logger("model.build")

_ScoreStep = Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,32}$")]
_EnrichStage = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_-]{0,31}$")]
_Text = Annotated[str, StringConstraints(max_length=64)]


class BuildPipelinePayload(BaseModel):
    """Validated ``job.payload`` of kind ``build_pipeline`` (U02-96)."""

    model_config = ConfigDict(extra="forbid", strict=False, frozen=True)

    stages: Annotated[list[Stage], Field(min_length=1, max_length=5)]
    build_id: str | None = None
    depth: Literal["fast", "standard", "deep"] = "standard"
    score_steps: Annotated[list[_ScoreStep], Field(min_length=1, max_length=10)] | None = None
    enrich_stage: _EnrichStage | None = None
    rekey_night: bool = False
    schedule: _Text | None = None
    fire_at: _Text | None = None

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        first = STAGE_ORDER.index(self.stages[0])
        if tuple(self.stages) != STAGE_ORDER[first : first + len(self.stages)]:
            msg = "stages must be consecutive in STAGE_ORDER"
            raise ValueError(msg)
        if ("build" in self.stages) == (self.build_id is not None):
            msg = "build_id is required exactly when stage build is not requested"
            raise ValueError(msg)
        if self.build_id is not None and warehouse.BUILD_ID_RE.fullmatch(self.build_id) is None:
            msg = "build_id is not a build ID"
            raise ValueError(msg)
        if self.score_steps is not None and "score" not in self.stages:
            msg = "score_steps requires stage score"
            raise ValueError(msg)
        if self.enrich_stage is not None and "enrich" not in self.stages:
            msg = "enrich_stage requires stage enrich"
            raise ValueError(msg)
        return self


@dataclasses.dataclass(frozen=True, slots=True)
class SqlRangeResult:
    """Outcome of ``run_sql_range``: ``yield`` when it stopped before a file."""

    status: StageStatus
    files_run: tuple[str, ...]
    durations_ms: Mapping[str, int]


@dataclasses.dataclass(slots=True)
class _BuildRun:
    """Mutable state of one ``run_build_pipeline`` call (U02-99)."""

    ctx: JobContext
    payload: BuildPipelinePayload
    cfg: HernessConfig
    layout: DataLayout
    build_id: str
    now: datetime.datetime
    llm_factory: object | None  # T02-19: impl 03 LlmFactory, passed to run_enrichment
    stages_done: list[str]
    con: duckdb.DuckDBPyConnection | None = None
    files: list[SqlFile] = dataclasses.field(default_factory=list)
    context: RenderContext | None = None
    durations_ms: dict[str, int] = dataclasses.field(default_factory=dict)
    sql_ms: dict[str, int] = dataclasses.field(default_factory=dict)
    row_counts: dict[str, int] = dataclasses.field(default_factory=dict)
    result: dict[str, JsonValue] = dataclasses.field(default_factory=dict)


def _elapsed_ms(started: float) -> int:
    return int((clock.monotonic() - started) * 1000)


def _run_file(con: duckdb.DuckDBPyConnection, file: SqlFile, context: RenderContext) -> int:
    """Render and execute one file; return its duration in ms (U02-97 steps 2-5)."""
    started = clock.monotonic()
    try:
        sql = render_sql(file, context)
    except ConfigError:
        _log.error("model.build.render_failed", build_id=context.build_id, file=file.name)
        raise
    index = 0
    try:
        statements = con.extract_statements(sql)
        for index, statement in enumerate(statements, start=1):  # noqa: B007 - index reported
            con.execute(statement.query)
    except duckdb.Error as exc:
        # ``from None``: the raw DuckDB text may quote row values (TH02-14)
        raise BuildSqlError(context.build_id, file.name, index, str(exc)) from None
    duration = _elapsed_ms(started)
    _log.info(
        "model.build.sql_file_done",
        build_id=context.build_id,
        file=file.name,
        statements=len(statements),
        duration_ms=duration,
    )
    return duration


def run_sql_range(  # noqa: PLR0913 - U02-97 signature
    con: duckdb.DuckDBPyConnection,
    files: Sequence[SqlFile],
    lo: int,
    hi: int,
    *,
    context: RenderContext,
    should_yield: Callable[[], bool],
    heartbeat: Callable[[str], None],
) -> SqlRangeResult:
    """Execute the files numbered ``lo..hi`` in lexical order (U02-97).

    Raises ConfigError for a range outside 0-999 or touching 400-499, ConfigError for a
    render failure and BuildSqlError (sanitised) for a failing statement.
    """
    if not 0 <= lo <= hi <= 999 or (lo <= _FACTS[1] and hi >= _FACTS[0]):  # noqa: PLR2004
        msg = "SQL range must lie within 0-999 and exclude 400-499"
        raise ConfigError(msg)
    ran: list[str] = []
    durations: dict[str, int] = {}
    for file in sorted((f for f in files if lo <= f.number <= hi), key=lambda f: f.name):
        if should_yield():
            return SqlRangeResult("yield", tuple(ran), durations)
        durations[file.name] = _run_file(con, file, context)
        ran.append(file.name)
        heartbeat(f"sql {file.name}")
        fault_point("build.mid_sql")
    return SqlRangeResult("done", tuple(ran), durations)


def _connection(run: _BuildRun, *, create: bool = False) -> duckdb.DuckDBPyConnection:
    """The run's writable connection, opened on first use."""
    if run.con is None:
        view = support.db_settings(run.cfg.sources.build)
        run.con = open_for_build(run.build_id, create=create, cfg=view, layout=run.layout)
    return run.con


def _sql(run: _BuildRun, lo: int, hi: int) -> StageStatus:
    assert run.context is not None  # noqa: S101 - set by _stage_build before any range
    outcome = run_sql_range(
        _connection(run),
        run.files,
        lo,
        hi,
        context=run.context,
        should_yield=run.ctx.should_yield,
        heartbeat=run.ctx.heartbeat,
    )
    run.sql_ms.update(outcome.durations_ms)
    return outcome.status


def _deleted_ids(inventory: LakeInventory) -> list[str]:
    """Union of the deletion requests of every present entity (T10-32, R-68)."""
    ids = {
        record_id
        for entity in inventory.entities.values()
        if entity.present
        for record_id in ops.deleted_record_ids(entity.source, entity.entity)
    }
    return sorted(ids)


def _stage_build(run: _BuildRun) -> StageStatus:
    """Stage ``build``: create the file and run 000-299 (U02-99)."""
    con = _connection(run, create=True)
    cfg = run.cfg
    probe = build_render_context(cfg, LakeInventory(root=run.layout.raw, entities={}), run.build_id)
    pairs = [(source, name) for source, names in probe.extra_entities.items() for name in names]
    inventory = scan_lake(run.layout, extra_entities=pairs)
    run.context = dataclasses.replace(probe, lake=inventory)
    run.files = discover_sql_files()
    if _sql(run, 0, 99) == "yield":
        return "yield"
    watermarks = {
        f"{w.source}/{w.entity}": clock.format_utc(w.value) for w in ops.list_watermarks()
    }
    meta.insert_build_row(
        con,
        build_id=run.build_id,
        started_at=run.now,
        git_sha=meta.git_sha(env=os.environ, repo_dir=_REPO_DIR),
        config_hash=config_hash(cfg),
        dataset_kind=meta.dataset_kind(cfg.profile, inventory),
        source_watermarks=watermarks,
    )
    # deletions are registered before staging so 100-299 never see a deleted record
    register_reference_tables(
        con,
        mappings=cfg.mappings,
        build_cfg=cfg.sources.build,
        deleted_ids=_deleted_ids(inventory),
        approved=ops.approved_mapping_suggestions(),
    )
    if _sql(run, 100, 299) == "yield":
        return "yield"
    run.row_counts = meta.collect_row_counts(con, ["core"])
    meta.update_build_row(con, row_counts=run.row_counts)
    con.execute("CHECKPOINT")
    return "done"


# Stage units by name. T02-19: enrich, score (U02-100, U02-101); T02-20: dq (U02-102);
# T02-21: promote (U02-103). A payload naming a missing stage is rejected before any work.
_STAGE_UNITS: Final[Mapping[str, _StageUnit]] = {"build": _stage_build}


def _error_label(loc: tuple[int | str, ...], kind: str) -> str:
    """A payload field name of the model itself; caller-supplied keys are never echoed."""
    if kind == "extra_forbidden":
        return "extra field"
    return str(loc[0]) if loc and loc[0] in BuildPipelinePayload.model_fields else "payload"


def _parse_payload(ctx: JobContext) -> BuildPipelinePayload:
    try:
        payload = BuildPipelinePayload.model_validate(dict(ctx.job.payload))
    except ValidationError as exc:
        _log.error("model.build.payload_invalid", job_id=ctx.job_id)
        fields = sorted({_error_label(e["loc"], e["type"]) for e in exc.errors()})
        msg = f"invalid build_pipeline payload: {', '.join(fields)[:_ERRORS_SHOWN]}"
        raise ConfigError(msg) from None
    missing = [stage for stage in payload.stages if stage not in _STAGE_UNITS]
    if missing:
        msg = f"build stage {missing[0]} is not available"
        raise ConfigError(msg)
    return payload


def _write_metrics(run: _BuildRun, status: str) -> None:
    support.write_metrics(
        build_id=run.build_id,
        layout=run.layout,
        status=status,
        stage_ms=run.durations_ms,
        sql_ms=run.sql_ms,
        row_counts=run.row_counts,
    )


def _save_state(run: _BuildRun) -> None:
    run.ctx.save_state({"build_id": run.build_id, "stages_done": list(run.stages_done)})


def _yield(run: _BuildRun) -> JobOutcome:
    _save_state(run)
    _write_metrics(run, "yield")
    return JobOutcome(status="yield", result={"build_id": run.build_id})


def _fail(run: _BuildRun, stage: str, exc: HernessError) -> None:
    """Mark the build failed (best effort), log and write metrics (U02-98 error path)."""
    try:
        meta.update_build_row(_connection(run), status="failed", finished_at=clock.now())
    except Exception as mark_exc:  # noqa: BLE001 - the original error must propagate
        error_class = type(mark_exc).__name__
        _log.error("model.build.mark_failed_error", build_id=run.build_id, error_class=error_class)
    error_class = type(exc).__name__
    _log.error("model.build.failed", build_id=run.build_id, stage=stage, error_class=error_class)
    _write_metrics(run, "failed")


def _run_stages(run: _BuildRun) -> JobOutcome:
    """U02-98 steps 5-8."""
    for name in [s for s in run.payload.stages if s not in run.stages_done]:
        if run.ctx.should_yield():
            return _yield(run)
        started = clock.monotonic()
        try:
            status = _STAGE_UNITS[name](run)
        except HernessError as exc:
            _fail(run, name, exc)
            raise
        except (duckdb.Error, OSError) as exc:  # e.g. CHECKPOINT, scan_lake: raw text dropped
            error = SchemaViolation(
                f"build stage {name} failed", build_id=run.build_id, error_type=type(exc).__name__
            )
            _fail(run, name, error)
            raise error from None
        if status == "yield":
            return _yield(run)
        run.stages_done.append(name)
        _save_state(run)
        run.durations_ms[name] = duration = _elapsed_ms(started)
        run.ctx.heartbeat(f"stage {name} done")
        _log.info("model.build.stage_done", build_id=run.build_id, stage=name, duration_ms=duration)
    promoted = "promote" in run.payload.stages
    if not promoted:
        meta.update_build_row(_connection(run), finished_at=clock.now())
    _write_metrics(run, "promoted" if promoted else "unpromoted")
    result: dict[str, JsonValue] = {
        "build_id": run.build_id,
        "stages": list(run.payload.stages),
        "promoted": promoted,
        "dq": None,
        "durations_ms": dict(run.durations_ms),
        "row_counts": dict(run.row_counts),
        "enrich": None,
        "scoring": None,
        "rekey_night": run.payload.rekey_night,
    }
    return JobOutcome(status="done", result=result | run.result)


def run_build_pipeline(ctx: JobContext, *, llm_factory: object | None = None) -> JobOutcome:
    """Run the requested stages on a new or unpromoted build (U02-98, flow F02-02).

    On a HernessError in a stage the build is marked ``failed`` (best effort), the error is
    logged and re-raised; ``CURRENT`` is never touched here.
    """
    payload = _parse_payload(ctx)
    cfg = get_config()
    layout = data_layout(cfg=cfg)
    now = clock.now()
    build_id, done = support.resolve_build(payload.build_id, ctx.load_state(), layout, now)
    support.delete_orphans(layout, protect=build_id)
    run = _BuildRun(ctx, payload, cfg, layout, build_id, now, llm_factory, done)
    try:
        return _run_stages(run)
    finally:
        if run.con is not None:
            run.con.close()
