"""Tests for herness.core.resilience.classify: classify and parse_retry_after (T08-04).

Secret-looking fixtures (`api_key=...`) are built at runtime so detect-secrets stays quiet.
"""

import concurrent.futures
import email.utils
import sqlite3
import subprocess
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core import resilience
from herness.core.errors import (
    AuthError,
    ConfigError,
    FatalError,
    HernessError,
    ModelUnavailable,
    QueryError,
    RateLimited,
    SourceUnavailable,
    StoreBusy,
)
from herness.core.resilience.classify import classify, parse_retry_after

# The package attribute `classify` is the U08-16 function; the module comes from sys.modules.
cm = sys.modules["herness.core.resilience.classify"]
pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
FAMILIES = ("source", "model", "decider", "store")
UNAVAILABLE: dict[str, type[HernessError]] = {
    "source": SourceUnavailable,
    "model": ModelUnavailable,
    "decider": ModelUnavailable,
    "store": StoreBusy,
}
NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
KEY_TEXT = "api" + "_key=" + "synthetic" + "_key_abc"  # R-67 fixture, built at runtime
EMAIL = "ops.person@example.com"
REQ = httpx.Request("GET", "http://127.0.0.1/x")


def _status_error(status: int, body: str = "", **headers: str) -> httpx.HTTPStatusError:
    resp = httpx.Response(status, request=REQ, text=body, headers=headers)
    return httpx.HTTPStatusError("bad status", request=REQ, response=resp)


def _classify(exc: BaseException, family: str) -> HernessError:
    return classify(exc, family=family)  # type: ignore[arg-type]


# --- UT08-10 classify rules -----------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, AuthError), (403, AuthError), (418, ConfigError), (429, RateLimited)]
    + [(s, None) for s in (500, 502, 503, 504, 529)],
)
def test_ut08_10_http_status_mapping(
    family: str, status: int, expected: type[HernessError] | None
) -> None:
    """UT08-10 HTTP 429/401/403/5xx/other map to RateLimited/AuthError/unavailable/Config."""
    err = _classify(_status_error(status), family)
    assert type(err) is (expected or UNAVAILABLE[family])
    assert err.message == f"{family} call failed: HTTPStatusError HTTP {status}" + (
        f": unexpected HTTP {status}" if status == 418 else ""
    )


@pytest.mark.usefixtures("reset_process_state")
def test_ut08_10_429_carries_parsed_retry_after(herness_cfg: object) -> None:
    """UT08-10 429 → RateLimited with Retry-After parsed (and clamped by config max)."""
    err = _classify(_status_error(429, **{"Retry-After": "7"}), "source")
    assert isinstance(err, RateLimited)
    assert err.retry_after == 7
    err = _classify(_status_error(429, **{"retry-after": "9999999"}), "model")
    assert isinstance(err, RateLimited)
    assert err.retry_after == 86400
    err = _classify(_status_error(429), "model")
    assert isinstance(err, RateLimited)
    assert err.retry_after is None


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("slow"),
        httpx.ReadTimeout("slow"),
        httpx.WriteTimeout("slow"),
        httpx.PoolTimeout("slow"),
        httpx.RemoteProtocolError("eof"),
        TimeoutError("t"),
        concurrent.futures.TimeoutError(),
    ],
    ids=lambda e: type(e).__name__,
)
def test_ut08_10_transport_errors_are_unavailable(family: str, exc: BaseException) -> None:
    """UT08-10 httpx transport errors and TimeoutError → unavailable for the family."""
    err = _classify(exc, family)
    assert type(err) is UNAVAILABLE[family]
    assert err.message == f"{family} call failed: {type(exc).__name__}"


def _fake_sdk(name: str, *, overloaded: bool) -> types.ModuleType:
    mod = types.ModuleType(name)

    class APIConnectionError(Exception):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    class APIStatusError(Exception):
        def __init__(self, status: int, body: str = "", **headers: str) -> None:
            super().__init__("status")
            self.status_code = status
            self.response = httpx.Response(status, request=REQ, text=body, headers=headers)

    mod.APIConnectionError = APIConnectionError  # type: ignore[attr-defined]
    mod.APITimeoutError = APITimeoutError  # type: ignore[attr-defined]
    mod.APIStatusError = APIStatusError  # type: ignore[attr-defined]
    if overloaded:

        class OverloadedError(APIStatusError):
            pass

        mod.OverloadedError = OverloadedError  # type: ignore[attr-defined]
    return mod


@pytest.mark.parametrize("sdk", ["openai", "anthropic"])
@pytest.mark.parametrize("family", FAMILIES)
def test_ut08_10_fake_sdk_errors(sdk: str, family: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-10 SDK connection/timeout → ModelUnavailable; APIStatusError → the HTTP rule."""
    mod = _fake_sdk(sdk, overloaded=sdk == "anthropic")
    monkeypatch.setitem(sys.modules, sdk, mod)
    assert type(_classify(mod.APIConnectionError(), family)) is ModelUnavailable
    assert type(_classify(mod.APITimeoutError(), family)) is ModelUnavailable
    err = _classify(mod.APIStatusError(401), family)
    assert type(err) is AuthError
    assert err.message == f"{family} call failed: APIStatusError HTTP 401"
    assert type(_classify(mod.APIStatusError(503), family)) is UNAVAILABLE[family]
    assert type(_classify(mod.APIStatusError(418), family)) is ConfigError
    assert type(_classify(mod.APIStatusError(429), family)) is RateLimited
    if sdk == "anthropic":
        assert type(_classify(mod.OverloadedError(529), family)) is ModelUnavailable


@pytest.mark.parametrize("family", FAMILIES)
def test_ut08_10_old_anthropic_without_overloaded_error(
    family: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-10 an anthropic module without OverloadedError still maps 529 via rule 2."""
    mod = _fake_sdk("anthropic", overloaded=False)
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    assert type(_classify(mod.APIStatusError(529), family)) is UNAVAILABLE[family]


def test_ut08_10_sdk_classes_ignored_when_module_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-10 without the SDK in sys.modules its look-alike classes are unclassified."""
    mod = _fake_sdk("openai", overloaded=False)
    monkeypatch.delitem(sys.modules, "openai", raising=False)
    err = _classify(mod.APIConnectionError(), "model")
    assert type(err) is FatalError
    assert err.message == "model call failed: APIConnectionError: unclassified APIConnectionError"


@pytest.mark.parametrize("family", FAMILIES)
def test_ut08_10_sqlite_errors(family: str) -> None:
    """UT08-10 sqlite locked/busy → StoreBusy; other OperationalError → FatalError."""
    assert type(_classify(sqlite3.OperationalError("database is locked"), family)) is StoreBusy
    assert type(_classify(sqlite3.OperationalError("Database BUSY"), family)) is StoreBusy
    err = _classify(sqlite3.OperationalError("no such table: secret_table"), family)
    assert type(err) is FatalError
    assert err.message == f"{family} call failed: OperationalError: sqlite error"
    assert "secret_table" not in err.message


@pytest.mark.parametrize("family", FAMILIES)
def test_ut08_10_fake_duckdb_errors(family: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-10 duckdb InterruptException → QueryError(timeout); IOException lock → StoreBusy."""
    mod = types.ModuleType("duckdb")
    mod.InterruptException = type("InterruptException", (Exception,), {})  # type: ignore[attr-defined]
    mod.IOException = type("IOException", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "duckdb", mod)
    err = _classify(mod.InterruptException("INTERRUPT"), family)
    assert type(err) is QueryError
    assert err.context["timeout"] is True
    assert err.message.endswith(": query interrupted after timeout")
    assert type(_classify(mod.IOException("Could not set LOCK on file"), family)) is StoreBusy
    other = _classify(mod.IOException("disk full"), family)
    assert type(other) is FatalError
    assert other.message == f"{family} call failed: IOException: unclassified IOException"


@pytest.mark.parametrize("family", FAMILIES)
def test_ut08_10_herness_error_unchanged_and_other_unclassified(family: str) -> None:
    """UT08-10 a HernessError is returned unchanged; ValueError → FatalError without text."""
    original = AuthError("nope")
    assert _classify(original, family) is original
    err = _classify(ValueError("customer text"), family)
    assert type(err) is FatalError
    assert err.message == f"{family} call failed: ValueError: unclassified ValueError"


def test_ut08_10_classify_imports_no_sdk() -> None:
    """UT08-10 importing classify leaves openai, anthropic and duckdb out of sys.modules."""
    code = (
        "import sys, herness.core.resilience.classify\n"
        "print(','.join(m for m in ('openai', 'anthropic', 'duckdb') if m in sys.modules))"
    )
    out = subprocess.run(  # noqa: S603 - fixed argv, this interpreter
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


def test_rf_package_classify_is_callable_in_either_import_order() -> None:
    """RF resilience.classify stays callable although a submodule shares its name."""
    code = (
        "import herness.core.resilience.classify\n"
        "from herness.core import resilience\n"
        "print(type(resilience.classify(TimeoutError(), family='model')).__name__)\n"
        "from herness.core.resilience import classify\n"
        "print(type(classify(TimeoutError(), family='source')).__name__)\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed argv, this interpreter
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.split() == ["ModelUnavailable", "SourceUnavailable"]
    err = resilience.classify(TimeoutError(), family="model")
    assert type(err) is ModelUnavailable


# --- UT08-11 message bounds and redaction ---------------------------------------------------


@pytest.mark.usefixtures("test_redactor")
def test_ut08_11_body_is_redacted_cut_and_bounded() -> None:
    """UT08-11 5 000-char 503 body: message ≤ 2 KB, detail ≤ 500, no e-mail or key text."""
    body = f"upstream failed for {EMAIL} using {KEY_TEXT}; " + "x" * 5000
    body = body[:5000]
    err = _classify(_status_error(503, body), "model")
    assert type(err) is ModelUnavailable
    prefix = "model call failed: HTTPStatusError HTTP 503: "
    assert err.message.startswith(prefix)
    assert len(err.message[len(prefix) :]) <= 500
    assert len(err.message.encode("utf-8")) <= 2048
    assert EMAIL not in err.message
    assert "synthetic" not in err.message
    assert "upstream failed for [EMAIL_" in err.message


@pytest.mark.usefixtures("test_redactor")
def test_ut08_11_email_straddling_char_500_is_redacted() -> None:
    """UT08-11 redact before cut (controller ruling): an e-mail across char 500 leaves no trace."""
    body = "y" * 494 + " leakyname@example.com tail"
    err = _classify(_status_error(502, body), "source")
    assert type(err) is SourceUnavailable
    assert "leaky" not in err.message
    detail = err.message.split(": ", 2)[2]
    assert len(detail) == 500


def test_ut08_11_redaction_failure_omits_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-11 redact_text returning None (fail closed) gives no detail part."""
    monkeypatch.setattr(cm, "redact_text", lambda _text: None)
    err = _classify(_status_error(503, f"hello {EMAIL}"), "model")
    assert err.message == "model call failed: HTTPStatusError HTTP 503"
    err = _classify(_status_error(418, f"hello {EMAIL}"), "model")
    assert err.message == "model call failed: HTTPStatusError HTTP 418: unexpected HTTP 418"


def test_ut08_11_redaction_raising_omits_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-11 a redactor that raises (e.g. no key) also fails closed to no detail."""

    def boom(_text: str) -> str:
        msg = "secret not found: redaction key"
        raise ConfigError(msg)

    monkeypatch.setattr(cm, "redact_text", boom)
    err = _classify(_status_error(500, "body"), "decider")
    assert err.message == "decider call failed: HTTPStatusError HTTP 500"


def test_ut08_11_multibyte_message_within_2kb(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-11 a multi-byte body still yields a message of at most 2 048 UTF-8 bytes."""
    monkeypatch.setattr(cm, "redact_text", lambda text: text)
    err = _classify(_status_error(503, "\U0001f600" * 3000), "model")
    assert len(err.message.encode("utf-8")) <= 2048


def test_ut08_11_unreadable_body_gives_no_detail() -> None:
    """UT08-11 a streamed (unread) response body is skipped, never raised."""
    resp = httpx.Response(503, request=REQ, stream=httpx.ByteStream(b"secret stream"))
    exc = httpx.HTTPStatusError("bad", request=REQ, response=resp)
    err = _classify(exc, "source")
    assert err.message == "source call failed: HTTPStatusError HTTP 503"


# --- UT08-112 / PT08-03 parse_retry_after ----------------------------------------------------


def _date(delta_s: float) -> str:
    return email.utils.format_datetime(NOW + timedelta(seconds=delta_s), usegmt=True)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "120"}, 120.0),
        ({"retry-after": " 120 "}, 120.0),
        ({"Retry-After": "1.5"}, None),
        ({"Retry-After": _date(60)}, 60.0),
        ({"Retry-After": _date(-60)}, 0.0),
        ({"Retry-After": "Sat, 26 Sep 2026 12:01:00 -0000"}, None),
        ({"Retry-After": "soon"}, None),
        ({"Retry-After": "999999"}, 3600.0),
        ({}, None),
        ({"X-RateLimit-Reset": str(int(NOW.timestamp()) + 90)}, 90.0),
        ({"x-ratelimit-reset": str(int(NOW.timestamp() * 1000) + 45_000)}, 45.0),
        ({"X-RateLimit-Reset": "30"}, 30.0),
        ({"X-RateLimit-Reset": "2.5"}, 2.5),
        ({"Retry-After": "soon", "X-RateLimit-Reset": "30"}, 30.0),
        ({"Retry-After": "10", "X-RateLimit-Reset": "30"}, 10.0),
        ({"X-RateLimit-Reset": "later"}, None),
        ({"Retry-After": "١٢"}, None),
    ],
)
def test_ut08_112_parse_retry_after(headers: dict[str, str], expected: float | None) -> None:
    """UT08-112 delay-seconds, HTTP dates, invalid forms, clamping and X-RateLimit-Reset."""
    got = parse_retry_after(headers, NOW, max_s=3600)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected, abs=1e-3)


def test_ut08_112_httpx_headers_and_config_default(herness_cfg: object) -> None:
    """UT08-112 httpx.Headers work; max_s None reads R.retry.retry_after_max_s (86 400)."""
    headers = httpx.Headers({"RETRY-AFTER": "9999999999"})
    assert parse_retry_after(headers, NOW) == 86400.0
    assert parse_retry_after(httpx.Headers({"Retry-After": "5"}), now=NOW) == 5.0


@given(
    headers=st.dictionaries(
        st.sampled_from(["Retry-After", "retry-after", "X-RateLimit-Reset", "Other"]),
        st.one_of(st.text(), st.integers(0, 10**17).map(str), st.just(_date(30))),
    ),
    max_s=st.floats(min_value=1e-6, max_value=1e9),
)
def test_pt08_03_never_raises_and_bounded(headers: dict[str, str], max_s: float) -> None:
    """PT08-03 any header strings and max_s > 0: never raises; None or within [0, max_s]."""
    got = parse_retry_after(headers, NOW, max_s=max_s)
    assert got is None or 0.0 <= got <= max_s


@given(delay=st.integers(0, 9_999_999_999), max_s=st.floats(1, 1e11))
def test_pt08_03_integer_delay_seconds_exact(delay: int, max_s: float) -> None:
    """PT08-03 integer delay-seconds below max_s are parsed exactly."""
    got = parse_retry_after({"Retry-After": str(delay)}, NOW, max_s=max_s)
    assert got == (float(delay) if delay < max_s else max_s)
