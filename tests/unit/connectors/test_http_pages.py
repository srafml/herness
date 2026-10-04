"""Tests for herness.connectors.http.SourceHttp, JsonPage and CursorGuard
(impl 01 U01-59; T01-14; TH01-02, TH01-04, TH01-06).

Pages come from ``httpx2.MockTransport`` clients (respx patches only ``httpx``), or from the
real egress-built client over ``MockNet`` where the transport's own guard matters.
"""

from __future__ import annotations

import datetime
import json
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from structlog.testing import capture_logs
from tests.support.egress_mock import MockNet
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors._http_data import (
    SYNTHETIC_TOKEN,
    SyntheticBearer,
    big_chunks,
    bind_resilience,
    load_sources,
    mock_client,
    reply,
    sequence,
)

import herness
from herness.connectors import http
from herness.connectors.http import (
    MAX_LINE_BYTES,
    CursorGuard,
    ForeignHostError,
    JsonPage,
    SourceHttp,
    http_client,
)
from herness.core import config as c
from herness.core.errors import (
    AuthError,
    HernessError,
    RateLimited,
    SchemaViolation,
    SourceUnavailable,
)
from herness.core.jobs.outcomes import decide_failure
from herness.core.jobs.ports import JobRow
from herness.core.resilience import ProcessState
from herness.core.resilience.breaker import CircuitBreaker, breaker

pytestmark = pytest.mark.unit

NOW = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def _isolate(
    fake_keyring: MemoryKeyring, ops_store: OpsStoreHandle, reset_process_state: ProcessState
) -> Iterator[None]:
    bind_resilience()
    yield
    c.reset_config()


@pytest.fixture
def slept(reset_process_state: ProcessState) -> list[float]:
    """Every retry sleep (the `process_state().sleep` seam retry.py uses); no real wait."""
    waits: list[float] = []
    reset_process_state.sleep = waits.append
    return waits


def _http(handler: Any, *, auth: httpx2.Auth | None = None) -> SourceHttp:
    return SourceHttp(mock_client(handler), breaker_key="servicenow", auth=auth, clock=lambda: NOW)


# --- UT01-62: size, JSON and retry of one page ------------------------------------------


def test_ut01_62_page_over_64_mib_is_schema_violation(slept: list[float]) -> None:
    """UT01-62 a 65 MiB page (streamed lazily) raises SchemaViolation("response too large")
    while it is read; not retried (fatal)."""
    handler, seen = sequence(reply(chunks=big_chunks(65)))
    with pytest.raises(SchemaViolation, match="response too large"):
        _http(handler).get_json("/api/now/table/incident")
    assert (len(seen), slept) == (1, [])


def test_ut01_62_invalid_json_is_schema_violation(slept: list[float]) -> None:
    """UT01-62 an invalid JSON body raises SchemaViolation("malformed JSON")."""
    handler, _ = sequence(reply(chunks=[b'{"result": [']))
    with pytest.raises(SchemaViolation, match="malformed JSON"):
        _http(handler).get_json("/api/now/table/incident")
    handler, _ = sequence(reply(chunks=[b"\xff\xfe not utf-8"]))
    with pytest.raises(SchemaViolation, match="malformed JSON"):
        _http(handler).get_json("/api/now/table/incident")
    assert slept == []


@pytest.mark.parametrize("status", [401, 403])
def test_ut01_62_auth_error_force_opens_the_source_breaker_once_without_retry(
    status: int, slept: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-62 (08 §9.2) a 401/403 page raises AuthError after exactly one request: no retry,
    `force_open` called once with that error on the breaker of the source key."""
    handler, seen = sequence(reply(status), reply(body={"result": []}))
    forced: list[tuple[str, HernessError]] = []
    original = CircuitBreaker.force_open

    def spy(self: CircuitBreaker, err: HernessError) -> None:
        forced.append((self.key, err))
        original(self, err)

    monkeypatch.setattr(CircuitBreaker, "force_open", spy)
    with pytest.raises(AuthError) as caught:
        _http(handler).get_json("/p")
    assert len(seen) == 1
    assert slept == []
    assert len(forced) == 1
    assert forced[0][0] == "servicenow"
    assert forced[0][1] is caught.value
    assert breaker("servicenow").state() == "open"


def test_ut01_62_503_twice_then_200_retries_only_that_page(slept: list[float]) -> None:
    """UT01-62 503, 503, 200 on page 2: the third attempt returns; page 1 is not refetched."""
    handler, seen = sequence(
        reply(body={"result": [1]}),
        reply(503),
        reply(503),
        reply(body={"result": [2]}),
    )
    source = _http(handler)
    first = source.get_json("/p", params={"page": 1})
    second = source.get_json("/p", params={"page": 2})
    assert (first.body, second.body) == ({"result": [1]}, {"result": [2]})
    assert [r.url.params["page"] for r in seen] == ["1", "2", "2", "2"]
    assert len(slept) == 2


def test_ut01_62_json_page_fields_and_default_headers() -> None:
    """UT01-62 a page has status, decoded body, headers and absolute `links` by rel; the
    request carries the herness User-Agent, Accept JSON, caller headers and the auth."""
    link = '</api/x?page=2>; rel="next", <https://sn.example/api/x?page=1>; rel="first"'
    handler, seen = sequence(reply(body={"a": 1}, headers={"Link": link, "X-Total": "3"}))
    page = _http(handler, auth=SyntheticBearer()).get_json(
        "/api/x", params={"page": 1}, headers={"X-Extra": "1", "Accept": "application/json"}
    )
    assert isinstance(page, JsonPage)
    assert (page.status, page.body, page.headers["x-total"]) == (200, {"a": 1}, "3")
    assert page.links == {
        "next": "https://sn.example/api/x?page=2",
        "first": "https://sn.example/api/x?page=1",
    }
    sent = seen[0].headers
    assert sent["User-Agent"] == f"herness/{herness.__version__}"
    assert (sent["Accept"], sent["X-Extra"]) == ("application/json", "1")
    assert sent["Authorization"] == f"Bearer {SYNTHETIC_TOKEN}"


def test_ut01_62_post_json_and_allow_status(slept: list[float]) -> None:
    """UT01-62 post_json sends the JSON body; a status in allow_status is returned as a page
    (an empty allowed body decodes to None) instead of being mapped to an error."""
    handler, seen = sequence(reply(body={"issues": []}), reply(404, chunks=[]))
    source = _http(handler)
    page = source.post_json("/rest/api/3/search/jql", json_body={"jql": "project = X"})
    assert page.body == {"issues": []}
    assert (seen[0].method, json.loads(seen[0].content)) == ("POST", {"jql": "project = X"})
    missing = source.get_json("/rest/api/3/changelog/bulkfetch", allow_status=frozenset({404}))
    assert (missing.status, missing.body) == (404, None)
    assert slept == []


def test_ut01_62_page_logs_metrics_and_fault_point(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """UT01-62 every attempt passes fault_point("http.page", source=...); a fetched page logs
    connectors.http.page_fetched (DEBUG) and counts herness_connectors_pages_total."""
    points: list[tuple[str, dict[str, str]]] = []
    counters: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(http, "fault_point", lambda name, **kw: points.append((name, kw)))
    monkeypatch.setattr(http, "record_counter", lambda name, **kw: counters.append((name, kw)))
    handler, _ = sequence(reply(503), reply(body={"ok": True}))
    with capture_logs() as logs:
        _http(handler).get_json("/p")
    assert points == [("http.page", {"source": "servicenow"})] * 2
    fetched = [e for e in logs if e["event"] == "connectors.http.page_fetched"]
    assert len(fetched) == 1
    assert fetched[0]["log_level"] == "debug"
    assert (fetched[0]["source"], fetched[0]["status"], fetched[0]["bytes"]) == (
        "servicenow",
        200,
        len(b'{"ok": true}'),
    )
    assert fetched[0]["elapsed_ms"] >= 0
    labels = [kw["labels"] for name, kw in counters if name == "herness_connectors_pages_total"]
    assert labels == [
        {"source": "servicenow", "status_class": "5xx"},
        {"source": "servicenow", "status_class": "2xx"},
    ]
    assert len(slept) == 1


def test_ut01_62_transport_error_is_classified(slept: list[float]) -> None:
    """UT01-62 an httpx2 transport error is classified (SourceUnavailable) and retried."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        msg = "synthetic refused"
        raise httpx2.ConnectError(msg, request=request)

    with pytest.raises(SourceUnavailable) as info:
        _http(handler).get_json("/p")
    assert "synthetic" not in info.value.message
    assert len(slept) == 5  # six attempts of source_http_page


# --- ST01-02: foreign next links never receive a request or the bearer ------------------


def test_st01_02_foreign_link_and_odata_next_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST01-02 `Link: <https://evil.example/...>; rel="next"` and an `@odata.nextLink` to
    another host raise ForeignHostError; the other host gets no request, so no bearer."""
    net = MockNet().install(monkeypatch)
    evil_link = '<https://evil.example/api/now/table/incident?p=2>; rel="next"'
    net.route(
        "sn.example", "/api/now/table/incident", json={"result": []}, headers={"Link": evil_link}
    )
    net.route(
        "sn.example",
        "/api/data/v9.2/accounts",
        json={"@odata.nextLink": "https://evil.example/odata?p=2", "value": []},
    )
    evil = net.route("evil.example", json={})
    settings = load_sources(tmp_path)
    client = http_client(settings, source="servicenow", max_concurrency=1)
    source = SourceHttp(client, breaker_key="servicenow", auth=SyntheticBearer())

    page = source.get_json("/api/now/table/incident")
    with pytest.raises(ForeignHostError):
        source.check_next_url(page.links["next"])
    odata = source.get_json("/api/data/v9.2/accounts")
    assert isinstance(odata.body, dict)
    with pytest.raises(ForeignHostError):
        source.check_next_url(odata.body["@odata.nextLink"])

    assert evil.call_count == 0
    sent = [r.headers.get("Authorization") for route in net.routes for r in route.calls]
    assert sent == [f"Bearer {SYNTHETIC_TOKEN}"] * 2  # only the source host saw the bearer


@pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
def test_st01_02_bearer_and_query_never_in_errors_or_logs(status: int, slept: list[float]) -> None:
    """ST01-02 with a bearer and a query string on the request, the raised error (message,
    context, details) and every log event of the fetch and its retries carry neither."""
    headers = {"Retry-After": "1"} if status == 429 else {}
    handler = lambda _r: reply(status, body={"error": "synthetic body"}, headers=headers)  # noqa: E731
    source = _http(handler, auth=SyntheticBearer())
    with capture_logs() as logs, pytest.raises(Exception) as info:  # noqa: PT011 - any mapped error
        source.get_json("/api/x", params={"sysparm_query": "synthetic_filter"})
    err = info.value
    text = " ".join([str(err), repr(err), str(getattr(err, "context", "")), str(err.__dict__)])
    for leaked in (
        SYNTHETIC_TOKEN,
        "Bearer",
        "sysparm_query",
        "synthetic_filter",
        "synthetic body",
    ):
        assert leaked not in text
        assert leaked not in repr(logs)
    assert "/api/x" in str(err)


# --- ST01-04: endless pagination and oversized pages ------------------------------------


def test_st01_04_repeated_cursor_is_schema_violation() -> None:
    """ST01-04 a source that returns the same cursor forever is stopped by CursorGuard."""
    handler = lambda _r: reply(body={"data": [1], "meta": {"after": "same"}})  # noqa: E731
    source, guard = _http(handler), CursorGuard()

    def paginate() -> None:
        cursor = ""
        while True:
            page = source.get_json("/api/v2/events", params={"cursor": cursor})
            assert isinstance(page.body, dict)
            cursor = str(page.body["meta"]["after"])
            guard.step(cursor)

    with pytest.raises(SchemaViolation, match="pagination cursor repeated"):
        paginate()


def test_st01_04_page_limit_is_schema_violation() -> None:
    """ST01-04 distinct cursors past the page limit raise "page limit exceeded"."""
    guard = CursorGuard(max_pages=3)
    for n in range(3):
        guard.step(f"c{n}")
    with pytest.raises(SchemaViolation, match="page limit exceeded"):
        guard.step("c3")
    assert CursorGuard().max_pages == http.MAX_PAGES_PER_STREAM == 1_000_000


def test_st01_04_egress_client_caps_65_mib_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """ST01-04 through the real egress client (cap = MAX_RESPONSE_BYTES) a 65 MiB page is
    refused by the transport while read and surfaces as SchemaViolation, not EgressBlocked."""
    net = MockNet().install(monkeypatch)
    net.route("sn.example", content=big_chunks(65))
    client = http_client(load_sources(tmp_path), source="servicenow", max_concurrency=1)
    with pytest.raises(SchemaViolation, match="response too large"):
        SourceHttp(client, breaker_key="servicenow", auth=None).get_json("/api/x")
    assert slept == []


# --- ST01-06: a huge Retry-After is not slept on -----------------------------------------


def test_st01_06_huge_retry_after_reraised_and_job_rescheduled(slept: list[float]) -> None:
    """ST01-06 429 with `Retry-After: 1000000`: RateLimited is re-raised by retry_page at
    once (over retry_after_cap_s 300) with no sleep; the job is rescheduled, not blocked."""
    handler, seen = sequence(reply(429, headers={"Retry-After": "1000000"}))
    with pytest.raises(RateLimited) as info:
        _http(handler).get_json("/api/x")
    assert (len(seen), slept) == (1, [])
    assert info.value.retry_after == 86400.0  # clamped to resilience.retry.retry_after_max_s
    row = JobRow(
        job_id="job_01J0000000000000000000000A",
        kind="sync",
        gpu_class="none",
        status="running",
        priority=60,
        attempts=1,
        max_attempts=3,
        payload={},
    )
    action = decide_failure(info.value, row, NOW, rng=random.Random(0))
    assert action.action == "requeue"
    assert action.scheduled_for == NOW + datetime.timedelta(seconds=86400)


def test_st01_06_small_retry_after_is_waited_then_retried(slept: list[float]) -> None:
    """ST01-06 control: a Retry-After under the cap is honoured by one sleep, then retried."""
    handler, seen = sequence(reply(429, headers={"Retry-After": "7"}), reply(body={}))
    assert _http(handler).get_json("/api/x").status == 200
    assert (len(seen), slept) == (2, [7.0])


# --- post_form_lines (U01-59; FT01-04 is the fault test of T01-19) -----------------------


def test_ut01_62_form_lines_yields_objects(slept: list[float]) -> None:
    """UT01-62 post_form_lines posts form data and yields one object per non-empty line,
    across chunk boundaries."""
    chunks = [b'{"a": 1}\n\n{"b"', b": 2}\r\n", b'{"c": 3}']
    handler, seen = sequence(reply(503), reply(chunks=chunks))
    rows = list(_http(handler).post_form_lines("/services/search/jobs/export", data={"q": "x"}))
    assert rows == [{"a": 1}, {"b": 2}, {"c": 3}]
    assert (seen[1].method, seen[1].content) == ("POST", b"q=x")
    assert len(slept) == 1


def test_ut01_62_form_lines_limits_and_errors(slept: list[float]) -> None:
    """UT01-62 a line over MAX_LINE_BYTES, an invalid JSON line or a JSON non-object line
    raise SchemaViolation; a non-2xx status maps like any page; an empty export yields none."""
    long_line = [b"{" + b" " * MAX_LINE_BYTES]
    cases = [
        (long_line, "line too large"),
        ([b"not json\n"], "malformed JSON"),
        ([b"[1]\n"], "malformed JSON"),
    ]
    for chunks, message in cases:
        handler, _ = sequence(reply(chunks=chunks))
        with pytest.raises(SchemaViolation, match=message):
            list(_http(handler).post_form_lines("/export", data={}))
    handler, _ = sequence(reply(400))
    with pytest.raises(Exception, match="source rejected request"):
        list(_http(handler).post_form_lines("/export", data={}))
    handler, _ = sequence(reply(chunks=[b"\n"]))
    assert list(_http(handler).post_form_lines("/export", data={})) == []
    assert slept == []


def test_ut01_62_form_lines_later_stream_error_not_retried(slept: list[float]) -> None:
    """UT01-62 a stream error after the first line raises SourceUnavailable without retry."""

    def chunks() -> Iterator[bytes]:
        yield b'{"a": 1}\n'
        msg = "synthetic reset"
        raise httpx2.ReadError(msg)

    handler, seen = sequence(reply(chunks=chunks()))
    lines = _http(handler).post_form_lines("/export", data={})
    assert next(lines) == {"a": 1}
    with pytest.raises(SourceUnavailable, match="stream interrupted"):
        next(lines)
    assert (len(seen), slept) == (1, [])
