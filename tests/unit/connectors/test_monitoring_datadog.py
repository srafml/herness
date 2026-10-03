"""UT01-81 Datadog adapter (T01-21, U01-83): cursor-paged events search and daily rollups."""

from __future__ import annotations

import datetime
import json
from typing import Any

import httpx2
import pytest
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.connectors._auth_data import store
from tests.unit.connectors._dd_splunk_data import DD_API_KEY, DD_APP_KEY, http, settings, wire
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
from herness.connectors.monitoring.datadog import DatadogAdapter
from herness.core import registry
from herness.core.errors import AuthError, ConfigError, RateLimited, SchemaViolation
from herness.core.resilience import ProcessState

pytestmark = pytest.mark.unit

SINCE = datetime.datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
UNTIL = datetime.datetime(2026, 3, 4, 8, 0, tzinfo=UTC)
D = datetime.date
EVENT_QUERY = "source:alert status:error"


@pytest.fixture(autouse=True)
def _resilience(monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState) -> None:
    del reset_process_state
    stub_resilience(monkeypatch)


def _adapter(
    replies: list[tuple[int, Any]], *, batch_rows: int = 5000, **extra: Any
) -> tuple[DatadogAdapter, Source]:
    source = Source(replies)
    base: dict[str, Any] = {
        "page_size": 2,
        "event_query": EVENT_QUERY,
        "metric_queries": [query("error_rate", "sum:trace.errors{*} by {service}", agg="sum")],
    }
    cfg = settings("datadog", **(base | extra))
    adapter = DatadogAdapter(
        cfg, clock=lambda: FETCHED, http=http("datadog", source), batch_rows=batch_rows
    )
    return adapter, source


def _item(key: str, **inner: Any) -> dict[str, Any]:
    tags = inner.pop("tags", ["env:prod"])
    attrs = {"service": "checkout", "host": "web-1", "priority": "normal", "status": "error"}
    attrs |= {"title": f"Alert {key}", "aggregation_key": f"agg-{key}"}
    return {
        "id": key,
        "type": "event",
        "attributes": {
            "timestamp": "2026-03-02T10:00:00Z",
            "tags": tags,
            "attributes": {k: v for k, v in (attrs | inner).items() if v is not ...},
        },
    }


def _page(*items: dict[str, Any], after: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"data": list(items)}
    if after is not None:
        body["meta"] = {"page": {"after": after}}
    return body


def _body(index: int, source: Source) -> Any:
    return json.loads(source.seen[index].content)


# --- events ------------------------------------------------------------------------------


def test_ut01_81_events_three_pages_until_no_cursor() -> None:
    """UT01-81 events: three pages, the last without meta.page.after; all events kept; later
    requests repeat the settings body with only the cursor added (cursor pagination only)."""
    adapter, source = _adapter(
        [
            (200, _page(_item("e1"), _item("e2"), after="synthetic-c1")),
            (200, _page(_item("e3"), _item("e4"), after="synthetic-c2")),
            (200, _page(_item("e5"))),
        ]
    )
    out = rows(list(adapter.events(SINCE, UNTIL)))
    assert [r["event_key"] for r in out] == ["e1", "e2", "e3", "e4", "e5"]
    assert len(source.seen) == 3
    assert {(r.method, r.url.path) for r in source.seen} == {("POST", "/api/v2/events/search")}
    first = {
        "filter": {
            "query": EVENT_QUERY,
            "from": "2026-03-01T10:00:00Z",
            "to": "2026-03-04T08:00:00Z",
        },
        "sort": "timestamp",
        "page": {"limit": 2},
    }
    assert _body(0, source) == first
    assert _body(1, source) == first | {"page": {"limit": 2, "cursor": "synthetic-c1"}}
    assert _body(2, source) == first | {"page": {"limit": 2, "cursor": "synthetic-c2"}}


def test_ut01_81_event_field_mapping() -> None:
    """UT01-81 field mapping (V-4 default): id, timestamp, inner service/host/priority/status/
    title/aggregation_key; end_ts and incident_ref NULL; payload is the item."""
    item = _item("e1")
    adapter, _ = _adapter([(200, _page(item))])
    [row] = rows(list(adapter.events(SINCE, UNTIL)))
    assert {k: row[k] for k in EVENT_COLUMNS} == {
        "source_tool": "datadog",
        "event_key": "e1",
        "ts": "2026-03-02T10:00:00Z",
        "service": "checkout",
        "host": "web-1",
        "severity_raw": "normal",
        "title": "Alert e1",
        "status": "error",
        "dedup_key": "agg-e1",
        "end_ts": None,
        "incident_ref": None,
    }
    assert json.loads(row["_payload"]) == item
    assert row["_source_key"] == "datadog:e1"


def test_ut01_81_service_falls_back_to_tag() -> None:
    """UT01-81 service: inner.service, else the first `service:<v>` tag, else NULL; a missing
    inner object reads as {}."""
    tagged = _item("e1", service=..., tags=["env:prod", 7, "service:billing", "service:other"])
    bare = _item("e2", service=..., tags=["env:prod"])
    no_inner = {"id": "e3", "attributes": {"timestamp": "2026-03-02T10:00:00Z", "tags": None}}
    adapter, _ = _adapter([(200, _page(tagged, bare, no_inner))])
    out = rows(list(adapter.events(SINCE, UNTIL)))
    assert [r["service"] for r in out] == ["billing", None, None]
    assert out[2]["host"] is None


def test_ut01_81_events_stop_on_empty_data() -> None:
    """UT01-81 an empty `data` ends paging even when a cursor is present."""
    adapter, source = _adapter([(200, _page(after="synthetic-c1"))])
    assert list(adapter.events(SINCE, UNTIL)) == []
    assert len(source.seen) == 1


def test_ut01_81_no_event_query_makes_no_request() -> None:
    """UT01-81 `event_query` None: events yields nothing and sends nothing."""
    adapter, source = _adapter([(200, _page())], event_query=None)
    assert list(adapter.events(SINCE, UNTIL)) == []
    assert source.seen == []


def test_ut01_81_repeated_cursor_is_schema_violation() -> None:
    """UT01-81 the same `after` twice trips CursorGuard (TH01-04)."""
    adapter, _ = _adapter([(200, _page(_item("e1"), after="synthetic-c1"))])
    with pytest.raises(SchemaViolation, match="cursor repeated"):
        list(adapter.events(SINCE, UNTIL))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"errors": []}, "bad events response"),
        (_page(_item("e1"), after=5), "bad page cursor"),
        (_page(_item("e1"), after=""), "bad page cursor"),
        (_page({"id": "", "attributes": {}}), "event without id"),
        (_page({"id": 5}), "event without id"),
        (_page("not-an-item"), "event without id"),
        (_page({"id": "e1", "attributes": {"timestamp": "yesterday"}}), "unparseable timestamp"),
        (_page(_item("e1", title=5)), "monitoring row"),
    ],
)
def test_ut01_81_bad_event_pages(body: Any, message: str) -> None:
    """UT01-81 bad shapes, cursors, ids, timestamps and field types are SchemaViolation."""
    adapter, _ = _adapter([(200, body)])
    with pytest.raises(SchemaViolation, match=message):
        list(adapter.events(SINCE, UNTIL))


def test_ut01_81_event_batches_flush_at_batch_rows() -> None:
    """UT01-81 rows flush every `batch_rows` across pages."""
    adapter, _ = _adapter(
        [(200, _page(_item("e1"), _item("e2"), after="synthetic-c1")), (200, _page(_item("e3")))],
        batch_rows=2,
    )
    assert [b.num_rows for b in adapter.events(SINCE, UNTIL)] == [2, 1]


def test_ut01_81_rate_limited_uses_x_ratelimit_reset() -> None:
    """UT01-81 HTTP 429 with `X-RateLimit-Reset: 12` → RateLimited.retry_after == 12 once
    the page retries are spent (U01-60 via parse_retry_after, R-70)."""
    seen: list[httpx2.Request] = []

    def limited(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(429, headers={"X-RateLimit-Reset": "12"}, content=b"{}")

    cfg = settings("datadog", event_query=EVENT_QUERY)
    adapter = DatadogAdapter(cfg, clock=lambda: FETCHED, http=http("datadog", limited))
    with pytest.raises(RateLimited) as caught:
        list(adapter.events(SINCE, UNTIL))
    assert caught.value.retry_after == 12
    assert len(seen) >= 1


# --- metrics -----------------------------------------------------------------------------


def _ms(day: D, hour: int = 0) -> int:
    return (epoch(day) + hour * 3600) * 1000


def _series(service: str, points: list[list[Any]], *extra: str) -> dict[str, Any]:
    return {"tag_set": [*extra, f"service:{service}"], "pointlist": points, "metric": "x"}


def test_ut01_81_metrics_rollup_request_and_rows() -> None:
    """UT01-81 metrics: GET /api/v1/query with from=a, to=b-1 s, the `.rollup(agg, 86400)`
    query appended; one row per day a <= date < b, others dropped; null → NULL."""
    body = {
        "status": "ok",
        "series": [
            _series(
                "checkout",
                [
                    [_ms(D(2026, 2, 28)), 1.0],
                    [_ms(D(2026, 3, 1)), 0.5],
                    [_ms(D(2026, 3, 3)), None],
                    [_ms(D(2026, 3, 4)), 9.0],
                ],
                "env:prod",
            )
        ],
    }
    adapter, source = _adapter([(200, body)])
    out = rows(list(adapter.daily_metrics(SINCE, UNTIL)))
    assert source.seen[0].url.path == "/api/v1/query"
    assert source.params(0) == {
        "from": str(epoch(D(2026, 3, 1))),
        "to": str(epoch(D(2026, 3, 4)) - 1),
        "query": "sum:trace.errors{*} by {service}.rollup(sum, 86400)",
    }
    assert [{k: r[k] for k in METRIC_COLUMNS} for r in out] == [
        {"source_tool": "datadog", "date": "2026-03-01", "service": "checkout",
         "metric_name": "error_rate", "value": "0.5", "unit": "ratio"},
        {"source_tool": "datadog", "date": "2026-03-03", "service": "checkout",
         "metric_name": "error_rate", "value": None, "unit": "ratio"},
    ]  # fmt: skip
    assert json.loads(out[0]["_payload"])["point"] == [_ms(D(2026, 3, 1)), 0.5]


def test_ut01_81_metrics_no_complete_day_makes_no_request() -> None:
    """UT01-81 a window inside one UTC day has no complete day: no request."""
    adapter, source = _adapter([(200, {"status": "ok", "series": []})])
    until = SINCE + datetime.timedelta(hours=2)
    assert list(adapter.daily_metrics(SINCE, until)) == []
    assert source.seen == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"status": "error", "series": [], "error": "bad query"}, "bad query response"),
        ({"status": "ok"}, "bad query response"),
        ({"status": "ok", "series": [{"tag_set": ["env:prod"], "pointlist": []}]}, "service tag"),
        ({"status": "ok", "series": [{"pointlist": []}]}, "service tag"),
        ({"status": "ok", "series": ["x"]}, "service tag"),
        ({"status": "ok", "series": [{"tag_set": ["service:a"]}]}, "service tag"),
        ({"status": "ok", "series": [_series("a", [[1, 2, 3]])]}, "bad point"),
        ({"status": "ok", "series": [_series("a", [[True, 1.0]])]}, "bad point"),
        ({"status": "ok", "series": [_series("a", [[1e20, 1.0]])]}, "bad point"),
        ({"status": "ok", "series": [_series("a", [[_ms(D(2026, 3, 1)), "x"]])]}, "monitoring row"),
    ],
)
def test_ut01_81_bad_metric_bodies(body: Any, message: str) -> None:
    """UT01-81 `status == "error"`, missing service tag and bad points are SchemaViolation."""
    adapter, _ = _adapter([(200, body)])
    with pytest.raises(SchemaViolation, match=message):
        list(adapter.daily_metrics(SINCE, UNTIL))


def test_ut01_81_metric_query_without_agg_is_config_error() -> None:
    """UT01-81 a query without `agg` (refused by MonitoringSettings) is ConfigError, unsent."""
    adapter, source = _adapter([(200, {})], metric_queries=[query()])
    with pytest.raises(ConfigError, match="needs agg"):
        list(adapter.daily_metrics(SINCE, UNTIL))
    assert source.seen == []


def test_ut01_81_metric_batches_flush_across_queries() -> None:
    """UT01-81 rows flush every `batch_rows`, across queries."""
    body = {"status": "ok", "series": [_series("a", [[_ms(D(2026, 3, d)), 1.0] for d in (1, 2)])]}
    queries = [query("error_rate", "q1", agg="sum"), query("request_count", "q2", agg="count")]
    adapter, _ = _adapter([(200, body)], batch_rows=3, metric_queries=queries)
    assert [b.num_rows for b in adapter.daily_metrics(SINCE, UNTIL)] == [3, 1]


# --- check, auth, factory ----------------------------------------------------------------


def test_ut01_81_check_validates() -> None:
    """UT01-81 check() is GET /api/v1/validate and needs `{"valid": true}`."""
    adapter, source = _adapter([(200, {"valid": True})])
    adapter.check()
    assert (source.seen[0].method, source.seen[0].url.path) == ("GET", "/api/v1/validate")
    bad, _ = _adapter([(200, {"valid": False})])
    with pytest.raises(SchemaViolation, match="bad validate response"):
        bad.check()


def test_ut01_81_keys_ride_only_the_configured_origin(
    monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> None:
    """UT01-81 with no `http` the adapter builds `monitoring:datadog` on first use; both
    DD-API-KEY and DD-APPLICATION-KEY reach base_url requests and never another origin."""
    del fake_keyring
    store("datadog_key", {"api_key": DD_API_KEY, "app_key": DD_APP_KEY})
    source = Source([(200, {"valid": True})])
    calls = wire(monkeypatch, source)
    adapter = DatadogAdapter(settings("datadog"), clock=lambda: FETCHED)
    assert calls == []
    adapter.check()
    assert calls == ["monitoring:datadog"]
    sent = source.seen[0].headers
    assert (sent["DD-API-KEY"], sent["DD-APPLICATION-KEY"]) == (DD_API_KEY, DD_APP_KEY)
    adapter._http.get_json("https://other.example.invalid/api/v1/validate")
    foreign = source.seen[1].headers
    assert "DD-API-KEY" not in foreign
    assert "DD-APPLICATION-KEY" not in foreign


def test_ut01_81_auth_failure_never_echoes_keys(
    monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> None:
    """UT01-81 HTTP 403 is AuthError whose message and context carry no key (TH01-03)."""
    del fake_keyring
    store("datadog_key", {"api_key": DD_API_KEY, "app_key": DD_APP_KEY})
    wire(monkeypatch, Source([(403, {"errors": [DD_API_KEY]})]))
    adapter = DatadogAdapter(settings("datadog", event_query=EVENT_QUERY), clock=lambda: FETCHED)
    with pytest.raises(AuthError) as caught:
        list(adapter.events(SINCE, UNTIL))
    text = f"{caught.value} {caught.value!r} {dict(caught.value.context)}"
    assert DD_API_KEY not in text
    assert DD_APP_KEY not in text
    assert "synthetic" not in repr(adapter._http._auth)


def test_ut01_81_factory_signature_makes_no_request() -> None:
    """UT01-81 `get("monitoring_adapter", "datadog")(settings, clock=clock)` resolves the
    built-in row and builds no client."""
    adapter = registry.get("monitoring_adapter", "datadog")(
        settings("datadog"), clock=lambda: FETCHED
    )
    assert adapter.tool == "datadog"
    assert "_http" not in vars(adapter)
