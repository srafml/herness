"""ST10-39 and ST10-54 against real local servers on 127.0.0.1 (impl 10 card T10-17).

ST10-39 drives the guarded client's TLS layer: the guard decision is stubbed to "allowed"
(the destination is a local port, which U10-51 step 3 would refuse) and the CA set is
overridden for the test so a self-signed 127.0.0.1 certificate can be trusted.
ST10-54 sends the loopback client to a stub server that answers 302 to example.org.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
import structlog
from pydantic import SecretStr
from tests.support.egress_harness import API, load, make_guard
from tests.support.egress_servers import loopback_stub, self_signed, tls_server
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import egress as eg
from herness.core import egress_clients as ec
from herness.core import time as clock
from herness.core.errors import EgressBlocked

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolate(fake_keyring: MemoryKeyring) -> Iterator[None]:
    yield
    c.reset_config()


@pytest.fixture
def tls_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> eg.EgressGuard:
    """A hybrid guard whose check always admits (the TLS layer is under test)."""
    guard = make_guard(load(tmp_path, "hybrid"))

    def admit(_request: httpx2.Request, _opts: object) -> eg.EgressTicket:
        return eg.EgressTicket("egr_tls_test", 1, clock.monotonic())

    monkeypatch.setattr(guard, "_admit", admit)
    return guard


def _get(guard: eg.EgressGuard, url: str) -> httpx2.Response:
    with guard.http_client("reasoning_final", "aggregated_evidence", timeout=10.0) as client:
        return client.get(url)


def test_st10_39_self_signed_certificate_is_refused(
    tls_guard: eg.EgressGuard, tmp_path: Path
) -> None:
    """ST10-39 a self-signed certificate not in the CA set: certificate verification error."""
    cert, key = self_signed(tmp_path)
    with tls_server(cert, key) as port, pytest.raises(httpx2.ConnectError, match="CERTIFICATE"):
        _get(tls_guard, f"https://127.0.0.1:{port}/")


def test_st10_39_trusted_certificate_ignores_env_proxy(
    tls_guard: eg.EgressGuard, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-39 control: with the CA overridden the call succeeds; HTTPS_PROXY is ignored."""
    cert, key = self_signed(tmp_path)
    monkeypatch.setattr(ec.certifi, "where", lambda: str(cert))
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")  # nothing listens: using it would fail
    with tls_server(cert, key) as port:
        response = _get(tls_guard, f"https://127.0.0.1:{port}/")
    assert (response.status_code, response.text) == (200, "ok")


def test_st10_39_tls_1_0_handshake_fails(
    tls_guard: eg.EgressGuard, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-39 a server offering only TLS 1.0 (trusted certificate): handshake failure."""
    cert, key = self_signed(tmp_path)
    monkeypatch.setattr(ec.certifi, "where", lambda: str(cert))
    with (
        tls_server(cert, key, tls10_only=True) as port,
        pytest.raises(httpx2.ConnectError, match=r"(?i)ssl|tls|protocol|handshake|version"),
    ):
        _get(tls_guard, f"https://127.0.0.1:{port}/")


def test_st10_54_stub_redirect_is_not_followed() -> None:
    """ST10-54 a loopback stub answering 302 to example.org: not followed; forced -> blocked."""
    bearer = SecretStr("-".join(("st10", "54", "stub")))
    with (
        structlog.testing.capture_logs() as logs,
        loopback_stub() as (base, stub),
        eg.loopback_http_client(base, timeout_s=5.0, bearer=bearer) as client,
    ):
        response = client.get("/redirect")
        with pytest.raises(EgressBlocked) as info:
            client.get("/redirect", follow_redirects=True)
    assert response.status_code == 302
    assert response.headers["Location"] == "http://example.org/"
    assert info.value.reason == "not_loopback"
    assert [path for _m, path, _a in stub.requests] == ["/redirect", "/redirect"]
    assert {auth for _m, _p, auth in stub.requests} == {f"Bearer {bearer.get_secret_value()}"}
    assert [entry["host"] for entry in logs if "host" in entry] == ["example.org"]
    assert bearer.get_secret_value() not in repr(logs)


def test_st10_54_async_stub_reached() -> None:
    """ST10-54 the async loopback client reaches the stub and does not follow its redirect."""
    import asyncio  # noqa: PLC0415 - only this test runs an event loop

    async def run(base: str) -> int:
        async with eg.aloopback_http_client(base, timeout_s=5.0) as client:
            return (await client.get("/redirect")).status_code

    with loopback_stub() as (base, stub):
        assert asyncio.run(run(base)) == 302
    assert [path for _m, path, _a in stub.requests] == ["/redirect"]


def test_st10_54_real_request_line_has_no_egress_line(tmp_path: Path) -> None:
    """ST10-54 local calls are not egress: no egress line, no redaction re-scan."""
    cfg = load(tmp_path, "hybrid")
    with loopback_stub() as (base, _stub), eg.loopback_http_client(base, timeout_s=5.0) as client:
        assert client.post("/health", content=json.dumps({"x": API}).encode()).status_code == 200
    assert list(cfg.paths.logs.glob("egress-*.jsonl")) == []
