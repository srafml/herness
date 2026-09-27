"""ST11-13 (TH11-10), bind part: stub servers listen on loopback only (T11-23).

The baseline half (a mock run may only save a `-mock` baseline) belongs to the --mock-llm
baseline card.
"""

from __future__ import annotations

import socket

import pytest
from tests.support import _stub_http_core, stub_http
from tests.support.fake_llm import FakeLLMServer
from tests.support.stub_http import StubHTTPServer

from herness.core.egress_clients import LOOPBACK_HOSTS
from herness.core.egress_socket import SocketPolicy
from herness.core.errors import EgressBlocked
from herness.eval.scripted import ScriptBook

pytestmark = pytest.mark.unit


@pytest.fixture
def binds(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record every socket bind (and let it happen)."""
    seen: list[object] = []
    real_bind = socket.socket.bind

    def bind(sock: socket.socket, address: object) -> None:
        seen.append(address)
        real_bind(sock, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "bind", bind)
    return seen


def test_st11_13_stub_servers_bind_loopback_only(binds: list[object]) -> None:
    """ST11-13 the base stub and FakeLLMServer bind 127.0.0.1, which the socket guard allows."""
    policy = SocketPolicy(frozenset(LOOPBACK_HOSTS), None)
    for server in (StubHTTPServer(), FakeLLMServer(ScriptBook([]), service_name=None)):
        with server:
            host, port = server.server_address
            assert host == "127.0.0.1"
            assert server.base_url.startswith("http://127.0.0.1:")
            policy.check_connect((host, port))  # loopback: allowed by the impl 10 guard
    assert [addr[0] for addr in binds] == ["127.0.0.1", "127.0.0.1"]  # type: ignore[index]
    with pytest.raises(EgressBlocked):
        policy.check_connect(("203.0.113.9", 80))  # the guard itself still blocks the rest


@pytest.mark.parametrize("host", ["0.0.0.0", "", "::", "192.0.2.10", "localhost"])  # noqa: S104
def test_st11_13_non_loopback_bind_refused_before_any_socket(
    monkeypatch: pytest.MonkeyPatch, binds: list[object], host: str
) -> None:
    """ST11-13 a non-127.0.0.1 host is refused before a socket is created or bound."""
    created: list[object] = []
    real_init = socket.socket.__init__

    def init(sock: socket.socket, *args: object, **kwargs: object) -> None:
        created.append(args)
        real_init(sock, *args, **kwargs)  # type: ignore[arg-type]

    class _Wide(StubHTTPServer):
        bind_host = host  # a subclass trying to widen the listen address

    monkeypatch.setattr(socket.socket, "__init__", init)
    for module in (stub_http, _stub_http_core):  # nor can a patched module constant
        monkeypatch.setattr(module, "LOOPBACK", host)
    with pytest.raises(PermissionError, match=r"127\.0\.0\.1 only"):
        _Wide()
    with pytest.raises(PermissionError, match=r"127\.0\.0\.1 only"):
        _stub_http_core.LoopbackServer((host, 0), StubHTTPServer.__new__(StubHTTPServer))
    assert (created, binds) == ([], [])


def test_st11_13_server_bind_rechecks_the_address(binds: list[object]) -> None:
    """ST11-13 server_bind refuses a non-loopback address set after construction."""
    server = StubHTTPServer()
    try:
        inner = server._server
        inner.server_address = ("0.0.0.0", 0)  # noqa: S104
        with pytest.raises(PermissionError):
            inner.server_bind()
        assert len(binds) == 1  # only the original loopback bind happened
    finally:
        server.stop()
