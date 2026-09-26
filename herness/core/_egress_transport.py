"""Guarded transports of the egress component (impl 10 U10-54; design 10 §4.5, §5.4).

Size-forced private sibling of ``herness.core.egress`` (T10-17), which re-exports
``GuardedTransport`` and ``AsyncGuardedTransport``. Each request is decided by the guard
before the inner transport (and so the connection pool) sees it; the response stream is
counted and capped, and exactly one ``completed`` line is written when it closes or the
send fails. The transports build no client or transport of their own (ST10-25).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Final, cast

import httpx

from herness.core import time as clock
from herness.core.egress_clients import AsyncCountingStream, CountingStream, StreamCounter
from herness.core.egress_log import LINE_KEYS
from herness.core.errors import EgressBlocked
from herness.core.logging import get_logger

if TYPE_CHECKING:
    from herness.core.egress import EgressGuard, EgressTicket, PayloadClass, Purpose

__all__ = ["MAX_RESPONSE_BYTES", "USAGE_MAX_BYTES", "AsyncGuardedTransport", "GuardedTransport"]
__all__ += ["TransportOpts"]

MAX_RESPONSE_BYTES: Final = 52_428_800  # 50 MiB (U10-50)
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


def _tokens(value: object) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return cast(int, value) if ok else None


def _provider_usage(body: bytes | None) -> tuple[int | None, int | None]:
    """``(tokens_in, tokens_out)`` from ``usage``: Anthropic names, else OpenAI-compatible."""
    try:
        data = json.loads(body) if body else None
    except (ValueError, RecursionError):
        data = None
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None, None
    tokens_in = usage.get("input_tokens", usage.get("prompt_tokens"))
    tokens_out = usage.get("output_tokens", usage.get("completion_tokens"))
    return _tokens(tokens_in), _tokens(tokens_out)


class _Call:
    """One admitted request: its completion is written at most once (U10-54 steps 4-6)."""

    def __init__(
        self, guard: EgressGuard, request: httpx.Request, opts: TransportOpts, ticket: EgressTicket
    ) -> None:
        self._guard, self._request, self._opts, self._ticket = guard, request, opts, ticket
        self._done = False

    def counter(self, response: httpx.Response) -> StreamCounter:
        """A capped counter for ``response``; a JSON body is kept for its token counts."""
        egress_id = self._ticket.egress_id
        is_json = response.headers.get("content-type", "").startswith("application/json")
        msg = f"egress blocked ({egress_id}): response_too_large"
        error = partial(EgressBlocked, msg, egress_id=egress_id, reason="response_too_large")
        return StreamCounter(MAX_RESPONSE_BYTES, USAGE_MAX_BYTES if is_json else 0, error)

    def closed(self, response: httpx.Response, counter: StreamCounter) -> None:
        """The response stream closed: one ``completed`` line with the provider figures."""
        reason = "response_too_large" if counter.overflowed else None
        self.complete(response.status_code, counter, reason)

    def settle(self, response: httpx.Response, counter: StreamCounter) -> None:
        """An inner transport returned a body already read: count it and complete now."""
        try:
            counter.add(response.content)
        finally:
            self.closed(response, counter)

    def complete(
        self, status: int | None, counter: StreamCounter | None, reason: str | None
    ) -> None:
        """Write the ``completed`` line; on a failed send ``status_code`` is null."""
        if self._done:
            return
        self._done = True
        ticket, url = self._ticket, self._request.url
        tokens_in, tokens_out = _provider_usage(counter.body if counter else None)
        bytes_in = counter.bytes_in if counter else 0
        latency_ms = round((clock.monotonic() - ticket.started_monotonic) * 1000)
        line: dict[str, Any] = dict.fromkeys(LINE_KEYS) | self._opts.fields()
        line |= {"egress_id": ticket.egress_id, "decision": "completed", "reason": reason}
        line |= {"destination": url.host.lower(), "method": self._request.method}
        line |= {"path": url.path, "status_code": status, "bytes_in": bytes_in}
        line |= {"latency_ms": latency_ms, "tokens_out": tokens_out}
        line["tokens_in"] = ticket.tokens_in if tokens_in is None else tokens_in
        self._guard._write_completed(line)
        _log.info(
            "egress.call.completed",
            egress_id=ticket.egress_id,
            status_code=status,
            latency_ms=latency_ms,
            bytes_in=bytes_in,
        )
        # T08-05: herness_egress_bytes_total{direction="in"} += bytes_in
        # T08-05: herness_egress_tokens_total{direction="out", destination} += tokens_out
        # T08-05: herness_egress_latency_seconds.observe(latency_ms / 1000)


class GuardedTransport(httpx.BaseTransport):
    """Run the guard's check before the inner transport; log completion (U10-54)."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        guard: EgressGuard,
        purpose: Purpose,
        payload_class: PayloadClass,
        run_id: str | None,
        task_id: str | None,
    ) -> None:
        self._inner, self._guard = inner, guard
        self._opts = TransportOpts(purpose, payload_class, run_id, task_id)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Admit ``request`` (``EgressBlocked`` on refusal), send it, count the response."""
        call = _Call(self._guard, request, self._opts, self._guard._admit(request, self._opts))
        try:
            response = self._inner.handle_request(request)
        except Exception as exc:
            call.complete(None, None, type(exc).__name__)
            raise
        counter = call.counter(response)
        if response.is_closed:  # e.g. a mock transport: nothing left to stream
            call.settle(response, counter)
            return response
        stream = cast(httpx.SyncByteStream, response.stream)
        response.stream = CountingStream(stream, counter, partial(call.closed, response))
        return response

    def close(self) -> None:
        """Close the inner transport."""
        self._inner.close()


class AsyncGuardedTransport(httpx.AsyncBaseTransport):
    """Async twin of ``GuardedTransport``; the check and log writes run in a thread (U10-54)."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        guard: EgressGuard,
        purpose: Purpose,
        payload_class: PayloadClass,
        run_id: str | None,
        task_id: str | None,
    ) -> None:
        self._inner, self._guard = inner, guard
        self._opts = TransportOpts(purpose, payload_class, run_id, task_id)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Admit ``request`` (``EgressBlocked`` on refusal), send it, count the response."""
        ticket = await asyncio.to_thread(self._guard._admit, request, self._opts)
        call = _Call(self._guard, request, self._opts, ticket)
        try:
            response = await self._inner.handle_async_request(request)
        except Exception as exc:
            await asyncio.to_thread(call.complete, None, None, type(exc).__name__)
            raise
        counter = call.counter(response)
        if response.is_closed:  # e.g. a mock transport: nothing left to stream
            await asyncio.to_thread(call.settle, response, counter)
            return response

        async def closed(counter: StreamCounter) -> None:
            await asyncio.to_thread(call.closed, response, counter)

        stream = cast(httpx.AsyncByteStream, response.stream)
        response.stream = AsyncCountingStream(stream, counter, closed)
        return response

    async def aclose(self) -> None:
        """Close the inner transport."""
        await self._inner.aclose()
