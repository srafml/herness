"""Per-request auth of the HTTP sources (impl 01 U01-63 to U01-65; design 01 §5.7, §5.9).

Secrets and tokens live only in the auth objects as ``SecretStr`` (masked ``repr``, TH01-03)
and reach only the configured origin (scheme, host, port; ``basic`` is ``httpx2.BasicAuth``'s
header, sent as a static header). Tokens: refreshed 300 s early, one fetch in flight (TH01-16).
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Callable, Generator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx2
import msal  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from herness.connectors.http import map_http_error
from herness.connectors.settings_base import AuthSettings
from herness.core import errors as e
from herness.core import secrets
from herness.core import time as clock
from herness.core.logging import get_logger

_AUTHORITY: Final = "https://login.microsoftonline.com/"
_NOT_HTTP: Final = frozenset({"key_pair", "connection_string"})
_STATIC: Final = frozenset({"basic", "api_token", "pat", "bearer", "api_and_app_key"})
_OAUTH: Final = {"oauth_client_credentials": (), "oauth_password": ("username", "password")}
_BASIC_MEMBERS: Final = {"basic": ("username", "password"), "api_token": ("email", "token")}
_REFUSED: Final = frozenset({400, 401})  # token endpoint statuses meaning bad credentials
_ACCEPT: Final = {"Accept": "application/json"}
_KNOWN: Final = _STATIC | _OAUTH.keys() | {"msal_client_credentials"}
_log = get_logger("connectors.auth")

type _Flow = Generator[httpx2.Request, httpx2.Response]
type _Clock = Callable[[], datetime]


def _now() -> datetime:
    return clock.now()  # looked up per call, so a patched `herness.core.time.now` applies


def _origin(url: httpx2.URL) -> tuple[str, str, int | None]:
    return url.scheme, (url.host or "").lower(), url.port


class _TokenResponse(BaseModel):
    """An OAuth token body or an MSAL success result; other members are ignored."""

    model_config = ConfigDict(hide_input_in_errors=True)
    access_token: SecretStr = Field(min_length=1, max_length=8192)
    expires_in: int = Field(ge=1, le=86400)
    token_type: str


def _token_body(data: object) -> _TokenResponse:
    try:
        return _TokenResponse.model_validate(json.loads(data) if isinstance(data, bytes) else data)
    except ValueError:  # pydantic ValidationError, JSONDecodeError, UnicodeDecodeError
        msg = "bad token response"
        raise e.SchemaViolation(msg) from None


class _OriginAuth(httpx2.Auth):
    """Masked ``repr``/``str`` and the same-origin check shared by every auth object."""

    def __init__(self, url: str) -> None:
        self._origin = _origin(httpx2.URL(url))

    def _own(self, request: httpx2.Request) -> bool:
        return _origin(request.url) == self._origin

    def __repr__(self) -> str:
        return f"{type(self).__name__}(***)"

    __str__ = __repr__


class StaticHeaderAuth(_OriginAuth):
    """Fixed credential headers: basic, API tokens, PAT, bearer, Datadog keys (U01-63)."""

    def __init__(self, headers: Mapping[str, SecretStr], *, base_url: str) -> None:
        super().__init__(base_url)
        self._headers = dict(headers)

    def auth_flow(self, request: httpx2.Request) -> _Flow:
        if self._own(request):
            for name, value in self._headers.items():
                request.headers[name] = value.get_secret_value()
        yield request


class _TokenCache(_OriginAuth):
    """A bearer token refreshed ``refresh_margin_s`` before expiry; one fetch in flight."""

    def __init__(self, url: str, *, source: str, clock: _Clock, refresh_margin_s: int) -> None:
        super().__init__(url)
        self._source, self._clock = source, clock
        self._lock, self._margin = threading.Lock(), timedelta(seconds=refresh_margin_s)
        self._token: SecretStr | None = None
        self._refresh_at = datetime.min.replace(tzinfo=UTC)

    _fetch: Callable[[], _TokenResponse]  # each subclass defines it

    def _current(self, stale: SecretStr | None = None) -> SecretStr:
        """The cached token; fetched first when missing, due, or the ``stale`` one."""
        with self._lock:
            token = self._token
            if token is None or token is stale or self._refresh_at <= self._clock():
                self._token = None  # dropped first: a failed fetch leaves no token behind
                token = self._token = (body := self._fetch()).access_token
                expires_at = self._clock() + timedelta(seconds=body.expires_in)
                self._refresh_at = expires_at - self._margin
                _log.debug("connectors.auth.token_refreshed", source=self._source)
            return token

    def auth_flow(self, request: httpx2.Request) -> _Flow:
        if not self._own(request):
            yield request
            return
        token = self._current()
        request.headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        response = yield request
        if response.status_code == 401:  # noqa: PLR2004 - HTTP status
            token = self._current(stale=token)
            request.headers["Authorization"] = f"Bearer {token.get_secret_value()}"
            yield request


class OAuthTokenAuth(_TokenCache):
    """ServiceNow OAuth 2.0 token, ``client_credentials`` or ``password`` grant (U01-64)."""

    def __init__(
        self,
        token_url: str,
        form: Mapping[str, SecretStr | str],
        *,
        client: httpx2.Client,
        clock: _Clock = _now,
        refresh_margin_s: int = 300,
        source: str = "servicenow",
    ) -> None:
        super().__init__(token_url, source=source, clock=clock, refresh_margin_s=refresh_margin_s)
        self._url, self._form, self._client = token_url, dict(form), client

    def _fetch(self) -> _TokenResponse:
        """One form POST through the source client: no page retry, no loop."""
        form = self._form.items()
        data = {k: v.get_secret_value() if isinstance(v, SecretStr) else v for k, v in form}
        try:
            response = self._client.post(self._url, data=data, headers=_ACCEPT)
        except httpx2.HTTPError:
            msg = "token endpoint unavailable"
            raise e.SourceUnavailable(msg) from None
        err = map_http_error(response, now=self._clock())
        if err is not None:
            raise e.AuthError(err.message) if response.status_code in _REFUSED else err
        return _token_body(response.content)


def _msal_app(client_id: str, cred: str | dict[str, str], url: str) -> Any:  # noqa: ANN401 - msal is untyped
    """The MSAL confidential client; it has its own HTTP session (R-06 socket guard)."""
    return msal.ConfidentialClientApplication(client_id, client_credential=cred, authority=url)


class MsalTokenProvider(_TokenCache):
    """Dataverse token from MSAL ``acquire_token_for_client`` (U01-65)."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        credential: SecretStr | Mapping[str, SecretStr],
        scope: str,
        clock: _Clock = _now,
        refresh_margin_s: int = 300,
    ) -> None:
        super().__init__(scope, source="dataverse", clock=clock, refresh_margin_s=refresh_margin_s)
        self._authority, self._client_id = f"{_AUTHORITY}{tenant_id}", client_id
        self._credential, self._scope = credential, scope
        self._app: Any = None

    def _secret(self) -> str | dict[str, str]:
        cred = self._credential
        if isinstance(cred, SecretStr):
            return cred.get_secret_value()
        return {k: v.get_secret_value() for k, v in cred.items()}

    def _fetch(self) -> _TokenResponse:
        """Build the app once, acquire a token; on failure the error code only."""
        try:
            if self._app is None:
                self._app = _msal_app(self._client_id, self._secret(), self._authority)
            result = self._app.acquire_token_for_client(scopes=[self._scope])
        except OSError as exc:  # requests.RequestException is an OSError (lint bans `requests`)
            msg = f"token service unavailable: {type(exc).__name__}"
            raise e.SourceUnavailable(msg) from None
        if not result.get("access_token"):
            msg = f"msal error {result.get('error')}"
            raise e.AuthError(msg)
        return _token_body(result)


def _members(ref: str, data: dict[str, SecretStr], *names: str) -> dict[str, SecretStr]:
    for member in names:
        if member not in data:
            msg = f"secret {secrets.SecretRef.parse(ref).name} lacks {member}"
            raise e.ConfigError(msg)
    return data


def _static(method: str, source: str, ref: str, base_url: str) -> StaticHeaderAuth:
    if method == "api_and_app_key":
        data = _members(ref, secrets.resolve_json(ref), "api_key", "app_key")
        headers = {"DD-API-KEY": data["api_key"], "DD-APPLICATION-KEY": data["app_key"]}
    elif method == "basic" or (method == "api_token" and source == "jira"):
        user, password = _BASIC_MEMBERS[method]
        data = _members(ref, secrets.resolve_json(ref), user, password)
        pair = f"{data[user].get_secret_value()}:{data[password].get_secret_value()}"
        headers = {"Authorization": SecretStr("Basic " + base64.b64encode(pair.encode()).decode())}
    else:
        scheme = "Api-Token" if method == "api_token" else "Bearer"
        token = secrets.resolve(ref).get_secret_value()
        headers = {"Authorization": SecretStr(f"{scheme} {token}")}
    return StaticHeaderAuth(headers, base_url=base_url)


def _msal(auth: AuthSettings, ref: str, base_url: str, clock: _Clock) -> MsalTokenProvider:
    data = _members(ref, secrets.resolve_json(ref), "client_id")
    if "certificate_pem" in data:
        _members(ref, data, "thumbprint")
        cred: SecretStr | dict[str, SecretStr] = {
            "private_key": data["certificate_pem"],
            "thumbprint": data["thumbprint"],
        }
    else:
        cred = _members(ref, data, "client_secret")["client_secret"]
    if auth.tenant_id is None:
        msg = "auth.tenant_id is required for method msal_client_credentials"
        raise e.ConfigError(msg)
    cid, scope = data["client_id"].get_secret_value(), f"{base_url}/.default"
    return MsalTokenProvider(
        tenant_id=auth.tenant_id, client_id=cid, credential=cred, scope=scope, clock=clock
    )


def build_auth(
    auth: AuthSettings,
    *,
    source: str,
    base_url: str,
    token_client: httpx2.Client,
    clock: _Clock = _now,
) -> httpx2.Auth | None:
    """The per-request auth of ``source`` per the §3.13 shapes table; ``None`` for ``none``.

    ``ConfigError`` for a missing secret, a missing JSON member (named, never the value),
    ``key_pair``/``connection_string`` or a method outside the table (U01-63)."""
    method, ref, base = auth.method, auth.credentials, base_url.removesuffix("/")
    if method == "none":
        return None
    if method in _NOT_HTTP:
        msg = "not an HTTP auth method"
        raise e.ConfigError(msg)
    if method not in _KNOWN or ref is None:
        msg = f"auth method {method} is not supported" if ref else "auth.credentials is required"
        raise e.ConfigError(msg)
    if method in _STATIC:
        return _static(method, source, ref, base)
    if method not in _OAUTH:
        return _msal(auth, ref, base, clock)
    extra = _OAUTH[method]
    data = _members(ref, secrets.resolve_json(ref), "client_id", "client_secret", *extra)
    form: dict[str, SecretStr | str] = {k: data[k] for k in ("client_id", "client_secret", *extra)}
    form["grant_type"] = "password" if extra else "client_credentials"
    return OAuthTokenAuth(f"{base}/oauth_token.do", form, client=token_client, clock=clock)
