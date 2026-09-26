"""Loopback clients and the shared transport parts of the egress component (impl 10 U10-59).

Part of ``herness.core.egress``, which re-exports the public names (R-06). With ``egress.py``
this is the only module that builds ``httpx`` clients and transports (ENG §2.1, ST10-25).
It also holds the TLS context and the counting response stream of the guarded clients
(U10-52 step 1, U10-54 step 4).
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Final

import certifi
import httpx
from pydantic import SecretStr

from herness.core.errors import ConfigError, EgressBlocked
from herness.core.logging import get_logger

__all__ = ["LOOPBACK_HOSTS", "AsyncCountingStream", "AsyncLoopbackOnlyTransport"]
__all__ += ["CountingStream", "LoopbackOnlyTransport", "StreamCounter", "aloopback_http_client"]
__all__ += ["check_timeout", "loopback_http_client", "loopback_refusal", "tls_context"]

LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})  # U10-50
MAX_TIMEOUT_S: Final = 3_600.0
_log = get_logger("core.egress")


def tls_context() -> ssl.SSLContext:
    """TLS 1.2 or later, certificate and host name verified against certifi (U10-52 step 1)."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def check_timeout(timeout: float, name: str) -> None:
    """``ConfigError`` unless ``0 < timeout <= 3600`` (NaN fails too)."""
    if not 0 < timeout <= MAX_TIMEOUT_S:
        msg = f"{name} must be greater than 0 and at most 3600 seconds"
        raise ConfigError(msg)


def loopback_refusal(url: httpx.URL) -> EgressBlocked | None:
    """The refusal for a URL that is not plain ``http(s)`` to a loopback host, else None."""
    host = url.host.lower()
    if host in LOOPBACK_HOSTS and not url.userinfo and url.scheme in {"http", "https"}:
        return None
    _log.warning("egress.loopback.blocked", host=host)  # host only: never the URL or user info
    # T08-05: herness_socket_blocked_total{event="loopback_client"} += 1
    msg = f"loopback client used for {host}"
    return EgressBlocked(msg, reason="not_loopback")


class LoopbackOnlyTransport(httpx.BaseTransport):
    """Refuse every request whose host is not loopback, before the inner transport (U10-59)."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Send ``request`` when its URL is loopback; ``EgressBlocked`` otherwise."""
        if (refusal := loopback_refusal(request.url)) is not None:
            raise refusal
        return self._inner.handle_request(request)

    def close(self) -> None:
        """Close the inner transport."""
        self._inner.close()


class AsyncLoopbackOnlyTransport(httpx.AsyncBaseTransport):
    """Async twin of ``LoopbackOnlyTransport`` (U10-59)."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Send ``request`` when its URL is loopback; ``EgressBlocked`` otherwise."""
        if (refusal := loopback_refusal(request.url)) is not None:
            raise refusal
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        """Close the inner transport."""
        await self._inner.aclose()


def _loopback_settings(
    base_url: str, timeout_s: float, bearer: SecretStr | None
) -> tuple[httpx.Timeout, dict[str, str]]:
    """Check the U10-59 preconditions; the client timeout and default headers."""
    try:
        url = httpx.URL(base_url)
    except (httpx.InvalidURL, ValueError, TypeError):
        url = httpx.URL()  # no scheme and no host: refused below
    if (refusal := loopback_refusal(url)) is not None:
        raise refusal
    check_timeout(timeout_s, "timeout_s")
    headers = {} if bearer is None else {"Authorization": f"Bearer {bearer.get_secret_value()}"}
    return httpx.Timeout(timeout_s, connect=min(timeout_s, 5.0)), headers


def loopback_http_client(
    base_url: str, *, timeout_s: float, bearer: SecretStr | None = None
) -> httpx.Client:
    """The only client for local model servers and their health checks (U10-59, R-06)."""
    timeout, headers = _loopback_settings(base_url, timeout_s, bearer)
    return httpx.Client(
        base_url=base_url,
        transport=LoopbackOnlyTransport(httpx.HTTPTransport(retries=0)),
        headers=headers,
        follow_redirects=False,
        trust_env=False,
        timeout=timeout,
    )


def aloopback_http_client(
    base_url: str, *, timeout_s: float, bearer: SecretStr | None = None
) -> httpx.AsyncClient:
    """Async twin of ``loopback_http_client`` (U10-59)."""
    timeout, headers = _loopback_settings(base_url, timeout_s, bearer)
    return httpx.AsyncClient(
        base_url=base_url,
        transport=AsyncLoopbackOnlyTransport(httpx.AsyncHTTPTransport(retries=0)),
        headers=headers,
        follow_redirects=False,
        trust_env=False,
        timeout=timeout,
    )


# --- counting response stream (U10-54 step 4; reused by the source client, U10-110) --------


class StreamCounter:
    """Bytes read from one response, a cap, and the body kept while it stays small."""

    def __init__(self, limit: int, keep: int, error: Callable[[], EgressBlocked]) -> None:
        self.bytes_in = 0
        self.overflowed = False
        self._limit, self._keep, self._error = limit, keep, error
        self._parts: list[bytes] | None = [] if keep > 0 else None

    def add(self, chunk: bytes) -> None:
        """Count ``chunk``; ``EgressBlocked`` once the total passes the cap."""
        self.bytes_in += len(chunk)
        if self._parts is not None and self.bytes_in <= self._keep:
            self._parts.append(chunk)
        else:
            self._parts = None  # past ``keep``: stop holding the body
        if self.bytes_in > self._limit:
            self.overflowed, self._parts = True, None
            raise self._error()

    @property
    def body(self) -> bytes | None:
        """The whole body when it was kept (at most ``keep`` bytes), else None."""
        return None if self._parts is None else b"".join(self._parts)


class CountingStream(httpx.SyncByteStream):
    """Count a sync response stream; call ``on_close`` exactly once when it closes."""

    def __init__(
        self,
        inner: httpx.SyncByteStream,
        counter: StreamCounter,
        on_close: Callable[[StreamCounter], None],
    ) -> None:
        self._inner, self._counter, self._on_close = inner, counter, on_close
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._inner:
            self._counter.add(chunk)
            yield chunk

    def close(self) -> None:
        """Close the inner stream, then report the count."""
        if self._closed:
            return
        self._closed = True
        try:
            self._inner.close()
        finally:
            self._on_close(self._counter)


class AsyncCountingStream(httpx.AsyncByteStream):
    """Async twin of ``CountingStream``."""

    def __init__(
        self,
        inner: httpx.AsyncByteStream,
        counter: StreamCounter,
        on_close: Callable[[StreamCounter], Awaitable[None]],
    ) -> None:
        self._inner, self._counter, self._on_close = inner, counter, on_close
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._counter.add(chunk)
            yield chunk

    async def aclose(self) -> None:
        """Close the inner stream, then report the count."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._inner.aclose()
        finally:
            await self._on_close(self._counter)
