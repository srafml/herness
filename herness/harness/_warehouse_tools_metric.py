"""`get_metric` of the spec 05 warehouse tools (impl 05 U05-42; design 05 §5.4.2); private.

Split out of `warehouse_tools` (module budget). Never builds metric SQL: spec 04
`compute_metric` runs the catalog metric; the evidence row is built from its `MetricResult`.
"""

from __future__ import annotations

import datetime
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Final, cast

import duckdb
from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import ConfigError, HernessError, QueryError, ToolInputError
from herness.core.ids import normalize_sql
from herness.core.resilience import retry_call
from herness.core.types import Evidence, ToolContext, ToolResult
from herness.harness import _warehouse_tools_sql as ws
from herness.harness.tools import TOOL_CONTENT_MAX_CHARS, RecordedResult, format_result
from herness.metrics.catalog import MetricCatalog, catalog_from_config
from herness.metrics.compute import MetricResult, compute_metric
from herness.metrics.settings import EntityType, Period

MAX_WINDOW_DAYS: Final = 1_100
MAX_ENTITY_IDS: Final = 50
ID_CHARS: Final = 200
ENTITY_TYPES: Final = ("service", "team", "org", "work_item", "cluster")
PERIODS: Final = ("week", "month", "quarter")
FILTER_KEYS: Final = (
    "priority", "service_id", "team_id", "org_id", "cluster_id", "work_item_type", "severity",
    "change_type",
)  # fmt: skip
TIMEOUT_HINT: Final = "narrow the window or entity list"
ROW_COLUMNS: Final = (
    "entity_id", "period_start", "value", "numerator", "denominator", "sample_size", "flags",
)  # fmt: skip
_ROW_TYPES: Final = ("VARCHAR", "DATE", "DOUBLE", "DOUBLE", "DOUBLE", "BIGINT", "VARCHAR[]")
_DATE_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ID_ITEMS: Final[dict[str, JsonValue]] = {"type": "string", "minLength": 1, "maxLength": ID_CHARS}
_PRIORITY: Final[dict[str, JsonValue]] = {"type": "integer", "minimum": 1, "maximum": 5}
# Strict-compatible (R-26): every catalog filter key listed and required, null when unused.
_FILTERS_SCHEMA: Final = ws.strict_schema(
    {
        key: {"type": ["array", "null"], "items": _PRIORITY if key == "priority" else _ID_ITEMS}
        for key in FILTER_KEYS
    }
) | {"type": ["object", "null"]}


def _choice(args: Mapping[str, JsonValue], name: str, allowed: Sequence[str]) -> str:
    value = ws.text(args, name)
    if value not in allowed:
        msg = f"argument {name} must be one of {', '.join(allowed)}"
        raise ToolInputError(msg)
    return value


def _nullable(args: Mapping[str, JsonValue], name: str, kind: type) -> JsonValue:
    if name not in args:
        msg = f"missing argument {name}"
        raise ToolInputError(msg)
    value = args[name]
    if value is not None and not isinstance(value, kind):
        msg = f"argument {name} must be a {kind.__name__} or null"
        raise ToolInputError(msg)
    return value


def _entity_ids(args: Mapping[str, JsonValue]) -> list[str] | None:
    value = _nullable(args, "entity_ids", list)
    items = value if isinstance(value, list) else []
    ids = [v for v in items if isinstance(v, str) and len(v) <= ID_CHARS]
    if value is not None and (ids != value or len(ids) > MAX_ENTITY_IDS):
        msg = f"entity_ids must hold at most {MAX_ENTITY_IDS} IDs of at most {ID_CHARS} chars"
        raise ToolInputError(msg)
    return None if value is None else ids


def _filters(args: Mapping[str, JsonValue]) -> dict[str, JsonValue] | None:
    """Only the keys given a value; keys and items are validated by `compute_metric`."""
    value = _nullable(args, "filters", dict)
    used: dict[str, JsonValue] = dict(value) if isinstance(value, dict) else {}
    return {k: v for k, v in used.items() if v is not None} or None


def _date(args: Mapping[str, JsonValue], name: str) -> datetime.date | None:
    value = ws.opt_text(args, name)
    if value is None:
        return None
    try:
        return datetime.date.fromisoformat(value if _DATE_RE.fullmatch(value) else "")
    except ValueError:
        msg = f"argument {name} must be a date YYYY-MM-DD"
        raise ToolInputError(msg) from None


def window(args: Mapping[str, JsonValue]) -> tuple[datetime.date, datetime.date] | None:
    """U05-42 precondition: both or neither, `start <= end`, span <= 1,100 days."""
    start, end = _date(args, "start"), _date(args, "end")
    if start is None and end is None:
        return None
    if start is None or end is None:
        msg = "start and end must both be set or both be null"
        raise ToolInputError(msg)
    if start > end:
        msg = "start must not be after end"
        raise ToolInputError(msg)
    if (end - start).days > MAX_WINDOW_DAYS:
        msg = f"window longer than {MAX_WINDOW_DAYS} days (36 months)"
        raise ToolInputError(msg, hint=TIMEOUT_HINT)
    return start, end


def catalog_line(entry: Mapping[str, object]) -> str:
    """`name — unit, better, grains; description` of one `catalog.describe()` entry."""
    grains = entry["grains"]
    joined = "/".join(map(str, grains)) if isinstance(grains, list) else ""
    head = f"{entry['name']} — {entry['unit']}, {entry['better']}, {joined}"
    return f"{head}; {ws.one_line(entry['description'])}"


def unknown_metric(exc: ToolInputError, catalog: MetricCatalog) -> ToolResult:
    """Error result: the `from_error` text, then one line per enabled entry, <= 12,000."""
    base = ToolResult.from_error(exc)
    content = base.content
    for line in (catalog_line(d) for d in catalog.describe() if d["enabled"]):
        if len(content) + 1 + len(line) > TOOL_CONTENT_MAX_CHARS:
            break
        content += "\n" + line
    return ToolResult(ok=False, content=content, error=base.error)


def _run(
    ctx: ToolContext,
    args: tuple[str, str, list[str] | None, str],
    filters: Mapping[str, object] | None,
    win: tuple[datetime.date, datetime.date] | None,
) -> MetricResult:
    """Step 3: `compute_metric` on this task's cursor, interrupted after `timeout_s`."""
    name, entity_type, entity_ids, period = args
    cur = cast("duckdb.DuckDBPyConnection", ctx.warehouse.cursor())
    fired = threading.Event()

    def interrupt() -> None:
        fired.set()
        cur.interrupt()

    timeout_s = ctx.sql_limits.timeout_s
    timer = threading.Timer(timeout_s, interrupt)
    timer.daemon = True
    timer.start()
    try:
        kind, grain = cast("EntityType", entity_type), cast("Period", period)
        return compute_metric(name, kind, entity_ids, grain, filters, window=win, con=cur)
    except (HernessError, duckdb.Error) as exc:
        if fired.is_set():  # our timer is the only source of interrupts
            msg = f"timeout after {timeout_s}s"
            raise QueryError(msg, hint=TIMEOUT_HINT) from exc
        raise
    finally:
        timer.cancel()


def _record(ctx: ToolContext, mr: MetricResult, duration_ms: int) -> None:
    """Step 4: the evidence row built from the `MetricResult`, and its use."""
    ev = Evidence(
        query_id=mr.query_id,
        run_id=ctx.run_id,
        build_id=mr.build_id,
        sql=normalize_sql(mr.sql),
        params=mr.params,
        result_hash=mr.result_hash,
        row_count=mr.row_count,
        result_sample=mr.result_sample,
        executed_at=clock.now(),
        duration_ms=duration_ms,
    )
    use = (ev.query_id, ctx.run_id, ctx.task_id, clock.now())
    retry_call("sqlite_write", ctx.ops.record_evidence, ev)
    retry_call("sqlite_write", ctx.ops.record_evidence_use, *use)


def _content(mr: MetricResult, line: str) -> tuple[str, int]:
    """Step 5: header, catalog line, then the rows under the `format_result` cell rules."""
    header = (
        f"query_id={mr.query_id} metric={mr.metric} unit={mr.unit} better={mr.better}"
        f" period={mr.period} rows={mr.row_count}"
    )
    rows = [tuple(getattr(r, c) for c in ROW_COLUMNS) for r in mr.rows]
    table = RecordedResult(
        mr.query_id, list(ROW_COLUMNS), list(_ROW_TYPES), rows, mr.row_count, False, True
    )
    prefix = f"{header}\n{line}\n"
    text, shown = format_result(table, max_chars=TOOL_CONTENT_MAX_CHARS - len(prefix))
    return prefix + text, shown


class GetMetric:
    """`get_metric`: one catalog metric through spec 04 `compute_metric` (U05-42)."""

    name = "get_metric"
    description = (
        "Compute one catalog metric for an entity type and period (week, month, quarter)."
        " Optional entity_ids, a start/end date window (both or neither) and filters."
    )
    input_schema: dict[str, JsonValue] = ws.strict_schema(
        {
            "name": {"type": "string", "minLength": 1, "maxLength": 64},
            "entity_type": {"type": "string", "enum": list(ENTITY_TYPES)},
            "entity_ids": {"type": ["array", "null"], "items": _ID_ITEMS, "maxItems": 50},
            "period": {"type": "string", "enum": list(PERIODS)},
            "start": {"type": ["string", "null"], "format": "date"},
            "end": {"type": ["string", "null"], "format": "date"},
            "filters": _FILTERS_SCHEMA,
        }
    )

    def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        name = ws.text(kwargs, "name")
        entity_type = _choice(kwargs, "entity_type", ENTITY_TYPES)
        period = _choice(kwargs, "period", PERIODS)
        entity_ids, filters = _entity_ids(kwargs), _filters(kwargs)
        win = window(kwargs)
        catalog = catalog_from_config()
        try:
            metric = catalog.get(name)
        except ToolInputError as exc:
            return unknown_metric(exc, catalog)
        started = clock.monotonic()
        request = (metric.name, entity_type, entity_ids, period)
        mr = _run(ctx, request, filters, win)
        if mr.build_id != ctx.build_id:
            msg = f"metric build {mr.build_id} is not the task build {ctx.build_id}"
            raise ConfigError(msg)
        _record(ctx, mr, round((clock.monotonic() - started) * 1000))
        line = catalog_line(next(d for d in catalog.describe() if d["name"] == metric.name))
        content, shown = _content(mr, line)
        return ToolResult(
            ok=True,
            content=content,
            data=mr.model_dump(mode="json", exclude={"sql"}),
            query_ids=[mr.query_id],
            row_count=mr.row_count,
            truncated=shown < mr.row_count,
        )
