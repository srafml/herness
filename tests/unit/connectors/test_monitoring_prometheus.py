"""UT01-80 Prometheus/Mimir adapter (T01-20, U01-82): daily ``query_range`` metrics."""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.connectors._auth_data import store
from tests.unit.connectors._http_data import mock_client
from tests.unit.connectors._prometheus_data import (
    FETCHED,
    PROM_TOKEN,
    TENANT,
    UTC,
    Source,
    epoch,
    matrix,
    query,
    rows,
    settings,
    source_http,
    stub_resilience,
)

from herness.connectors.monitoring import prometheus as prometheus_module
from herness.connectors.monitoring.base import METRIC_COLUMNS
from herness.connectors.monitoring.prometheus import PrometheusAdapter
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


def _adapter(
    replies: list[tuple[int, Any]], *, batch_rows: int = 5000, **extra: Any
) -> tuple[PrometheusAdapter, Source]:
    source = Source(replies)
    cfg = settings("prometheus", **({"metric_queries": [query()]} | extra))
    http = source_http("prometheus", source)
    adapter = PrometheusAdapter(cfg, clock=lambda: FETCHED, http=http, batch_rows=batch_rows)
    return adapter, source


def _points(*days: datetime.date) -> list[list[Any]]:
    return [[epoch(day), f"{index}.5"] for index, day in enumerate(days)]


def test_ut01_80_query_range_params_dates_and_tenant_header() -> None:
    """UT01-80 one query_range per query: step=86400, start = a + 1 day, end = b (epoch s),
    under the Mimir /prometheus prefix with X-Scope-OrgID; date = t - 1 day."""
    body = matrix(({"service": "checkout"}, _points(D(2026, 3, 2), D(2026, 3, 3), D(2026, 3, 4))))
    adapter, source = _adapter([(200, body)], tenant=TENANT)
    got = rows(list(adapter.daily_metrics(SINCE, UNTIL)))
    assert len(source.seen) == 1
    request = source.seen[0]
    assert request.url.path == "/prometheus/api/v1/query_range"
    assert request.url.host == "mimir.example.invalid"
    assert request.headers["X-Scope-OrgID"] == TENANT
    params = source.params(0)
    assert params == {
        "query": "sum by (service) (rate(x[1d]))",
        "start": str(epoch(D(2026, 3, 2))),
        "end": str(epoch(D(2026, 3, 4))),
        "step": "86400",
    }
    assert [r["date"] for r in got] == ["2026-03-01", "2026-03-02", "2026-03-03"]
    assert [r["value"] for r in got] == ["0.5", "1.5", "2.5"]
    assert {r["service"] for r in got} == {"checkout"}
    assert {r["metric_name"] for r in got} == {"error_rate"}
    assert {r["unit"] for r in got} == {"ratio"}
    assert {r["source_tool"] for r in got} == {"prometheus"}
    assert got[0]["_source_key"] == "prometheus|error_rate|checkout|2026-03-01"
    assert set(METRIC_COLUMNS) <= set(got[0])


def test_ut01_80_series_without_service_label_is_schema_violation() -> None:
    """UT01-80 a series missing the configured service label is a SchemaViolation."""
    body = matrix(({"job": "api"}, _points(D(2026, 3, 2))))
    adapter, _ = _adapter([(200, body)])
    with pytest.raises(SchemaViolation, match="series without service label"):
        list(adapter.daily_metrics(SINCE, UNTIL))


def test_ut01_80_custom_service_label_and_payload() -> None:
    """UT01-80 `service_label` selects the label; payload keeps query name, metric, t and v."""
    metric = {"svc": "billing", "__name__": "x"}
    adapter, _ = _adapter(
        [(200, matrix((metric, [[epoch(D(2026, 3, 2)), "NaN"]])))],
        metric_queries=[query("availability_pct", "q", service_label="svc")],
    )
    (row,) = rows(list(adapter.daily_metrics(SINCE, UNTIL)))
    assert row["service"] == "billing"
    assert row["value"] is None  # NaN
    assert '"query": "availability_pct"' in row["_payload"]
    assert '"v": "NaN"' in row["_payload"]


def test_ut01_80_no_tenant_no_org_header() -> None:
    """UT01-80 without `tenant` no X-Scope-OrgID header is sent."""
    adapter, source = _adapter([(200, matrix())])
    assert list(adapter.daily_metrics(SINCE, UNTIL)) == []
    assert "X-Scope-OrgID" not in source.seen[0].headers


def test_ut01_80_no_complete_day_no_request() -> None:
    """UT01-80 when a + 1 day > b there is nothing to ask: no request, no rows."""
    adapter, source = _adapter([(200, matrix())])
    same_day = SINCE.replace(hour=23)
    assert list(adapter.daily_metrics(SINCE, same_day)) == []
    assert source.seen == []


def test_ut01_80_chunks_of_at_most_10000_days() -> None:
    """UT01-80 a long window is split into chunks of <= 10,000 points; step stays 86400."""
    first = datetime.datetime(1990, 1, 1, tzinfo=UTC)
    until = first + datetime.timedelta(days=10_001, hours=3)
    adapter, source = _adapter([(200, matrix())], metric_queries=[query(), query("request_count")])
    assert list(adapter.daily_metrics(first, until)) == []
    assert len(source.seen) == 4  # 2 queries x 2 chunks
    day = 86_400
    start = epoch(first.date()) + day
    chunks = [(int(p["start"]), int(p["end"])) for p in map(source.params, range(2))]
    assert chunks == [(start, start + 9_999 * day), (start + 10_000 * day, start + 10_000 * day)]
    assert {source.params(i)["step"] for i in range(4)} == {"86400"}
    assert source.params(2)["query"] == "sum by (service) (rate(x[1d]))"


def test_ut01_80_batches_of_batch_rows() -> None:
    """UT01-80 rows are emitted as metric_batch chunks of `batch_rows`."""
    body = matrix(({"service": "a"}, _points(D(2026, 3, 2), D(2026, 3, 3), D(2026, 3, 4))))
    adapter, _ = _adapter([(200, body)], batch_rows=2)
    batches = list(adapter.daily_metrics(SINCE, UNTIL))
    assert [b.num_rows for b in batches] == [2, 1]


@pytest.mark.parametrize(
    "body",
    [
        {"status": "error", "data": {"resultType": "matrix", "result": []}},
        {"status": "success", "data": {"resultType": "vector", "result": []}},
        {"status": "success", "data": {"resultType": "matrix", "result": {}}},
        {"status": "success", "data": []},
        [],
        matrix(({"service": "a"}, [[1, "1", 3]])),
        matrix(({"service": "a"}, [["1", "1"]])),
        matrix(({"service": "a"}, [[True, "1"]])),
        matrix(({"service": "a"}, [[1, 1.0]])),
        matrix(({"service": "a"}, [[1, "abc"]])),
        matrix(({"service": "a"}, [[1e20, "1"]])),
        matrix(({"service": "a"}, ["x"])),
        {"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {}}]}},
        {"status": "success", "data": {"resultType": "matrix", "result": ["x"]}},
    ],
)
def test_ut01_80_bad_shapes_are_schema_violations(body: Any) -> None:
    """UT01-80 a body or sample of the wrong shape is a SchemaViolation."""
    adapter, _ = _adapter([(200, body)])
    with pytest.raises(SchemaViolation):
        list(adapter.daily_metrics(SINCE, UNTIL))


def test_ut01_80_non_string_service_is_schema_violation() -> None:
    """UT01-80 a non-string service label value is refused by metric_batch."""
    adapter, _ = _adapter([(200, matrix(({"service": 7}, _points(D(2026, 3, 2)))))])  # type: ignore[dict-item]
    with pytest.raises(SchemaViolation, match="monitoring row"):
        list(adapter.daily_metrics(SINCE, UNTIL))


def test_ut01_80_check_and_events() -> None:
    """UT01-80 check() is GET /api/v1/query?query=vector(1) with the tenant header; events
    yields nothing (no alert history API)."""
    adapter, source = _adapter([(200, {"status": "success", "data": {}})], tenant=TENANT)
    adapter.check()
    assert source.seen[0].url.path == "/prometheus/api/v1/query"
    assert source.params(0) == {"query": "vector(1)"}
    assert source.seen[0].headers["X-Scope-OrgID"] == TENANT
    assert list(adapter.events(SINCE, UNTIL)) == []
    assert len(source.seen) == 1


def test_ut01_80_check_failures() -> None:
    """UT01-80 a non-success check body is a SchemaViolation; HTTP 401 is AuthError."""
    adapter, _ = _adapter([(200, {"status": "error"})])
    with pytest.raises(SchemaViolation, match="bad query response"):
        adapter.check()
    adapter, _ = _adapter([(401, {})])
    with pytest.raises(AuthError):
        adapter.check()


def test_ut01_80_builds_its_own_http_lazily(
    monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> None:
    """UT01-80 with no `http`, construction makes no client; first use builds the egress
    client of `monitoring:prometheus` and the bearer auth, sent with the tenant header."""
    del fake_keyring
    store("prometheus_key", PROM_TOKEN)
    source = Source([(200, {"status": "success"})])
    calls: list[tuple[str, int]] = []

    def fake_client(cfg: Any, *, source: str, max_concurrency: int) -> Any:
        calls.append((source, max_concurrency))
        return mock_client(source_handler, cfg.base_url)

    source_handler = source
    monkeypatch.setattr(prometheus_module, "http_client", fake_client)
    adapter = PrometheusAdapter(settings("prometheus", tenant=TENANT, max_concurrency=3))
    assert calls == []
    adapter.check()
    adapter.check()
    assert calls == [("monitoring:prometheus", 3)]
    sent = source.seen[0].headers
    assert sent["Authorization"] == f"Bearer {PROM_TOKEN}"
    assert sent["X-Scope-OrgID"] == TENANT


def test_ut01_80_registered_builtin() -> None:
    """UT01-80 the registry resolves `monitoring_adapter` `prometheus` to the adapter."""
    assert registry.get("monitoring_adapter", "prometheus") is PrometheusAdapter
    assert PrometheusAdapter.tool == "prometheus"
