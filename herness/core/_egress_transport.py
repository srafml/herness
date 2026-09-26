"""Guarded transports of the egress component (impl 10 U10-54; design 10 §4.5, §5.4).

Size-forced private sibling of ``herness.core.egress`` (T10-17), which re-exports the
transports; it builds no client or transport (ST10-25). The guard decides each request
before the pool sees it; the body is decoded, counted and capped; one ``completed`` line
follows close, a failed or cancelled send, or gc of an unclosed response (``not_closed``,
queued by gc and written at the next safe point: callers must close streamed responses).
"""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import asdict, dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Final, cast

import httpx2

from herness.core import egress_clients as ec
from herness.core import time as clock
from herness.core._egress_streams import PENDING, drain
from herness.core.egress_log import LINE_KEYS
from herness.core.errors import EgressBlocked
from herness.core.logging import get_logger

if TYPE_CHECKING:
    from herness.core.egress import EgressGuard, EgressTicket, PayloadClass, Purpose

__all__ = ["MAX_RESPONSE_BYTES", "USAGE_MAX_BYTES", "AsyncGuardedTransport", "GuardedTransport"]
__all__ += ["TransportOpts"]

MAX_RESPONSE_BYTES: Final = ec.MAX_RESPONSE_BYTES  # 50 MiB (U10-50)
USAGE_MAX_BYTES: Final = 10_485_760  # JSON bodies up to 10 MiB are read for token counts
_log = get_logger("core.egress")


@dataclass(frozen=True)
class TransportOpts:
    """What a guarded client was built for; copied onto every egress line it causes."""

    purpose: Purpose
    payload_class: PayloadClass
    run_id: str | None
    task_id: str | None

    def fields(self) -> dict[str, Any]:
        """The four fields as keyword arguments or egress line values."""
        return asdict(self)


class _Call:
    """One admitted request: its completion is written at most once (U10-54 steps 4-6)."""

    def __init__(
        self, guard: EgressGuard, request: httpx2.Request, opts: TransportOpts, ticket: EgressTicket
    ) -> None:
        self._guard, self._request, self._opts, self._ticket = guard, request, opts, ticket
        request.headers["Accept-Encoding"] = "identity"  # every hop, whatever the caller set
        self._done = False
        self._unclosed: weakref.finalize[Any, Any] | None = None

    def _error(self, reason: str) -> EgressBlocked:
        msg = f"egress blocked ({self._ticket.egress_id}): {reason}"
        return EgressBlocked(msg, egress_id=self._ticket.egress_id, reason=reason)

    def counter(self, response: httpx2.Response) -> ec.StreamCounter | None:
        """A decoding, capped counter for the body (None: already read); JSON is kept."""
        is_json = response.headers.get("content-type", "").startswith("application/json")
        keep, status = USAGE_MAX_BYTES if is_json else 0, response.status_code
        try:
            counter, read = ec.open_body(response, MAX_RESPONSE_BYTES, keep, self._error)
        except EgressBlocked as exc:  # too large by content-length, or an unsupported encoding
            self.complete(status, None, exc.reason)
            raise
        if read:  # e.g. a mock transport: the body is already there
            self.closed(response, counter)
            return None
        late = partial(self.complete, status, counter, "not_closed")  # queued, never written in gc
        self._unclosed = weakref.finalize(response, PENDING.append, late)
        self._unclosed.atexit = False  # type: ignore[misc]  # typeshed lacks the slot
        return counter

    def closed(self, response: httpx2.Response, counter: ec.StreamCounter) -> None:
        """The response stream closed: one ``completed`` line with the provider figures."""
        reason = "response_too_large" if counter.overflowed else None
        self.complete(response.status_code, counter, reason)

    def complete(
        self, status: int | None, counter: ec.StreamCounter | None, reason: str | None
    ) -> None:
        """Write the ``completed`` line; on a failed send ``status_code`` is null."""
        if self._done:
            return
        self._done = True
        if self._unclosed is not None:
            self._unclosed.detach()  # closed in time: nothing is queued at collection
        ticket, url = self._ticket, self._request.url
        tokens_in, tokens_out = ec.provider_usage(counter.body if counter else None)
        bytes_in = counter.bytes_in if counter else 0
        latency_ms = round((clock.monotonic() - ticket.started_monotonic) * 1000)
        line: dict[str, Any] = dict.fromkeys(LINE_KEYS) | self._opts.fields()
        line |= {"egress_id": ticket.egress_id, "decision": "completed", "reason": reason}
        line |= {"destination": url.host.lower(), "method": self._request.method}
        line |= {"path": url.path, "status_code": status, "bytes_in": bytes_in}
        line |= {"latency_ms": latency_ms, "tokens_out": tokens_out}
        line["tokens_in"] = ticket.tokens_in if tokens_in is None else tokens_in
        self._guard._write_completed(line)
        fields = {"status_code": status, "latency_ms": latency_ms, "bytes_in": bytes_in}
        _log.info("egress.call.completed", egress_id=ticket.egress_id, **fields)
        # T08-05: herness_egress_bytes_total{direction="in"} += bytes_in
        # T08-05: herness_egress_tokens_total{direction="out", destination} += tokens_out
        # T08-05: herness_egress_latency_seconds.observe(latency_ms / 1000)


def _drained_admit(guard: EgressGuard, req: httpx2.Request, opts: TransportOpts) -> EgressTicket:
    drain()
    return guard._admit(req, opts)


class GuardedTransport(httpx2.BaseTransport):
    """Run the guard's check before the inner transport; log completion (U10-54)."""

    def __init__(
        self,
        inner: httpx2.BaseTransport,
        *,
        guard: EgressGuard,
        purpose: Purpose,
        payload_class: PayloadClass,
        run_id: str | None,
        task_id: str | None,
    ) -> None:
        self._inner, self._guard = inner, guard
        self._opts = TransportOpts(purpose, payload_class, run_id, task_id)

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        """Admit ``request`` (``EgressBlocked`` on refusal), send it, count the response."""
        ticket = _drained_admit(self._guard, request, self._opts)
        call = _Call(self._guard, request, self._opts, ticket)
        try:
            response = self._inner.handle_request(request)
        except BaseException as exc:  # KeyboardInterrupt too: the line is still written
            call.complete(None, None, type(exc).__name__)
            raise
        try:
            counter = call.counter(response)
        except EgressBlocked:
            response.close()
            raise
        if counter is not None:
            stream = cast(httpx2.SyncByteStream, response.stream)
            response.stream = ec.CountingStream(stream, counter, partial(call.closed, response))
        return response

    def close(self) -> None:
        """Close the inner transport; flush queued ``not_closed`` lines."""
        try:
            drain()
        finally:
            self._inner.close()


class AsyncGuardedTransport(httpx2.AsyncBaseTransport):
    """Async twin of ``GuardedTransport``; the check runs in a worker thread (U10-54)."""

    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        *,
        guard: EgressGuard,
        purpose: Purpose,
        payload_class: PayloadClass,
        run_id: str | None,
        task_id: str | None,
    ) -> None:
        self._inner, self._guard = inner, guard
        self._opts = TransportOpts(purpose, payload_class, run_id, task_id)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Admit ``request`` (``EgressBlocked`` on refusal), send it, count the response."""
        ticket = await asyncio.to_thread(_drained_admit, self._guard, request, self._opts)
        call = _Call(self._guard, request, self._opts, ticket)
        try:
            response = await self._inner.handle_async_request(request)
        except BaseException as exc:  # CancelledError too: written synchronously, not awaited
            call.complete(None, None, type(exc).__name__)
            raise
        try:
            counter = call.counter(response)
        except EgressBlocked:
            await response.aclose()
            raise
        if counter is not None:

            async def closed(counter: ec.StreamCounter) -> None:
                await asyncio.to_thread(call.closed, response, counter)

            stream = cast(httpx2.AsyncByteStream, response.stream)
            response.stream = ec.AsyncCountingStream(stream, counter, closed)
        return response

    async def aclose(self) -> None:
        """Close the inner transport; flush queued ``not_closed`` lines."""
        try:
            await asyncio.to_thread(drain)
        finally:
            await self._inner.aclose()
