"""Response streams and loopback transports of the egress component (impl 10 U10-54, U10-59).

Size-forced private sibling of ``herness.core.egress_clients`` (T10-17 fix round 3 ruling),
which re-exports every public name. It holds the decoding, counting response stream shared by
the guarded and loopback transports, the queue of stream-close jobs gc defers, and the
host-restricting, byte-capped loopback transports. It builds no client and no pool transport
(those stay in ``egress.py`` and ``egress_clients.py``, ST10-25).
"""

from __future__ import annotations

import zlib
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from functools import partial
from typing import Final, cast

import httpx2

from herness.core.errors import EgressBlocked
from herness.core.logging import get_logger

__all__ = ["DECODABLE", "LOOPBACK_HOSTS", "MAX_RESPONSE_BYTES", "AsyncCountingStream"]
__all__ += ["AsyncLoopbackOnlyTransport", "CountingStream", "LoopbackOnlyTransport"]
__all__ += ["PENDING", "StreamCounter", "drain", "loopback_refusal", "open_body"]

LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})  # U10-50
MAX_RESPONSE_BYTES: Final = 52_428_800  # 50 MiB (U10-50)
DECODABLE: Final = frozenset({"", "identity", "gzip", "x-gzip", "deflate"})
_PIECE: Final = 1_048_576  # inflate output per step: a tiny body cannot expand at once
_log = get_logger("core.egress")
PENDING: Final[deque[Callable[[], None]]] = deque()  # late stream-close jobs queued by gc


def loopback_refusal(url: httpx2.URL) -> EgressBlocked | None:
    """The refusal for a URL that is not plain ``http(s)`` to a loopback host, else None."""
    host = url.host.lower()
    if host in LOOPBACK_HOSTS and not url.userinfo and url.scheme in {"http", "https"}:
        return None
    _log.warning("egress.loopback.blocked", host=host)  # host only: never the URL or user info
    # T08-05: herness_socket_blocked_total{event="loopback_client"} += 1
    msg = f"loopback client used for {host}"
    return EgressBlocked(msg, reason="not_loopback")


class _Inflater:
    """zlib inflation in bounded pieces (``gzip``; ``deflate`` with or without its header)."""

    def __init__(self, encoding: str) -> None:
        self._raw_next = encoding == "deflate"  # a bare deflate stream is tried once
        self._obj = zlib.decompressobj(15 if self._raw_next else 47)  # 47: gzip or zlib header

    def pieces(self, data: bytes) -> Iterator[bytes]:
        while data:
            try:
                out = self._obj.decompress(data, _PIECE)
            except zlib.error:
                if not self._raw_next:
                    msg = "response body could not be decoded"
                    raise httpx2.DecodingError(msg) from None
                self._raw_next, self._obj = False, zlib.decompressobj(-15)
                continue
            self._raw_next = False
            if out:
                yield out
            data = self._obj.unconsumed_tail

    def flush(self) -> bytes:
        return self._obj.flush()


class StreamCounter:
    """Decoded bytes of one response, a cap on them, and the body kept while it stays small.

    ``bytes_in`` counts the body as the caller reads it, after ``content-encoding`` is undone
    here; the transport drops the header so the client does not decode again (TH10-21).
    """

    def __init__(
        self, limit: int, keep: int, error: Callable[[], EgressBlocked], encoding: str = ""
    ) -> None:
        self.bytes_in = 0
        self.overflowed = False
        self.keep, self._limit, self._error = keep, limit, error
        self._parts: list[bytes] | None = [] if keep > 0 else None
        self._inflater = _Inflater(encoding) if encoding not in {"", "identity"} else None

    def add(self, chunk: bytes) -> None:
        """Count decoded ``chunk``; ``EgressBlocked`` once the total passes the cap."""
        self.bytes_in += len(chunk)
        if self._parts is not None and self.bytes_in <= self.keep:
            self._parts.append(chunk)
        else:
            self._parts = None  # past ``keep``: stop holding the body
        if self.bytes_in > self._limit:
            self.overflowed, self._parts = True, None
            raise self._error()

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        """Decode and count one wire chunk; yield what the caller reads."""
        for piece in self._inflater.pieces(chunk) if self._inflater else (chunk,):
            self.add(piece)
            yield piece

    def finish(self) -> Iterator[bytes]:
        """Decode and count what the decoder still holds at the end of the body."""
        if self._inflater is not None and (tail := self._inflater.flush()):
            self.add(tail)
            yield tail

    @property
    def body(self) -> bytes | None:
        """The whole decoded body when it was kept (at most ``keep`` bytes), else None."""
        return None if self._parts is None else b"".join(self._parts)


def open_body(
    response: httpx2.Response, limit: int, keep: int, error: Callable[[str], EgressBlocked]
) -> tuple[StreamCounter, bool]:
    """Check and count a response body: ``(counter, already_read)``.

    A body the inner transport already read is counted at once. Otherwise a ``content-length``
    over ``limit`` is refused before any byte is read, an encoding other than identity, gzip
    or deflate is refused unread (``unsupported_encoding``), and a decoded body drops its
    ``content-encoding`` and ``content-length`` so the client does not decode it again.
    """
    headers, too_large = response.headers, partial(error, "response_too_large")
    if response.is_closed:  # e.g. a mock transport: the decoded body is already there
        counter = StreamCounter(limit, keep, too_large)
        counter.add(response.content)
        return counter, True
    length = headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise too_large()
    encoding = headers.get("content-encoding", "").strip().lower()
    if encoding not in DECODABLE:  # br, zstd, stacked codings
        reason = "unsupported_encoding"
        raise error(reason)
    for name in ("content-encoding", "content-length") if encoding else ():
        headers.pop(name, None)
    return StreamCounter(limit, keep, too_large, encoding), False


class CountingStream(httpx2.SyncByteStream):
    """Count a sync response stream; call ``on_close`` exactly once when it closes."""

    def __init__(
        self,
        inner: httpx2.SyncByteStream,
        counter: StreamCounter,
        on_close: Callable[[StreamCounter], None],
    ) -> None:
        self._inner, self._counter, self._on_close = inner, counter, on_close
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._inner:
            yield from self._counter.feed(chunk)
        yield from self._counter.finish()

    def close(self) -> None:
        """Close the inner stream, then report the count."""
        if self._closed:
            return
        self._closed = True
        try:
            self._inner.close()
        finally:
            self._on_close(self._counter)


class AsyncCountingStream(httpx2.AsyncByteStream):
    """Async twin of ``CountingStream``."""

    def __init__(
        self,
        inner: httpx2.AsyncByteStream,
        counter: StreamCounter,
        on_close: Callable[[StreamCounter], Awaitable[None]],
    ) -> None:
        self._inner, self._counter, self._on_close = inner, counter, on_close
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            for piece in self._counter.feed(chunk):
                yield piece
        for piece in self._counter.finish():
            yield piece

    async def aclose(self) -> None:
        """Close the inner stream, then report the count."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._inner.aclose()
        finally:
            await self._on_close(self._counter)


def drain() -> None:
    """Run the jobs gc queued (``not_closed`` lines) at a safe point, never inside gc."""
    while True:
        try:
            job = PENDING.popleft()  # atomic: several threads may drain at once
        except IndexError:
            return
        try:
            job()
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the rest
            _log.error("egress.log.failed", error_type=type(exc).__name__)
            # T08-05: herness_egress_log_failures_total += 1


# --- loopback transports (U10-59) -------------------------------------------------------------


def _loopback_error(reason: str) -> EgressBlocked:
    return EgressBlocked(f"loopback response refused: {reason}", reason=reason)


def _admit(request: httpx2.Request) -> None:
    if (refusal := loopback_refusal(request.url)) is not None:
        raise refusal
    request.headers["Accept-Encoding"] = "identity"  # every request, whatever the caller set


async def _nothing(_counter: StreamCounter) -> None:
    return None


class LoopbackOnlyTransport(httpx2.BaseTransport):
    """Refuse non-loopback requests before the inner transport; cap the response (U10-59)."""

    def __init__(self, inner: httpx2.BaseTransport, max_response_bytes: int) -> None:
        self._inner, self._limit = inner, max_response_bytes

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        """Send a loopback ``request``; ``EgressBlocked`` for another host or a large body."""
        _admit(request)
        response = self._inner.handle_request(request)
        try:
            counter, read = open_body(response, self._limit, 0, _loopback_error)
        except EgressBlocked:
            response.close()
            raise
        if not read:
            stream = cast(httpx2.SyncByteStream, response.stream)
            response.stream = CountingStream(stream, counter, lambda _c: None)
        return response

    def close(self) -> None:
        """Close the inner transport."""
        self._inner.close()


class AsyncLoopbackOnlyTransport(httpx2.AsyncBaseTransport):
    """Async twin of ``LoopbackOnlyTransport`` (U10-59)."""

    def __init__(self, inner: httpx2.AsyncBaseTransport, max_response_bytes: int) -> None:
        self._inner, self._limit = inner, max_response_bytes

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Send a loopback ``request``; ``EgressBlocked`` for another host or a large body."""
        _admit(request)
        response = await self._inner.handle_async_request(request)
        try:
            counter, read = open_body(response, self._limit, 0, _loopback_error)
        except EgressBlocked:
            await response.aclose()
            raise
        if not read:
            stream = cast(httpx2.AsyncByteStream, response.stream)
            response.stream = AsyncCountingStream(stream, counter, _nothing)
        return response

    async def aclose(self) -> None:
        """Close the inner transport."""
        await self._inner.aclose()
