"""Private sibling of ``stub_http`` (T11-23 spec note): the loopback server and handler.

``LoopbackServer`` refuses any host but ``127.0.0.1`` before its socket exists and again in
``server_bind`` (TH11-10, ST11-13); ``Handler`` reads at most 8 MB per body (413 otherwise,
unread), times out stalled clients, and hands each request to the owning ``StubHTTPServer``.
Imported only by ``tests.support.stub_http``, which re-exports the public names.
"""

from __future__ import annotations

import contextlib
import json
import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from typing import Any, Final, Protocol

__all__ = [
    "KILL_PATH",
    "LOOPBACK",
    "MAX_BODY_BYTES",
    "Handler",
    "LoopbackServer",
    "StubRequest",
    "StubResponse",
    "drop",
]

LOOPBACK: Final = "127.0.0.1"
MAX_BODY_BYTES: Final = 8 * 1024 * 1024
KILL_PATH: Final = "/__control/kill"
_SOCKET_TIMEOUT_S: Final = 10.0


@dataclass(frozen=True, slots=True)
class StubRequest:
    """One received request; header names are lower-cased."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass(slots=True)
class StubResponse:
    """What a stub answers: a whole body, streamed ``chunks``, or a bare disconnect."""

    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    chunks: Iterable[bytes] | None = None  # sent without Content-Length; the close ends it
    disconnect: bool = False  # close the connection without writing a response

    @classmethod
    def json(cls, status: int, payload: object, **headers: str) -> StubResponse:
        """A JSON response."""
        body = json.dumps(payload).encode("utf-8")
        return cls(status, body, {"Content-Type": "application/json", **headers})


class _Owner(Protocol):
    """The ``StubHTTPServer`` side the server and handler call back into."""

    def respond(self, request: StubRequest) -> StubResponse: ...
    def _completed(self) -> bool: ...
    def _shut_listener(self) -> None: ...
    def _drop_connections(self, keep: socket.socket | None = None) -> None: ...
    def _track(self, conn: socket.socket) -> bool: ...
    def _untrack(self, conn: socket.socket) -> None: ...


def _require_loopback(host: object) -> None:
    if host != "127.0.0.1":  # a literal: nothing a subclass or patch sets can widen it
        msg = "stub servers bind 127.0.0.1 only (TH11-10)"
        raise PermissionError(msg)


def drop(conn: socket.socket) -> None:
    """Close a connection now, whatever state it is in."""
    with contextlib.suppress(OSError):
        conn.shutdown(socket.SHUT_RDWR)
    conn.close()


class LoopbackServer(ThreadingHTTPServer):
    """Threaded HTTP server that can only listen on 127.0.0.1; daemon handler threads."""

    daemon_threads = True
    allow_reuse_address = False  # never share a port with another listener

    def __init__(self, address: tuple[str, int], owner: _Owner) -> None:
        _require_loopback(address[0])  # before the socket exists (ST11-13)
        self.owner = owner
        super().__init__(address, Handler)

    def server_bind(self) -> None:
        """Re-check the address, bind, and skip ``HTTPServer``'s reverse-DNS ``getfqdn``."""
        _require_loopback(self.server_address[0])
        TCPServer.server_bind(self)
        self.server_name, self.server_port = LOOPBACK, int(self.server_address[1])

    def verify_request(self, request: Any, client_address: Any) -> bool:
        """Track the connection; refused once the stub is stopped or killed."""
        return self.owner._track(request)

    def shutdown_request(self, request: Any) -> None:
        """Untrack, then close the connection."""
        self.owner._untrack(request)
        super().shutdown_request(request)


class Handler(BaseHTTPRequestHandler):
    """Bounded body read, the ``/__control/kill`` route, and the owner's ``respond``."""

    server: LoopbackServer
    timeout = _SOCKET_TIMEOUT_S  # per-connection socket timeout (StreamRequestHandler.setup)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Quiet: tests read state from the server, not stderr."""

    def _body_length(self) -> int | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        return length if length >= 0 else None

    def _dispatch(self) -> None:
        length = self._body_length()
        if length is None or length > MAX_BODY_BYTES:
            self.close_connection = True  # the body is never read
            self._write(StubResponse.json(400 if length is None else 413, {"error": "body"}))
            return
        try:
            body = self.rfile.read(length)
        except TimeoutError:
            self.close_connection = True
            return
        headers = {name.lower(): value for name, value in self.headers.items()}
        request = StubRequest(self.command, self.path.split("?", 1)[0], headers, body)
        owner = self.server.owner
        control = request.method == "POST" and request.path == KILL_PATH
        response = StubResponse(204) if control else owner.respond(request)
        due = control or owner._completed()
        if due:  # stop listening before the client can see the answer (no connect race)
            owner._shut_listener()
        self._write(response)
        if due:
            owner._drop_connections(keep=self.connection)

    do_GET = _dispatch  # noqa: N815 - http.server method names
    do_POST = _dispatch  # noqa: N815 - http.server method names

    def _write(self, response: StubResponse) -> None:
        if response.disconnect:
            self.close_connection = True
            drop(self.connection)
            return
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        if response.chunks is None:
            self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        for chunk in [response.body] if response.chunks is None else response.chunks:
            self.wfile.write(chunk)
            self.wfile.flush()
