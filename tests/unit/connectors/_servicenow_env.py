"""End-to-end ServiceNow environment for ST01-02/03 and the IT01 flows (T01-16).

The connector is built the production way (``ServiceNowConnector(settings)``: the egress
source client of ``http_client`` with its host guard, the auth of ``build_auth``) from a
loaded config whose ``sources.yaml`` enables ServiceNow on the synthetic host; only the pool
transport is replaced (``httpx2.HTTPTransport`` → ``MockTransport`` over ``SnHost``, the
``tests.support.egress_mock`` technique), so no socket is opened. ``SnHost`` is the OAuth
token endpoint plus ``FakeServiceNow``; every credential and token is a ``synthetic``
sentinel (R-67).
"""

from __future__ import annotations

import datetime
import json
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx2
import keyring
import pyarrow.parquet as pq
import pytest
from tests.support.sn_cassettes import BASE_URL, T0, FakeServiceNow
from tests.support.sync_env import init_sync_config

from herness.connectors.runner import SyncRunner
from herness.connectors.servicenow import ServiceNowConnector
from herness.connectors.settings import ServiceNowSettings
from herness.core import config as c
from herness.core import secrets

TOKEN_PATH = "/oauth_token.do"  # noqa: S105 - a URL path
CLIENT_ID = "synthetic-sn-client-id-t0116"
CLIENT_SECRET = "synthetic-sn-client-secret-t0116"  # noqa: S105  # pragma: allowlist secret
USERNAME = "synthetic-sn-user-t0116"
PASSWORD = "synthetic-sn-password-t0116"  # noqa: S105  # pragma: allowlist secret
ACCESS = "synthetic-sn-access-token-t0116-"  # + fetch number  # pragma: allowlist secret
SENTINELS = (CLIENT_ID, CLIENT_SECRET, USERNAME, PASSWORD, ACCESS)
NOW = T0 + datetime.timedelta(days=3)  # every incident_table record is older


def sources_yaml(*, fields: str = "[number, opened_at, priority, state, assignment_group]") -> str:
    """``sources.yaml`` enabling ServiceNow (OAuth password grant) on the synthetic host."""
    return f"""\
version: 1
sources:
  servicenow:
    enabled: true
    base_url: {BASE_URL}
    auth: {{method: oauth_password, credentials: "secret:sn_oauth"}}
    page_size: 100
    batch_rows: 1000
    overlap_minutes: 60
    settle_seconds: 60
    backfill: {{start: 2026-02-20}}
    reconcile: {{max_delete_pct: 50}}
    entities:
      incident:
        fields: {fields}
        window_hours: 48
"""


@dataclass
class SnHost:
    """The token endpoint (statuses answered in order, the last repeats) and the table API."""

    table: FakeServiceNow
    token: list[int] = field(default_factory=lambda: [200])
    token_calls: list[httpx2.Request] = field(default_factory=list)
    bearers: list[str | None] = field(default_factory=list)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == TOKEN_PATH:
            self.token_calls.append(request)
            status = self.token.pop(0) if len(self.token) > 1 else self.token[0]
            if status != 200:
                body = {"error": "invalid_grant", "error_description": PASSWORD}
                return httpx2.Response(status, json=body)
            token = {"access_token": f"{ACCESS}{len(self.token_calls)}", "token_type": "Bearer"}
            return httpx2.Response(200, json=token | {"expires_in": 1800})
        self.bearers.append(request.headers.get("Authorization"))
        return self.table(request)

    def form(self, number: int = 0) -> dict[str, str]:
        """The decoded form of token request ``number``."""
        return dict(urllib.parse.parse_qsl(self.token_calls[number].content.decode()))


def store_secret() -> None:
    """The OAuth secret ``sn_oauth`` (JSON members) in the (in-memory) keyring."""
    value = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "username": USERNAME,
        "password": PASSWORD,
    }
    keyring.set_password(secrets._SERVICE, "sn_oauth", json.dumps(value))


def install(
    monkeypatch: pytest.MonkeyPatch, host: Callable[[httpx2.Request], httpx2.Response]
) -> None:
    """Route every ``httpx2`` pool transport built from now on to ``host``."""
    monkeypatch.setattr(httpx2, "HTTPTransport", lambda *_a, **_k: httpx2.MockTransport(host))


def load(root: Path, yaml_text: str | None = None) -> ServiceNowSettings:
    """Load the config tree under ``root`` (cached for ``get_config``); its section. A tree
    already there gets the new ``sources.yaml`` and is reloaded."""
    text, config_dir = yaml_text or sources_yaml(), root / "config"
    if config_dir.is_dir():
        (config_dir / "sources.yaml").write_text(text, encoding="utf-8")
        c.reset_config()
        cfg = c.init_config("local", config_dir=config_dir, env={})
    else:
        cfg = init_sync_config(root, text)
    section = cfg.sources.sources.servicenow
    assert isinstance(section, ServiceNowSettings)
    return section


@dataclass
class Clock:
    """A settable clock shared by the runner and the connector."""

    now: datetime.datetime = NOW

    def __call__(self) -> datetime.datetime:
        return self.now


def runner(settings: ServiceNowSettings, clock: Clock, data_root: Path) -> SyncRunner:
    """A runner over a production-built connector (real LakeWriter under ``data_root/raw``)."""
    conn = ServiceNowConnector(settings, clock=clock)
    return SyncRunner(conn, settings, clock=clock, data_root=data_root)


def lake_rows(data_root: Path, entity: str = "incident") -> list[dict[str, object]]:
    """Every row of the committed lake files of ``servicenow/<entity>``."""
    return [
        row for path in lake_files(data_root, entity) for row in pq.read_table(path).to_pylist()
    ]


def lake_files(data_root: Path, entity: str = "incident") -> list[Path]:
    """The committed lake files of ``servicenow/<entity>``."""
    return sorted((data_root / "raw" / "servicenow" / entity).rglob("*.parquet"))
