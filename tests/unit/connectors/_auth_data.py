"""Sentinel secrets, a scripted mock source and a fake MSAL app for the auth tests (T01-15).

Every secret and token value starts with ``synthetic`` (R-67) so the leak tests can grep
for them. The mock source is an ``httpx2.MockTransport`` client (``_http_data.mock_client``):
connectors never build clients, tests may (T01-14 pattern).
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx2
import keyring

from herness.core import secrets

BASE = "https://sn.example"
TOKEN_PATH = "/oauth_token.do"  # noqa: S105 - a URL path
DATA_PATH = "/api/now/table/incident"
TENANT = "00000000-0000-4000-8000-000000000001"
CLIENT_ID = "synthetic-client-id-t0115"
CLIENT_SECRET = "synthetic-client-secret-t0115"  # noqa: S105  # pragma: allowlist secret
USERNAME = "synthetic-user-t0115"
PASSWORD = "synthetic-password-t0115"  # noqa: S105  # pragma: allowlist secret
PLAIN_TOKEN = "synthetic-plain-token-t0115"  # noqa: S105  # pragma: allowlist secret
API_KEY = "synthetic-dd-api-key-t0115"  # pragma: allowlist secret
APP_KEY = "synthetic-dd-app-key-t0115"  # pragma: allowlist secret
CERT_PEM = "synthetic-certificate-pem-t0115"
THUMBPRINT = "synthetic-thumbprint-t0115"
ACCESS = "synthetic-access-token-t0115-"  # + fetch number  # pragma: allowlist secret

SENTINELS = (CLIENT_ID, CLIENT_SECRET, USERNAME, PASSWORD, PLAIN_TOKEN, API_KEY, APP_KEY)
SENTINELS += (CERT_PEM, ACCESS)


def store(name: str, value: str | dict[str, str]) -> None:
    """Store ``value`` (a dict as JSON) as secret ``name`` in the (in-memory) keyring."""
    text = value if isinstance(value, str) else json.dumps(value)
    keyring.set_password(secrets._SERVICE, name, text)


def form_of(request: httpx2.Request) -> dict[str, str]:
    """The decoded ``application/x-www-form-urlencoded`` body of ``request``."""
    return dict(urllib.parse.parse_qsl(request.content.decode()))


@dataclass
class Source:
    """A scripted ServiceNow-like host: a token endpoint and one data path.

    ``data`` and ``token`` are lists of statuses answered in order; the last one repeats.
    Token responses carry ``ACCESS<n>`` (n = 1, 2, ...) unless ``token_body`` is set.
    """

    data: list[int] = field(default_factory=lambda: [200])
    token: list[int] = field(default_factory=lambda: [200])
    expires_in: int = 600
    token_body: Any = None
    records: list[dict[str, str]] = field(default_factory=list)
    token_calls: list[httpx2.Request] = field(default_factory=list)
    data_calls: list[httpx2.Request] = field(default_factory=list)
    other_calls: list[httpx2.Request] = field(default_factory=list)
    sent: list[str | None] = field(default_factory=list)

    @staticmethod
    def _next(statuses: list[int]) -> int:
        return statuses.pop(0) if len(statuses) > 1 else statuses[0]

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == TOKEN_PATH:
            self.token_calls.append(request)
            status = self._next(self.token)
            body = self.token_body
            if body is None:
                number = len(self.token_calls)
                body = {"access_token": f"{ACCESS}{number}", "token_type": "Bearer"}
                body |= {"expires_in": self.expires_in, "scope": "useraccount"}
            if status != 200:
                body = {"error": "invalid_client", "error_description": CLIENT_SECRET}
            return httpx2.Response(status, json=body)
        if request.url.path == DATA_PATH:
            self.data_calls.append(request)
            self.sent.append(request.headers.get("Authorization"))  # a retry reuses the request
            status = self._next(self.data)
            return httpx2.Response(status, json={"result": self.records})
        self.other_calls.append(request)
        return httpx2.Response(401, json={})

    def bearers(self) -> list[str | None]:
        """The ``Authorization`` header of every data request as sent, in order."""
        return list(self.sent)


@dataclass
class FakeMsalApp:
    """Stand-in for ``msal.ConfidentialClientApplication``: answers ``results`` in order
    (an exception instance is raised); records every ``acquire_token_for_client`` call."""

    results: list[dict[str, Any] | BaseException]
    calls: list[list[str]] = field(default_factory=list)

    def acquire_token_for_client(self, scopes: list[str]) -> dict[str, Any]:
        self.calls.append(scopes)
        item = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(item, BaseException):
            raise item
        return item


@dataclass
class MsalFactory:
    """Replacement for ``herness.connectors.auth._msal_app``; records its arguments."""

    app: FakeMsalApp
    built: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, client_id: str, credential: Any, authority: str) -> FakeMsalApp:
        self.built.append(
            {"client_id": client_id, "credential": credential, "authority": authority}
        )
        return self.app


def msal_token(number: int, expires_in: int = 600) -> dict[str, Any]:
    """An MSAL success result carrying ``ACCESS<number>``."""
    return {"access_token": f"{ACCESS}{number}", "expires_in": expires_in, "token_type": "Bearer"}
