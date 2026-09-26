"""Canned responses for the ``httpx2`` egress clients (``respx`` patches only ``httpx``).

``MockNet.install`` swaps ``httpx2.HTTPTransport`` / ``AsyncHTTPTransport`` for
``httpx2.MockTransport`` over its routes, so the guarded and loopback transports run for real
above it and no socket is opened. Bodies are streamed (not pre-read), like a real pool.
"""

from __future__ import annotations

import json as _json
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest


@dataclass
class Route:
    """One canned answer: matched on host, and on method and path when given."""

    host: str
    method: str | None = None
    path: str | None = None
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    chunks: Iterable[bytes] = ()
    error: BaseException | None = None
    calls: list[httpx2.Request] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)


class MockNet:
    """Routes for ``httpx2`` requests; an unmatched request is a ``ConnectError``."""

    def __init__(self) -> None:
        self.routes: list[Route] = []

    def route(  # noqa: PLR0913 - a test helper mirroring respx
        self,
        host: str,
        path: str | None = None,
        method: str | None = None,
        *,
        status: int = 200,
        json: Any = None,
        content: bytes | Iterable[bytes] = b"",
        headers: dict[str, str] | None = None,
        error: BaseException | None = None,
    ) -> Route:
        heads = dict(headers or {})
        if json is not None:
            content = _json.dumps(json).encode()
            heads.setdefault("content-type", "application/json")
        chunks = [content] if isinstance(content, bytes) else content
        found = Route(host, method, path, status, heads, chunks, error)
        self.routes.append(found)
        return found

    def _match(self, request: httpx2.Request) -> Route:
        for found in self.routes:
            method_ok = found.method in {None, request.method}
            if (
                found.host == request.url.host
                and method_ok
                and found.path in {None, request.url.path}
            ):
                found.calls.append(request)
                if found.error is not None:
                    raise found.error
                return found
        msg = f"no route for {request.url.host}"
        raise httpx2.ConnectError(msg, request=request)

    def _sync(self, request: httpx2.Request) -> httpx2.Response:
        found = self._match(request)
        chunks: Iterator[bytes] = iter(found.chunks)
        return httpx2.Response(found.status, headers=found.headers, content=chunks)

    def _async(self, request: httpx2.Request) -> httpx2.Response:
        found = self._match(request)

        async def chunks() -> AsyncIterator[bytes]:
            for chunk in found.chunks:
                yield chunk

        return httpx2.Response(found.status, headers=found.headers, content=chunks())

    def install(self, monkeypatch: pytest.MonkeyPatch) -> MockNet:
        """Make every ``httpx2`` pool transport built from now on answer from these routes."""
        monkeypatch.setattr(
            httpx2, "HTTPTransport", lambda *_a, **_k: httpx2.MockTransport(self._sync)
        )
        monkeypatch.setattr(
            httpx2, "AsyncHTTPTransport", lambda *_a, **_k: httpx2.MockTransport(self._async)
        )
        return self
