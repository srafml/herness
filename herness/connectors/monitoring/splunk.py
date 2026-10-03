"""Splunk adapter (impl 01 U01-84; design 01 §5.9): export searches, one JSON result per line.

Searches come only from SPL that passes ``validate_spl`` (checked again here, TH01-07) and
bound epoch-second windows; nothing from a response reaches a request. Each line is at most
1 MiB (``post_form_lines``) and each response at most the shared 64 MiB egress cap (TH01-04),
so the window is exported in UTC-day pieces, one streamed request each (spec note: the unit
says one request per query; paging keeps every response under the cap). The bearer token
comes from ``build_auth`` and reaches only ``base_url``.
"""

from __future__ import annotations

import datetime
import functools
import json
import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Final

import pyarrow as pa

from herness.connectors.base import DEFAULT_BATCH_ROWS
from herness.connectors.http import SourceHttp
from herness.connectors.monitoring.base import (
    EventRow,
    MetricRow,
    complete_days,
    event_batch,
    floor_day,
    metric_batch,
)
from herness.connectors.monitoring.prometheus import source_http
from herness.connectors.rows import parse_source_timestamp
from herness.connectors.settings import MetricQuery, MonitoringAdapterSettings, validate_spl
from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.registry import register

__all__ = ["SplunkAdapter"]

_TOOL: Final = "splunk"
_INFO: Final = "/services/server/info"
_EXPORT: Final = "/services/search/v2/jobs/export"
_UTC: Final = datetime.UTC
_EPOCH: Final = datetime.datetime(1970, 1, 1, tzinfo=_UTC)
_SECOND: Final = datetime.timedelta(seconds=1)
_DAY: Final = datetime.timedelta(days=1)
_FAILED: Final = frozenset({"ERROR", "FATAL"})
_NUMBER: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_TEXT: Final = ("service", "host", "severity_raw", "title", "status", "dedup_key")

type _DT = datetime.datetime


def _bad(msg: str) -> SchemaViolation:
    return SchemaViolation(msg, source="monitoring", tool=_TOOL)


def _search(spl: str) -> str:
    """``spl`` re-validated (``ConfigError`` when forbidden), prefixed ``search `` unless it
    starts with ``|``."""
    try:
        validate_spl(spl)
    except ValueError as exc:
        msg = f"forbidden SPL: {exc}"
        raise ConfigError(msg, source="monitoring", tool=_TOOL) from None
    return spl if spl.startswith("|") else "search " + spl


def _windows(since: _DT, until: _DT) -> Iterator[tuple[int, int]]:
    """``[since, until)`` as epoch-second windows cut at each UTC midnight (deterministic from
    the bounds alone); empty when ``since >= until``."""
    low = since
    while low < until:
        high = min(floor_day(low) + _DAY, until)
        yield (low - _EPOCH) // _SECOND, (high - _EPOCH) // _SECOND
        low = high


def _time(value: object, field: str) -> _DT:
    """A Splunk time: a number (or numeric string) is epoch seconds, else ISO-8601."""
    if isinstance(value, str) and _NUMBER.fullmatch(value):
        value = float(value)
    return parse_source_timestamp(value, field=field, epoch_unit="s")


def _results(lines: Iterator[dict[str, object]]) -> Iterator[Mapping[str, Any]]:
    """The final ``result`` objects of an export stream; an ``ERROR``/``FATAL`` message is a
    search error; preview lines and lines without ``result`` are skipped."""
    for line in lines:
        messages = line.get("messages")
        if isinstance(messages, list) and any(
            isinstance(m, dict) and m.get("type") in _FAILED for m in messages
        ):
            msg = "splunk search error"
            raise _bad(msg)
        result = line.get("result")
        if line.get("preview") is True or result is None:
            continue
        if not isinstance(result, dict):
            msg = "bad result"
            raise _bad(msg)
        yield result


def _event(result: Mapping[str, Any]) -> EventRow:
    key = result.get("event_key")
    if not (isinstance(key, str) and key):
        msg = "result without event_key"
        raise _bad(msg)
    end = result.get("end_ts")
    text = {name: result.get(name) for name in _TEXT}  # non-strings refused by event_batch
    return EventRow(
        event_key=key,
        ts=_time(result.get("_time"), "_time"),
        service=text["service"],
        host=text["host"],
        severity_raw=text["severity_raw"],
        title=text["title"],
        status=text["status"],
        dedup_key=text["dedup_key"],
        end_ts=None if end is None else _time(end, "end_ts"),
        incident_ref=result.get("incident_ref"),
        payload=json.dumps(result),
    )


def _metric(query: MetricQuery, result: Mapping[str, Any]) -> MetricRow:
    service, raw = result.get(query.service_label), result.get(query.value_field)
    if service is None:
        msg = "result without service field"
        raise _bad(msg)
    try:
        value = float(raw)  # type: ignore[arg-type]  # bad types raise below
    except (TypeError, ValueError):
        msg = "bad metric value"
        raise _bad(msg) from None
    return MetricRow(
        date=_time(result.get("_time"), "_time").date(),
        service=service,  # a non-string is refused by metric_batch
        metric_name=query.name,
        value=value,  # NaN and infinities become None in metric_batch
        unit=query.unit,
        payload=json.dumps({"query": query.name, "result": result}),
    )


@register("monitoring_adapter", _TOOL)
class SplunkAdapter:
    """Splunk export searches for events and daily metrics (U01-84). One instance per thread."""

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
        """``GET /services/server/info?output_mode=json``; a body without ``entry`` is a shape
        error."""
        body = self._http.get_json(_INFO, params={"output_mode": "json"}).body
        if not (isinstance(body, dict) and isinstance(body.get("entry"), list)):
            msg = "bad server info response"
            raise _bad(msg)

    def _export(self, spl: str, since: _DT, until: _DT) -> Iterator[Mapping[str, Any]]:
        """The results of ``spl`` over ``[since, until)``, one export request per UTC day."""
        search = _search(spl)
        for earliest, latest in _windows(since, until):
            data = {"search": search, "earliest_time": str(earliest)}
            data |= {"latest_time": str(latest), "output_mode": "json"}
            yield from _results(self._http.post_form_lines(_EXPORT, data=data))

    def events(self, since: _DT, until: _DT) -> Iterator[pa.RecordBatch]:
        """Event rows of ``event_query`` over ``[since, until)``; nothing when it is unset."""
        spl = self._settings.event_query
        if spl is None:
            return
        rows: list[EventRow] = []
        for result in self._export(spl, since, until):
            rows.append(_event(result))
            if len(rows) >= self._batch_rows:
                yield self._events(rows)
                rows = []
        if rows:
            yield self._events(rows)

    def daily_metrics(self, since: _DT, until: _DT) -> Iterator[pa.RecordBatch]:
        """Per query, rows over ``[a, b)`` of ``complete_days``; dates outside are dropped."""
        first, last = complete_days(since, until)
        rows: list[MetricRow] = []
        for query in self._settings.metric_queries:
            for result in self._export(query.query, first, last):
                row = _metric(query, result)
                if first.date() <= row["date"] < last.date():
                    rows.append(row)
                if len(rows) >= self._batch_rows:
                    yield self._metrics(rows)
                    rows = []
        if rows:
            yield self._metrics(rows)

    def _events(self, rows: list[EventRow]) -> pa.RecordBatch:
        at = self._clock()
        return event_batch(_TOOL, rows, fetched_at=at, batch_rows=self._batch_rows)

    def _metrics(self, rows: list[MetricRow]) -> pa.RecordBatch:
        at = self._clock()
        return metric_batch(_TOOL, rows, fetched_at=at, batch_rows=self._batch_rows)
