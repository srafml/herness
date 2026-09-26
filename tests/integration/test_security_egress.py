"""Integration tests of the guarded egress clients (impl 10 IT10-03, IT10-04; card T10-17).

Also checks T08-08's ``kill_service`` fault action against the real loopback client.
No real network: ``MockNet`` answers cloud routes and a stub server listens on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.support.egress_harness import API, egress_lines, load
from tests.support.egress_mock import MockNet
from tests.support.egress_servers import loopback_stub, record_connects
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import egress as eg
from herness.core.errors import EgressBlocked
from herness.core.logging import configure_logging, reset_logging
from herness.core.resilience import faults

pytestmark = pytest.mark.integration

MARKER = "QZX-EVIDENCE-5519"


@pytest.fixture(autouse=True)
def _isolate(fake_keyring: MemoryKeyring) -> Iterator[None]:
    yield
    eg.reset_guard()
    c.reset_config()


def _messages_body() -> bytes:
    """An adapter-like request: aggregated evidence only, carrying a unique marker."""
    evidence = {"metric": "mttr_hours", "by_service": {"svc-7": 12.5}, "note": MARKER}
    return json.dumps(
        {
            "model": "claude-x",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": json.dumps(evidence)}],
        }
    ).encode()


def test_it10_03_hybrid_adapter_call_logs_without_payload(
    tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT10-03 hybrid with the gate: allowed and completed lines; no payload text in any log."""
    fake_keyring.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()
    cfg = load(tmp_path, "hybrid")
    logs = cfg.paths.logs
    guard = eg.get_guard()  # as T05-08 builds it: get_guard().async_http_client(...)
    reply = {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 40}}
    reply["usage"]["output_tokens"] = 9

    async def adapter_call() -> int:
        async with guard.async_http_client(
            "reasoning_final", "aggregated_evidence", run_id="run_it03", timeout=60.0
        ) as client:
            response = await client.post(API, content=_messages_body())
            return response.status_code

    configure_logging("DEBUG", log_dir=logs, scrubber=lambda _l, _m, event: event)
    try:
        route = MockNet().install(monkeypatch).route("api.anthropic.com", json=reply)
        assert asyncio.run(adapter_call()) == 200
        assert route.call_count == 1
    finally:
        reset_logging()
    allowed, completed = egress_lines(logs)
    assert (allowed["decision"], completed["decision"]) == ("allowed", "completed")
    assert allowed["egress_id"] == completed["egress_id"]
    assert (completed["tokens_in"], completed["tokens_out"]) == (40, 9)
    assert completed["run_id"] == "run_it03"
    files = [p for p in logs.rglob("*") if p.is_file()]
    assert {p.name.split("-")[0] for p in files} >= {"egress", "herness"}
    for path in files:
        assert MARKER.encode() not in path.read_bytes(), path.name


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_it10_04_local_profile_blocks_every_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """IT10-04 profile local: every http_client() request is EgressBlocked; zero connects."""
    load(tmp_path, "local")
    connects = record_connects(monkeypatch)
    guard = eg.get_guard()
    kinds: list[tuple[Any, Any]] = [
        ("reasoning_final", "aggregated_evidence"),
        ("reasoning", "redacted_text"),
        ("bulk_classification", "none"),
    ]

    async def async_send(purpose: Any, payload: Any) -> None:
        async with guard.async_http_client(purpose, payload) as client:
            await client.request(method, API, content=b"{}")

    for purpose, payload in kinds:  # real httpx2 pools underneath: any send would connect
        with guard.http_client(purpose, payload) as client, pytest.raises(EgressBlocked):
            client.request(method, API, content=b"{}")
        with pytest.raises(EgressBlocked):
            asyncio.run(async_send(purpose, payload))
    assert connects == []
    reasons = {line["reason"] for line in egress_lines(guard._cfg.paths.logs)}
    assert reasons == {"profile_forbids_egress"}


def test_ut08_49_kill_service_through_real_loopback_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT08-49 kill_service POSTs /__control/kill through the real loopback_http_client."""
    with loopback_stub() as (base, stub):
        monkeypatch.setenv("HERNESS_STUB_SERVICES", json.dumps({"openjev": base}))
        faults._kill_service("openjev")
    assert stub.requests == [("POST", "/__control/kill", None)]
