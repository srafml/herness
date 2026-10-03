"""Interactive metric API (impl 04 U04-49 … U04-53; design 04 §3.1, §6 row 1, §9).

Every number comes from the one recorded wrapper SELECT (U04-40) via `run_recorded`, copied by
column name. Grain, period and filter keys are allowlisted; caller values are typed bind
parameters (TH04-01). Logs carry IDs and counts only, never filter values (TH04-11).
"""

import contextlib
import dataclasses
import datetime
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, Final, Literal, Self, get_args

import duckdb
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from herness.core.config import get_config
from herness.core.errors import ConfigError, QueryError, SchemaViolation, ToolInputError
from herness.core.logging import get_logger
from herness.core.resilience.metrics import record_counter
from herness.metrics._request import normalize_entity_ids, normalize_filters
from herness.metrics.catalog import METRIC_FLAGS, MetricCatalog, catalog_from_config
from herness.metrics.evidence import RecordedQuery, run_recorded
from herness.metrics.peers import PeerGroupInfo, peer_group  # re-export (§2 row, T04-17)
from herness.metrics.render import render_metric_query
from herness.metrics.settings import Better, EntityType, FilterKey, MetricDef, Period, Unit
from herness.metrics.windows import Window, custom_window, default_window, resolve_as_of
from herness.store.warehouse import open_readonly

__all__ = [
    "MetricResult",
    "MetricRow",
    "PeerGroupInfo",
    "compute_metric",
    "metric_series",
    "peer_group",
    "validate_metric_request",
]

_log: Final = get_logger("metrics")

_PAIR: Final = 2
_PERIODS: Final = ", ".join(get_args(Period))
_BUILD_SQL: Final = "SELECT build_id, started_at FROM meta.build"
_COLUMN_SQL: Final = (
    "SELECT count(*) FROM information_schema.columns WHERE table_catalog = current_database()"
    " AND table_schema = ? AND table_name = ? AND column_name = ?"
)
_COUNTER: Final = "herness_metrics_compute_calls_total"

type Outcome = Literal["ok", "input_error", "query_error"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _check_flags(flags: list[str]) -> list[str]:
    unknown = sorted(set(flags) - METRIC_FLAGS)
    if unknown:
        msg = f"unknown metric flag {unknown[0][:32]}"
        raise ValueError(msg)
    if flags != sorted(flags):
        msg = "flags must be sorted"
        raise ValueError(msg)
    return flags


class MetricRow(_Frozen):
    """One metric row of the wrapper SELECT (U04-49, design 04 §3.1)."""

    entity_id: str
    period_start: datetime.date
    value: float | None
    numerator: float | None
    denominator: float | None
    sample_size: int = Field(ge=0)
    flags: list[str]

    _flags = field_validator("flags")(_check_flags)


class MetricResult(_Frozen):
    """Result of `compute_metric` with everything ops `evidence` needs (U04-50)."""

    metric: str
    entity_type: EntityType
    period: Period
    unit: Unit
    better: Better
    rows: list[MetricRow]
    query_id: str
    sql: str
    params: dict[str, Any]
    result_hash: str
    result_sample: list[dict[str, Any]] = Field(max_length=50)
    row_count: int = Field(ge=0)
    build_id: str
    catalog_version: str
    flags: list[str]
    _source: RecordedQuery | None = PrivateAttr(default=None)

    _flags = field_validator("flags")(_check_flags)

    @model_validator(mode="after")
    def _row_count(self) -> Self:
        if self.row_count != len(self.rows):
            msg = "row_count must equal len(rows)"
            raise ValueError(msg)
        return self

    def recorded(self) -> RecordedQuery:
        """Rebuild the `RecordedQuery` of this result for `to_evidence` (U04-50)."""
        src = self._source
        if src is None:
            msg = "MetricResult was not produced by compute_metric"
            raise ConfigError(msg)
        rows = None if src.rows is None else list(src.rows)
        return dataclasses.replace(src, params=dict(src.params), rows=rows)


# --- U04-51 request validation ---------------------------------------------------------------


def validate_metric_request(
    catalog: MetricCatalog,
    name: str,
    entity_type: str,
    entity_ids: Sequence[str] | None,
    period: str,
    filters: Mapping[str, Any] | None,
    /,
) -> tuple[MetricDef, dict[FilterKey, list[object]], list[str] | None]:
    """Every input check of design 04 §6 row 1 before any SQL runs (U04-51)."""
    metric = catalog.get(name)
    if not metric.enabled:
        msg = f"metric {metric.name} is disabled"
        raise ToolInputError(msg)
    if period not in get_args(Period):
        msg = f"bad period {str(period)[:32]}; allowed: {_PERIODS}"
        raise ToolInputError(msg)
    if entity_type not in metric.grains:
        grains = ", ".join(metric.grains)
        msg = f"grain {str(entity_type)[:32]} not supported by {metric.name}; allowed: {grains}"
        raise ToolInputError(msg)
    return metric, normalize_filters(metric, filters), normalize_entity_ids(entity_ids)


# --- U04-52 compute_metric -------------------------------------------------------------------


@contextlib.contextmanager
def _connection(con: duckdb.DuckDBPyConnection | None) -> Iterator[duckdb.DuckDBPyConnection]:
    """`con` as given (never closed here), else a read-only CURRENT warehouse closed on exit."""
    if con is not None:
        yield con
        return
    opened = open_readonly(None)
    try:
        yield opened
    finally:
        opened.close()


def _check_columns(con: duckdb.DuckDBPyConnection, metric: MetricDef) -> None:
    for column in metric.requires_columns:
        schema, table, name = column.split(".")
        row = con.execute(_COLUMN_SQL, [schema, table, name]).fetchone()
        if row is None or row[0] == 0:
            msg = f"metric {metric.name} needs {column}, absent in this build"
            raise ToolInputError(msg)


def _read_build(con: duckdb.DuckDBPyConnection) -> tuple[str, datetime.datetime]:
    msg = "meta.build must hold one row"
    try:
        rows = con.execute(_BUILD_SQL).fetchall()
    except duckdb.Error as err:
        raise SchemaViolation(msg) from err
    build_id, started_at = rows[0] if len(rows) == 1 else (None, None)
    if not isinstance(build_id, str) or not isinstance(started_at, datetime.datetime):
        raise SchemaViolation(msg)
    return build_id, started_at


def _window(
    period: Period,
    window: object,
    as_of: datetime.date,
    tz: str,
    counts: Mapping[str, int],
) -> Window:
    if window is None:
        return default_window(period, as_of, tz, counts)
    items: tuple[object, ...] = window if isinstance(window, tuple) else ()
    dates = [d for d in items if type(d) is datetime.date]
    if len(dates) != _PAIR or len(items) != _PAIR:
        msg = "window must be a (start, end) pair of dates"
        raise ToolInputError(msg)
    return custom_window(period, dates[0], dates[1], as_of, tz)


def _rows(rq: RecordedQuery) -> list[MetricRow]:
    """MetricRows copied by column name from the wrapper rows (U04-40); no arithmetic."""
    index = {name: i for i, (name, _) in enumerate(rq.columns)}
    fields = ("entity_id", "period_start", "value", "numerator", "denominator", "sample_size")
    return [
        MetricRow.model_validate(
            {f: row[index[f]] for f in fields} | {"flags": row[index["flags"]]}
        )
        for row in rq.rows or []
    ]


def _compute(  # noqa: PLR0913 - mirrors the U04-52 signature
    name: str,
    entity_type: EntityType,
    entity_ids: Sequence[str] | None,
    period: Period,
    *,
    filters: Mapping[str, Any] | None,
    window: tuple[datetime.date, datetime.date] | None,
    con: duckdb.DuckDBPyConnection | None,
) -> MetricResult:
    catalog = catalog_from_config()
    weights = get_config().weights
    metric, norm, ids = validate_metric_request(
        catalog, name, entity_type, entity_ids, period, filters
    )
    tz = weights.business_timezone
    with _connection(con) as c:
        _check_columns(c, metric)
        build_id, started_at = _read_build(c)
        as_of = resolve_as_of(started_at, tz, catalog.scoring.as_of)
        counts = {str(k): v for k, v in catalog.defaults.windows.items()}
        win = _window(period, window, as_of, tz, counts)
        rendered = render_metric_query(
            metric,
            entity_type=entity_type,
            window=win,
            filters=norm,
            entity_ids=ids,
            catalog=catalog,
            weights=weights,
        )
        params = {"bind": rendered.bind, "template": rendered.template}
        timeout = catalog.defaults.compute_timeout_s
        rq = run_recorded(c, rendered.sql, params, None, build_id=build_id, timeout_s=timeout)
    static_flags = rendered.bind.get("static_flags", [])
    result = MetricResult(
        metric=metric.name,
        entity_type=entity_type,
        period=period,
        unit=metric.unit,
        better=metric.better,
        rows=_rows(rq),
        query_id=rq.query_id,
        sql=rq.sql,
        params=rq.params,
        result_hash=rq.result_hash,
        result_sample=rq.result_sample,
        row_count=rq.row_count,
        build_id=rq.build_id,
        catalog_version=catalog.version,
        flags=sorted(static_flags) if isinstance(static_flags, list) else [],
    )
    result._source = rq
    return result


def _count(outcome: Outcome) -> None:
    # T08-05: record_counter is the interim sink until the ops metric writer lands.
    record_counter(_COUNTER, component="metrics", labels={"outcome": outcome})


def compute_metric(  # noqa: PLR0913 - design 04 §3.1 signature
    name: str,
    entity_type: EntityType,
    entity_ids: Sequence[str] | None,
    period: Period,
    filters: Mapping[str, Any] | None = None,
    *,
    window: tuple[datetime.date, datetime.date] | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> MetricResult:
    """Compute one metric for one grain and period on a read-only warehouse (U04-52).

    Nothing is written to the warehouse or the ops store. Raises ToolInputError (request,
    missing column, window), QueryError (timeout, DuckDB error, too many rows) and StoreBusy
    when the warehouse cannot be opened.
    """
    start = time.perf_counter()
    try:
        result = _compute(
            name, entity_type, entity_ids, period, filters=filters, window=window, con=con
        )
    except ToolInputError:
        _count("input_error")
        raise
    except QueryError:
        _count("query_error")
        raise
    _count("ok")
    _log.info(
        "metrics.compute.completed",
        metric=result.metric,
        entity_type=result.entity_type,
        period=result.period,
        row_count=result.row_count,
        query_id=result.query_id,
        duration_ms=int((time.perf_counter() - start) * 1000),
    )
    return result


# --- U04-53 metric_series --------------------------------------------------------------------


def metric_series(  # noqa: PLR0913 - design 04 §3.1 signature (DD04-03)
    metric: str,
    entity_type: EntityType,
    entity_ids: Sequence[str] | None,
    start: datetime.date,
    end: datetime.date,
    period: Period = "week",
    *,
    filters: Mapping[str, Any] | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
    on_evidence: Callable[[RecordedQuery], None] | None = None,
) -> tuple[list[MetricRow], str]:
    """One recorded query over `[start, end)`: its rows and `query_id` (U04-53)."""
    result = compute_metric(
        metric, entity_type, entity_ids, period, filters, window=(start, end), con=con
    )
    if on_evidence is not None:
        on_evidence(result.recorded())
    return result.rows, result.query_id
