"""Prometheus and Mimir adapter (impl 01 U01-82; design 01 §5.9).

Daily aggregates only: ``query_range`` with ``step=86400`` over complete UTC days, in chunks of
at most 10,000 points per series. There is no alert history API, so ``events`` yields nothing
(alert load arrives as the metric ``alert_firing_minutes``). Queries and windows come from
validated settings and bound times only; responses are checked for shape (TH01-04). The Mimir
``X-Scope-OrgID`` header rides only on requests to paths under the configured ``base_url``.
"""

from __future__ import annotations

import datetime
import functools
import json
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Final

import pyarrow as pa

from herness.connectors.auth import build_auth
from herness.connectors.base import DEFAULT_BATCH_ROWS
from herness.connectors.http import SourceHttp, http_client
from herness.connectors.monitoring.base import MetricRow, complete_days, metric_batch
from herness.connectors.settings import MetricQuery, MonitoringAdapterSettings
from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.core.registry import register

__all__ = ["PrometheusAdapter", "source_http"]

_TOOL: Final = "prometheus"
_STEP_S: Final = 86_400  # the only step ever requested
_CHUNK_DAYS: Final = 10_000  # points per series per request, below the 11,000 limit
_DAY: Final = datetime.timedelta(days=1)
_UTC: Final = datetime.UTC
_QUERY: Final = "/api/v1/query"
_RANGE: Final = "/api/v1/query_range"

type _DT = datetime.datetime


def source_http(
    settings: MonitoringAdapterSettings, tool: str, clock: Callable[[], _DT]
) -> SourceHttp:
    """The adapter's page fetcher (impl 01 §3.16): the egress client of ``monitoring:<tool>``
    and the tool's per-request auth, which reaches only the configured origin (U01-63)."""
    key = f"monitoring:{tool}"
    client = http_client(settings, source=key, max_concurrency=settings.max_concurrency)
    auth = build_auth(
        settings.auth, source=tool, base_url=settings.base_url, token_client=client, clock=clock
    )
    return SourceHttp(client, breaker_key=key, auth=auth, clock=clock)


def _bad(msg: str) -> SchemaViolation:
    return SchemaViolation(msg, source="monitoring", tool=_TOOL)


def _chunks(start: _DT, end: _DT) -> Iterator[tuple[_DT, _DT]]:
    """``[start, end]`` (day-aligned, inclusive) in pieces of at most ``_CHUNK_DAYS`` days."""
    low = start
    while low <= end:
        high = min(low + (_CHUNK_DAYS - 1) * _DAY, end)
        yield low, high
        low = high + _DAY


def _matrix(body: object) -> list[Any]:
    """The series list of a successful ``matrix`` body; anything else is a shape error."""
    data = body.get("data") if isinstance(body, dict) else None
    result = data.get("result") if isinstance(data, dict) else None
    success = isinstance(body, dict) and body.get("status") == "success"
    matrix = isinstance(data, dict) and data.get("resultType") == "matrix"
    if not (success and matrix and isinstance(result, list)):
        msg = "bad query_range response"
        raise _bad(msg)
    return result


def _point(point: object) -> tuple[datetime.date, float, Any, Any]:
    """``[t, v]`` → the day ending at ``t`` (``UTC(t) - 1 day``), ``float(v)``, ``t``, ``v``."""
    t: Any = None
    v: Any = None
    if isinstance(point, list) and len(point) == 2:  # noqa: PLR2004 - [t, v]
        t, v = point
    try:
        if isinstance(t, bool) or not isinstance(t, int | float) or not isinstance(v, str):
            raise TypeError  # noqa: TRY301 - one shape error below
        day = (datetime.datetime.fromtimestamp(t, _UTC) - _DAY).date()
        return day, float(v), t, v
    except (TypeError, ValueError, OverflowError, OSError):
        msg = "bad sample"
        raise _bad(msg) from None


def _series_rows(query: MetricQuery, series: object) -> Iterator[MetricRow]:
    """The rows of one series; its ``service_label`` value names the service."""
    metric = series.get("metric") if isinstance(series, dict) else None
    values = series.get("values") if isinstance(series, dict) else None
    if not isinstance(metric, dict) or not isinstance(values, list):
        msg = "bad series"
        raise _bad(msg)
    service = metric.get(query.service_label)
    if service is None:
        msg = "series without service label"
        raise _bad(msg)
    for point in values:
        day, value, t, v = _point(point)
        payload = json.dumps({"query": query.name, "metric": metric, "t": t, "v": v})
        yield MetricRow(
            date=day,
            service=service,  # a non-string is refused by metric_batch
            metric_name=query.name,
            value=value,  # NaN and infinities become None in metric_batch
            unit=query.unit,
            payload=payload,
        )


@register("monitoring_adapter", _TOOL)
class PrometheusAdapter:
    """Prometheus and Mimir (U01-82): daily metrics, no events. One instance per thread."""

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
        tenant = settings.tenant
        self._headers: dict[str, str] = {} if tenant is None else {"X-Scope-OrgID": tenant}

    @functools.cached_property
    def _http(self) -> SourceHttp:
        """Built on first use, so construction resolves no secret and opens no client."""
        given = self._given
        return given if given is not None else source_http(self._settings, _TOOL, self._clock)

    def _get(self, path: str, params: Mapping[str, Any]) -> object:
        return self._http.get_json(path, params=params, headers=self._headers).body

    def check(self) -> None:
        """``GET /api/v1/query?query=vector(1)``; a non-success body is a shape error."""
        body = self._get(_QUERY, {"query": "vector(1)"})
        if not (isinstance(body, dict) and body.get("status") == "success"):
            msg = "bad query response"
            raise _bad(msg)

    def events(self, since: _DT, until: _DT) -> Iterator[pa.RecordBatch]:
        """Nothing: there is no alert history API (design 01 §5.9)."""
        del since, until
        return iter(())

    def daily_metrics(self, since: _DT, until: _DT) -> Iterator[pa.RecordBatch]:
        """One row per (query, service, complete day); the point at ``t`` covers the day
        ending at ``t``, so the request runs from ``a + 1 day`` to ``b``."""
        first, last = complete_days(since, until)
        start = first + _DAY
        if start > last:
            return
        rows: list[MetricRow] = []
        for query in self._settings.metric_queries:
            for low, high in _chunks(start, last):
                for row in self._query_rows(query, low, high):
                    rows.append(row)
                    if len(rows) >= self._batch_rows:
                        yield self._batch(rows)
                        rows = []
        if rows:
            yield self._batch(rows)

    def _query_rows(self, query: MetricQuery, low: _DT, high: _DT) -> Iterator[MetricRow]:
        """One ``query_range`` request (epoch seconds, ``step=86400``) and its rows."""
        start, end = int(low.timestamp()), int(high.timestamp())
        params = {"query": query.query, "start": start, "end": end, "step": _STEP_S}
        for series in _matrix(self._get(_RANGE, params)):
            yield from _series_rows(query, series)

    def _batch(self, rows: list[MetricRow]) -> pa.RecordBatch:
        at = self._clock()
        return metric_batch(_TOOL, rows, fetched_at=at, batch_rows=self._batch_rows)
