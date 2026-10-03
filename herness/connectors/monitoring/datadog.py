"""Datadog adapter (impl 01 U01-83; design 01 §5.9): events search and daily rollups.

Events page by cursor only: later requests repeat the settings-built body with the
``meta.page.after`` cursor, which passes ``CursorGuard`` (TH01-04); nothing else from a
response reaches a request. Metrics ask ``/api/v1/query`` for ``<query>.rollup(<agg>, 86400)``
over complete UTC days. ``DD-API-KEY`` and ``DD-APPLICATION-KEY`` come from ``build_auth`` and
reach only ``base_url`` (U01-63). Event field paths follow the §13 V-4 default.
"""

from __future__ import annotations

import datetime
import functools
import json
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Final

import pyarrow as pa

from herness.connectors.base import DEFAULT_BATCH_ROWS
from herness.connectors.http import CursorGuard, SourceHttp
from herness.connectors.monitoring.base import (
    EventRow,
    MetricRow,
    complete_days,
    event_batch,
    metric_batch,
)
from herness.connectors.monitoring.prometheus import source_http
from herness.connectors.rows import parse_source_timestamp
from herness.connectors.settings import MetricQuery, MonitoringAdapterSettings
from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.registry import register

__all__ = ["DatadogAdapter"]

_TOOL: Final = "datadog"
_VALIDATE: Final = "/api/v1/validate"
_EVENTS: Final = "/api/v2/events/search"
_QUERY: Final = "/api/v1/query"
_DAY_S: Final = 86_400
_UTC: Final = datetime.UTC

type _DT = datetime.datetime


def _bad(msg: str) -> SchemaViolation:
    return SchemaViolation(msg, source="monitoring", tool=_TOOL)


def _iso(ts: _DT) -> str:
    return ts.astimezone(_UTC).isoformat().replace("+00:00", "Z")


def _obj(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _page(body: object) -> tuple[list[Any], str | None]:
    """``(data, meta.page.after)``; a non-list ``data`` or a non-string cursor is a shape
    error; an absent or ``null`` cursor ends paging."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        msg = "bad events response"
        raise _bad(msg)
    after = _obj(_obj(_obj(body).get("meta")).get("page")).get("after")
    if after is None or (isinstance(after, str) and after):
        return data, after
    msg = "bad page cursor"
    raise _bad(msg)


def _service(inner: Mapping[str, Any], tags: object) -> Any:  # noqa: ANN401 - JSON value
    """``inner.service``, else the value of the first ``service:<v>`` tag, else ``None``."""
    service = inner.get("service")
    if service is None and isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str) and tag.startswith("service:"):
                return tag.removeprefix("service:")
    return service


def _event(item: object) -> EventRow:
    """One events-search item as an event row (V-4 default field paths)."""
    key = item.get("id") if isinstance(item, dict) else None
    if not (isinstance(key, str) and key):
        msg = "event without id"
        raise _bad(msg)
    attrs = _obj(_obj(item).get("attributes"))
    inner = _obj(attrs.get("attributes"))
    return EventRow(
        event_key=key,
        ts=parse_source_timestamp(attrs.get("timestamp"), field="timestamp"),
        service=_service(inner, attrs.get("tags")),  # a non-string is refused by event_batch
        host=inner.get("host"),
        severity_raw=inner.get("priority"),
        title=inner.get("title"),
        status=inner.get("status"),
        dedup_key=inner.get("aggregation_key"),
        end_ts=None,
        incident_ref=None,
        payload=json.dumps(item),
    )


def _series_list(body: object) -> list[Any]:
    """The ``series`` of a query body; ``status == "error"`` or another shape is an error."""
    series = body.get("series") if isinstance(body, dict) else None
    if not isinstance(series, list) or _obj(body).get("status") == "error":
        msg = "bad query response"
        raise _bad(msg)
    return series


def _point(point: object) -> tuple[datetime.date, Any]:
    """``[ms, v]`` → ``(UTC(ms / 1000).date(), v)``; else a shape error."""
    if isinstance(point, list) and len(point) == 2:  # noqa: PLR2004 - [ms, v]
        ms, value = point
        if isinstance(ms, int | float) and not isinstance(ms, bool):
            try:
                return datetime.datetime.fromtimestamp(ms / 1000, _UTC).date(), value
            except (ValueError, OverflowError, OSError):
                pass
    msg = "bad point"
    raise _bad(msg)


def _series_rows(
    query: MetricQuery, series: object, first: datetime.date, last: datetime.date
) -> Iterator[MetricRow]:
    """Rows of one series with a date in ``[first, last)``; its ``<service_label>:`` tag names
    the service."""
    tags = series.get("tag_set") if isinstance(series, dict) else None
    points = _obj(series).get("pointlist")
    prefix = f"{query.service_label}:"
    listed = tags if isinstance(tags, list) else []
    found = [t for t in listed if isinstance(t, str) and t.startswith(prefix)]
    if not found or not isinstance(points, list):
        msg = "series without service tag"
        raise _bad(msg)
    service = found[0].removeprefix(prefix)
    for point in points:
        day, value = _point(point)
        if first <= day < last:
            payload = json.dumps({"query": query.name, "tag_set": tags, "point": point})
            yield MetricRow(
                date=day,
                service=service,
                metric_name=query.name,
                value=value,  # null → None; a non-number is refused by metric_batch
                unit=query.unit,
                payload=payload,
            )


@register("monitoring_adapter", _TOOL)
class DatadogAdapter:
    """Datadog events and daily rollups (U01-83). One instance per thread."""

    tool: str = _TOOL

    def __init__(
        self,
        settings: MonitoringAdapterSettings,
        *,
        clock: Callable[[], _DT] = clock.now,  # default: module; body: parameter
        http: SourceHttp | None = None,
        batch_rows: int = DEFAULT_BATCH_ROWS,
    ) -> None:
        self._settings, self._clock, self._given = settings, clock, http
        self._batch_rows = batch_rows

    @functools.cached_property
    def _http(self) -> SourceHttp:
        """Built on first use, so construction resolves no secret and opens no client."""
        given = self._given
        return given if given is not None else source_http(self._settings, _TOOL, self._clock)

    def check(self) -> None:
        """``GET /api/v1/validate``; anything but ``{"valid": true}`` is a shape error."""
        body = self._http.get_json(_VALIDATE).body
        if not (isinstance(body, dict) and body.get("valid") is True):
            msg = "bad validate response"
            raise _bad(msg)

    def events(self, since: _DT, until: _DT) -> Iterator[pa.RecordBatch]:
        """Events of ``[since, until)`` page by page; nothing when ``event_query`` is unset."""
        query = self._settings.event_query
        if query is None:
            return
        limit = self._settings.page_size
        body: dict[str, Any] = {
            "filter": {"query": query, "from": _iso(since), "to": _iso(until)},
            "sort": "timestamp",
            "page": {"limit": limit},
        }
        guard, rows = CursorGuard(), []
        while True:
            data, after = _page(self._http.post_json(_EVENTS, json_body=body).body)
            for item in data:
                rows.append(_event(item))
                if len(rows) >= self._batch_rows:
                    yield self._events(rows)
                    rows = []
            if not data or after is None:
                break
            guard.step(after)
            body = body | {"page": {"limit": limit, "cursor": after}}
        if rows:
            yield self._events(rows)

    def daily_metrics(self, since: _DT, until: _DT) -> Iterator[pa.RecordBatch]:
        """Per query one daily rollup over ``[a, b - 1 s]`` (epoch s); rows for the complete
        days ``a <= date < b`` only."""
        first, last = complete_days(since, until)
        if first >= last:
            return
        rows: list[MetricRow] = []
        for query in self._settings.metric_queries:
            for row in self._query_rows(query, first, last):
                rows.append(row)
                if len(rows) >= self._batch_rows:
                    yield self._metrics(rows)
                    rows = []
        if rows:
            yield self._metrics(rows)

    def _query_rows(self, query: MetricQuery, first: _DT, last: _DT) -> Iterator[MetricRow]:
        if query.agg is None:  # settings require it for datadog (U01-09)
            msg = "datadog metric query needs agg"
            raise ConfigError(msg, source="monitoring", tool=_TOOL)
        start = int(first.timestamp())
        params = {
            "from": start,
            "to": int(last.timestamp()) - 1,
            "query": f"{query.query}.rollup({query.agg}, {_DAY_S})",
        }
        for series in _series_list(self._http.get_json(_QUERY, params=params).body):
            yield from _series_rows(query, series, first.date(), last.date())

    def _events(self, rows: list[EventRow]) -> pa.RecordBatch:
        at = self._clock()
        return event_batch(_TOOL, rows, fetched_at=at, batch_rows=self._batch_rows)

    def _metrics(self, rows: list[MetricRow]) -> pa.RecordBatch:
        at = self._clock()
        return metric_batch(_TOOL, rows, fetched_at=at, batch_rows=self._batch_rows)
