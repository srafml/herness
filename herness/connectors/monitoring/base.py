"""Monitoring base: adapter protocol, fan-out connector, row builders, day helpers (impl 01
U01-78 to U01-81; design 01 §3.3, §4.2). No network call or HTTP client here: adapters read
through ``herness.connectors.http`` (T01-14). Streams are keyed ``monitoring:<tool>`` (R-62)
with the tool checked against ``TOOLS``; ``source_tool`` is that validated name, never row
data. Rows go through ``RowBatcher``, so keys pass ``record_id`` validation (TH01-05)."""

from __future__ import annotations

import datetime
import json
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from itertools import chain
from typing import Final, Protocol, TypedDict, cast

import pyarrow as pa
import pyarrow.compute as pc

from herness.connectors.base import DEFAULT_BATCH_ROWS, METADATA_FIELDS
from herness.connectors.rows import RowBatcher
from herness.connectors.settings import MonitoringSettings
from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.registry import register

__all__ = [
    "EVENT_COLUMNS", "METRIC_COLUMNS", "TOOLS", "EventRow", "MetricRow", "MonitoringAdapter",
    "MonitoringConnector", "complete_days", "event_batch", "floor_day", "metric_batch",
]  # fmt: skip

TOOLS: Final = ("prometheus", "datadog", "splunk", "dynatrace")
EVENT_COLUMNS: Final = (
    "source_tool", "event_key", "ts", "service", "host", "severity_raw", "title", "status",
    "dedup_key", "end_ts", "incident_ref",
)  # fmt: skip
METRIC_COLUMNS: Final = ("source_tool", "date", "service", "metric_name", "value", "unit")

_SOURCE: Final = "monitoring"
_ENTITIES: Final = ("event", "metric_daily")
_WATERMARK_FIELDS: Final[Mapping[str, str]] = {"event": "ts", "metric_daily": "date"}
_EVENT_TEXT: Final = ("service", "host", "severity_raw", "title", "status", "dedup_key")
_UTC = datetime.UTC
_DAY: Final = datetime.timedelta(days=1)

type _DT = datetime.datetime

type _Item = tuple[str, datetime.datetime, str, dict[str, str | None]]


class EventRow(TypedDict):
    """One event or problem of a tool (U01-80); ``ts`` and ``end_ts`` aware."""

    event_key: str
    ts: datetime.datetime
    service: str | None
    host: str | None
    severity_raw: str | None
    title: str | None
    status: str | None
    dedup_key: str | None
    end_ts: datetime.datetime | None
    incident_ref: str | None
    payload: str


class MetricRow(TypedDict):
    """One daily aggregate of a metric for a service (U01-80)."""

    date: datetime.date
    service: str
    metric_name: str
    value: float | None
    unit: str
    payload: str


class MonitoringAdapter(Protocol):
    """One monitoring tool behind the ``monitoring`` connector (U01-78, design 01 §3.3).

    Rows come from ``event_batch`` / ``metric_batch`` with ``source_tool = tool``; events
    and daily aggregates only, never raw series. One instance per thread."""

    tool: str

    def check(self) -> None:
        """One cheap authenticated read."""
        ...

    def events(self, since: _DT, until: _DT) -> Iterator[pa.RecordBatch]:
        """Event rows with ``ts`` in ``[since, until)`` as the tool filters them; any order."""
        ...

    def daily_metrics(self, since: _DT, until: _DT) -> Iterator[pa.RecordBatch]:
        """Metric rows for the complete UTC days of ``complete_days(since, until)`` only."""
        ...


def _aware(value: object) -> bool:
    return isinstance(value, datetime.datetime) and value.utcoffset() is not None


def floor_day(ts: datetime.datetime) -> datetime.datetime:
    """00:00:00 UTC of ``ts``'s UTC date (U01-81); naive input raises ``ConfigError``."""
    if not _aware(ts):
        msg = "floor_day needs a timezone-aware datetime"
        raise ConfigError(msg, source=_SOURCE)
    day = ts.astimezone(_UTC)
    return datetime.datetime(day.year, day.month, day.day, tzinfo=_UTC)


def complete_days(since: _DT, until: _DT) -> tuple[_DT, _DT]:
    """``(floor_day(since), floor_day(until))``: the half-open range of whole UTC days that
    ended before ``until``; empty when the first is not before the second (U01-81)."""
    return floor_day(since), floor_day(until)


class _RowError(Exception):
    """A row precondition failed; turned into ``SchemaViolation`` without the row value."""


def _violation(tool: str) -> SchemaViolation:
    known = tool if tool in TOOLS else "unknown"
    return SchemaViolation("monitoring row", source=_SOURCE, tool=known)


def _text(value: object, *, null: bool = True) -> str | None:
    """``value`` when it is a string (or ``None`` and ``null``), else a row error."""
    if not (isinstance(value, str) or (null and value is None)):
        raise _RowError
    return value


def _str(value: object, *, empty: bool = True) -> str:
    text = cast("str", _text(value, null=False))
    if not (empty or text):
        raise _RowError
    return text


def _key(value: object) -> str:
    return _str(value, empty=False)


def _iso(value: object) -> str:
    if not _aware(value):
        raise _RowError
    return cast("datetime.datetime", value).astimezone(_UTC).isoformat().replace("+00:00", "Z")


def _event_item(tool: str, row: Mapping[str, object]) -> _Item:
    key = _key(row.get("event_key"))
    ts, end = row.get("ts"), row.get("end_ts")
    fields: dict[str, str | None] = {"source_tool": tool, "event_key": key, "ts": _iso(ts)}
    fields |= {name: _text(row.get(name)) for name in _EVENT_TEXT}
    fields["end_ts"] = None if end is None else _iso(end)
    fields["incident_ref"] = _text(row.get("incident_ref"))
    return f"{tool}:{key}", cast("datetime.datetime", ts), _str(row.get("payload")), fields


def _value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _RowError
    number = float(value)
    return json.dumps(number) if math.isfinite(number) else None


def _metric_item(tool: str, row: Mapping[str, object]) -> _Item:
    day = row.get("date")
    if not isinstance(day, datetime.date) or isinstance(day, datetime.datetime):
        raise _RowError
    service, name = _key(row.get("service")), _key(row.get("metric_name"))
    fields: dict[str, str | None] = {
        "source_tool": tool,
        "date": day.isoformat(),
        "service": service,
        "metric_name": name,
        "value": _value(row.get("value")),
        "unit": _str(row.get("unit")),
    }
    end = datetime.datetime(day.year, day.month, day.day, tzinfo=_UTC) + _DAY
    key = f"{tool}|{name}|{service}|{day.isoformat()}"
    return key, end, _str(row.get("payload")), fields


def _build[R](
    tool: str, entity: str, rows: Sequence[R], item: Callable[[str, R], _Item], at: _DT, limit: int
) -> pa.RecordBatch:
    """Validated rows as one batch: metadata columns, then the entity's column tuple."""
    if tool not in TOOLS or not 1 <= len(rows) <= limit or not _aware(at):
        raise _violation(tool)
    columns = EVENT_COLUMNS if entity == "event" else METRIC_COLUMNS
    batcher = RowBatcher(_SOURCE, entity, batch_rows=len(rows), columns=columns, clock=lambda: at)
    try:
        for row in rows:
            source_key, updated_at, payload, fields = item(tool, row)
            batch = batcher.add(source_key, updated_at, payload, fields)
    except (_RowError, SchemaViolation, AttributeError, OverflowError):  # 9999-12-31 + 1 day
        raise _violation(tool) from None
    return cast("pa.RecordBatch", batch)  # the last add fills the buffer and flushes


def event_batch(
    tool: str, rows: Sequence[EventRow], *, fetched_at: _DT, batch_rows: int = DEFAULT_BATCH_ROWS
) -> pa.RecordBatch:
    """Lake rows for ``monitoring/event`` (U01-80): ``_source_key = f"{tool}:{event_key}"``,
    ``_source_updated_at = ts``; ``ts``/``end_ts`` ISO-8601 with ``Z``. A violated
    precondition raises ``SchemaViolation("monitoring row")``."""
    return _build(tool, "event", rows, _event_item, fetched_at, batch_rows)


def metric_batch(
    tool: str, rows: Sequence[MetricRow], *, fetched_at: _DT, batch_rows: int = DEFAULT_BATCH_ROWS
) -> pa.RecordBatch:
    """Lake rows for ``monitoring/metric_daily`` (U01-80): ``_source_key`` =
    ``<tool>|<metric_name>|<service>|<date>``, ``_source_updated_at`` = ``date`` + 1 day at
    00:00 UTC; ``value`` as ``json.dumps(float)``, ``None`` for NaN or infinite."""
    return _build(tool, "metric_daily", rows, _metric_item, fetched_at, batch_rows)


def _same(batch: pa.RecordBatch, column: str, value: str) -> bool:
    col = batch.column(column)  # a null never matches: null metadata is rejected
    return col.null_count == 0 and bool(pc.all(pc.equal(col, value)).as_py())


def _checked(tool: str, entity: str, batches: Iterator[pa.RecordBatch]) -> Iterator[pa.RecordBatch]:
    """Pass adapter batches on after checking they are ``monitoring/<entity>`` rows of ``tool``."""
    needed = (*METADATA_FIELDS, "source_tool")
    for batch in batches:
        names = batch.schema.names
        ok = all(name in names for name in needed) and _same(batch, "_source", _SOURCE)
        ok = ok and _same(batch, "_entity", entity) and _same(batch, "source_tool", tool)
        if not ok:
            raise _violation(tool)
        yield batch


@register("connector", "monitoring")
class MonitoringConnector:
    """Fan-out connector over the enabled adapters (U01-79): ``Connector`` and
    ``SupportsToolStreams``, not ``SupportsKeyListing``. One instance per thread."""

    name: str = _SOURCE

    def __init__(
        self,
        settings: MonitoringSettings,
        *,
        adapters: Sequence[MonitoringAdapter],
        clock: Callable[[], datetime.datetime] = clock.now,  # default: module; body: parameter
    ) -> None:
        if not adapters:
            msg = "monitoring needs at least one enabled adapter"
            raise ConfigError(msg, source=_SOURCE)
        by_tool: dict[str, MonitoringAdapter] = {}
        for adapter in adapters:
            tool = getattr(adapter, "tool", None)
            if tool not in TOOLS or tool in by_tool:
                msg = "unknown or duplicate monitoring adapter tool"
                raise ConfigError(msg, source=_SOURCE)
            by_tool[cast("str", tool)] = adapter
        self._settings, self._clock, self._adapters = settings, clock, by_tool
        self.entities: tuple[str, ...] = tuple(e for e in _ENTITIES if e in settings.entities)

    def _adapter(self, tool: str) -> MonitoringAdapter:
        adapter = self._adapters.get(tool)
        if adapter is None:
            msg = "unknown monitoring tool"
            raise ConfigError(msg, source=_SOURCE, tool=tool if tool in TOOLS else "unknown")
        return adapter

    def _entity(self, entity: str) -> str:
        if entity not in self.entities:
            msg = f"unknown monitoring entity {entity[:64]}"
            raise ConfigError(msg, source=_SOURCE)
        return entity

    def watermark_field(self, entity: str) -> str:
        """``ts`` for ``event``, ``date`` for ``metric_daily``."""
        return _WATERMARK_FIELDS[self._entity(entity)]

    def check(self) -> None:
        """Call ``check()`` on every adapter in order; the first error propagates."""
        for adapter in self._adapters.values():
            adapter.check()

    def tools(self) -> tuple[str, ...]:
        """The enabled tool names in config order (U01-18)."""
        return tuple(self._adapters)

    def stream_key(self, tool: str) -> str:
        """``f"monitoring:{tool}"``: breaker key, watermark and slice source (U01-18)."""
        self._adapter(tool)
        return f"{_SOURCE}:{tool}"

    def sync_tool(
        self, tool: str, entity: str, since: _DT | None, until: _DT
    ) -> Iterator[pa.RecordBatch]:
        """One tool's rows of ``entity`` in ``[since, until)``; ``since=None`` starts at the
        entity's backfill start. Tool, entity and bounds are checked before any read."""
        adapter, entity = self._adapter(tool), self._entity(entity)
        if since is None:
            since = self._settings.backfill_for(entity).resolve_start(until)
        if not (_aware(since) and _aware(until)):
            msg = "monitoring sync needs timezone-aware datetimes"
            raise ConfigError(msg, source=_SOURCE)
        if since >= until:
            return iter(())
        read = adapter.events if entity == "event" else adapter.daily_metrics
        return _checked(tool, entity, read(since, until))

    def sync(
        self, entity: str, since: _DT | None, until: _DT | None = None
    ) -> Iterator[pa.RecordBatch]:
        """``sync_tool`` chained over all tools (``until=None`` → the clock); for protocol
        checks, not used by the runner."""
        self._entity(entity)
        end = self._clock() if until is None else until
        return chain.from_iterable(self.sync_tool(t, entity, since, end) for t in self.tools())
