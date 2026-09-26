"""Chat constants and pure helpers (impl 06 U06-126, U06-130 … U06-133, design 06 §5.13).

Used by `ChatService`: mode banners and tool sets per mode, the tool wrapper that streams
`ToolEvent`s and `EvidenceEvent`s, the review-intent check for service-initiated escalation,
trimming of sentences whose numbers failed verification, and chunking of the answer text.
"""

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Final, Literal, cast

from pydantic import JsonValue

from herness.core.numbers import parse_markers
from herness.core.types import (
    AsyncTool,
    ChatAnswer,
    ChatEvent,
    ChatMode,
    EvidenceEvent,
    NumberRef,
    Tool,
    ToolContext,
    ToolEvent,
    ToolResult,
    VerificationResult,
)

type ReviewKind = Literal["funding_review", "org_review"]

MODE_MESSAGES: Final[Mapping[ChatMode, str]] = MappingProxyType(
    {
        "live": "",
        "small_model": (
            "Outside chat hours: answering with a reduced model; answers may be less thorough."
        ),
        "defer": (
            "The GPU is running scheduled work. Your question is queued and the answer will"
            " appear here when a slot is free."
        ),
        "cloud": "Answered by an off-network model under the approved data policy.",
    }
)

_FULL_TOOLS: Final[tuple[str, ...]] = (
    "list_tables", "describe_table", "run_sql", "get_metric", "get_scores", "get_cluster",
    "get_record", "semantic_search", "recall_memory", "propose_memory", "list_findings",
    "escalate",
)  # fmt: skip
# Tools that return ticket text; never offered to an off-network model (design 06 §5.13).
_TEXT_TOOLS: Final[frozenset[str]] = frozenset({"get_record", "get_cluster", "semantic_search"})

CHAT_TOOLS: Final[Mapping[ChatMode, tuple[str, ...]]] = MappingProxyType(
    {
        "live": _FULL_TOOLS,
        "small_model": _FULL_TOOLS,
        "cloud": tuple(name for name in _FULL_TOOLS if name not in _TEXT_TOOLS),
        "defer": (),
    }
)

EGRESS_NOTICE: Final = "Answered by the local reduced model; the off-network request was blocked."

NO_VERIFIED_ANSWER: Final = "No verified answer could be produced."

_FUND_QUESTION_RE: Final = re.compile(r"\bwhat should we fund\b", re.IGNORECASE)
_IMPROVE_QUESTION_RE: Final = re.compile(r"\bwhich teams?\b.*\bimprove\b", re.IGNORECASE)
_RANK_RE: Final = re.compile(r"\b(rank|ranking|prioriti[sz]e|top\s+\w+)\b", re.IGNORECASE)
_PLURAL_RE: Final = re.compile(r"\b(teams|services|epics|initiatives|candidates)\b", re.IGNORECASE)
_FUNDING_RE: Final = re.compile(r"fund|invest|epic|initiative|candidate", re.IGNORECASE)
_MIN_RANKED_ENTITIES: Final = 3
_SENTENCE_SPLIT_RE: Final = re.compile(r"(?<=[.!?])\s+")


class ObservedTool:
    """Wraps a tool so each call emits one `ToolEvent` and then new `EvidenceEvent`s (U06-130).

    A query id is emitted as evidence at most once per turn: `ChatService` passes one `seen`
    set to every wrapper of the turn (without it, the set is private to this wrapper). Sync
    tools run in a worker thread and an awaitable they return is awaited. A raised error emits
    `ToolEvent(ok=False)` and re-raises.
    """

    def __init__(
        self,
        inner: Tool | AsyncTool,
        emit: Callable[[ChatEvent], None],
        *,
        seen: set[str] | None = None,
    ) -> None:
        self._inner = inner
        self._emit = emit
        self._seen: set[str] = set() if seen is None else seen

    @property
    def name(self) -> str:
        """The wrapped tool's name."""
        return self._inner.name

    @property
    def description(self) -> str:
        """The wrapped tool's description."""
        return self._inner.description

    @property
    def input_schema(self) -> dict[str, JsonValue]:
        """The wrapped tool's input schema."""
        return self._inner.input_schema

    async def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        """Run the wrapped tool and emit its events in call order."""
        try:
            result = await self._invoke(ctx, kwargs)
        except Exception:
            self._emit(ToolEvent(name=self.name, query_id=None, ok=False))
            raise
        first = result.query_ids[0] if result.query_ids else None
        self._emit(ToolEvent(name=self.name, query_id=first, ok=result.ok))
        for query_id in result.query_ids:
            if query_id not in self._seen:
                self._seen.add(query_id)
                self._emit(EvidenceEvent(query_id=query_id))
        return result

    async def _invoke(self, ctx: ToolContext, kwargs: dict[str, JsonValue]) -> ToolResult:
        call = cast("Callable[..., object]", self._inner)
        if inspect.iscoroutinefunction(self._inner.__call__):
            result = await cast("Awaitable[object]", call(ctx, **kwargs))
        else:
            result = await asyncio.to_thread(call, ctx, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        if not isinstance(result, ToolResult):
            msg = "tool returned a value that is not a ToolResult"
            raise TypeError(msg)
        return result


def has_review_intent(
    question: str, entities: Mapping[str, list[str]]
) -> tuple[bool, ReviewKind | None]:
    """Decide service-initiated escalation and its pipeline (U06-131, design 06 §5.13).

    `entities` maps entity type to ids as `detect_entities` returns it; candidates are under
    the `candidate` key. Pure.
    """
    named = sum(len(ids) for ids in entities.values())
    ranked = _RANK_RE.search(question) is not None and (
        named >= _MIN_RANKED_ENTITIES or _PLURAL_RE.search(question) is not None
    )
    intent = (
        _FUND_QUESTION_RE.search(question) is not None
        or _IMPROVE_QUESTION_RE.search(question) is not None
        or ranked
    )
    if not intent:
        return False, None
    if _FUNDING_RE.search(question) is not None or entities.get("candidate"):
        return True, "funding_review"
    return True, "org_review"


def _sentences(text: str) -> list[tuple[int, int, str]]:
    """Offsets of the sentences of `text` split at `(?<=[.!?])\\s+`, each with the separator
    that follows it (empty for the last)."""
    spans: list[tuple[int, int, str]] = []
    start = 0
    for sep in _SENTENCE_SPLIT_RE.finditer(text):
        spans.append((start, sep.start(), sep.group()))
        start = sep.end()
    spans.append((start, len(text), ""))
    return spans


def _join(parts: list[tuple[str, str, bool]]) -> str:
    """Join the kept `(sentence, separator_after, kept)` parts with their original separators.

    Between two kept sentences the separator with the most line breaks among those around the
    removed sentences is used, so paragraph breaks and list items survive a removal.
    """
    out: list[str] = []
    pending: str | None = None
    for sentence, sep, kept in parts:
        if kept:
            if pending is not None:
                out.append(pending)
            out.append(sentence)
            pending = sep
        elif pending is not None:
            pending = max(pending, sep, key=lambda s: s.count("\n"))
    return "".join(out)


def _failing_ids(v: VerificationResult) -> set[str]:
    failing = {c.number_id for item in v.items for c in item.checks if c.result != "match"}
    failing.update(marker for item in v.items for marker in item.unknown_markers)
    return failing


def trim_failing_claims(answer: ChatAnswer, v: VerificationResult) -> tuple[ChatAnswer, list[str]]:
    """Remove sentences with failing numbers after the repair turn (U06-132).

    A sentence goes when it cites a failing or unknown marker or overlaps an uncited span of
    `v` (offsets into `answer.text`). Returns the new answer and the removed sentences. Pure.
    """
    failing = _failing_ids(v)
    uncited = [(span.start, span.end) for item in v.items for span in item.uncited]
    parts: list[tuple[str, str, bool]] = []
    removed: list[str] = []
    for start, end, sep in _sentences(answer.text):
        sentence = answer.text[start:end]
        if not sentence:
            continue
        cites_failing = any(m.id in failing for m in parse_markers(sentence).markers)
        drop = cites_failing or any(s < end and start < e for s, e in uncited)
        if drop:
            removed.append(sentence)
        parts.append((sentence, sep, not drop))
    if not removed:
        return answer, []
    text = _join(parts)
    cited = {m.id for m in parse_markers(text).markers}
    numbers: list[NumberRef] = [n for n in answer.numbers if n.id in cited]
    dropped_queries = {n.query_id for n in answer.numbers} - {n.query_id for n in numbers}
    query_ids = [q for q in answer.query_ids if q not in dropped_queries]
    if not text:
        # Nothing verified is left, so no query supports the fallback text.
        text, query_ids = NO_VERIFIED_ANSWER, []
    update = {"text": text, "numbers": numbers, "query_ids": query_ids}
    return answer.model_copy(update=update), removed


def chunk_text(text: str, size: int = 64) -> list[str]:
    """Split `text` into chunks of at most `size` chars for token events (U06-133).

    Each chunk breaks after the last whitespace inside its window when there is one; the
    concatenation of the chunks equals `text`. Pure.
    """
    if size < 1:
        msg = "chunk size must be at least 1"
        raise ValueError(msg)
    chunks: list[str] = []
    start = 0
    while len(text) - start > size:
        window = text[start : start + size]
        cut = max((i for i, ch in enumerate(window) if ch.isspace()), default=-1) + 1
        end = start + (cut or size)
        chunks.append(text[start:end])
        start = end
    if start < len(text):
        chunks.append(text[start:])
    return chunks
