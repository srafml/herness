"""Local servers and a socket recorder for the guarded-client tests (impl 10 §11 ``fake_socket``).

Everything listens on 127.0.0.1 only; nothing here reaches a real network.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import http.server
import socket
import ssl
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def record_connects(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record and refuse every outbound connect, sync (``socket.connect``) and asyncio.

    The only connect let through is the event loop's own self-pipe (``socket.socketpair``
    falls back to a loopback connect on Windows); it is not recorded.
    """
    calls: list[object] = []
    real_connect = socket.socket.connect

    def connect(sock: socket.socket, address: object) -> None:
        if sys._getframe(1).f_code.co_name == "_fallback_socketpair":
            real_connect(sock, address)  # type: ignore[arg-type]
            return
        calls.append(address)
        raise ConnectionRefusedError(address)

    async def create_connection(_loop: object, *args: object, **_kw: object) -> NoReturn:
        calls.append(args[1:3])
        raise ConnectionRefusedError(args[1:3])

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect)
    monkeypatch.setattr(asyncio.BaseEventLoop, "create_connection", create_connection)
    return calls


@dataclass
class StubLog:
    """What the loopback stub server saw: ``(method, path, authorization header)``."""

    requests: list[tuple[str, str, str | None]] = field(default_factory=list)


class _Handler(http.server.BaseHTTPRequestHandler):
    log: StubLog

    def _answer(self) -> None:
        self.log.requests.append((self.command, self.path, self.headers.get("Authorization")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.org/")
        else:
            self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    do_GET = _answer  # noqa: N815 - http.server method names
    do_POST = _answer  # noqa: N815 - http.server method names

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base signature
        return None


@contextmanager
def loopback_stub() -> Iterator[tuple[str, StubLog]]:
    """An HTTP server on 127.0.0.1: ``/redirect`` answers 302 to example.org, else 200 ``ok``."""
    log = StubLog()
    handler = type("Handler", (_Handler,), {"log": log})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", log
    finally:
        server.shutdown()
        server.server_close()


def self_signed(tmp_path: Path) -> tuple[Path, Path]:
    """A self-signed certificate for ``localhost`` and its key, written under ``tmp_path``."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _serve_once(listener: socket.socket, ctx: ssl.SSLContext) -> None:
    try:
        conn, _ = listener.accept()
    except OSError:
        return
    try:
        with ctx.wrap_socket(conn, server_side=True) as tls:
            tls.recv(65536)
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
    except (OSError, ssl.SSLError):
        conn.close()  # a refused handshake is the expected outcome of some tests


@contextmanager
def tls_server(cert: Path, key: Path, *, tls10_only: bool = False) -> Iterator[int]:
    """A one-shot HTTPS server on 127.0.0.1; ``tls10_only`` offers TLS 1.0 and nothing else."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    if tls10_only:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.maximum_version = ssl.TLSVersion.TLSv1
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    thread = threading.Thread(target=_serve_once, args=(listener, ctx), daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()
        thread.join(timeout=5)
