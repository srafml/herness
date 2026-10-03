"""UT01-83 Dynatrace adapter (T01-20, U01-85): problems with ``nextPageKey`` and daily metrics."""

from __future__ import annotations

import datetime
import json
from typing import Any

import pytest
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.connectors._auth_data import store
from tests.unit.connectors._http_data import mock_client
from tests.unit.connectors._prometheus_data import (
    DT_TOKEN,
    FETCHED,
    UTC,
    Source,
    query,
    rows,
    settings,
    source_http,
    stub_resilience,
)

from herness.connectors.monitoring import prometheus as prometheus_module
from herness.connectors.monitoring.base import EVENT_COLUMNS
from herness.connectors.monitoring.dynatrace import DynatraceAdapter
from herness.core import registry
from herness.core.errors import AuthError, SchemaViolation
from herness.core.resilience import ProcessState

pytestmark = pytest.mark.unit

SINCE = datetime.datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
UNTIL = datetime.datetime(2026, 3, 4, 8, 0, tzinfo=UTC)
D = datetime.date


@pytest.fixture(autouse=True)
def _resilience(monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState) -> None:
    del reset_process_state
    stub_resilience(monkeypatch)


def _ms(ts: datetime.datetime) -> int:
    return int(ts.timestamp()) * 1000


def _day_ms(day: datetime.date) -> int:
    return _ms(datetime.datetime(day.year, day.month, day.day, tzinfo=UTC))


def _adapter(
    replies: list[tuple[int, Any]], *, batch_rows: int = 5000, **extra: Any
) -> tuple[DynatraceAdapter, Source]:
    source = Source(replies)
    base: dict[str, Any] = {
        "page_size": 500,
        "metric_queries": [query("availability_pct", "builtin:x")],
    }
    cfg = settings("dynatrace", **(base | extra))
    http = source_http("dynatrace", source)
    adapter = DynatraceAdapter(cfg, clock=lambda: FETCHED, http=http, batch_rows=batch_rows)
    return adapter, source


def _problem(key: str, **extra: Any) -> dict[str, Any]:
    problem: dict[str, Any] = {
        "problemId": key,
        "displayId": f"P-{key}",
        "title": "Response time degradation",
        "status": "OPEN",
        "severityLevel": "PERFORMANCE",
        "impactLevel": "SERVICES",
        "startTime": _ms(SINCE) + 60_000,
        "endTime": -1,
        "rootCauseEntity": {"entityId": {"id": "SERVICE-1"}, "name": "checkout"},
        "affectedEntities": [{"entityId": {"id": "SERVICE-2"}, "name": "billing"}],
    }
    return problem | extra


def _page(*problems: dict[str, Any], key: str | None = None) -> dict[str, Any]:
    return {
        "totalCount": len(problems),
        "pageSize": 500,
        "nextPageKey": key,
        "problems": list(problems),
    }


def test_ut01_83_problems_two_pages_second_sends_only_next_page_key() -> None:
    """UT01-83 the first call has from/to (epoch ms), pageSize and problemSelector; the second
    sends only nextPageKey; endTime -1 gives end_ts NULL; field mapping per U01-85."""
    first = _page(_problem("p1"), key="synthetic-page-key-2")
    closed = _problem("p2", status="CLOSED", endTime=_ms(SINCE) + 3_600_000, rootCauseEntity=None)
    second = {"problems": [closed, _problem("p3", rootCauseEntity=None, affectedEntities=[])]}
    adapter, source = _adapter([(200, first), (200, second)], event_query='status("open")')
    got = rows(list(adapter.events(SINCE, UNTIL)))
    assert len(source.seen) == 2
    assert source.seen[0].url.path == "/api/v2/problems"
    assert source.seen[0].url.host == "tenant.example.invalid"
    assert source.params(0) == {
        "from": str(_ms(SINCE)),
        "to": str(_ms(UNTIL)),
        "pageSize": "500",
        "problemSelector": 'status("open")',
    }
    assert source.seen[1].url.path == "/api/v2/problems"
    assert source.params(1) == {"nextPageKey": "synthetic-page-key-2"}
    assert [r["event_key"] for r in got] == ["p1", "p2", "p3"]
    p1, p2, p3 = got
    assert p1["end_ts"] is None
    assert p1["ts"] == "2026-03-01T10:01:00Z"
    assert p1["service"] == "checkout"
    assert (p1["title"], p1["status"], p1["severity_raw"]) == (
        "Response time degradation",
        "OPEN",
        "PERFORMANCE",
    )
    assert (p1["dedup_key"], p1["host"], p1["incident_ref"]) == ("P-p1", None, None)
    assert p1["_source_key"] == "dynatrace:p1"
    assert json.loads(p1["_payload"])["problemId"] == "p1"
    assert p2["end_ts"] == "2026-03-01T11:00:00Z"
    assert p2["status"] == "CLOSED"
    assert p2["service"] == "billing"  # no root cause: the first affected entity
    assert p3["service"] is None
    assert {r["source_tool"] for r in got} == {"dynatrace"}
    assert set(EVENT_COLUMNS) <= set(p1)


def test_ut01_83_absent_end_time_and_no_selector() -> None:
    """UT01-83 an absent endTime gives end_ts NULL; without event_query no problemSelector."""
    problem = _problem("p1")
    del problem["endTime"]
    adapter, source = _adapter([(200, {"problems": [problem]})])
    (row,) = rows(list(adapter.events(SINCE, UNTIL)))
    assert row["end_ts"] is None
    assert "problemSelector" not in source.params(0)
    assert len(source.seen) == 1


def test_ut01_83_repeated_page_key_is_schema_violation() -> None:
    """UT01-83 CursorGuard.step on each key: a repeated nextPageKey stops the stream."""
    page = _page(_problem("p1"), key="synthetic-same-key")
    adapter, source = _adapter([(200, page)])
    with pytest.raises(SchemaViolation, match="cursor repeated"):
        list(adapter.events(SINCE, UNTIL))
    assert len(source.seen) == 2


def test_ut01_83_events_in_batches_of_batch_rows() -> None:
    """UT01-83 problems are emitted as event_batch chunks of `batch_rows`."""
    page = _page(_problem("p1"), _problem("p2"), _problem("p3"))
    adapter, _ = _adapter([(200, page)], batch_rows=2)
    assert [b.num_rows for b in adapter.events(SINCE, UNTIL)] == [2, 1]


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"problems": {}},
        {"problems": ["x"]},
        {"problems": [], "nextPageKey": 7},
        {"problems": [], "nextPageKey": ""},
        _page(_problem("p1", startTime="1")),
        _page(_problem("p1", startTime=True)),
        _page(_problem("p1", startTime=10**30)),
        _page(_problem("p1", endTime="x")),
        _page(_problem("p1", problemId=None)),
        _page(_problem("p1", title=5)),
    ],
)
def test_ut01_83_bad_problem_shapes_are_schema_violations(body: Any) -> None:
    """UT01-83 a problems body, key or problem of the wrong shape is a SchemaViolation."""
    adapter, _ = _adapter([(200, body)])
    with pytest.raises(SchemaViolation):
        list(adapter.events(SINCE, UNTIL))


def _series(
    service: str | None, stamps: list[int], values: list[Any], **dims: str
) -> dict[str, Any]:
    dimension_map = dims if service is None else {"dt.entity.service": service} | dims
    return {
        "dimensions": list(dimension_map.values()),
        "dimensionMap": dimension_map,
        "timestamps": stamps,
        "values": values,
    }


def _metrics(*series: dict[str, Any], key: str | None = None) -> dict[str, Any]:
    result = [{"metricId": "builtin:x", "data": list(series)}]
    return {"totalCount": len(series), "nextPageKey": key, "resolution": "1d", "result": result}


def test_ut01_83_metric_params_and_bucket_date_rule() -> None:
    """UT01-83 metrics: metricSelector, resolution=1d, from = a, to = b (epoch ms); bucket date
    = UTC date of the timestamp - 1 day, kept when a <= date < b; null values -> None."""
    stamps = [_day_ms(D(2026, 3, d)) for d in (1, 2, 3, 4, 5)]
    body = _metrics(_series("SERVICE-1", stamps, [9.0, 99.5, None, 97.0, 1.0]))
    adapter, source = _adapter([(200, body)])
    got = rows(list(adapter.daily_metrics(SINCE, UNTIL)))
    assert len(source.seen) == 1
    assert source.seen[0].url.path == "/api/v2/metrics/query"
    assert source.params(0) == {
        "metricSelector": "builtin:x",
        "resolution": "1d",
        "from": str(_day_ms(D(2026, 3, 1))),
        "to": str(_day_ms(D(2026, 3, 4))),
    }
    assert [r["date"] for r in got] == ["2026-03-01", "2026-03-02", "2026-03-03"]
    assert [r["value"] for r in got] == ["99.5", None, "97.0"]
    assert {r["service"] for r in got} == {"SERVICE-1"}
    assert {r["metric_name"] for r in got} == {"availability_pct"}
    assert got[0]["_source_key"] == "dynatrace|availability_pct|SERVICE-1|2026-03-01"
    assert json.loads(got[0]["_payload"])["timestamp"] == stamps[1]


def test_ut01_83_custom_service_dimension_and_batches() -> None:
    """UT01-83 `service_dimension` selects the dimension; rows come in `batch_rows` chunks
    across queries."""
    stamps = [_day_ms(D(2026, 3, 2)), _day_ms(D(2026, 3, 3))]
    body = _metrics(_series(None, stamps, [1, 2], host="h1", svc="api"))
    queries = [
        query("availability_pct", "a", service_dimension="svc"),
        query("error_rate", "b", service_dimension="svc"),
    ]
    adapter, source = _adapter([(200, body)], batch_rows=3, metric_queries=queries)
    batches = list(adapter.daily_metrics(SINCE, UNTIL))
    assert [b.num_rows for b in batches] == [3, 1]
    assert {r["service"] for r in rows(batches)} == {"api"}
    assert [source.params(i)["metricSelector"] for i in range(2)] == ["a", "b"]


def test_ut01_83_no_complete_day_no_request() -> None:
    """UT01-83 when a >= b there is no metrics request."""
    adapter, source = _adapter([(200, _metrics())])
    assert list(adapter.daily_metrics(SINCE, SINCE.replace(hour=23))) == []
    assert source.seen == []


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (_metrics(key="synthetic-next"), "unexpected pagination"),
        (_metrics(_series(None, [1], [1.0])), "series without service dimension"),
        (_metrics(_series("S", [_day_ms(D(2026, 3, 2))], [])), "bad values"),
        (_metrics(_series("S", [_day_ms(D(2026, 3, 2))], ["x"])), "monitoring row"),
        (_metrics(_series("S", ["x"], [1.0])), "bad timestamp"),
        ({"result": {}}, "bad result"),
        ({"result": [{"data": {}}]}, "bad data"),
        ({"result": [{"data": ["x"]}]}, "service dimension"),
    ],
)
def test_ut01_83_bad_metric_shapes_are_schema_violations(body: Any, match: str) -> None:
    """UT01-83 a metrics body of the wrong shape, or with a nextPageKey, is a SchemaViolation."""
    adapter, _ = _adapter([(200, body)])
    with pytest.raises(SchemaViolation, match=match):
        list(adapter.daily_metrics(SINCE, UNTIL))


def test_ut01_83_check_and_api_token_on_every_page(
    monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> None:
    """UT01-83 check() is GET /api/v2/problems?pageSize=1&from=now-5m; with no `http` the
    adapter builds the `monitoring:dynatrace` client on first use and sends
    `Authorization: Api-Token <token>` on every page (same origin only)."""
    del fake_keyring
    store("dynatrace_key", DT_TOKEN)
    source = Source(
        [(200, _page()), (200, _page(_problem("p1"), key="synthetic-k")), (200, _page())]
    )
    calls: list[str] = []

    def fake_client(cfg: Any, *, source: str, max_concurrency: int) -> Any:
        del max_concurrency
        calls.append(source)
        return mock_client(handler, cfg.base_url)

    handler = source
    monkeypatch.setattr(prometheus_module, "http_client", fake_client)
    adapter = DynatraceAdapter(settings("dynatrace"), clock=lambda: FETCHED)
    assert calls == []
    adapter.check()
    assert source.params(0) == {"pageSize": "1", "from": "now-5m"}
    assert len(rows(list(adapter.events(SINCE, UNTIL)))) == 1
    assert calls == ["monitoring:dynatrace"]
    assert {r.headers["Authorization"] for r in source.seen} == {f"Api-Token {DT_TOKEN}"}
    assert source.params(2) == {"nextPageKey": "synthetic-k"}


def test_ut01_83_check_bad_body() -> None:
    """UT01-83 a check body without a problems list is a SchemaViolation."""
    adapter, _ = _adapter([(200, {"error": {}})])
    with pytest.raises(SchemaViolation, match="bad problems"):
        adapter.check()


def test_ut01_83_registered_builtin() -> None:
    """UT01-83 the registry resolves `monitoring_adapter` `dynatrace` to the adapter."""
    assert registry.get("monitoring_adapter", "dynatrace") is DynatraceAdapter
    assert DynatraceAdapter.tool == "dynatrace"


def test_ut01_83_auth_failure_never_echoes_the_token(
    monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> None:
    """UT01-83 HTTP 401 is AuthError whose message and context carry no token (TH01-03)."""
    del fake_keyring
    store("dynatrace_key", DT_TOKEN)
    source = Source([(401, {"error": {"message": DT_TOKEN}})])

    def fake_client(cfg: Any, *, source: str, max_concurrency: int) -> Any:
        del source, max_concurrency
        return mock_client(handler, cfg.base_url)

    handler = source
    monkeypatch.setattr(prometheus_module, "http_client", fake_client)
    adapter = DynatraceAdapter(settings("dynatrace"), clock=lambda: FETCHED)
    with pytest.raises(AuthError) as caught:
        list(adapter.events(SINCE, UNTIL))
    text = f"{caught.value} {caught.value!r} {dict(caught.value.context)}"
    assert DT_TOKEN not in text
    assert "synthetic" not in repr(adapter._http._auth)


@pytest.mark.parametrize("tool", ["prometheus", "dynatrace"])
def test_ut01_83_factory_signature_makes_no_request(tool: str) -> None:
    """UT01-83 the factory call `get("monitoring_adapter", tool)(settings, clock=clock)`
    builds an adapter without resolving secrets or opening a client."""
    adapter = registry.get("monitoring_adapter", tool)(settings(tool), clock=lambda: FETCHED)
    assert adapter.tool == tool
    assert "_http" not in vars(adapter)
