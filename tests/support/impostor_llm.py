"""A `FakeLLMServer` that crafts its answers (T05-27: ST05-20, IT05-11).

`TamperingLLMServer` serves the scripted turns of `FakeLLMServer` (U11-45) and then:

* passes every non-stream completion through `tamper` (the impostor of TH05-20: a 5 MB text,
  another model name, tool arguments that are not a JSON object);
* serves a stream whose text is `stream_text` (when set) in 1 MB deltas;
* turns a scripted `http_500` fault on a stream request into a 200 stream that sends the first
  deltas of `partial_text` and then the server's error event with code 500, as vLLM does when
  generation fails mid-stream (IT05-11, flow F05-12).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

from tests.support.fake_llm import FakeLLMServer
from tests.support.stub_http import StubRequest, StubResponse

from herness.eval.scripted import ScriptBook

__all__ = ["TamperingLLMServer", "sse"]

type Tamper = Callable[[dict[str, Any]], dict[str, Any]]

_SSE_HEADERS: Final = {"Content-Type": "text/event-stream"}
_MB: Final = 1_000_000
_DELTA: Final = 16


def sse(payload: object) -> bytes:
    """One server-sent event line pair."""
    return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"


def _chunk(content: str | None = None, finish: str | None = None) -> dict[str, Any]:
    delta = {} if content is None else {"role": "assistant", "content": content}
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    return {"id": "chatcmpl-impostor", "object": "chat.completion.chunk", "created": 0,
            "model": "scripted", "choices": [choice]}  # fmt: skip


def _wants_stream(raw: bytes) -> bool:
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("stream") is True


class TamperingLLMServer(FakeLLMServer):
    """`FakeLLMServer` with crafted answers (see the module docstring)."""

    def __init__(
        self,
        scripts: Path | ScriptBook,
        *,
        tamper: Tamper | None = None,
        stream_text: str | None = None,
        partial_text: str = "",
    ) -> None:
        super().__init__(scripts, service_name=None)
        self.tamper = tamper
        self.stream_text = stream_text
        self.partial_text = partial_text
        self.cut_streams = 0

    def respond(self, request: StubRequest) -> StubResponse:
        """The scripted answer, crafted as configured."""
        is_chat = request.method == "POST" and request.path == "/v1/chat/completions"
        streamed = is_chat and _wants_stream(request.body)
        if streamed and self.stream_text is not None:
            return StubResponse(200, headers=dict(_SSE_HEADERS), chunks=self._big_stream())
        response = super().respond(request)
        if streamed and response.status == 500 and response.chunks is None:
            self.cut_streams += 1
            return StubResponse(200, headers=dict(_SSE_HEADERS), chunks=self._cut_stream())
        if is_chat and self.tamper is not None and response.status == 200 and not streamed:
            body = self.tamper(json.loads(response.body))
            return StubResponse.json(200, body)
        return response

    def _big_stream(self) -> Iterator[bytes]:
        text = self.stream_text or ""
        for start in range(0, len(text), _MB):
            yield sse(_chunk(text[start : start + _MB]))
        yield sse(_chunk(finish="stop"))
        yield b"data: [DONE]\n\n"

    def _cut_stream(self) -> Iterator[bytes]:
        text = self.partial_text
        for start in range(0, len(text), _DELTA):
            yield sse(_chunk(text[start : start + _DELTA]))
        error = {"message": "scripted fault http_500", "type": "server_error", "code": 500}
        yield sse({"error": error})
