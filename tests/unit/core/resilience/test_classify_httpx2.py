"""httpx2 twins of the httpx classify cases (T08-04b): every rule that fires for an httpx
exception fires identically for its httpx2 counterpart (the classes are not subclasses).

Secret-looking fixtures (`api_key=...`) are built at runtime so detect-secrets stays quiet.
"""

import sys
from datetime import UTC, datetime

import httpx
import httpx2
import pytest

from herness.core.errors import (
    AuthError,
    ConfigError,
    FatalError,
    HernessError,
    ModelUnavailable,
    RateLimited,
    SourceUnavailable,
    StoreBusy,
)
from herness.core.resilience.breaker import breaker_transition
from herness.core.resilience.classify import classify
from herness.core.resilience.settings import BreakerSettings

cm = sys.modules["herness.core.resilience.classify"]

pytestmark = pytest.mark.unit

FAMILIES = ("source", "model", "decider", "store")
UNAVAILABLE: dict[str, type[HernessError]] = {
    "source": SourceUnavailable,
    "model": ModelUnavailable,
    "decider": ModelUnavailable,
    "store": StoreBusy,
}
KEY_TEXT = "api" + "_key=" + "synthetic" + "_key_abc"  # R-67 fixture, built at runtime
EMAIL = "ops.person@example.com"
URL = "http://127.0.0.1/x"
TRANSPORT = (
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
)


def _status_error2(status: int, body: str = "", **headers: str) -> httpx2.HTTPStatusError:
    req = httpx2.Request("GET", URL)
    resp = httpx2.Response(status, request=req, text=body, headers=headers)
    return httpx2.HTTPStatusError("bad status", request=req, response=resp)


def _status_error1(status: int, body: str = "", **headers: str) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", URL)
    resp = httpx.Response(status, request=req, text=body, headers=headers)
    return httpx.HTTPStatusError("bad status", request=req, response=resp)


def _classify(exc: BaseException, family: str) -> HernessError:
    return classify(exc, family=family)  # type: ignore[arg-type]


def _same(a: HernessError, b: HernessError) -> None:
    assert type(a) is type(b)
    assert a.message == b.message
    assert getattr(a, "retry_after", None) == getattr(b, "retry_after", None)


# --- UT08-10 classify rules, httpx2 twins ---------------------------------------------------


def test_ut08_10_httpx2_is_not_an_httpx_subclass() -> None:
    """UT08-10 precondition: httpx2 errors are foreign to httpx, so the twins need own rules."""
    assert not issubclass(httpx2.HTTPStatusError, httpx.HTTPError)
    assert not issubclass(httpx2.ConnectError, httpx.HTTPError)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, AuthError), (403, AuthError), (404, ConfigError), (418, ConfigError)]
    + [(429, RateLimited)]
    + [(s, None) for s in (500, 502, 503, 504, 529)],
)
def test_ut08_10_httpx2_http_status_mapping(
    family: str, status: int, expected: type[HernessError] | None
) -> None:
    """UT08-10 httpx2 429/401/403/5xx/other 4xx map exactly as their httpx twins."""
    err = _classify(_status_error2(status), family)
    assert type(err) is (expected or UNAVAILABLE[family])
    assert err.message == f"{family} call failed: HTTPStatusError HTTP {status}" + (
        f": unexpected HTTP {status}" if expected is ConfigError else ""
    )
    _same(err, _classify(_status_error1(status), family))


@pytest.mark.usefixtures("reset_process_state")
def test_ut08_10_httpx2_429_carries_parsed_retry_after(herness_cfg: object) -> None:
    """UT08-10 httpx2 429 → RateLimited with Retry-After parsed and clamped, like httpx."""
    del herness_cfg
    for headers, want in (({"Retry-After": "7"}, 7.0), ({"retry-after": "9999999"}, 86400.0)):
        err = _classify(_status_error2(429, **headers), "source")
        assert isinstance(err, RateLimited)
        assert err.retry_after == want
        _same(err, _classify(_status_error1(429, **headers), "source"))
    err = _classify(_status_error2(429), "model")
    assert isinstance(err, RateLimited)
    assert err.retry_after is None


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("name", TRANSPORT)
def test_ut08_10_httpx2_transport_errors_are_unavailable(family: str, name: str) -> None:
    """UT08-10 httpx2 connect error, timeouts and remote protocol error → unavailable."""
    exc = getattr(httpx2, name)("boom")
    err = _classify(exc, family)
    assert type(err) is UNAVAILABLE[family]
    assert err.message == f"{family} call failed: {name}"
    _same(err, _classify(getattr(httpx, name)("boom"), family))


def test_ut08_10_other_httpx2_errors_stay_unclassified() -> None:
    """UT08-10 httpx2 errors outside rule 3 (e.g. a decoding error) fall to rule 8, as httpx."""
    err = _classify(httpx2.DecodingError("bad gzip"), "source")
    assert type(err) is FatalError
    assert err.message == "source call failed: DecodingError: unclassified DecodingError"
    _same(err, _classify(httpx.DecodingError("bad gzip"), "source"))


# --- UT08-11 message bounds and redaction, httpx2 twins -------------------------------------


@pytest.mark.usefixtures("test_redactor")
def test_ut08_11_httpx2_body_is_redacted_cut_and_bounded() -> None:
    """UT08-11 httpx2 5 000-char 503 body: message ≤ 2 KB, detail ≤ 500, no e-mail or key."""
    body = (f"upstream failed for {EMAIL} using {KEY_TEXT}; " + "x" * 5000)[:5000]
    err = _classify(_status_error2(503, body), "model")
    assert type(err) is ModelUnavailable
    prefix = "model call failed: HTTPStatusError HTTP 503: "
    assert err.message.startswith(prefix)
    assert len(err.message[len(prefix) :]) <= 500
    assert len(err.message.encode("utf-8")) <= 2048
    assert EMAIL not in err.message
    assert "synthetic" not in err.message
    assert "upstream failed for [EMAIL_" in err.message
    _same(err, _classify(_status_error1(503, body), "model"))


@pytest.mark.usefixtures("test_redactor")
def test_ut08_11_httpx2_redact_before_cut() -> None:
    """UT08-11 httpx2: an e-mail across char 500, and one at the 4 000-char window edge, leak
    nothing (redact first 4 000, drop the last 500 redacted chars, then cut 500)."""
    body = "y" * 494 + " leakyname@example.com tail"
    err = _classify(_status_error2(502, body), "source")
    assert "leaky" not in err.message
    assert len(err.message.split(": ", 2)[2]) == 500
    _same(err, _classify(_status_error1(502, body), "source"))
    edge = "w " * 1995 + "jane.victim@examplecorp.org more " + "z" * 300
    assert edge[: cm._REDACT_WINDOW].endswith("jane.victi")
    err = _classify(_status_error2(503, edge), "model")
    assert "jane" not in err.message
    assert len(err.message.split(": ", 2)[2]) == 500
    _same(err, _classify(_status_error1(503, edge), "model"))


def test_ut08_11_httpx2_multibyte_message_within_2kb(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-11 an httpx2 multi-byte body still yields a message of at most 2 048 UTF-8 bytes."""
    monkeypatch.setattr(cm, "redact_text", lambda text: text)
    err = _classify(_status_error2(503, "\U0001f600" * 3000), "model")
    assert len(err.message.encode("utf-8")) <= 2048
    _same(err, _classify(_status_error1(503, "\U0001f600" * 3000), "model"))


def test_ut08_11_httpx2_redaction_failure_omits_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-11 httpx2: redact_text returning None fails closed to no detail part."""
    monkeypatch.setattr(cm, "redact_text", lambda _text: None)
    err = _classify(_status_error2(503, f"hello {EMAIL}"), "model")
    assert err.message == "model call failed: HTTPStatusError HTTP 503"


def test_ut08_11_httpx2_unreadable_body_gives_no_detail() -> None:
    """UT08-11 a streamed (unread) httpx2 response body is skipped, never raised."""
    req = httpx2.Request("GET", URL)
    resp = httpx2.Response(503, request=req, stream=httpx2.ByteStream(b"secret stream"))
    err = _classify(httpx2.HTTPStatusError("bad", request=req, response=resp), "source")
    assert err.message == "source call failed: HTTPStatusError HTTP 503"


# --- UT08-12 breaker transitions fed by httpx2 classifications ------------------------------


@pytest.mark.parametrize("name", TRANSPORT)
def test_ut08_12_httpx2_failures_drive_the_breaker_like_httpx(name: str) -> None:
    """UT08-12 a classified httpx2 transport error trips the breaker exactly as its twin."""
    settings = BreakerSettings(failure_threshold=1, cooldown_s=60, cooldown_max_s=900)
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    outs = []
    for mod in (httpx2, httpx):
        err = _classify(getattr(mod, name)("down"), "source")
        assert isinstance(err, SourceUnavailable)
        outs.append(breaker_transition(None, "failure", now, settings, key="k", error=err.message))
    (row2, kinds2), (row1, kinds1) = outs
    assert (row2.state, row2.trips, row2.last_error, kinds2) == (
        "open",
        1,
        f"source call failed: {name}",
        ["breaker_open"],
    )
    assert (row2, kinds2) == (row1, kinds1)
