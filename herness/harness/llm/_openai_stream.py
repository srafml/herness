"""Stream buffers of the OpenAI-compatible adapter's ``astream`` (impl 05 U05-26).

Private sibling of ``openai_compat`` (module-map row, T05-07): accumulates one stream's text,
reasoning and tool calls, enforcing the adapter bounds while the buffers grow (TH05-20, w14-s05
ruling), and rebuilds the completion ``acomplete`` would have received for the same content.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from openai.types.chat import ChatCompletion

from herness.core.errors import OutputValidationError
from herness.harness.llm.base import (
    MAX_RESPONSE_TEXT_CHARS,
    MAX_TOOL_CALLS,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
)

if TYPE_CHECKING:
    from openai import AsyncStream
    from openai.types.chat import ChatCompletionChunk
    from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall

__all__ = ["StreamBuffers", "next_chunk"]

_MAX_ID_CHARS: Final = 128


@dataclass(slots=True)
class _CallBuffer:
    id: str
    name: str
    parts: list[str] = field(default_factory=list)
    size: int = 0


@dataclass(slots=True)
class StreamBuffers:
    """The accumulated content of one stream, bounded while it grows (TH05-20, U05-26)."""

    client: str
    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    sizes: dict[str, int] = field(default_factory=lambda: {"text": 0, "reasoning": 0})
    calls: dict[int, _CallBuffer] = field(default_factory=dict)
    head: dict[str, object] = field(default_factory=dict)
    finish: object = None
    usage: object = None

    def _grow(self, buffer: str, piece: str) -> None:
        size = self.sizes[buffer] + len(piece)
        if size > MAX_RESPONSE_TEXT_CHARS:
            msg = "response text exceeds limit"
            raise OutputValidationError(msg, client=self.client)
        self.sizes[buffer] = size
        (self.text if buffer == "text" else self.reasoning).append(piece)

    def _call(self, frag: ChoiceDeltaToolCall) -> ToolCallDelta:
        index: object = frag.index  # built without validation: any JSON value may arrive
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise TypeError  # malformed
        if index not in self.calls:
            if index >= MAX_TOOL_CALLS:  # at most MAX_TOOL_CALLS distinct indices, no gaps grown
                msg = f"response has more than {MAX_TOOL_CALLS} tool calls"
                raise OutputValidationError(msg, client=self.client)
            name = frag.function.name if frag.function is not None else None
            if not isinstance(frag.id, str) or not isinstance(name, str):
                raise TypeError  # the first fragment names the call
            self.calls[index] = _CallBuffer(frag.id, name)
        call = self.calls[index]
        piece = (frag.function.arguments if frag.function is not None else None) or ""
        if not isinstance(piece, str):
            raise TypeError
        call.size += len(piece)
        if call.size > MAX_RESPONSE_TEXT_CHARS:
            msg = f"tool call {call.id[:_MAX_ID_CHARS]} arguments exceed limit"
            raise OutputValidationError(msg, client=self.client)
        call.parts.append(piece)
        return ToolCallDelta(call.id, call.name, piece)

    def add(self, chunk: ChatCompletionChunk) -> list[StreamEvent]:
        """Buffer one chunk (U05-26 step 2); the events to yield, in order."""
        if not self.head:
            self.head = {"id": chunk.id, "created": chunk.created, "model": chunk.model}
        if chunk.usage is not None:
            self.usage = chunk.usage
        if not chunk.choices:
            return []
        choice = chunk.choices[0]
        self.finish = choice.finish_reason or self.finish
        delta = choice.delta
        events: list[StreamEvent] = []
        if delta.content is not None and not isinstance(delta.content, str):
            raise TypeError
        if delta.content:
            self._grow("text", delta.content)
            events.append(TextDelta(delta.content))
        extra = delta.model_extra or {}
        reasoning = extra.get("reasoning") or extra.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            self._grow("reasoning", reasoning)
        events.extend(self._call(frag) for frag in delta.tool_calls or [])
        return events

    def completion(self) -> ChatCompletion:
        """The completion ``acomplete`` would have received for the same content (step 3)."""
        message: dict[str, object] = {"role": "assistant", "content": "".join(self.text)}
        if self.reasoning:
            message["reasoning"] = "".join(self.reasoning)
        if self.calls:
            message["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": "".join(c.parts)},
                }
                for _, c in sorted(self.calls.items())
            ]
        choice = {"index": 0, "finish_reason": self.finish, "message": message}
        body: dict[str, Any] = {"object": "chat.completion", "choices": [choice], **self.head}
        body["usage"] = self.usage
        return ChatCompletion.construct(**body)


async def next_chunk(
    stream: AsyncStream[ChatCompletionChunk], deadline: float
) -> ChatCompletionChunk | None:
    """The next chunk, or ``None`` at the end; ``TimeoutError`` once ``deadline`` passes."""
    if asyncio.get_running_loop().time() >= deadline:  # a buffered chunk would not wait
        raise TimeoutError
    async with asyncio.timeout_at(deadline):
        return await anext(stream, None)
