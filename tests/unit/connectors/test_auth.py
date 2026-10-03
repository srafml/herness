"""Tests for herness.connectors.auth (impl 01 U01-63 to U01-65; T01-15): auth objects per
method from the secret shapes table, the OAuth token cache and the MSAL token cache.

The source is an ``httpx2.MockTransport`` client (respx patches only ``httpx``); time is a
``FakeClock`` passed as the ``clock`` parameter; MSAL is a fake app injected through the
module's private ``_msal_app`` factory, so no test opens a socket.
"""

from __future__ import annotations

import base64
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx2
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from tests.support.fake_clock import FIXTURE_START, FakeClock
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.connectors import _auth_data as d
from tests.unit.connectors._http_data import mock_client

from herness.connectors import auth as auth_module
from herness.connectors.auth import (
    MsalTokenProvider,
    OAuthTokenAuth,
    StaticHeaderAuth,
    build_auth,
)
from herness.connectors.settings_base import AuthSettings
from herness.core import config as c
from herness.core import time as clock
from herness.core.errors import AuthError, ConfigError, SchemaViolation, SourceUnavailable

pytestmark = pytest.mark.unit

DV_BASE = "https://org.crm.dynamics.com"


@pytest.fixture(autouse=True)
def _isolate(fake_keyring: MemoryKeyring) -> Iterator[None]:
    yield
    c.reset_config()


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")


def _settings(method: str, ref: str | None = "secret:sn") -> AuthSettings:
    tenant = d.TENANT if method == "msal_client_credentials" else None
    return AuthSettings(method=method, credentials=ref, tenant_id=tenant)


def _send(auth: httpx2.Auth | None, source: d.Source, base: str = d.BASE) -> httpx2.Request:
    client = mock_client(source, base_url=base)
    client.get(d.DATA_PATH, auth=auth)
    return source.data_calls[-1]


def _token_auth(
    source: d.Source, fc: FakeClock, form: dict[str, SecretStr | str] | None = None
) -> OAuthTokenAuth:
    client = mock_client(source)
    form = form or {"grant_type": "client_credentials", "client_id": SecretStr(d.CLIENT_ID)}
    return OAuthTokenAuth(f"{d.BASE}{d.TOKEN_PATH}", form, client=client, clock=fc.now)


# --- UT01-64: build_auth per method ----------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "method", "secret", "expected"),
    [
        (
            "servicenow",
            "basic",
            {"username": d.USERNAME, "password": d.PASSWORD},
            {"Authorization": _basic(d.USERNAME, d.PASSWORD)},
        ),
        (
            "prometheus",
            "basic",
            {"username": d.USERNAME, "password": d.PASSWORD},
            {"Authorization": _basic(d.USERNAME, d.PASSWORD)},
        ),
        (
            "jira",
            "api_token",
            {"email": d.USERNAME, "token": d.PLAIN_TOKEN},
            {"Authorization": _basic(d.USERNAME, d.PLAIN_TOKEN)},
        ),
        ("dynatrace", "api_token", d.PLAIN_TOKEN, {"Authorization": f"Api-Token {d.PLAIN_TOKEN}"}),
        ("jira", "pat", d.PLAIN_TOKEN, {"Authorization": f"Bearer {d.PLAIN_TOKEN}"}),
        ("prometheus", "bearer", d.PLAIN_TOKEN, {"Authorization": f"Bearer {d.PLAIN_TOKEN}"}),
        ("splunk", "bearer", d.PLAIN_TOKEN, {"Authorization": f"Bearer {d.PLAIN_TOKEN}"}),
        (
            "datadog",
            "api_and_app_key",
            {"api_key": d.API_KEY, "app_key": d.APP_KEY},
            {"DD-API-KEY": d.API_KEY, "DD-APPLICATION-KEY": d.APP_KEY},
        ),
    ],
)
def test_ut01_64_static_headers_per_shapes_table(
    source: str, method: str, secret: str | dict[str, str], expected: dict[str, str]
) -> None:
    """UT01-64 each static method sets the headers of the §3.13 shapes table."""
    d.store("sn", secret)
    src = d.Source()
    auth = build_auth(
        _settings(method), source=source, base_url=d.BASE, token_client=mock_client(src)
    )
    assert isinstance(auth, StaticHeaderAuth)
    sent = _send(auth, src)
    for name, value in expected.items():
        assert sent.headers[name] == value
    assert src.token_calls == []


def test_ut01_64_basic_header_equals_httpx2_basic_auth() -> None:
    """UT01-64 the `basic` header is byte-identical to what httpx2.BasicAuth would send."""
    d.store("sn", {"username": d.USERNAME, "password": d.PASSWORD})
    src = d.Source()
    auth = build_auth(
        _settings("basic"), source="servicenow", base_url=d.BASE, token_client=mock_client(src)
    )
    ours = _send(auth, src).headers["Authorization"]
    theirs = _send(httpx2.BasicAuth(d.USERNAME, d.PASSWORD), src).headers["Authorization"]
    assert ours == theirs


@pytest.mark.parametrize(
    ("method", "secret", "grant"),
    [
        (
            "oauth_client_credentials",
            {"client_id": d.CLIENT_ID, "client_secret": d.CLIENT_SECRET},
            {"grant_type": "client_credentials"},
        ),
        (
            "oauth_password",
            {
                "client_id": d.CLIENT_ID,
                "client_secret": d.CLIENT_SECRET,
                "username": d.USERNAME,
                "password": d.PASSWORD,
            },
            {"grant_type": "password", "username": d.USERNAME, "password": d.PASSWORD},
        ),
    ],
)
def test_ut01_64_servicenow_oauth_posts_form_to_oauth_token_do(
    method: str, secret: dict[str, str], grant: dict[str, str]
) -> None:
    """UT01-64 ServiceNow OAuth: one form POST to `{base_url}/oauth_token.do` carrying the
    grant and the client credentials; the data request carries the fetched bearer."""
    d.store("sn", secret)
    src = d.Source()
    auth = build_auth(
        _settings(method), source="servicenow", base_url=d.BASE, token_client=mock_client(src)
    )
    assert isinstance(auth, OAuthTokenAuth)
    sent = _send(auth, src)
    assert len(src.token_calls) == 1
    token_request = src.token_calls[0]
    assert token_request.method == "POST"
    assert str(token_request.url) == f"{d.BASE}{d.TOKEN_PATH}"
    expected = {"client_id": d.CLIENT_ID, "client_secret": d.CLIENT_SECRET} | grant
    assert d.form_of(token_request) == expected
    assert sent.headers["Authorization"] == f"Bearer {d.ACCESS}1"


@pytest.mark.parametrize(
    ("secret", "credential"),
    [
        ({"client_id": d.CLIENT_ID, "client_secret": d.CLIENT_SECRET}, d.CLIENT_SECRET),
        (
            {"client_id": d.CLIENT_ID, "certificate_pem": d.CERT_PEM, "thumbprint": d.THUMBPRINT},
            {"private_key": d.CERT_PEM, "thumbprint": d.THUMBPRINT},
        ),
    ],
)
def test_ut01_64_dataverse_msal_secret_or_certificate(
    secret: dict[str, str], credential: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-64 Dataverse: MsalTokenProvider with authority of the tenant, the client secret
    or the certificate dict, and scope `{base_url}/.default`."""
    d.store("sn", secret)
    factory = d.MsalFactory(d.FakeMsalApp([d.msal_token(1)]))
    monkeypatch.setattr(auth_module, "_msal_app", factory)
    src = d.Source()
    auth = build_auth(
        _settings("msal_client_credentials"),
        source="dataverse",
        base_url=DV_BASE,
        token_client=mock_client(src, base_url=DV_BASE),
    )
    assert isinstance(auth, MsalTokenProvider)
    sent = _send(auth, src, DV_BASE)
    assert sent.headers["Authorization"] == f"Bearer {d.ACCESS}1"
    assert factory.built == [
        {
            "client_id": d.CLIENT_ID,
            "credential": credential,
            "authority": f"https://login.microsoftonline.com/{d.TENANT}",
        }
    ]
    assert factory.app.calls == [[f"{DV_BASE}/.default"]]


def test_ut01_64_method_none_returns_none() -> None:
    """UT01-64 method `none` builds no auth and reads no secret."""
    auth = build_auth(
        _settings("none", None),
        source="prometheus",
        base_url=d.BASE,
        token_client=mock_client(d.Source()),
    )
    assert auth is None


@pytest.mark.parametrize("method", ["key_pair", "connection_string"])
def test_ut01_64_non_http_methods_are_config_errors(method: str) -> None:
    """UT01-64 `key_pair` and `connection_string` are not HTTP auth methods."""
    d.store("sn", d.PLAIN_TOKEN)
    with pytest.raises(ConfigError, match="not an HTTP auth method"):
        build_auth(
            _settings(method),
            source="mongodb",
            base_url=d.BASE,
            token_client=mock_client(d.Source()),
        )


@pytest.mark.parametrize(
    ("source", "method", "secret", "member"),
    [
        ("servicenow", "basic", {"username": d.USERNAME}, "password"),
        ("jira", "api_token", {"token": d.PLAIN_TOKEN}, "email"),
        ("datadog", "api_and_app_key", {"api_key": d.API_KEY}, "app_key"),
        ("servicenow", "oauth_client_credentials", {"client_id": d.CLIENT_ID}, "client_secret"),
        (
            "servicenow",
            "oauth_password",
            {"client_id": d.CLIENT_ID, "client_secret": d.CLIENT_SECRET, "username": d.USERNAME},
            "password",
        ),
        ("dataverse", "msal_client_credentials", {"client_id": d.CLIENT_ID}, "client_secret"),
        (
            "dataverse",
            "msal_client_credentials",
            {"client_id": d.CLIENT_ID, "certificate_pem": d.CERT_PEM},
            "thumbprint",
        ),
        ("dataverse", "msal_client_credentials", {"client_secret": d.CLIENT_SECRET}, "client_id"),
    ],
)
def test_ut01_64_missing_member_is_config_error_without_values(
    source: str, method: str, secret: dict[str, str], member: str
) -> None:
    """UT01-64 a secret JSON missing a required member → ConfigError("secret sn lacks
    <member>"); no secret value in the error."""
    d.store("sn", secret)
    with pytest.raises(ConfigError) as info:
        build_auth(
            _settings(method), source=source, base_url=d.BASE, token_client=mock_client(d.Source())
        )
    assert info.value.message == f"secret sn lacks {member}"
    text = f"{info.value} {info.value!r} {info.value.context} {info.value.details}"
    assert all(s not in text for s in d.SENTINELS)


def test_ut01_64_missing_secret_and_bad_json_are_config_errors() -> None:
    """UT01-64 an absent secret, or a JSON method whose secret is not JSON, → ConfigError."""
    client = mock_client(d.Source())
    with pytest.raises(ConfigError, match="secret not found: sn"):
        build_auth(_settings("bearer"), source="splunk", base_url=d.BASE, token_client=client)
    d.store("sn", d.PLAIN_TOKEN)
    with pytest.raises(ConfigError) as info:
        build_auth(_settings("basic"), source="servicenow", base_url=d.BASE, token_client=client)
    assert d.PLAIN_TOKEN not in str(info.value)


def test_ut01_64_unknown_method_is_config_error() -> None:
    """UT01-64 a method outside the shapes table → ConfigError naming the method."""
    d.store("sn", d.PLAIN_TOKEN)
    with pytest.raises(ConfigError, match="not supported"):
        build_auth(
            _settings("digest"),
            source="splunk",
            base_url=d.BASE,
            token_client=mock_client(d.Source()),
        )


def test_ut01_64_credentials_required_for_secret_methods() -> None:
    """UT01-64 a secret method without a `secret:` reference (settings forbid it; a model
    built without validation still gets ConfigError)."""
    settings = AuthSettings.model_construct(method="bearer", credentials=None)
    with pytest.raises(ConfigError, match=r"auth.credentials is required"):
        build_auth(settings, source="splunk", base_url=d.BASE, token_client=mock_client(d.Source()))


def test_ut01_64_dataverse_without_tenant_is_config_error() -> None:
    """UT01-64 MSAL needs a tenant id (settings require it; a model built without
    validation still gets ConfigError, not an AttributeError)."""
    d.store("sn", {"client_id": d.CLIENT_ID, "client_secret": d.CLIENT_SECRET})
    settings = AuthSettings.model_construct(
        method="msal_client_credentials", credentials="secret:sn"
    )
    with pytest.raises(ConfigError, match="tenant_id"):
        build_auth(
            settings, source="dataverse", base_url=DV_BASE, token_client=mock_client(d.Source())
        )


def test_ut01_64_repr_and_str_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-64 `repr()` and `str()` of every auth object show `***`, never a value."""
    monkeypatch.setattr(auth_module, "_msal_app", d.MsalFactory(d.FakeMsalApp([d.msal_token(1)])))
    src = d.Source()
    objects: list[httpx2.Auth] = [
        StaticHeaderAuth({"Authorization": SecretStr(f"Bearer {d.PLAIN_TOKEN}")}, base_url=d.BASE),
        _token_auth(src, FakeClock(FIXTURE_START)),
        MsalTokenProvider(
            tenant_id=d.TENANT,
            client_id=d.CLIENT_ID,
            credential=SecretStr(d.CLIENT_SECRET),
            scope=f"{DV_BASE}/.default",
        ),
    ]
    _send(objects[1], src)
    _send(objects[2], d.Source(), DV_BASE)
    for obj in objects:
        for text in (repr(obj), str(obj)):
            assert "***" in text
            assert all(s not in text for s in d.SENTINELS)


def test_ut01_64_foreign_origin_gets_no_credentials() -> None:
    """UT01-64 a request to another scheme, host or port carries no credentials."""
    auth = StaticHeaderAuth(
        {"Authorization": SecretStr(f"Bearer {d.PLAIN_TOKEN}")}, base_url=d.BASE
    )
    src = d.Source()
    client = mock_client(src)
    for url in (
        "https://evil.example/api/now/table/incident",
        "http://sn.example/api/now/table/incident",
        "https://sn.example:8443/api/now/table/incident",
    ):
        client.get(url, auth=auth)
    assert src.bearers() == [None, None, None]
    client.get("https://SN.example:443/api/now/table/incident", auth=auth)
    assert src.bearers()[-1] == f"Bearer {d.PLAIN_TOKEN}"


# --- UT01-65: OAuth token cache ---------------------------------------------------------------


def test_ut01_65_cached_until_margin_then_refreshed_and_401_retried_once() -> None:
    """UT01-65 expires_in 600, frozen time: requests at t and t+299 share one fetch; at
    t+301 a refresh; a 401 drops the token, fetches a new one and retries once."""
    fc = FakeClock(FIXTURE_START)
    src = d.Source(data=[200, 200, 200, 401, 200])
    auth = _token_auth(src, fc)
    client = mock_client(src)

    client.get(d.DATA_PATH, auth=auth)
    fc.advance(299)
    client.get(d.DATA_PATH, auth=auth)
    assert len(src.token_calls) == 1
    fc.advance(2)  # t+301: expires_at - 300 <= now
    client.get(d.DATA_PATH, auth=auth)
    assert len(src.token_calls) == 2
    response = client.get(d.DATA_PATH, auth=auth)  # 401, then the retry answers 200

    assert response.status_code == 200
    assert len(src.token_calls) == 3
    assert src.bearers() == [f"Bearer {d.ACCESS}{n}" for n in (1, 1, 2, 2, 3)]


def test_ut01_65_refresh_boundary_is_exactly_t_plus_300() -> None:
    """UT01-65 at t+300 (expires_at - margin == now) the token is refreshed."""
    fc = FakeClock(FIXTURE_START)
    src = d.Source()
    auth = _token_auth(src, fc)
    _send(auth, src)
    fc.advance(300)
    _send(auth, src)
    assert len(src.token_calls) == 2


def test_ut01_65_second_401_is_returned_not_retried_again() -> None:
    """UT01-65 the retry happens once: a second 401 is the response (no loop)."""
    fc = FakeClock(FIXTURE_START)
    src = d.Source(data=[401])
    auth = _token_auth(src, fc)
    response = mock_client(src).get(d.DATA_PATH, auth=auth)
    assert response.status_code == 401
    assert len(src.data_calls) == 2
    assert len(src.token_calls) == 2


def test_ut01_65_401_drops_token_even_when_refetch_fails() -> None:
    """UT01-65 after a 401 the token is dropped: a failing refetch raises, and the next
    request fetches again instead of reusing the refused token."""
    fc = FakeClock(FIXTURE_START)
    src = d.Source(data=[401, 200], token=[200, 500, 200])
    auth = _token_auth(src, fc)
    client = mock_client(src)
    with pytest.raises(SourceUnavailable):
        client.get(d.DATA_PATH, auth=auth)
    assert client.get(d.DATA_PATH, auth=auth).status_code == 200
    assert len(src.token_calls) == 3
    assert src.bearers() == [f"Bearer {d.ACCESS}1", f"Bearer {d.ACCESS}3"]


def test_ut01_65_foreign_origin_no_token_no_retry() -> None:
    """UT01-65 a request off the token URL's origin fetches no token, carries no bearer and
    is not retried on 401."""
    fc = FakeClock(FIXTURE_START)
    src = d.Source()
    auth = _token_auth(src, fc)
    response = mock_client(src).get("https://evil.example/x", auth=auth)
    assert response.status_code == 401
    assert src.token_calls == []
    assert len(src.other_calls) == 1
    assert "Authorization" not in src.other_calls[0].headers


@pytest.mark.parametrize(
    ("status", "error"),
    [(400, AuthError), (401, AuthError), (403, AuthError), (500, SourceUnavailable)],
)
def test_ut01_65_token_endpoint_errors_are_mapped(status: int, error: type[Exception]) -> None:
    """UT01-65 a non-2xx token response → the map_http_error class (400/401 → AuthError);
    exactly one token POST, no retry; the error body never reaches the message."""
    fc = FakeClock(FIXTURE_START)
    src = d.Source(token=[status])
    with pytest.raises(error) as info:
        _send(_token_auth(src, fc), src)
    assert type(info.value) is error
    assert len(src.token_calls) == 1
    assert src.data_calls == []
    assert d.CLIENT_SECRET not in f"{info.value} {info.value!r}"
    assert d.TOKEN_PATH in str(info.value)


@pytest.mark.parametrize(
    "body",
    [
        {"token_type": "Bearer", "expires_in": 600},
        {"access_token": "", "token_type": "Bearer", "expires_in": 600},
        {"access_token": "x" * 8193, "token_type": "Bearer", "expires_in": 600},
        {"access_token": d.ACCESS, "token_type": "Bearer", "expires_in": 0},
        {"access_token": d.ACCESS, "token_type": "Bearer", "expires_in": 86401},
        {"access_token": d.ACCESS, "expires_in": 600},
        ["not", "an", "object"],
    ],
)
def test_ut01_65_bad_token_body_is_schema_violation(body: object) -> None:
    """UT01-65 a token body outside TokenResponse → SchemaViolation("bad token response")
    without the token text."""
    fc = FakeClock(FIXTURE_START)
    src = d.Source(token_body=body)
    with pytest.raises(SchemaViolation) as info:
        _send(_token_auth(src, fc), src)
    assert info.value.message == "bad token response"
    assert info.value.__cause__ is None
    assert d.ACCESS not in repr(info.value)


def test_ut01_65_malformed_json_token_body_is_schema_violation() -> None:
    """UT01-65 a token body that is not JSON → SchemaViolation("bad token response")."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"<html>")

    fc = FakeClock(FIXTURE_START)
    auth = OAuthTokenAuth(f"{d.BASE}{d.TOKEN_PATH}", {}, client=mock_client(handler), clock=fc.now)
    with pytest.raises(SchemaViolation, match="bad token response"):
        mock_client(handler).get(d.DATA_PATH, auth=auth)


def test_ut01_65_token_transport_error_is_source_unavailable() -> None:
    """UT01-65 a transport failure on the token POST → SourceUnavailable."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        msg = "synthetic connect failure"
        raise httpx2.ConnectError(msg, request=request)

    fc = FakeClock(FIXTURE_START)
    auth = OAuthTokenAuth(f"{d.BASE}{d.TOKEN_PATH}", {}, client=mock_client(handler), clock=fc.now)
    with pytest.raises(SourceUnavailable):
        mock_client(d.Source()).get(d.DATA_PATH, auth=auth)


def test_ut01_65_one_fetch_in_flight_across_threads() -> None:
    """UT01-65 eight threads needing a token at once cause exactly one token POST."""
    fc = FakeClock(FIXTURE_START)
    src = d.Source()
    auth = _token_auth(src, fc)
    client = mock_client(src)
    barrier = threading.Barrier(8)

    def call(_n: int) -> int:
        barrier.wait()
        return client.get(d.DATA_PATH, auth=auth).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(call, range(8)))
    assert statuses == [200] * 8
    assert len(src.token_calls) == 1


def test_ut01_65_default_clock_is_herness_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-65 without a `clock` argument the cache follows `herness.core.time.now`."""
    fake_clock = FakeClock(FIXTURE_START)
    monkeypatch.setattr(clock, "now", fake_clock.now)
    src = d.Source()
    auth = OAuthTokenAuth(f"{d.BASE}{d.TOKEN_PATH}", {}, client=mock_client(src))
    _send(auth, src)
    fake_clock.advance(301)
    _send(auth, src)
    assert len(src.token_calls) == 2


# --- UT01-90: MSAL token cache ----------------------------------------------------------------


def _msal(
    monkeypatch: pytest.MonkeyPatch, results: list[dict[str, object] | BaseException], fc: FakeClock
) -> tuple[MsalTokenProvider, d.MsalFactory]:
    factory = d.MsalFactory(d.FakeMsalApp(results))
    monkeypatch.setattr(auth_module, "_msal_app", factory)
    provider = MsalTokenProvider(
        tenant_id=d.TENANT,
        client_id=d.CLIENT_ID,
        credential=SecretStr(d.CLIENT_SECRET),
        scope=f"{DV_BASE}/.default",
        clock=fc.now,
    )
    return provider, factory


def test_ut01_90_cached_until_expiry_minus_300_then_error_is_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT01-90 fake MSAL app: a token, cached until exp - 300 s; the next call returns an
    error result → AuthError("msal error <code>") without the error description."""
    fc = FakeClock(FIXTURE_START)
    error = {"error": "invalid_client", "error_description": f"AADSTS7000215 {d.CLIENT_SECRET}"}
    provider, factory = _msal(monkeypatch, [d.msal_token(1), error], fc)
    src = d.Source()
    _send(provider, src, DV_BASE)
    fc.advance(299)
    _send(provider, src, DV_BASE)
    assert len(factory.app.calls) == 1
    assert src.bearers() == [f"Bearer {d.ACCESS}1"] * 2
    fc.advance(1)
    with pytest.raises(AuthError) as info:
        _send(provider, src, DV_BASE)
    assert info.value.message == "msal error invalid_client"
    assert "AADSTS" not in repr(info.value)
    assert d.CLIENT_SECRET not in repr(info.value)
    assert len(factory.built) == 1  # the app is built once


def test_ut01_90_401_refetches_and_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-90 a 401 on the real request drops the MSAL token, acquires a new one and
    retries once."""
    fc = FakeClock(FIXTURE_START)
    provider, factory = _msal(monkeypatch, [d.msal_token(1), d.msal_token(2)], fc)
    src = d.Source(data=[401, 200])
    response = mock_client(src, base_url=DV_BASE).get(d.DATA_PATH, auth=provider)
    assert response.status_code == 200
    assert src.bearers() == [f"Bearer {d.ACCESS}1", f"Bearer {d.ACCESS}2"]
    assert len(factory.app.calls) == 2


def test_ut01_90_transport_error_is_source_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-90 an MSAL transport error (requests' exceptions are OSErrors) →
    SourceUnavailable, also when it happens while the app is built."""
    fc = FakeClock(FIXTURE_START)
    provider, _ = _msal(monkeypatch, [ConnectionError("synthetic network down")], fc)
    with pytest.raises(SourceUnavailable) as info:
        _send(provider, d.Source(), DV_BASE)
    assert "synthetic" not in str(info.value)

    def failing(*_args: object) -> object:
        raise OSError(d.CLIENT_SECRET)

    monkeypatch.setattr(auth_module, "_msal_app", failing)
    other = MsalTokenProvider(
        tenant_id=d.TENANT,
        client_id=d.CLIENT_ID,
        credential=SecretStr(d.CLIENT_SECRET),
        scope=f"{DV_BASE}/.default",
        clock=fc.now,
    )
    with pytest.raises(SourceUnavailable) as info:
        _send(other, d.Source(), DV_BASE)
    assert d.CLIENT_SECRET not in f"{info.value} {info.value!r}"
    assert info.value.__cause__ is None


def test_ut01_90_bad_msal_result_is_schema_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-90 an MSAL result with a token but a bad `expires_in` → SchemaViolation."""
    fc = FakeClock(FIXTURE_START)
    bad = {"access_token": d.ACCESS, "token_type": "Bearer", "expires_in": "soon"}
    provider, _ = _msal(monkeypatch, [bad], fc)
    with pytest.raises(SchemaViolation, match="bad token response"):
        _send(provider, d.Source(), DV_BASE)


def test_ut01_90_real_msal_factory_builds_confidential_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT01-90 the default factory passes client id, credential and authority to
    msal.ConfidentialClientApplication (constructor replaced: no network)."""
    seen: list[dict[str, object]] = []

    class Recorder:
        def __init__(self, client_id: str, **kwargs: object) -> None:
            seen.append({"client_id": client_id, **kwargs})

    monkeypatch.setattr(auth_module.msal, "ConfidentialClientApplication", Recorder)
    app = auth_module._msal_app(d.CLIENT_ID, d.CLIENT_SECRET, "https://login.example/t")
    assert isinstance(app, Recorder)
    assert seen == [
        {
            "client_id": d.CLIENT_ID,
            "client_credential": d.CLIENT_SECRET,
            "authority": "https://login.example/t",
        }
    ]


def test_ut01_90_token_refreshed_log_has_source_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-90 `connectors.auth.token_refreshed` is logged at DEBUG with `source` only."""
    fc = FakeClock(FIXTURE_START)
    provider, _ = _msal(monkeypatch, [d.msal_token(1)], fc)
    with capture_logs() as logs:
        _send(provider, d.Source(), DV_BASE)
    events = [e for e in logs if e["event"] == "connectors.auth.token_refreshed"]
    assert events == [
        {
            "component": "connectors.auth",
            "event": "connectors.auth.token_refreshed",
            "log_level": "debug",
            "source": "dataverse",
        }
    ]
