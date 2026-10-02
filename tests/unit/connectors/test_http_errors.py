"""Tests for herness.connectors.http.map_http_error and its two SchemaViolation subclasses
(impl 01 U01-60, U01-62; T01-14; TH01-03)."""

from __future__ import annotations

import copy
import datetime

import httpx2
import pytest

from herness.connectors.http import ForeignHostError, SourceNotFound, map_http_error
from herness.core.errors import (
    AuthError,
    ConfigError,
    FatalError,
    HernessError,
    RateLimited,
    SchemaViolation,
    SourceUnavailable,
)

pytestmark = pytest.mark.unit

NOW = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.UTC)
URL = "https://sn.example/api/now/table/incident?sysparm_query=synthetic_filter"
BODY = b'{"error": "synthetic body detail"}'


def _response(
    status: int, method: str = "GET", headers: dict[str, str] | None = None
) -> httpx2.Response:
    request = httpx2.Request(method, URL)
    return httpx2.Response(status, headers=headers or {}, content=BODY, request=request)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (302, SchemaViolation),
        (400, ConfigError),
        (401, AuthError),
        (403, AuthError),
        (404, SourceNotFound),
        (408, SourceUnavailable),
        (429, RateLimited),
        (500, SourceUnavailable),
        (529, SourceUnavailable),
        (418, SchemaViolation),
    ],
)
def test_ut01_60_status_table(status: int, expected: type[HernessError]) -> None:
    """UT01-60 each status maps to the U01-60 table class; the message carries status,
    method and path, never the query string or the body."""
    err = map_http_error(_response(status, "POST"), now=NOW)
    assert type(err) is expected
    assert err is not None
    assert f"HTTP {status} POST /api/now/table/incident" in err.message
    assert "sysparm" not in err.message
    assert "synthetic" not in err.message
    assert "synthetic" not in str(err.context) + str(err.details)


def test_ut01_60_success_maps_to_none() -> None:
    """UT01-60 a 200 response is not an error."""
    assert map_http_error(_response(200), now=NOW) is None


def test_ut01_60_redirect_and_unexpected_status_reasons() -> None:
    """UT01-60 3xx is "unexpected redirect", another 4xx "unexpected status"."""
    redirect = map_http_error(_response(302), now=NOW)
    other = map_http_error(_response(418), now=NOW)
    assert redirect is not None
    assert other is not None
    assert redirect.message.startswith("unexpected redirect")
    assert other.message.startswith("unexpected status")
    assert map_http_error(_response(400), now=NOW).message.startswith("source rejected request")  # type: ignore[union-attr]


def test_ut01_60_429_retry_after_seconds() -> None:
    """UT01-60 a 429 with `Retry-After: 7` carries retry_after == 7.0 (parsed by impl 08)."""
    err = map_http_error(_response(429, headers={"Retry-After": "7"}), now=NOW)
    assert isinstance(err, RateLimited)
    assert err.retry_after == 7.0


def test_ut01_60_429_without_header_has_no_retry_after() -> None:
    """UT01-60 a 429 without a usable header carries retry_after None."""
    err = map_http_error(_response(429, headers={"Retry-After": "soon"}), now=NOW)
    assert isinstance(err, RateLimited)
    assert err.retry_after is None


def test_ut01_60_404_is_source_not_found_with_path() -> None:
    """UT01-60 404 is SourceNotFound (a SchemaViolation) carrying the path without query."""
    err = map_http_error(_response(404), now=NOW)
    assert isinstance(err, SourceNotFound)
    assert isinstance(err, SchemaViolation)
    assert err.path == "/api/now/table/incident"


def test_ut01_60_response_without_request_still_maps() -> None:
    """UT01-60 a response built without a request maps by status alone."""
    err = map_http_error(httpx2.Response(503), now=NOW)
    assert isinstance(err, SourceUnavailable)
    assert "HTTP 503" in err.message


def test_ut01_63_error_classes_are_fatal_and_copy() -> None:
    """UT01-63 ForeignHostError and SourceNotFound are SchemaViolation (fatal) subclasses
    whose `host` / `path` survive a copy through `__reduce__`."""
    foreign = ForeignHostError("evil.example")
    missing = SourceNotFound("/rest/api/3/issue/1/changelog")
    assert isinstance(foreign, SchemaViolation)
    assert isinstance(foreign, FatalError)
    assert foreign.host == "evil.example"
    assert missing.path == "/rest/api/3/issue/1/changelog"
    again = copy.deepcopy(foreign)
    assert (type(again), again.host, again.message) == (
        ForeignHostError,
        "evil.example",
        foreign.message,
    )
    again_missing = copy.deepcopy(missing)
    assert again_missing.path == missing.path
