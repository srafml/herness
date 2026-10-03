"""UT01-82 Splunk adapter (T01-21, U01-84): streamed export searches, paged per UTC day."""

from __future__ import annotations

import datetime
import json
import urllib.parse
from typing import Any

import pytest
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.connectors._auth_data import store
from tests.unit.connectors._dd_splunk_data import SPLUNK_TOKEN, Lines, http, settings, stream, wire
from tests.unit.connectors._http_data import MIB
from tests.unit.connectors._prometheus_data import (
    FETCHED,
    UTC,
    Source,
    epoch,
    query,
    rows,
    stub_resilience,
)

from herness.connectors.monitoring.base import EVENT_COLUMNS, METRIC_COLUMNS
from herness.connectors.monitoring.splunk import SplunkAdapter
from herness.core import registry
from herness.core.errors import AuthError, ConfigError, SchemaViolation
from herness.core.resilience import ProcessState

pytestmark = pytest.mark.unit

SINCE = datetime.datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
UNTIL = datetime.datetime(2026, 3, 2, 8, 0, 30, 500_000, tzinfo=UTC)
D = datetime.date
EVENTS_SPL = "index=alerts | table event_key _time service title"
METRIC_SPL = "| tstats count where index=web by _time span=1d service"
DAY_S = 86_400


@pytest.fixture(autouse=True)
def _resilience(monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState) -> None:
    del reset_process_state
    stub_resilience(monkeypatch)


def _adapter(
    bodies: list[Any], *, batch_rows: int = 5000, **extra: Any
) -> tuple[SplunkAdapter, Lines]:
    source = Lines(bodies)
    base: dict[str, Any] = {
        "event_query": EVENTS_SPL,
        "metric_queries": [query("request_count", METRIC_SPL, value_field="count")],
    }
    cfg = settings("splunk", **(base | extra))
    adapter = SplunkAdapter(
        cfg, clock=lambda: FETCHED, http=http("splunk", source), batch_rows=batch_rows
    )
    return adapter, source


def _form(source: Lines, index: int) -> dict[str, str]:
    request = source.seen[index]
    assert (request.method, request.url.path) == ("POST", "/services/search/v2/jobs/export")
    return dict(urllib.parse.parse_qsl(request.content.decode()))


def _result(key: str, **extra: Any) -> dict[str, Any]:
    result = {"event_key": key, "_time": "2026-03-01T11:00:00.000+00:00", "service": "checkout"}
    return {"preview": False, "result": result | extra}


# --- the export stream -------------------------------------------------------------------


def test_ut01_82_preview_skipped_and_search_prefix_added() -> None:
    """UT01-82 export: the `search ` prefix is added, epoch-second bounds and JSON output
    are sent, preview lines and lines without `result` are skipped."""
    body = stream(
        {"preview": True, "result": {"event_key": "early", "_time": "1772362800"}},
        {"messages": [{"type": "INFO", "text": "info"}]},
        _result("e1"),
        {"lastrow": True},
    )
    adapter, source = _adapter([body, stream()])
    out = rows(list(adapter.events(SINCE, UNTIL)))
    assert [r["event_key"] for r in out] == ["e1"]
    assert _form(source, 0) == {
        "search": "search " + EVENTS_SPL,
        "earliest_time": str(int(SINCE.timestamp())),
        "latest_time": str(epoch(D(2026, 3, 2))),
        "output_mode": "json",
    }


def test_ut01_82_pipe_search_is_sent_unprefixed() -> None:
    """UT01-82 an SPL starting with `|` is sent as it is."""
    adapter, source = _adapter([stream()], event_query=METRIC_SPL)
    assert list(adapter.events(SINCE, UNTIL)) == []
    assert _form(source, 0)["search"] == METRIC_SPL


@pytest.mark.parametrize("kind", ["ERROR", "FATAL"])
def test_ut01_82_error_message_line_is_schema_violation(kind: str) -> None:
    """UT01-82 a `messages` entry of type ERROR or FATAL → SchemaViolation("splunk search
    error"), even after good lines."""
    body = stream(_result("e1"), {"messages": [{"type": kind, "text": "Error in 'search'"}]})
    adapter, _ = _adapter([body])
    with pytest.raises(SchemaViolation, match="splunk search error"):
        list(adapter.events(SINCE, UNTIL))


def test_ut01_82_two_mib_line_is_schema_violation() -> None:
    """UT01-82 a 2 MiB line passes the 1 MiB line cap → SchemaViolation (TH01-04)."""
    long_line = b'{"result": {"event_key": "' + b"x" * (2 * MIB) + b'"}}\n'
    adapter, _ = _adapter([[*stream(_result("e1")), long_line]])
    with pytest.raises(SchemaViolation, match="line too large"):
        list(adapter.events(SINCE, UNTIL))


def test_ut01_82_export_paged_per_utc_day() -> None:
    """UT01-82 paging (controller ruling, spec note): [since, until) is exported in pieces cut
    at UTC midnight, one request each, so no single response nears the 64 MiB cap; bounds
    are whole epoch seconds, contiguous and deterministic."""
    since = datetime.datetime(2026, 3, 1, 22, 0, 0, 900_000, tzinfo=UTC)
    until = datetime.datetime(2026, 3, 4, 1, 0, tzinfo=UTC)
    adapter, source = _adapter([stream(_result("e1")), stream(_result("e2")), stream()])
    assert len(rows(list(adapter.events(since, until)))) == 2
    windows = [
        (_form(source, i)["earliest_time"], _form(source, i)["latest_time"]) for i in range(4)
    ]
    assert windows == [
        (str(int(since.timestamp())), str(epoch(D(2026, 3, 2)))),
        (str(epoch(D(2026, 3, 2))), str(epoch(D(2026, 3, 3)))),
        (str(epoch(D(2026, 3, 3))), str(epoch(D(2026, 3, 4)))),
        (str(epoch(D(2026, 3, 4))), str(int(until.timestamp()))),
    ]
    assert len(source.seen) == 4


# --- events ------------------------------------------------------------------------------


def test_ut01_82_event_field_mapping() -> None:
    """UT01-82 events: event_key, `_time` (ISO, epoch number or numeric string), optional
    text fields and end_ts; payload is the result."""
    full = _result(
        "e1",
        host="web-1",
        severity_raw="high",
        title="Disk full",
        status="open",
        dedup_key="d1",
        end_ts=1772366400,
        incident_ref="INC0001",
    )
    numeric = _result("e2", _time=1772362800.5)
    text_epoch = _result("e3", _time="1772362800", service=None)
    adapter, _ = _adapter([stream(full, numeric, text_epoch), stream()])
    out = rows(list(adapter.events(SINCE, UNTIL)))
    assert {k: out[0][k] for k in EVENT_COLUMNS} == {
        "source_tool": "splunk",
        "event_key": "e1",
        "ts": "2026-03-01T11:00:00Z",
        "service": "checkout",
        "host": "web-1",
        "severity_raw": "high",
        "title": "Disk full",
        "status": "open",
        "dedup_key": "d1",
        "end_ts": "2026-03-01T12:00:00Z",
        "incident_ref": "INC0001",
    }
    assert json.loads(out[0]["_payload"]) == full["result"]
    assert [r["ts"] for r in out[1:]] == ["2026-03-01T11:00:00.500000Z", "2026-03-01T11:00:00Z"]
    assert out[2]["service"] is None
    assert out[2]["end_ts"] is None


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ({"result": {"_time": "1772362800"}}, "without event_key"),
        ({"result": {"event_key": "", "_time": "1772362800"}}, "without event_key"),
        ({"result": ["not", "an", "object"]}, "bad result"),
        ({"result": {"event_key": "e1", "_time": "soon"}}, "unparseable timestamp in _time"),
        ({"result": {"event_key": "e1", "_time": "1772362800", "end_ts": "x"}}, "end_ts"),
        ({"result": {"event_key": "e1", "_time": "1772362800", "title": 5}}, "monitoring row"),
    ],
)
def test_ut01_82_bad_event_results(line: dict[str, Any], message: str) -> None:
    """UT01-82 a result without event_key, a non-object result, bad times and non-string
    fields are SchemaViolation."""
    adapter, _ = _adapter([stream(line)])
    with pytest.raises(SchemaViolation, match=message):
        list(adapter.events(SINCE, UNTIL))


def test_ut01_82_no_event_query_makes_no_request() -> None:
    """UT01-82 `event_query` None: nothing sent, nothing yielded."""
    adapter, source = _adapter([stream()], event_query=None)
    assert list(adapter.events(SINCE, UNTIL)) == []
    assert source.seen == []


def test_ut01_82_forbidden_spl_is_config_error_and_unsent() -> None:
    """UT01-82 / UT01-08 SPL that fails validate_spl (settings bypassed) is ConfigError before
    any request (TH01-07)."""
    adapter, source = _adapter([stream()], event_query="index=x | stats count | delete")
    with pytest.raises(ConfigError, match="forbidden SPL"):
        list(adapter.events(SINCE, UNTIL))
    assert source.seen == []


def test_ut01_82_event_batches_flush_at_batch_rows() -> None:
    """UT01-82 rows flush every `batch_rows`, across day pieces."""
    adapter, _ = _adapter([stream(_result("e1"), _result("e2"), _result("e3"))], batch_rows=2)
    assert [b.num_rows for b in adapter.events(SINCE, UNTIL)] == [2, 2, 2]


# --- metrics -----------------------------------------------------------------------------


def _metric(day: D, service: str = "checkout", count: Any = "42") -> dict[str, Any]:
    return {
        "preview": False,
        "result": {"_time": str(epoch(day)), "service": service, "count": count},
    }


def test_ut01_82_metrics_per_day_rows() -> None:
    """UT01-82 metrics: per query the complete days [a, b) in day pieces; service from
    `service_label`, value float(`value_field`); dates outside [a, b) dropped."""
    until = datetime.datetime(2026, 3, 3, 8, 0, tzinfo=UTC)
    adapter, source = _adapter(
        [
            stream(_metric(D(2026, 2, 28)), _metric(D(2026, 3, 1))),
            stream(_metric(D(2026, 3, 2), count=7.5)),
        ]
    )
    out = rows(list(adapter.daily_metrics(SINCE, until)))
    assert [
        (_form(source, i)["earliest_time"], _form(source, i)["latest_time"]) for i in (0, 1)
    ] == [
        (str(epoch(D(2026, 3, 1))), str(epoch(D(2026, 3, 1)) + DAY_S)),
        (str(epoch(D(2026, 3, 2))), str(epoch(D(2026, 3, 3)))),
    ]
    assert _form(source, 0)["search"] == METRIC_SPL
    assert [{k: r[k] for k in METRIC_COLUMNS} for r in out] == [
        {"source_tool": "splunk", "date": "2026-03-01", "service": "checkout",
         "metric_name": "request_count", "value": "42.0", "unit": "ratio"},
        {"source_tool": "splunk", "date": "2026-03-02", "service": "checkout",
         "metric_name": "request_count", "value": "7.5", "unit": "ratio"},
    ]  # fmt: skip
    assert json.loads(out[0]["_payload"])["query"] == "request_count"


def test_ut01_82_metrics_no_complete_day_makes_no_request() -> None:
    """UT01-82 a window inside one UTC day has no complete day: no request."""
    adapter, source = _adapter([stream()])
    assert list(adapter.daily_metrics(SINCE, SINCE + datetime.timedelta(hours=1))) == []
    assert source.seen == []


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"_time": "1772323200", "count": "1"}, "without service field"),
        ({"_time": "1772323200", "service": "a"}, "bad metric value"),
        ({"_time": "1772323200", "service": "a", "count": "many"}, "bad metric value"),
        ({"_time": "1772323200", "service": 5, "count": "1"}, "monitoring row"),
    ],
)
def test_ut01_82_bad_metric_results(result: dict[str, Any], message: str) -> None:
    """UT01-82 a missing service field or a non-numeric value is SchemaViolation."""
    adapter, _ = _adapter([stream({"result": result})])
    with pytest.raises(SchemaViolation, match=message):
        list(adapter.daily_metrics(SINCE, UNTIL + datetime.timedelta(days=1)))


def test_ut01_82_metric_batches_flush_at_batch_rows() -> None:
    """UT01-82 metric rows flush every `batch_rows`."""
    until = datetime.datetime(2026, 3, 4, 8, 0, tzinfo=UTC)
    days = [stream(_metric(D(2026, 3, d))) for d in (1, 2, 3)]
    adapter, _ = _adapter(days, batch_rows=2)
    assert [b.num_rows for b in adapter.daily_metrics(SINCE, until)] == [2, 1]


# --- check, auth, factory ----------------------------------------------------------------


def test_ut01_82_check_reads_server_info() -> None:
    """UT01-82 check() is GET /services/server/info?output_mode=json; a body without an
    `entry` list is a SchemaViolation."""
    source = Source([(200, {"entry": [{"name": "server-info"}]}), (200, {"messages": []})])
    adapter = SplunkAdapter(settings("splunk"), clock=lambda: FETCHED, http=http("splunk", source))
    adapter.check()
    assert (source.seen[0].method, source.seen[0].url.path) == ("GET", "/services/server/info")
    assert source.params(0) == {"output_mode": "json"}
    with pytest.raises(SchemaViolation, match="bad server info response"):
        adapter.check()


def test_ut01_82_bearer_on_every_export(
    monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> None:
    """UT01-82 with no `http` the adapter builds `monitoring:splunk` on first use and sends
    the bearer token on each day's export; a 401 never echoes it (TH01-03)."""
    del fake_keyring
    store("splunk_key", SPLUNK_TOKEN)
    source = Lines([stream(_result("e1"))])
    calls = wire(monkeypatch, source)
    adapter = SplunkAdapter(settings("splunk", event_query=EVENTS_SPL), clock=lambda: FETCHED)
    assert calls == []
    assert len(rows(list(adapter.events(SINCE, UNTIL)))) == 2
    assert calls == ["monitoring:splunk"]
    assert {r.headers["Authorization"] for r in source.seen} == {f"Bearer {SPLUNK_TOKEN}"}
    wire(monkeypatch, Source([(401, {"messages": [{"text": SPLUNK_TOKEN}]})]))
    denied = SplunkAdapter(settings("splunk", event_query=EVENTS_SPL), clock=lambda: FETCHED)
    with pytest.raises(AuthError) as caught:
        list(denied.events(SINCE, UNTIL))
    assert SPLUNK_TOKEN not in f"{caught.value} {caught.value!r} {dict(caught.value.context)}"


def test_ut01_82_factory_signature_makes_no_request() -> None:
    """UT01-82 `get("monitoring_adapter", "splunk")(settings, clock=clock)` resolves the
    built-in row and builds no client."""
    adapter = registry.get("monitoring_adapter", "splunk")(
        settings("splunk"), clock=lambda: FETCHED
    )
    assert adapter.tool == "splunk"
    assert "_http" not in vars(adapter)
