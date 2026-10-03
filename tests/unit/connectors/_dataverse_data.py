"""Settings, synthetic OData pages and a scripted mock source for the Dataverse tests (T01-24).

Hosts are reserved example domains (``org.example.com``); every credential and token starts
with ``synthetic`` (R-67). ``login.microsoftonline.com`` appears only because the settings
require the MSAL authority in ``hosts`` (U01-12); no test reaches it (MSAL is faked).
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
from tests.unit.connectors._http_data import SyntheticBearer, mock_client

from herness.connectors.dataverse import DataverseConnector
from herness.connectors.http import SourceHttp
from herness.connectors.settings import DataverseSettings

BASE = "https://org.example.com"
ENTITYSET_PATH = "/api/data/v9.2/cr123_projects"
FORMATTED = "@OData.Community.Display.V1.FormattedValue"
TENANT = "00000000-0000-4000-8000-000000000024"
NOW = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.UTC)
PREFER = (
    'odata.maxpagesize=500,odata.include-annotations="OData.Community.Display.V1.FormattedValue"'
)
ROW_TEXT = "synthetic-row-text-t0124"


def settings(**extra: Any) -> DataverseSettings:
    """A valid Dataverse section with one entity ``project`` (page size 500)."""
    data: dict[str, Any] = {
        "enabled": True,
        "base_url": BASE,
        "hosts": ["login.microsoftonline.com"],
        "auth": {
            "method": "msal_client_credentials",
            "tenant_id": TENANT,
            "credentials": "secret:dataverse_app",
        },
        "page_size": 500,
        "entities": {
            "project": {
                "entityset": "cr123_projects",
                "key_field": "cr123_projectid",
                "select": ["cr123_name", "statuscode", "modifiedon"],
            }
        },
    }
    return DataverseSettings.model_validate(data | extra)


def row(number: int, *, minute: int = 0) -> dict[str, Any]:
    """One synthetic project row with formatted-value annotations."""
    stamp = f"2026-08-01T10:{minute:02d}:{number % 60:02d}Z"
    return {
        "@odata.etag": f'W/"{1000 + number}"',
        "cr123_projectid": f"00000000-0000-4000-8000-{number:012d}",
        "cr123_name": f"Project {number}",
        "statuscode": 1,
        "statuscode" + FORMATTED: "Active",
        "modifiedon": stamp,
        "modifiedon" + FORMATTED: f"8/1/2026 10:{minute:02d} AM",
    }


def next_link(skiptoken: int) -> str:
    """An absolute same-origin ``@odata.nextLink`` as Dataverse sends it."""
    token = f"%3Ccookie%20pagenumber=%22{skiptoken}%22%20/%3E"
    return f"{BASE}{ENTITYSET_PATH}?$select=cr123_projectid&$skiptoken={token}"


def page(rows: list[dict[str, Any]], nxt: str | None = None) -> dict[str, Any]:
    """An OData collection body, with ``@odata.nextLink`` when ``nxt`` is given."""
    body: dict[str, Any] = {"@odata.context": f"{BASE}/api/data/v9.2/$metadata", "value": rows}
    if nxt is not None:
        body["@odata.nextLink"] = nxt
    return body


@dataclass
class Server:
    """Answers ``bodies`` in order (``(status, body, headers)``; the last one repeats) and
    records every request."""

    bodies: list[tuple[int, Any, dict[str, str]]]
    requests: list[httpx2.Request] = field(default_factory=list)

    @classmethod
    def of(cls, *bodies: Any) -> Server:
        return cls([(200, body, {}) for body in bodies])

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status, body, headers = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        return httpx2.Response(status, headers=headers, content=json.dumps(body).encode())


def connector(
    server: Callable[[httpx2.Request], httpx2.Response], **extra: Any
) -> DataverseConnector:
    """A connector over a mock-transport ``SourceHttp`` at ``BASE`` with a synthetic bearer."""
    http = SourceHttp(
        mock_client(server, BASE), breaker_key="dataverse", auth=SyntheticBearer(), clock=_now
    )
    return DataverseConnector(settings(**extra), http=http, clock=_now)


def _now() -> datetime.datetime:
    return NOW
