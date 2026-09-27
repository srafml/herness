"""Tests for tests.support.stub_http.StubHTTPServer (U11-44; UT11-72, T11-23)."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import httpx2
import pytest
from tests.support import _stub_http_core
from tests.support.stub_http import (
    MAX_BODY_BYTES,
    StubFault,
    StubHTTPServer,
    StubRequest,
    StubResponse,
)

from herness.core import egress

pytestmark = pytest.mark.unit


class _Echo(StubHTTPServer):
    """Answers every non-control request with its method, path and body length."""

    def respond(self, request: StubRequest) -> StubResponse:
        if request.path == "/missing":
            return super().respond(request)
        payload = {"method": request.method, "path": request.path, "size": len(request.body)}
        return StubResponse.json(200, payload)


def _client(server: StubHTTPServer) -> httpx2.Client:
    return egress.loopback_http_client(server.base_url, timeout_s=10)


def _refused(server: StubHTTPServer) -> bool:
    try:
        with socket.create_connection(server.server_address, timeout=10):
            return False
    except ConnectionRefusedError:
        return True


def test_ut11_72_binds_loopback_on_an_ephemeral_port() -> None:
    """UT11-72 port 0: bound to 127.0.0.1 on an ephemeral port; base_url names it."""
    with _Echo() as server, _client(server) as client:
        host, port = server.server_address
        assert (host, port > 0) == ("127.0.0.1", True)
        assert server.base_url == f"http://127.0.0.1:{port}"
        assert client.get("/a").json() == {"method": "GET", "path": "/a", "size": 0}
        assert client.get("/missing").status_code == 404


def test_ut11_72_kill_after_two_refuses_the_third_connect() -> None:
    """UT11-72 kill_after(2): two requests answered, then the third connect is refused."""
    server = _Echo()
    server.kill_after(2)
    server.start()
    with _client(server) as client:
        assert client.post("/1", content=b"ab").json()["size"] == 2
        assert client.post("/2").status_code == 200
    assert server.completed == 2
    assert _refused(server)


def test_ut11_72_control_kill_answers_204_then_kills() -> None:
    """UT11-72 POST /__control/kill → 204, then connects are refused."""
    server = _Echo()
    server.start()
    with _client(server) as client:
        assert client.post("/__control/kill").status_code == 204
    assert _refused(server)
    server.stop()  # idempotent after kill


def test_ut11_72_kill_drops_open_connections() -> None:
    """UT11-72 kill closes an idle open connection immediately, not only the listener."""
    server = _Echo()
    server.start()
    conn = socket.create_connection(server.server_address, timeout=10)
    try:
        server.kill()  # the handler thread waits for a request line on `conn`
        assert conn.recv(1) == b""  # the server side is gone
    except ConnectionResetError:
        pass  # Windows may report the drop as a reset: also closed
    finally:
        conn.close()
    assert _refused(server)


def test_ut11_72_service_file_lists_the_names(stub_services: Path) -> None:
    """UT11-72 service_name servers merge `{name: base_url}` into HERNESS_STUB_SERVICES."""
    stub_services.write_text(json.dumps({"openjev": "http://127.0.0.1:1"}), encoding="utf-8")
    with (
        _Echo(service_name="vllm-reasoning") as a,
        _Echo(service_name="llamacpp-large") as b,
        _Echo() as unnamed,
    ):
        services = json.loads(stub_services.read_text(encoding="utf-8"))
    assert services == {
        "openjev": "http://127.0.0.1:1",
        "vllm-reasoning": a.base_url,
        "llamacpp-large": b.base_url,
    }
    assert unnamed.base_url not in services.values()
    assert [p.name for p in stub_services.parent.iterdir() if p.suffix == ".tmp"] == []


def test_ut11_72_concurrent_service_writes_keep_every_name(stub_services: Path) -> None:
    """UT11-72 service file writes are serialised under the lock: no name is lost."""
    servers = [_Echo(service_name=f"svc-{i}") for i in range(8)]
    threads = [threading.Thread(target=s.start) for s in servers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        services = json.loads(stub_services.read_text(encoding="utf-8"))
        assert services == {s.service_name: s.base_url for s in servers}
    finally:
        for s in servers:
            s.stop()


def test_ut11_72_oversized_body_is_413_unread() -> None:
    """UT11-72 a body declared over 8 MB gets 413 without the server reading it."""
    with _Echo() as server, socket.create_connection(server.server_address, timeout=10) as conn:
        head = f"POST /big HTTP/1.1\r\nHost: x\r\nContent-Length: {MAX_BODY_BYTES + 1}\r\n\r\n"
        conn.sendall(head.encode("ascii"))  # no body is ever sent
        reply = conn.recv(4096).decode("ascii")
    assert reply.startswith("HTTP/1.0 413")
    assert server.completed == 0


def test_ut11_72_stalled_client_times_out_its_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT11-72 a client that never finishes its body is cut by the per-request timeout."""
    monkeypatch.setattr(_stub_http_core.Handler, "timeout", 0.2)
    with _Echo() as server, socket.create_connection(server.server_address, timeout=10) as conn:
        conn.sendall(b"POST /slow HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\nab")
        assert conn.recv(4096) == b""  # closed by the server, no response
    assert server.completed == 0


def test_ut11_72_lifecycle_rules() -> None:
    """UT11-72 start twice is refused; stop then start is refused; faults validate."""
    server = _Echo()
    server.start()
    with pytest.raises(RuntimeError):
        server.start()
    server.stop()
    server.stop()
    with pytest.raises(RuntimeError):
        server.start()
    assert StubFault(at=2, kind="http_429", count=2).covers(3)
    assert not StubFault(at=2, kind="kill").covers(3)
    with pytest.raises(ValueError, match="count"):
        StubFault(at=0, kind="kill", count=0)
