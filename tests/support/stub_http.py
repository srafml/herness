"""Loopback threaded HTTP server base for the stub servers (U11-44; TH11-10).

Every stub binds ``127.0.0.1`` only (checked before the socket exists; the server and
handler live in the private sibling ``_stub_http_core``). Handler threads are daemons with
a per-connection socket timeout, bodies over 8 MB get 413 unread, and the counters and
open-connection set live under one lock. With ``service_name`` set, ``start`` merges
``{name: base_url}`` into the inline JSON object held by ``HERNESS_STUB_SERVICES`` in this
process's environment (DD11-04, resolved by T08-25; read by ``faults._stub_url`` under
``HERNESS_ENV=test``), which the ``stub_services`` fixture starts as ``{}``.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import ClassVar, Final, Literal, Self

import pytest
from tests.support._stub_http_core import (
    LOOPBACK,
    MAX_BODY_BYTES,
    LoopbackServer,
    StubRequest,
    StubResponse,
    drop,
)

__all__ = [
    "LOOPBACK",
    "MAX_BODY_BYTES",
    "SERVICES_ENV",
    "StubFault",
    "StubHTTPServer",
    "StubRequest",
    "StubResponse",
    "stub_services",
]

SERVICES_ENV: Final = "HERNESS_STUB_SERVICES"
_POLL_S: Final = 0.05
_SERVICES_LOCK: Final = (
    threading.Lock()
)  # the inline services object shared by every stub in-process

type StubFaultKind = Literal["http_429", "http_529", "malformed_json", "kill"]


@dataclass(frozen=True, slots=True)
class StubFault:
    """A fault served for request indexes ``at <= index < at + count`` (U11-46)."""

    at: int
    kind: StubFaultKind
    count: int = 1

    def __post_init__(self) -> None:
        if self.at < 0 or self.count < 1:
            msg = "StubFault needs at >= 0 and count >= 1"
            raise ValueError(msg)

    def covers(self, index: int) -> bool:
        """Does this fault apply to the request with 0-based ``index``?"""
        return self.at <= index < self.at + self.count


class StubHTTPServer:
    """Loopback ``ThreadingHTTPServer`` with start/stop/kill and service registration."""

    bind_host: ClassVar[str] = LOOPBACK  # anything else is refused before a socket exists

    def __init__(self, *, port: int = 0, service_name: str | None = None) -> None:
        self.service_name = service_name
        self._lock = threading.Lock()
        self._conns: set[socket.socket] = set()
        self._done = 0
        self._kill_at: int | None = None
        self._closed = False
        self._halt = threading.Event()  # set by stop/kill: wakes any `pause`
        self._thread: threading.Thread | None = None
        self._server = LoopbackServer((self.bind_host, port), self)

    @property
    def base_url(self) -> str:
        """``http://127.0.0.1:<port>``."""
        return f"http://{LOOPBACK}:{self._server.server_port}"

    @property
    def server_address(self) -> tuple[str, int]:
        """The bound ``(host, port)``."""
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    @property
    def completed(self) -> int:
        """Requests answered so far (control requests excluded)."""
        with self._lock:
            return self._done

    def start(self) -> None:
        """Serve in a daemon thread and register the service name, if any."""
        if self._thread is not None or self._closed:
            msg = "stub server already started or closed"
            raise RuntimeError(msg)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": _POLL_S}, daemon=True
        )
        self._thread.start()
        if self.service_name is not None:
            _register_service(self.service_name, self.base_url)

    def stop(self) -> None:
        """Stop accepting and close the listening socket; in-flight requests finish."""
        self._shut_listener()

    def kill(self) -> None:
        """Close the listening socket and every open connection now (connects are refused)."""
        self._shut_listener()
        self._drop_connections()

    def kill_after(self, n_requests: int) -> None:
        """``kill`` once ``n_requests`` requests have completed (now, if they already have)."""
        with self._lock:
            self._kill_at = n_requests
            due = self._done >= n_requests
        if due:
            self.kill()

    def respond(self, request: StubRequest) -> StubResponse:
        """Answer one request; subclasses override (the base knows only the control route)."""
        del request
        return StubResponse.json(404, {"error": "not found"})

    def pause(self, seconds: float) -> None:
        """Sleep up to ``seconds``, cut short by ``stop``/``kill`` (for ``hang`` faults)."""
        self._halt.wait(seconds)

    def _shut_listener(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._halt.set()
        if self._thread is not None:
            self._server.shutdown()  # the serve loop exits within one poll interval
        self._server.server_close()

    def _drop_connections(self, keep: socket.socket | None = None) -> None:
        with self._lock:
            conns = [conn for conn in self._conns if conn is not keep]
        for conn in conns:
            drop(conn)

    def _track(self, conn: socket.socket) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._conns.add(conn)
            return True

    def _untrack(self, conn: socket.socket) -> None:
        with self._lock:
            self._conns.discard(conn)

    def _completed(self) -> bool:
        """Count one answered request; True when ``kill_after`` is now due."""
        with self._lock:
            self._done += 1
            return self._kill_at is not None and self._done >= self._kill_at

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()


def _register_service(name: str, base_url: str) -> None:
    """Merge ``{name: base_url}`` into the inline JSON in ``HERNESS_STUB_SERVICES`` (DD11-04)."""
    with _SERVICES_LOCK:
        raw = os.environ.get(SERVICES_ENV)
        if raw is None:
            return  # no `stub_services` fixture: nothing is registered, nothing leaks
        services = json.loads(raw) if raw.strip() else {}
        services[name] = base_url
        os.environ[SERVICES_ENV] = json.dumps(services, sort_keys=True)


@pytest.fixture
def stub_services(monkeypatch: pytest.MonkeyPatch) -> Callable[[], dict[str, str]]:
    """Start ``HERNESS_STUB_SERVICES`` as ``{}`` (restored at teardown) for this test.

    Returns a callable giving the currently registered ``{name: base_url}`` mapping.
    """
    monkeypatch.setenv(SERVICES_ENV, "{}")

    def registered() -> dict[str, str]:
        parsed: dict[str, str] = json.loads(os.environ[SERVICES_ENV])
        return parsed

    return registered
