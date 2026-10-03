"""Settings, connector builders and batch readers for the ServiceNow connector tests (T01-16).

The connector is built over ``SourceHttp`` on an ``httpx2.MockTransport`` client (T01-14
pattern: connectors never build clients, tests may) answering from the seeded cassettes of
``tests.support.sn_cassettes``.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterable
from typing import Any

import httpx2
import pyarrow as pa
from tests.support.sn_cassettes import BASE_URL, T0
from tests.unit.connectors._http_data import mock_client

from herness.connectors.http import SourceHttp
from herness.connectors.servicenow import ServiceNowConnector
from herness.connectors.settings import ServiceNowSettings

FIELDS = ["number", "opened_at", "priority", "state", "assignment_group", "short_description"]
NOW = T0 + datetime.timedelta(days=5)

type Handler = Callable[[httpx2.Request], httpx2.Response]


def settings(**extra: Any) -> ServiceNowSettings:
    """A validated ServiceNow section on the synthetic host, page size 100."""
    data: dict[str, Any] = {
        "enabled": True,
        "base_url": BASE_URL,
        "auth": {"method": "basic", "credentials": "secret:sn"},
        "page_size": 100,
        "batch_rows": 1000,
        "entities": {"incident": {"fields": FIELDS}},
    }
    return ServiceNowSettings.model_validate(data | extra)


def connector(
    handler: Handler,
    cfg: ServiceNowSettings | None = None,
    *,
    clock: Callable[[], datetime.datetime] = lambda: NOW,
) -> ServiceNowConnector:
    """A connector whose ``SourceHttp`` answers from ``handler`` (no auth object)."""
    http = SourceHttp(mock_client(handler, base_url=BASE_URL), breaker_key="servicenow", auth=None)
    return ServiceNowConnector(cfg or settings(), http=http, clock=clock)


def rows(batches: Iterable[pa.RecordBatch]) -> list[dict[str, Any]]:
    """Every row of ``batches`` as a dict, in order."""
    return [row for b in batches for row in b.to_pylist()]


def params(request: httpx2.Request) -> dict[str, str]:
    """The query parameters of ``request``."""
    return dict(request.url.params)
