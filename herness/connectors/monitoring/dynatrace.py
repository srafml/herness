"""Dynatrace adapter (impl 01 U01-85; design 01 §5.9): problems and daily metrics.

Problems page with ``nextPageKey``: later requests send that key alone and every key passes
``CursorGuard`` (TH01-04); nothing else from a response ever reaches a request. Metrics use
``resolution=1d`` and must fit one page. A daily timestamp marks the end of its slot, so the
bucket date is the UTC date of the timestamp minus one day (§13 V-5 default). The
``Authorization: Api-Token`` header comes from ``build_auth`` and reaches only ``base_url``.
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
from herness.connectors.settings import MetricQuery, MonitoringAdapterSettings
from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.core.registry import register

__all__ = ["DynatraceAdapter"]

_TOOL: Final = "dynatrace"
_PROBLEMS: Final = "/api/v2/problems"
_METRICS: Final = "/api/v2/metrics/query"
_UTC: Final = datetime.UTC
_EPOCH: Final = datetime.datetime(1970, 1, 1, tzinfo=_UTC)
_MS: Final = datetime.timedelta(milliseconds=1)
_DAY: Final = datetime.timedelta(days=1)
_OPEN_END: Final = -1  # endTime of a problem that has not ended

type _DT = datetime.datetime


def _bad(msg: str) -> SchemaViolation:
    return SchemaViolation(msg, source="monitoring", tool=_TOOL)


def _ms(ts: _DT) -> int:
    """Epoch milliseconds of an aware datetime."""
    return (ts - _EPOCH) // _MS


def _at(value: object) -> _DT:
    """An epoch-milliseconds integer as an aware UTC datetime; else a shape error."""
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return _EPOCH + value * _MS
        except OverflowError:
            pass
    msg = "bad timestamp"
    raise _bad(msg)


def _member(body: object, name: str) -> tuple[Mapping[str, Any], list[Any]]:
    """``(body, body[name])`` when ``body`` is an object and that member a list; else a
    shape error."""
    value = body.get(name) if isinstance(body, dict) else None
    if not isinstance(value, list):
        msg = f"bad {name} response"
        raise _bad(msg)
    return body, value  # type: ignore[return-value]  # a dict: checked above


def _next_key(body: Mapping[str, Any]) -> str | None:
    """``nextPageKey``: absent or ``null`` ends paging; a non-string is a shape error."""
    key = body.get("nextPageKey")
    if key is None or (isinstance(key, str) and key):
        return key
    msg = "bad nextPageKey"
    raise _bad(msg)


def _name(entity: object) -> Any:  # noqa: ANN401 - a JSON value, checked by event_batch
    return entity.get("name") if isinstance(entity, dict) else None


def _service(problem: Mapping[str, Any]) -> Any:  # noqa: ANN401 - as above
    """``rootCauseEntity.name``, else the first ``affectedEntities[].name``, else ``None``."""
    name = _name(problem.get("rootCauseEntity"))
    affected = problem.get("affectedEntities")
    if name is None and isinstance(affected, list) and affected:
        name = _name(affected[0])
    return name


def _event(problem: object) -> EventRow:
    """One problem as an event row; ``end_ts`` is ``None`` for ``endTime`` -1 or absent."""
    if not isinstance(problem, dict):
        msg = "bad problem"
        raise _bad(msg)
    end = problem.get("endTime")
    return EventRow(
        event_key=problem.get("problemId", ""),  # bad or empty: refused by event_batch
        ts=_at(problem.get("startTime")),
        service=_service(problem),
        host=None,
        severity_raw=problem.get("severityLevel"),
        title=problem.get("title"),
        status=problem.get("status"),
        dedup_key=problem.get("displayId"),
        end_ts=None if end is None or end == _OPEN_END else _at(end),
        incident_ref=None,
        payload=json.dumps(problem),
    )


def _series(
    query: MetricQuery, data: object, first: datetime.date, last: datetime.date
) -> Iterator[MetricRow]:
    """The rows of one ``result[].data[]`` entry with a bucket date in ``[first, last)``."""
    dims = data.get("dimensionMap") if isinstance(data, dict) else None
    service = dims.get(query.service_dimension) if isinstance(dims, dict) else None
    if service is None:
        msg = "series without service dimension"
        raise _bad(msg)
    stamps, values = _member(data, "timestamps")[1], _member(data, "values")[1]
    if len(stamps) != len(values):
        msg = "bad values response"
        raise _bad(msg)
    for stamp, value in zip(stamps, values, strict=True):
        day = (_at(stamp) - _DAY).date()  # the timestamp ends the slot (V-5)
        if first <= day < last:
            point = {"query": query.name, "dimensionMap": dims, "timestamp": stamp, "value": value}
            yield MetricRow(
                date=day,
                service=service,  # a non-string is refused by metric_batch
                metric_name=query.name,
                value=value,  # null → None; a non-number is refused by metric_batch
                unit=query.unit,
                payload=json.dumps(point),
            )


@register("monitoring_adapter", _TOOL)
class DynatraceAdapter:
    """Dynatrace problems and daily metrics (U01-85). One instance per thread."""

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

    def _get(self, path: str, params: Mapping[str, Any]) -> object:
        return self._http.get_json(path, params=params).body

    def check(self) -> None:
        """``GET /api/v2/problems?pageSize=1&from=now-5m``."""
        _member(self._get(_PROBLEMS, {"pageSize": 1, "from": "now-5m"}), "problems")

    def events(self, since: _DT, until: _DT) -> Iterator[pa.RecordBatch]:
        """Problems of ``[since, until)``, page by page; re-fetched problems land as new
        versions (OPEN → CLOSED)."""
        params: dict[str, Any] = {"from": _ms(since), "to": _ms(until)}
        params["pageSize"] = self._settings.page_size
        if self._settings.event_query is not None:
            params["problemSelector"] = self._settings.event_query
        guard, rows = CursorGuard(), []
        while True:
            body, problems = _member(self._get(_PROBLEMS, params), "problems")
            for problem in problems:
                rows.append(_event(problem))
                if len(rows) >= self._batch_rows:
                    yield self._events(rows)
                    rows = []
            key = _next_key(body)
            if key is None:
                break
            guard.step(key)
            params = {"nextPageKey": key}  # later pages send the key alone
        if rows:
            yield self._events(rows)

    def daily_metrics(self, since: _DT, until: _DT) -> Iterator[pa.RecordBatch]:
        """Per query one ``resolution=1d`` request over ``[a, b)`` (epoch ms); rows for the
        complete days ``a <= date < b`` only; ``nextPageKey`` is a shape error."""
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
        params = {"metricSelector": query.query, "resolution": "1d"}
        body, results = _member(
            self._get(_METRICS, params | {"from": _ms(first), "to": _ms(last)}), "result"
        )
        if body.get("nextPageKey") is not None:
            msg = "unexpected pagination"
            raise _bad(msg)
        for result in results:
            for data in _member(result, "data")[1]:
                yield from _series(query, data, first.date(), last.date())

    def _events(self, rows: list[EventRow]) -> pa.RecordBatch:
        at = self._clock()
        return event_batch(_TOOL, rows, fetched_at=at, batch_rows=self._batch_rows)

    def _metrics(self, rows: list[MetricRow]) -> pa.RecordBatch:
        at = self._clock()
        return metric_batch(_TOOL, rows, fetched_at=at, batch_rows=self._batch_rows)
