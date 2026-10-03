"""Chat session memory: load, save a turn, correction capture (impl 07 U07-93 … U07-95).

Design 07 §5.12. Chat rows come only from the spec 09 area functions (R-09); chat text reaches
the model escaped inside one ``<untrusted_data source="chat">`` (U07-44, R-20). Model output is
data: the summary is post-processed before it is stored, a classification only becomes a
proposal through ``propose`` (chat → pending, TH07-23). Logs carry ids, counts, rules only."""

# fmt: off
import asyncio  # noqa: I001 - compact import layout keeps the 330-line budget
import json
import re
from collections.abc import Callable, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol, cast

from pydantic import JsonValue

from herness.core.errors import (
    CircuitOpen, EgressBlocked, ModelRefused, ModelUnavailable, OutputValidationError,
    PolicyViolation, RateLimited,
)
from herness.core.logging import get_logger
from herness.core.numbers import ANY_MARKER_RE
from herness.core.redact import Redactor
from herness.core.resilience import TracerLike, complete_validated
from herness.core.types import (
    LLMRequest, LLMResponse, MemoryProposal, MemoryRunContext, Message, Provenance, RequestMeta,
    SystemBlock, TextPart,
)
from herness.harness.llm.base import LLMClient
from herness.harness.llm.settings import ClientConfig
from herness.harness.memory._compactor_llm import memory_prompt
from herness.harness.memory.policy import find_uncited_numerals
from herness.harness.memory.render import escape_content, wrap_untrusted
from herness.harness.memory.settings import ChatMemoryConfig
from herness.harness.memory.types import ChatTurn, MemoryNotFound, ProposeResult, SessionContext
from herness.harness.tracing import llm_call_fields
from herness.store.ops import chat
from herness.store.ops.chat import ChatMessageRow, ChatSessionRow
from herness.store.ops.memory import session_memory_ids
# fmt: on

__all__ = [
    "CLASSIFY_SCHEMA", "ChatDeps", "ChatModels", "Proposer", "capture_correction",
    "session_load", "session_save_turn",
]  # fmt: skip

SUMMARY_PROMPT: Final = "chat_summary.md"
CLASSIFY_PROMPT: Final = "correction_classify.md"
MODEL_ROLE: Final = "chat"
DEPTH: Final = "fast"
SUMMARY_MAX_TOKENS: Final = 400
CLASSIFY_MAX_TOKENS: Final = 300
CONTENT_MAX_CHARS: Final = 2000
MESSAGE_MAX_CHARS: Final = 4000  # per message in the summary request (LLM10)
HISTORY_ROWS: Final = 200
SESSION_ID_MAX: Final = 64  # U07-93 precondition
SUMMARY_MAX_CHARS: Final = 6000  # chat_session.summary invariant (set_chat_summary limit)
SYNC_TIMEOUT_S: Final = 120.0
NUMBER: Final = "[number]"
_ID: Final[dict[str, JsonValue]] = {"type": "string", "minLength": 1, "maxLength": 200}
_DATA_KEYS: Final = ("statement", "effective_date", "suggested_action", "entities")
CLASSIFY_SCHEMA: Final[dict[str, JsonValue]] = {
    "type": "object", "additionalProperties": False,
    "required": ["is_correction", "statement", "entities", "effective_date",
                 "suggested_action", "confidence"],
    "properties": {
        "is_correction": {"type": "boolean"},
        "statement": {"type": "string", "maxLength": 1000},
        "entities": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "additionalProperties": False, "required": ["type", "id"],
            "properties": {"type": _ID, "id": _ID}}},
        "effective_date": {"anyOf": [{"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                                     {"type": "null"}]},
        "suggested_action": {"enum": ["none", "weight_change", "mapping_suggestion"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}  # fmt: skip  # U07-95 step 2, bounds as JSON Schema keywords
_MODEL_ERRORS: Final = (ModelUnavailable, ModelRefused, OutputValidationError, RateLimited,
                        CircuitOpen, EgressBlocked, TimeoutError)  # fmt: skip
_log = get_logger("harness.memory")


class ChatModels(Protocol):
    """The ``LLMRegistry`` subset the chat calls use (T05-10)."""

    def model_for(self, model_role: str, depth: str) -> str: ...
    def client(self, name: str) -> LLMClient: ...
    def config(self, name: str) -> ClientConfig: ...


class Proposer(Protocol):
    """``MemoryWriter.propose`` (T07-08)."""

    def propose(
        self, item: MemoryProposal, run_ctx: MemoryRunContext | None = None, *,
        now: datetime | None = None,
    ) -> ProposeResult: ...  # fmt: skip


@dataclass(frozen=True, slots=True)
class ChatDeps:
    """Collaborators of the chat units; construction reads both prompts (``ConfigError``).

    ``allowed``: compiled ``reports.allowed_numeral_patterns``; ``tracer``: ``llm_call``s."""

    cfg: ChatMemoryConfig
    llms: ChatModels
    writer: Proposer
    redactor: Redactor
    allowed: Sequence[re.Pattern[str]] = ()
    tracer: TracerLike | None = None

    def __post_init__(self) -> None:
        memory_prompt(SUMMARY_PROMPT)
        memory_prompt(CLASSIFY_PROMPT)


class _Caller:
    """``complete_validated``'s client: profile timeout, ``llm_call`` trace, refusal error."""

    def __init__(self, client: LLMClient, timeout_s: float, tracer: TracerLike | None,
                 prompt_hash: str) -> None:  # fmt: skip
        self.name, self._client, self._timeout_s = client.name, client, timeout_s
        self._tracer, self._hash = tracer, prompt_hash

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        resp = await asyncio.wait_for(self._client.acomplete(req), self._timeout_s)
        if self._tracer is not None:
            fields, payload = llm_call_fields(req, resp, prompt_hash=self._hash, gate_wait_ms=0)
            meta = req.metadata
            self._tracer.emit("llm_call", role=meta.role, step=meta.step, payload=payload, **fields)
        if resp.stop_reason == "refusal":
            msg = "model refused the chat memory request"
            raise ModelRefused(msg, category=resp.refusal_category, client=self.name)
        return resp


async def _ask(
    deps: ChatDeps, prompt: str, body: str, schema: tuple[str, dict[str, JsonValue], int],
    meta: tuple[str, str],
) -> dict[str, JsonValue]:  # fmt: skip
    """One validated call (no repair); ``schema`` = (name, JSON Schema, max output tokens),
    ``meta`` = (run_id, request key); ``body`` is already escaped and wrapped."""
    name = deps.llms.model_for(MODEL_ROLE, DEPTH)
    client, profile = deps.llms.client(name), deps.llms.config(name)
    text, digest = memory_prompt(prompt)
    (schema_name, response_schema, max_tokens), (run_id, key) = schema, meta
    req = LLMRequest(
        client=name, system=[SystemBlock(text=text)],
        messages=[Message(role="user", parts=[TextPart(text=body)])],
        response_schema=response_schema, response_schema_name=schema_name,
        max_output_tokens=min(max_tokens, profile.max_output_tokens),
        temperature=0.0 if profile.supports.sampling_params else None,
        tools=[], timeout_s=profile.timeout_s,
        metadata=RequestMeta(run_id=run_id, task_id=None, role=MODEL_ROLE,
                             model_role=MODEL_ROLE, step=0, request_key=key[:200]),
    )  # fmt: skip
    caller = _Caller(client, profile.timeout_s, deps.tracer, digest)
    resp = await complete_validated(caller, req, max_repairs=0, tracer=deps.tracer)
    raw = resp.parsed if resp.parsed is not None else json.loads(resp.text)  # validated JSON
    return cast("dict[str, JsonValue]", raw)  # the schema requires an object


def _visible(rows: Sequence[ChatMessageRow]) -> list[ChatMessageRow]:
    """User rows and finished assistant rows (U07-93 postcondition), in input order."""
    return [r for r in rows if r["role"] == "user" or
            (r["role"] == "assistant" and r["status"] == "done")]  # fmt: skip


def _session(session_id: str) -> ChatSessionRow:
    session = chat.get_chat_session(session_id) if len(session_id) <= SESSION_ID_MAX else None
    if session is None:
        raise MemoryNotFound("session", session_id)  # noqa: EM101 - a kind, not a message
    return session


def session_load(session_id: str, *, deps: ChatDeps) -> SessionContext:
    """Summary, the last ``cfg.last_messages`` visible messages (oldest first), memory ids."""
    session = _session(session_id)
    rows = _visible(chat.list_chat_messages(session_id, limit=HISTORY_ROWS))
    turns = [
        ChatTurn(message_id=r["message_id"], role=cast("Literal['user', 'assistant']", r["role"]),
                 content=r["content"], created_at=r["created_at"], query_ids=r["query_ids"])
        for r in rows[-deps.cfg.last_messages :]
    ]  # fmt: skip
    return SessionContext(session_id=session_id, summary=session["summary"], messages=turns,
                          memory_ids=session_memory_ids(session_id))  # fmt: skip


async def capture_correction(
    session_id: str, message_id: str, user_ref: str, run_id: str, *, deps: ChatDeps,
    now: datetime | None = None,
) -> ProposeResult | None:  # fmt: skip
    """Classify one user message; propose a ``user_correction`` when it is one (U07-95).

    A message that is not a user row of ``session_id`` gives ``None`` without a model call;
    the author's ownership of the session is checked by ``propose`` (TH07-22)."""
    message = await asyncio.to_thread(chat.get_chat_message, message_id)
    if message is None or message["session_id"] != session_id or message["role"] != "user":
        return None
    body = wrap_untrusted("chat", message_id, escape_content(message["content"]))
    schema = ("correction_classification", CLASSIFY_SCHEMA, CLASSIFY_MAX_TOKENS)
    try:
        out = await _ask(deps, CLASSIFY_PROMPT, body, schema, (run_id, f"correction:{message_id}"))
    except _MODEL_ERRORS as exc:
        _log.warning("memory.correction.classify_failed", session_id=session_id,
                     message_id=message_id, reason=type(exc).__name__)  # fmt: skip
        return None
    confidence = cast("float", out["confidence"])
    if out["is_correction"] is not True or confidence < deps.cfg.correction_min_confidence:
        return None
    statement = cast("str", out["statement"])
    proposal = MemoryProposal(
        layer="semantic", kind="user_correction",
        content=(statement or message["content"])[:CONTENT_MAX_CHARS],
        data={key: out[key] for key in _DATA_KEYS}, confidence=confidence,
        provenance=Provenance(author_type="human", author_role=None, author_ref=user_ref,
                              run_id=run_id, task_id=None, session_id=session_id,
                              source_message_id=message_id, via="chat"),
    )  # fmt: skip
    try:
        result = await asyncio.to_thread(deps.writer.propose, proposal, None, now=now)
    except PolicyViolation as exc:
        rule = str(exc.details.get("rule", "unknown"))
        _log.info("memory.correction.rejected", session_id=session_id, rule=rule)
        return None
    _log.info("memory.correction.captured", session_id=session_id, message_id=message_id,
              memory_id=result.memory_id, status=result.status)  # fmt: skip
    return result


def _post_process(text: str, deps: ChatDeps) -> str:
    """Markers and uncited numerals → ``[number]``; redact; cut at the last whitespace."""
    text = ANY_MARKER_RE.sub(NUMBER, text)
    for hit in reversed(find_uncited_numerals(text, deps.allowed)):
        text = text[: hit.start] + NUMBER + text[hit.end :]
    text = text if (found := deps.redactor.redact(text)) is None else found.text
    limit = min(deps.cfg.summary_max_chars, SUMMARY_MAX_CHARS)
    if len(text) > limit:
        cut = text[: limit + 1]
        space = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
        text = cut[:space] if space > 0 else text[:limit]
    return text.strip()


def _summary_schema(max_chars: int) -> dict[str, JsonValue]:
    """``{summary: string ≤ max_chars}`` (U07-94 step 4 as a structured call)."""
    summary: dict[str, JsonValue] = {"type": "string", "minLength": 1, "maxLength": max_chars}
    return {"type": "object", "additionalProperties": False, "required": ["summary"],
            "properties": {"summary": summary}}  # fmt: skip


async def _refresh_summary(session: ChatSessionRow, turns: int, run_id: str,
                           deps: ChatDeps) -> None:  # fmt: skip
    """U07-94 steps 4-6: one summary call over the prior summary and the latest messages."""
    session_id = session["session_id"]
    rows = await asyncio.to_thread(chat.list_chat_messages, session_id, limit=HISTORY_ROWS)
    window = _visible(rows)[-2 * deps.cfg.summary_every_turns :]
    lines = [f"{r['role']}: {r['content'][:MESSAGE_MAX_CHARS]}" for r in window]
    text = f"PRIOR SUMMARY:\n{session['summary'] or 'none'}\n\nMESSAGES:\n" + "\n\n".join(lines)
    body = wrap_untrusted("chat", session_id, escape_content(text))
    schema = ("chat_summary", _summary_schema(deps.cfg.summary_max_chars), SUMMARY_MAX_TOKENS)
    try:
        out = await _ask(deps, SUMMARY_PROMPT, body, schema, (run_id, f"chat_summary:{turns}"))
    except _MODEL_ERRORS as exc:
        _log.warning("memory.session.summary_failed", session_id=session_id,
                     reason=type(exc).__name__)  # fmt: skip
        return
    summary = _post_process(cast("str", out["summary"]), deps)
    if not summary:
        _log.warning("memory.session.summary_failed", session_id=session_id, reason="empty")
        return
    through = window[-1]["message_id"]
    await asyncio.to_thread(chat.set_chat_summary, session_id, summary, through_message_id=through)
    _log.info("memory.session.summary_refreshed", session_id=session_id, turns=turns,
              chars=len(summary))  # fmt: skip


def _turn_message(rows: Sequence[ChatMessageRow], run_id: str) -> str | None:
    """The latest user row before the assistant row of ``run_id`` (U07-94 step 3)."""
    found = next((i for i, r in enumerate(rows)
                  if r["role"] == "assistant" and r["run_id"] == run_id), 0)  # fmt: skip
    users = [r["message_id"] for r in rows[:found] if r["role"] == "user"]
    return users[-1] if users else None


async def _save_turn(session_id: str, run_id: str, deps: ChatDeps,
                     now: datetime | None) -> str | None:  # fmt: skip
    """U07-94 steps 2-7 on the event loop; SQLite calls leave it (ENG §2.5)."""
    session = await asyncio.to_thread(_session, session_id)
    rows = await asyncio.to_thread(chat.list_chat_messages, session_id, limit=HISTORY_ROWS)
    message_id, result = _turn_message(rows, run_id), None
    if message_id is not None:
        result = await capture_correction(session_id, message_id, session["user_ref"], run_id,
                                          deps=deps, now=now)  # fmt: skip
    turns = await asyncio.to_thread(chat.count_user_turns, session_id)
    if turns > 0 and turns % deps.cfg.summary_every_turns == 0:
        await _refresh_summary(session, turns, run_id, deps)
    return None if result is None else result.memory_id


def _run[T](make: Callable[[], Coroutine[object, object, T]]) -> T:
    """``asyncio.run``; on a thread with a running loop, in a fresh thread (≤ 120 s)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make())
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat-memory")
    try:
        return pool.submit(lambda: asyncio.run(make())).result(timeout=SYNC_TIMEOUT_S)
    finally:
        pool.shutdown(wait=False)


def session_save_turn(session_id: str, run_id: str, *, deps: ChatDeps,
                      now: datetime | None = None) -> str | None:  # fmt: skip
    """Correction capture and summary refresh after a chat turn (U07-94; R-32).

    Returns the ``memory_id`` of the correction captured in this turn (the new item or the
    item it merged into), else ``None``. Model failures never raise; ``MemoryNotFound`` and
    ``StoreBusy`` do. Past the 120 s wait the work is abandoned with a WARNING.
    """
    try:
        return _run(lambda: _save_turn(session_id, run_id, deps, now))
    except TimeoutError:
        _log.warning("memory.session.save_timeout", session_id=session_id, run_id=run_id)
        return None
