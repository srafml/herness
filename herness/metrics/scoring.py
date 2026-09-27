"""Scoring runner with the validate, metrics and check steps (impl 04 U04-55 … U04-59).

`run_scoring` runs the design 04 §3.3 steps on the build pipeline's writable connection, one
transaction per step, checkpointed through the job context (design 04 §6). Every stored number
comes from a recorded SELECT (`run_recorded`); Python only renders, orders and copies.
The check step lives in the private sibling `_scoring_checks` (module budget).
"""

import datetime
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Final, cast

import duckdb
from pydantic import JsonValue

from herness.core.config import config_hash, get_config
from herness.core.errors import ConfigError, QueryError, SchemaViolation
from herness.core.logging import get_logger
from herness.core.resilience.metrics import record_histogram
from herness.metrics._scoring_checks import existing_tables, run_check_step
from herness.metrics.catalog import MetricCatalog, catalog_from_config, validate_catalog
from herness.metrics.context import ScoringReport, StepContext, StepResult
from herness.metrics.evidence import IntoSpec, run_recorded
from herness.metrics.facts import FACT_TABLES
from herness.metrics.render import render_metric_query
from herness.metrics.settings import Period, WeightsConfig
from herness.metrics.windows import default_window, resolve_as_of

if TYPE_CHECKING:
    from herness.core.jobs.ports import JobContext

__all__ = [
    "STEPS",
    "ScoringReport",
    "missing_required_columns",
    "run_check_step",
    "run_metrics_step",
    "run_scoring",
]

STEPS: Final[tuple[str, ...]] = (
    "validate",
    "metrics",
    "funding",
    "org",
    "levers",
    "portfolio",
    "check",
)
METRIC_TABLE: Final = "metrics.metric_value"
_PERIODS: Final[tuple[Period, ...]] = ("week", "month", "quarter", "t12w", "t12m")
_METRIC_VALUE_DDL: Final = (
    "CREATE OR REPLACE TABLE metrics.metric_value (metric VARCHAR NOT NULL,"
    " entity_type VARCHAR NOT NULL, entity_id VARCHAR NOT NULL, period VARCHAR NOT NULL,"
    " period_start DATE NOT NULL, value DOUBLE, numerator DOUBLE, denominator DOUBLE,"
    " sample_size BIGINT NOT NULL, unit VARCHAR NOT NULL, flags VARCHAR[] NOT NULL,"
    " query_id VARCHAR NOT NULL)"
)
_COLUMN_SQL: Final = (
    "SELECT count(*) FROM information_schema.columns WHERE table_catalog = current_database()"
    " AND table_schema = ? AND table_name = ? AND column_name = ?"
)
_DISABLED: Final = "score_metric_disabled"
# value = number of missing columns, computed by DuckDB from the bound list (no Python math)
_DISABLED_ROW_SQL: Final = (
    "INSERT INTO meta.dq_result (check_name, severity, value, threshold, passed, details)"
    " SELECT ?, 'warn', CAST(len(c) AS DOUBLE), 0, false,"
    " json_object('metric', ?, 'columns', c) FROM (SELECT CAST(? AS VARCHAR[]) AS c)"
)
_log: Final = get_logger("metrics")

type StepFn = Callable[[duckdb.DuckDBPyConnection, StepContext], StepResult]


def _quote(identifier: str) -> str:
    """DuckDB identifier quoting (ENG §3.5)."""
    return '"' + identifier.replace('"', '""') + '"'


def missing_required_columns(
    con: duckdb.DuckDBPyConnection, catalog: MetricCatalog
) -> dict[str, list[str]]:
    """Metric -> its `requires_columns` entries that are absent or all NULL (U04-57)."""
    missing: dict[str, list[str]] = {}
    for name in catalog.names():
        absent: list[str] = []
        for column in catalog.get(name).requires_columns:
            schema, table, col = column.split(".")  # validated by the U04-15 pattern
            present = con.execute(_COLUMN_SQL, [schema, table, col]).fetchone()
            if present is None or present[0] == 0:
                absent.append(column)
                continue
            sql = f"SELECT count({_quote(col)}) FROM {_quote(schema)}.{_quote(table)}"  # noqa: S608 - quoted, validated identifiers
            filled = con.execute(sql).fetchone()
            if filled is None or filled[0] == 0:
                absent.append(column)
        if absent:
            missing[name] = absent
    return missing


def _rollback(
    con: duckdb.DuckDBPyConnection, step: str, build_id: str | None, err: BaseException
) -> None:
    """ROLLBACK; a failing rollback is logged and noted on `err`, which stays the one raised."""
    try:
        con.execute("ROLLBACK")
    except duckdb.Error as failure:
        name = type(failure).__name__
        _log.error(
            "metrics.scoring.rollback_failed", build_id=build_id, step=step, error_class=name
        )
        err.add_note(f"rollback after {step} failed: {name}")


def _validate_step(
    con: duckdb.DuckDBPyConnection, catalog: MetricCatalog, weights: WeightsConfig
) -> frozenset[str]:
    """Fact tables, catalog errors and `score_metric_disabled` warnings (U04-57)."""
    if not set(FACT_TABLES) <= existing_tables(con):
        msg = "fact tables missing; stage 400 did not run"
        raise SchemaViolation(msg)
    errors = [i for i in validate_catalog(catalog.config, weights=weights) if i.severity == "error"]
    if errors:
        msg = f"metric catalog invalid: {errors[0].path}: {errors[0].message}"
        raise ConfigError(msg)
    try:
        missing = missing_required_columns(con, catalog)
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("DELETE FROM meta.dq_result WHERE check_name = ?", [_DISABLED])
            for metric, columns in sorted(missing.items()):
                con.execute(_DISABLED_ROW_SQL, [_DISABLED, metric, columns])
            con.execute("COMMIT")
        except duckdb.Error as err:
            _rollback(con, "validate", None, err)
            raise
    except duckdb.Error as err:
        _log.error("metrics.scoring.step_failed", step="validate", error_class=type(err).__name__)
        msg = f"validate failed: {err}"
        raise SchemaViolation(msg) from err
    for metric, columns in sorted(missing.items()):
        _log.warning("metrics.scoring.metric_disabled", metric=metric, missing_count=len(columns))
    return frozenset(missing)


def run_metrics_step(con: duckdb.DuckDBPyConnection, sc: StepContext, /) -> StepResult:
    """Rewrite `metrics.metric_value` for every enabled metric x grain x period (U04-58)."""
    con.execute(_METRIC_VALUE_DDL)
    counts = cast("Mapping[str, int]", sc.catalog.defaults.windows)
    into = IntoSpec(METRIC_TABLE, "append", "query_id")
    for name in sorted(set(sc.catalog.names()) - sc.disabled_metrics):
        metric = sc.catalog.get(name)
        for grain in metric.grains:
            for period in _PERIODS:
                window = default_window(period, sc.as_of, sc.tz, counts)
                rendered = render_metric_query(
                    metric,
                    entity_type=grain,
                    window=window,
                    filters={},
                    entity_ids=None,
                    catalog=sc.catalog,
                    weights=sc.weights,
                )
                params = {"bind": rendered.bind, "template": rendered.template}
                try:
                    run_recorded(
                        con, rendered.sql, params, "metrics", build_id=sc.build_id, into=into
                    )
                except QueryError as err:
                    msg = f"metric {name} grain {grain} period {period} failed: {err.message}"
                    raise SchemaViolation(msg) from err
    row = con.execute(f"SELECT count(*) FROM {METRIC_TABLE}").fetchone()  # noqa: S608 - constant
    rows = int(row[0]) if row else 0
    return StepResult(row_counts={METRIC_TABLE: rows}, warnings=[], flags=[], failed_checks=[])


# Step functions by name; `validate` runs separately on every call.
_STEP_FUNCS: dict[str, StepFn | None] = {
    "metrics": run_metrics_step,
    "funding": None,  # T04-21: run_funding_step (herness.metrics.funding, T04-15)
    "org": None,  # T04-21: run_org_step (herness.metrics.org, T04-17)
    "levers": None,  # T04-21: run_levers_step (herness.metrics.levers, T04-18)
    "portfolio": None,  # T04-21: run_portfolio_step (herness.metrics.portfolio, T04-20)
    "check": run_check_step,
}


def _requested(steps: Sequence[str] | None) -> list[str]:
    """`STEPS` when None, else the listed names in `STEPS` order with `validate` first."""
    if steps is None:
        return list(STEPS)
    if isinstance(steps, str):  # a bare name would iterate its characters
        msg = f"unknown scoring step {steps[:64]}"
        raise ConfigError(msg)
    for step in steps:
        if step not in STEPS:
            msg = f"unknown scoring step {str(step)[:64]}"
            raise ConfigError(msg)
    return [s for s in STEPS if s == "validate" or s in steps]


def _started_at(con: duckdb.DuckDBPyConnection, build_id: str) -> datetime.datetime:
    """The build's `started_at`; ConfigError unless the connection is writable and building."""
    mode = con.execute("SELECT current_setting('access_mode')").fetchone()
    if mode is not None and str(mode[0]).lower() == "read_only":
        msg = "run_scoring needs the build pipeline's writable connection"
        raise ConfigError(msg)
    row = con.execute(
        "SELECT status, started_at FROM meta.build WHERE build_id = ?", [build_id]
    ).fetchone()
    status = "missing" if row is None else row[0]
    if row is None or status != "building":
        msg = f"build {build_id} is {status}; scoring runs only before promotion"
        raise ConfigError(msg)
    if not isinstance(row[1], datetime.datetime):
        msg = f"build {build_id} has no start time"
        raise SchemaViolation(msg)
    return row[1]


def _checkpoint(
    ctx: "JobContext | None", build_id: str, cfg_hash: str
) -> tuple[dict[str, JsonValue], list[str]]:
    """(job state, steps done earlier) when the checkpoint matches this build and config."""
    if ctx is None:
        return {}, []
    state = ctx.load_state()
    saved = state.get("scoring")
    if not isinstance(saved, dict):
        return state, []
    done = saved.get("steps_done")
    same = saved.get("build_id") == build_id and saved.get("config_hash") == cfg_hash
    if not same or not isinstance(done, list):
        return state, []
    return state, [s for s in done if isinstance(s, str) and s in _STEP_FUNCS]


def _run_step(con: duckdb.DuckDBPyConnection, step: str, fn: StepFn, sc: StepContext) -> StepResult:
    """One step in its own transaction; any error rolls it back (U04-56 step 7)."""
    con.execute("BEGIN TRANSACTION")
    try:
        result = fn(con, sc)
        con.execute("COMMIT")
    except Exception as err:
        _rollback(con, step, sc.build_id, err)
        _log.error(
            "metrics.scoring.step_failed",
            build_id=sc.build_id,
            step=step,
            error_class=type(err).__name__,
        )
        if isinstance(err, QueryError | duckdb.Error):
            message = err.message if isinstance(err, QueryError) else str(err)
            msg = f"{step} failed: {message}"
            raise SchemaViolation(msg) from err
        raise
    return result


class _Run:
    """Accumulates the report of one `run_scoring` call."""

    def __init__(self, build_id: str, done: list[str]) -> None:
        self.build_id = build_id
        self.done = done
        self.row_counts: dict[str, int] = {}
        self.duration_ms: dict[str, int] = {}
        self.flags: list[str] = []
        self.warnings: list[str] = []

    def add(self, step: str, result: StepResult, elapsed: float) -> None:
        self.done.append(step)
        self.row_counts.update(result.row_counts)
        self.duration_ms[step] = int(elapsed * 1000)
        self.flags.extend(f for f in result.flags if f not in self.flags)
        self.warnings.extend(result.warnings)

    def report(self) -> ScoringReport:
        done = ["validate", *(s for s in STEPS if s in self.done)]
        return ScoringReport(
            build_id=self.build_id,
            steps_done=done,
            row_counts=dict(self.row_counts),
            duration_ms=dict(self.duration_ms),
            flags=list(self.flags),
            warnings=list(self.warnings),
        )


def _save(ctx: "JobContext", state: dict[str, JsonValue], run: _Run, cfg_hash: str) -> None:
    done: list[JsonValue] = [s for s in STEPS if s in run.done]
    scoring: dict[str, JsonValue] = {
        "build_id": run.build_id,
        "config_hash": cfg_hash,
        "steps_done": done,
    }
    ctx.save_state({**state, "scoring": scoring})


def _execute(
    con: duckdb.DuckDBPyConnection,
    step: str,
    sc: StepContext,
    run: _Run,
    ctx: "JobContext | None",
    checkpoint: tuple[dict[str, JsonValue], str],
) -> None:
    """Run one pending step, record it and checkpoint it (U04-56 steps 7-9)."""
    fn = _STEP_FUNCS[step]
    if fn is None:
        _log.warning("metrics.scoring.step_unavailable", build_id=sc.build_id, step=step)
        run.warnings.append(f"step {step} is not available yet; skipped")
        return
    _log.info("metrics.scoring.step_started", build_id=sc.build_id, step=step)
    start = time.perf_counter()
    result = _run_step(con, step, fn, sc)
    elapsed = time.perf_counter() - start
    if result.failed_checks:  # dq rows are committed; the step is not checkpointed
        msg = "scoring invariants failed: " + ", ".join(result.failed_checks)
        raise SchemaViolation(msg)
    run.add(step, result, elapsed)
    if ctx is not None:
        _save(ctx, checkpoint[0], run, checkpoint[1])
        ctx.heartbeat(f"scoring:{step}")
    rows = sum(result.row_counts.values())
    _log.info(
        "metrics.scoring.step_completed",
        build_id=sc.build_id,
        step=step,
        duration_ms=run.duration_ms[step],
        rows=rows,
    )
    labels = {"step": step}
    record_histogram(
        "herness_metrics_step_duration_seconds", elapsed, component="metrics", labels=labels
    )


def _context(
    con: duckdb.DuckDBPyConnection,
    build_id: str,
    started_at: datetime.datetime,
    catalog: MetricCatalog,
    weights: WeightsConfig,
) -> StepContext:
    """Run the validate step and build the shared StepContext (U04-56 steps 4 and 6)."""
    tz = weights.business_timezone
    as_of = resolve_as_of(started_at, tz, catalog.scoring.as_of)
    disabled = _validate_step(con, catalog, weights)
    return StepContext(build_id, catalog, weights, as_of, tz, disabled)


def run_scoring(
    build_id: str,
    *,
    steps: Sequence[str] | None = None,
    con: duckdb.DuckDBPyConnection,
    ctx: "JobContext | None" = None,
) -> ScoringReport:
    """Run the requested scoring steps, resumable through the job checkpoint (U04-56)."""
    start = time.perf_counter()
    requested = _requested(steps)
    started_at = _started_at(con, build_id)
    cfg = get_config()
    catalog = catalog_from_config(cfg)
    cfg_hash = config_hash(cfg)
    state, done = _checkpoint(ctx, build_id, cfg_hash)
    sc = _context(con, build_id, started_at, catalog, cfg.weights)
    run = _Run(build_id, done)
    pending = [s for s in requested if s != "validate"]
    for index, step in enumerate(pending):
        if step in run.done:
            _log.info("metrics.scoring.step_skipped", build_id=build_id, step=step)
            continue
        _execute(con, step, sc, run, ctx, (state, cfg_hash))
        more = index + 1 < len(pending)
        if ctx is not None and more and step in run.done and ctx.should_yield():
            run.flags.append("yielded")
            return run.report()
    report = run.report()
    _log.info(
        "metrics.scoring.completed",
        build_id=build_id,
        steps_done=report.steps_done,
        duration_ms=int((time.perf_counter() - start) * 1000),
    )
    return report
