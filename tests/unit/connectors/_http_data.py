"""Mock-transport clients, a bearer auth and config builders for the HTTP layer tests (T01-14).

``httpx2.MockTransport`` stands in for the network (respx patches only ``httpx``; the egress
source client is ``httpx2``, T10-33 ruling). Tests that need the real host guard build the
client through ``herness.connectors.http.http_client`` with ``tests.support.egress_mock.MockNet``
installed, so ``SourceHostTransport`` runs for real above the mock.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator, Iterable
from pathlib import Path
from typing import Any

import httpx2
from tests.support.config_tree import write_full_config

from herness.connectors.settings import ServiceNowSettings
from herness.core import config as c
from herness.core.resilience import bind_ops_backend
from herness.store.ops.resilience import SqliteResilienceBackend

BASE = "https://sn.example"
SYNTHETIC_TOKEN = "synthetic-bearer-token-t0114"  # noqa: S105  # pragma: allowlist secret
MIB = 1_048_576

type Handler = Callable[[httpx2.Request], httpx2.Response]


def sources_yaml(base_url: str = BASE, verify: Path | None = None) -> str:
    """A ``sources.yaml`` with one enabled ServiceNow source at ``base_url``."""
    bundle = "" if verify is None else f"\n    verify: {verify.as_posix()}"
    return f"""\
version: 1
sources:
  servicenow:
    enabled: true
    base_url: {base_url}{bundle}
    auth: {{method: basic, credentials: "secret:sn"}}
    entities:
      incident:
        fields: [number]
"""


def load_sources(
    tmp_path: Path, base_url: str = BASE, verify: Path | None = None
) -> ServiceNowSettings:
    """Load a full config whose ``sources.yaml`` is ``sources_yaml(...)``; its settings."""
    cfg_dir = write_full_config(tmp_path)
    (cfg_dir / "sources.yaml").write_text(sources_yaml(base_url, verify), encoding="utf-8")
    cfg = c.init_config("hybrid", config_dir=cfg_dir, env={})
    settings = cfg.sources.sources.servicenow
    assert isinstance(settings, ServiceNowSettings)
    return settings


def bind_resilience() -> None:
    """Bind the ops store as the resilience backend (breakers, retry events), as `ops_db`."""
    bind_ops_backend(SqliteResilienceBackend())


class SyntheticBearer(httpx2.Auth):
    """Adds a synthetic bearer token, like the U01-63 auth objects do."""

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
        request.headers["Authorization"] = f"Bearer {SYNTHETIC_TOKEN}"
        yield request


def mock_client(handler: Handler, base_url: str = BASE) -> httpx2.Client:
    """A client over ``httpx2.MockTransport`` (tests only: connectors never build clients)."""
    return httpx2.Client(
        base_url=base_url, transport=httpx2.MockTransport(handler), follow_redirects=False
    )


def reply(
    status: int = 200,
    body: Any = None,
    *,
    headers: dict[str, str] | None = None,
    chunks: Iterable[bytes] | None = None,
) -> httpx2.Response:
    """A streamed response: ``chunks`` when given, else ``body`` as JSON (``{}`` when None)."""
    content = chunks if chunks is not None else iter([json.dumps(body or {}).encode()])
    return httpx2.Response(status, headers=headers or {}, content=content)


def sequence(*responses: httpx2.Response) -> tuple[Handler, list[httpx2.Request]]:
    """A handler answering ``responses`` in order, and the list of requests it saw."""
    seen: list[httpx2.Request] = []
    pending = list(responses)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return pending.pop(0)

    return handler, seen


def big_chunks(total_mib: int) -> Iterable[bytes]:
    """``total_mib`` one-MiB chunks, generated lazily (nothing large is materialised)."""
    return (b" " * MIB for _ in range(total_mib))
