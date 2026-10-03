"""Synthetic settings, scripted sources and an auth-wired adapter builder for the Datadog and
Splunk adapter tests (T01-21).

Hosts are under ``example.invalid`` and every credential starts with ``synthetic`` (R-67).
Adapters get a ``SourceHttp`` over an ``httpx2.MockTransport`` client; ``wired`` instead lets
the adapter build its own (real ``build_auth``) with only the egress client swapped for the mock.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest
from tests.unit.connectors._http_data import mock_client
from tests.unit.connectors._prometheus_data import FETCHED

from herness.connectors.http import SourceHttp
from herness.connectors.monitoring import prometheus as prometheus_module
from herness.connectors.settings_entities import MonitoringAdapterSettings

DD_BASE = "https://api.datadog.example.invalid"
SPLUNK_BASE = "https://splunk.example.invalid:8089"
DD_API_KEY = "synthetic-dd-api-key-t0121"  # pragma: allowlist secret
DD_APP_KEY = "synthetic-dd-app-key-t0121"  # pragma: allowlist secret
SPLUNK_TOKEN = "synthetic-splunk-bearer-t0121"  # noqa: S105  # pragma: allowlist secret
_AUTH = {"datadog": "api_and_app_key", "splunk": "bearer"}
_BASE = {"datadog": DD_BASE, "splunk": SPLUNK_BASE}

type Handler = Callable[[httpx2.Request], httpx2.Response]


def settings(tool: str, **extra: Any) -> MonitoringAdapterSettings:
    """Validated adapter settings of ``tool`` with secret ``secret:<tool>_key``."""
    data: dict[str, Any] = {
        "enabled": True,
        "base_url": _BASE[tool],
        "auth": {"method": _AUTH[tool], "credentials": f"secret:{tool}_key"},
    }
    return MonitoringAdapterSettings.model_validate(data | extra)


def http(tool: str, handler: Handler) -> SourceHttp:
    """A ``SourceHttp`` over the mock handler at the tool's synthetic base URL, no auth."""
    client = mock_client(handler, _BASE[tool])
    return SourceHttp(client, breaker_key=f"monitoring:{tool}", auth=None, clock=lambda: FETCHED)


def wire(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[str]:
    """Swap the egress client factory used by ``source_http`` for the mock; the source keys
    it was called with."""
    calls: list[str] = []

    def fake_client(cfg: Any, *, source: str, max_concurrency: int) -> httpx2.Client:
        del max_concurrency
        calls.append(source)
        return mock_client(handler, cfg.base_url)

    monkeypatch.setattr(prometheus_module, "http_client", fake_client)
    return calls


@dataclass
class Lines:
    """An export endpoint answering one streamed body per request (the last repeating);
    records the requests it saw."""

    bodies: list[Iterable[bytes]]
    seen: list[httpx2.Request] = field(default_factory=list)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        body = self.bodies[min(len(self.seen), len(self.bodies)) - 1]
        return httpx2.Response(200, content=iter(list(body)))


def stream(*objects: object) -> list[bytes]:
    """JSON lines of ``objects`` as one chunk each."""
    return [json.dumps(o).encode() + b"\n" for o in objects]
