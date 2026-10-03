"""Synthetic settings, a scripted source and response bodies for the Prometheus/Mimir and
Dynatrace adapter tests (T01-20).

Hosts are under ``example.invalid`` and every credential starts with ``synthetic`` (R-67).
The source is an ``httpx2.MockTransport`` client (``_http_data.mock_client``): adapters never
build clients themselves in these tests, they get a ``SourceHttp`` over the mock.
"""

from __future__ import annotations

import datetime
import json
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pyarrow as pa
import pytest
from tests.unit.connectors._http_data import mock_client

import herness.core.resilience.retry as retry_module
from herness.connectors.http import SourceHttp
from herness.connectors.settings_entities import MonitoringAdapterSettings

UTC = datetime.UTC
FETCHED = datetime.datetime(2026, 3, 5, 12, 0, tzinfo=UTC)
PROM_BASE = "https://mimir.example.invalid/prometheus"
DT_BASE = "https://tenant.example.invalid"
TENANT = "synthetic-tenant-t0120"
PROM_TOKEN = "synthetic-prom-bearer-t0120"  # noqa: S105  # pragma: allowlist secret
DT_TOKEN = "synthetic-dynatrace-token-t0120"  # noqa: S105  # pragma: allowlist secret
_AUTH = {"prometheus": "bearer", "dynatrace": "api_token"}
_BASE = {"prometheus": PROM_BASE, "dynatrace": DT_BASE}


def settings(tool: str, **extra: Any) -> MonitoringAdapterSettings:
    """Validated adapter settings of ``tool`` with secret ``secret:<tool>_key``."""
    data: dict[str, Any] = {
        "enabled": True,
        "base_url": _BASE[tool],
        "auth": {"method": _AUTH[tool], "credentials": f"secret:{tool}_key"},
    }
    return MonitoringAdapterSettings.model_validate(data | extra)


def query(
    name: str = "error_rate", text: str = "sum by (service) (rate(x[1d]))", **extra: Any
) -> dict[str, Any]:
    """One ``metric_queries`` entry."""
    return {"name": name, "query": text, "unit": "ratio"} | extra


@dataclass
class Source:
    """Answers ``replies`` (status, JSON body) in order, the last one repeating; records the
    requests it saw."""

    replies: list[tuple[int, Any]]
    seen: list[httpx2.Request] = field(default_factory=list)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        status, body = self.replies[min(len(self.seen), len(self.replies)) - 1]
        return httpx2.Response(status, content=iter([json.dumps(body).encode()]))

    def params(self, index: int) -> dict[str, str]:
        """The decoded query parameters of request ``index``."""
        return dict(urllib.parse.parse_qsl(self.seen[index].url.query.decode()))


def source_http(tool: str, source: Callable[[httpx2.Request], httpx2.Response]) -> SourceHttp:
    """A ``SourceHttp`` over the mock source at the tool's synthetic base URL, no auth."""
    client = mock_client(source, _BASE[tool])
    return SourceHttp(client, breaker_key=f"monitoring:{tool}", auth=None, clock=lambda: FETCHED)


def stub_resilience(monkeypatch: pytest.MonkeyPatch) -> None:
    """``retry_page`` without an ops store: no breaker guard, a no-op breaker."""

    class _Breaker:
        def record_failure(self, err: Exception) -> None:
            del err

        def record_success(self) -> None:
            return None

    monkeypatch.setattr(retry_module, "guard", lambda _key: None)
    monkeypatch.setattr(retry_module, "breaker", lambda _key: _Breaker())


def rows(batches: list[pa.RecordBatch]) -> list[dict[str, Any]]:
    """All rows of ``batches`` as dicts."""
    return [row for batch in batches for row in batch.to_pylist()]


def epoch(day: datetime.date) -> int:
    """Epoch seconds of 00:00 UTC on ``day``."""
    return int(datetime.datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def matrix(*series: tuple[dict[str, str], list[list[Any]]]) -> dict[str, Any]:
    """A successful ``query_range`` body with the given ``(metric, values)`` series."""
    result = [{"metric": metric, "values": values} for metric, values in series]
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}
