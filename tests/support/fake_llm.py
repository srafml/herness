"""Fake LLM drivers over the scripted engine (R-65; U11-42, U11-43, U11-45).

``FakeLLMClient`` is the in-process fake; ``respx_router`` replays scripts through the real
OpenAI-compatible and Anthropic adapters; ``FakeLLMServer`` is the OpenAI-compatible loopback
HTTP fake for multi-process tests. All three share one ``ScriptBook`` matcher and
``render_turn`` (the wire half lives in the private sibling ``_fake_llm_wire``). Scripts live
under ``tests/fixtures/llm_scripts/`` (R-65).

``respx_router`` keeps its R-65 name but is built on ``httpx2.MockTransport`` (the
``egress_mock.MockNet`` pattern), not ``respx``: the SDKs and the egress clients send
through ``httpx2``, which ``respx`` cannot intercept (spec note, T11-23).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import httpx2
import pytest
from tests.support._fake_llm_wire import Wire, answer, error_response, json_body, stream_chunks
from tests.support.egress_mock import MockNet
from tests.support.stub_http import StubHTTPServer, StubRequest, StubResponse

from herness.core.registry import register
from herness.eval.scripted import ScriptBook, load_scripts
from herness.eval.scripted_client import DedupKeyResolver, ScriptedLLMClient

__all__ = ["FakeLLMClient", "FakeLLMServer", "ScriptRouter", "fake_llm_registered", "respx_router"]

_MODELS: Final = {"object": "list", "data": [{"id": "scripted", "object": "model"}]}


def _book(scripts: Path | ScriptBook) -> ScriptBook:
    return scripts if isinstance(scripts, ScriptBook) else load_scripts(scripts)


class FakeLLMClient(ScriptedLLMClient):
    """In-process scripted client (U11-42): loads scripts when given a path."""

    def __init__(
        self, scripts: Path | ScriptBook, *, dedup_key_resolver: DedupKeyResolver | None = None
    ) -> None:
        super().__init__(_book(scripts), name="fake", dedup_key_resolver=dedup_key_resolver)

    @property
    def book(self) -> ScriptBook:
        """The script book (call counters for assertions)."""
        return self._book


@pytest.fixture
def fake_llm_registered() -> type[FakeLLMClient]:
    """Register ``FakeLLMClient`` as ``llm_client:fake``; the autouse reset removes it.

    Kind ``llm_client``: the tree's ``RegistryKind`` has no ``llm`` (spec text, T11-23 note).
    """
    return register("llm_client", "fake")(FakeLLMClient)


# --- U11-43 respx_router ----------------------------------------------------------------


def _endpoint(url: httpx2.URL) -> tuple[str, str, int, str]:
    """``(scheme, host, port, path)``; the query is ignored, default ports made explicit."""
    port = url.port or (443 if url.scheme == "https" else 80)
    return url.scheme, url.host, port, url.path


class ScriptRouter(MockNet):
    """Answers the two chat endpoints from a script book; other hosts use ``MockNet`` routes.

    ``install(monkeypatch)`` (or ``with router:``) swaps the ``httpx2`` pool transports, so
    the guarded and loopback transports run for real above it and no socket opens.
    """

    def __init__(self, book: ScriptBook, openai_url: str, anthropic_url: str) -> None:
        super().__init__()
        self.book = book
        self.endpoints: dict[tuple[str, str, int, str], Wire] = {
            _endpoint(httpx2.URL(openai_url)): "openai",
            _endpoint(httpx2.URL(anthropic_url)): "anthropic",
        }
        self.requests: list[httpx2.Request] = []
        self._patch: pytest.MonkeyPatch | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Self:
        """``MockNet.install``, typed to return this router."""
        super().install(monkeypatch)
        return self

    def _wire(self, request: httpx2.Request) -> Wire | None:
        return self.endpoints.get(_endpoint(request.url)) if request.method == "POST" else None

    def _reply(self, request: httpx2.Request, wire: Wire) -> httpx2.Response:
        self.requests.append(request)
        headers = {k.lower(): v for k, v in request.headers.items()}
        stub = StubRequest("POST", request.url.path, headers, request.content)
        served, response = answer(self.book, stub, wire)
        if served.fault == "hang":
            msg = "scripted fault hang"
            raise httpx2.ReadTimeout(msg, request=request)
        if served.fault == "disconnect":
            msg = "scripted fault disconnect"
            raise httpx2.RemoteProtocolError(msg, request=request)
        return httpx2.Response(response.status, headers=response.headers, content=response.body)

    def _sync(self, request: httpx2.Request) -> httpx2.Response:
        wire = self._wire(request)
        if wire is None:
            return super()._sync(request)
        request.read()
        return self._reply(request, wire)

    async def _async(self, request: httpx2.Request) -> httpx2.Response:  # type: ignore[override]
        wire = self._wire(request)
        if wire is None:
            return super()._async(request)
        await request.aread()
        return self._reply(request, wire)

    def transport(self) -> httpx2.MockTransport:
        """A mock transport over this router, for a test's own ``httpx2`` client."""
        return httpx2.MockTransport(self._sync)

    def __enter__(self) -> Self:
        self._patch = pytest.MonkeyPatch()
        self.install(self._patch)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._patch is not None:
            self._patch.undo()
            self._patch = None


def respx_router(
    scripts: Path | ScriptBook,
    *,
    openai_base_url: str = "http://127.0.0.1:8000/v1",
    anthropic_base_url: str = "https://api.anthropic.com",
) -> ScriptRouter:
    """Route ``POST {openai}/chat/completions`` and ``POST {anthropic}/v1/messages`` (U11-43).

    Returns a ``ScriptRouter`` (``httpx2.MockTransport`` based) in place of the spec's
    ``respx.MockRouter`` (T11-23 spec note).
    """
    openai_url = openai_base_url.rstrip("/") + "/chat/completions"
    anthropic_url = anthropic_base_url.rstrip("/") + "/v1/messages"
    return ScriptRouter(_book(scripts), openai_url, anthropic_url)


# --- U11-45 FakeLLMServer ---------------------------------------------------------------


class FakeLLMServer(StubHTTPServer):
    """OpenAI-compatible loopback HTTP fake (U11-45); counters live in the test process."""

    def __init__(
        self,
        scripts: Path | ScriptBook,
        port: int = 0,
        *,
        service_name: str | None = "vllm-reasoning",
        hang_s: float = 30.0,
    ) -> None:
        self.book = _book(scripts)
        self.hang_s = hang_s
        super().__init__(port=port, service_name=service_name)

    def respond(self, request: StubRequest) -> StubResponse:
        """``GET /v1/models`` and ``POST /v1/chat/completions`` (stream or not)."""
        if request.method == "GET" and request.path == "/v1/models":
            return StubResponse.json(200, _MODELS)
        if request.method != "POST" or request.path != "/v1/chat/completions":
            return super().respond(request)
        served, response = answer(self.book, request, "openai")
        if served.fault == "hang":
            self.pause(self.hang_s)
            return error_response(500, "scripted fault hang", "openai")
        if served.fault == "disconnect":
            return StubResponse(0, disconnect=True)
        if response.status != 200 or not json_body(request.body).get("stream"):
            return response
        chunks = stream_chunks(json.loads(response.body))
        return StubResponse(200, headers={"Content-Type": "text/event-stream"}, chunks=chunks)
