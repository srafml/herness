"""In-process scripted model client and registry wrapper (U11-40, U11-41; design 11 §5.2).

`ScriptedLLMClient` answers `LLMRequest`s from a validated `ScriptBook` (the T11-21 loader
enforces the size, script and turn caps), rendering each turn against the real tool
results with `render_turn`. It never reaches the network: it builds no HTTP client, and its
token counts go through `count_tokens` with a client config whose tokenizer is `estimate`,
the one path of `count_tokens` that calls neither the vLLM `/tokenize` endpoint nor the
Anthropic `count_tokens` API (R-17). `ScriptedRegistry` routes every client key to one
scripted client for `--mock-llm` runs without changing spec 05.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Final

from herness.core.errors import ModelUnavailable, OutputValidationError, RateLimited
from herness.core.ids import canonical_json
from herness.core.types import LLMRequest, LLMResponse, Message, RequestMeta, TextPart, Usage
from herness.eval.scripted import FaultKind, ScriptAction, ScriptBook, ScriptMismatch, ScriptTurn
from herness.eval.scripted_render import RenderedTurn, render_turn
from herness.harness.llm.base import Done, LLMClient, StreamEvent, TextDelta, ToolCallDelta
from herness.harness.llm.registry import LLMRegistry
from herness.harness.llm.settings import ClientConfig, RoleParams
from herness.harness.llm.tokens import count_tokens
from herness.store import ops

__all__ = ["DedupKeyResolver", "ScriptedLLMClient", "ScriptedRegistry", "ops_dedup_key_resolver"]

type DedupKeyResolver = Callable[[str | None], str | None]

_CHUNK: Final = 16
_JUDGE_PREFIX: Final = "judge:"
_MALFORMED_TEXT: Final = '{"truncated": '
_TEXT_FINALS: Final = frozenset({"text", "output"})
# Offline token counting: `tokenizer="estimate"` makes `count_tokens` return the estimate
# directly. The loopback base URL is never contacted; it only satisfies the config rules.
_ESTIMATE_CFG: Final = ClientConfig.model_validate(
    {
        "name": "scripted",
        "kind": "openai_compat",
        "base_url": "http://127.0.0.1:9/v1",
        "model": "scripted",
        "context_window": 1_000_000,
        "max_output_tokens": 65_536,
        "tokenizer": "estimate",
        "max_concurrency": 200,
        "price_per_mtok": {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"},
    }
)


def _judge_question(request_key: str) -> str | None:
    """`<qid>` of a judge request key `judge:<qid>:<hash>`, else None."""
    if not request_key.startswith(_JUDGE_PREFIX):
        return None
    body = request_key.removeprefix(_JUDGE_PREFIX)
    qid, sep, _hash = body.rpartition(":")
    return qid if sep and qid else None


def _prompt_hash(req: LLMRequest) -> str:
    """16 hex of the SHA-256 of the canonical system and messages (for mismatch reports)."""
    payload = {
        "system": [block.model_dump(mode="json") for block in req.system],
        "messages": [message.model_dump(mode="json") for message in req.messages],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]


def _is_tool_turn(turn: ScriptTurn | None) -> bool:
    if turn is None:
        return False
    if turn.tool_calls is not None:
        return True
    return next(iter(turn.final or {}), "text") not in _TEXT_FINALS


def _text_tokens(text: str) -> int:
    if not text:
        return 0
    message = Message(role="assistant", parts=[TextPart(text=text)])
    return count_tokens(_ESTIMATE_CFG, [message], [], [])[0]


class ScriptedLLMClient:
    """Scripted `LLMClient` + `StreamCapable`; thread-safe through the book's lock (U11-40)."""

    def __init__(
        self,
        book: ScriptBook,
        *,
        name: str = "fake",
        dedup_key_resolver: DedupKeyResolver | None = None,
    ) -> None:
        self.name = name
        self._book = book
        self._resolver = dedup_key_resolver

    def dedup_key(self, meta: RequestMeta) -> str:
        """The script key of a call (U11-40 step 2; DD11-02 default)."""
        explicit = getattr(meta, "dedup_key", None)  # the DD11-02 field, once it exists
        if isinstance(explicit, str):
            return explicit
        if meta.model_role == "eval_judge":
            return _judge_question(meta.request_key) or "*"
        if self._resolver is not None and meta.task_id:
            resolved = self._resolver(meta.task_id)
            if resolved is not None:
                return resolved
        return "chat" if meta.role == "chat" else "*"

    def complete(self, req: LLMRequest) -> LLMResponse:
        """Blocking `acomplete`; refused inside a running event loop (spec 05)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.acomplete(req))
        msg = "complete() called inside a running event loop; use acomplete()"
        raise RuntimeError(msg)

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        """Serve the next scripted turn or fault for the call."""
        meta = req.metadata
        key = self.dedup_key(meta)
        prompt_hash = _prompt_hash(req)
        action = self._book.next_action(meta.role, meta.model_role, key, prompt_hash=prompt_hash)
        if action.fault is not None:
            return self._fault(req, action, action.fault.kind)
        turn = action.script.turns[action.turn_index]
        try:
            rendered = render_turn(turn, req.messages, call_index=action.call_index)
        except ScriptMismatch as exc:
            raise ScriptMismatch(
                exc.message,
                role=meta.role,
                model_role=meta.model_role,
                dedup_key=key,
                call_index=action.call_index,
                prompt_hash=prompt_hash,
            ) from exc
        return self._response(req, rendered, action.call_index)

    async def astream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]:
        """16-character `TextDelta`s, one `ToolCallDelta` per call, then one `Done`."""
        response = await self.acomplete(req)
        text = response.text
        for start in range(0, len(text), _CHUNK):
            yield TextDelta(text[start : start + _CHUNK])
        for call in response.tool_calls:
            yield ToolCallDelta(call.id, call.name, canonical_json(call.arguments))
        yield Done(response)

    def _fault(self, req: LLMRequest, action: ScriptAction, kind: FaultKind) -> LLMResponse:
        if kind == "http_429":
            msg = "scripted rate limit"
            raise RateLimited(msg, retry_after=1.0, client=self.name)
        if kind != "malformed_json":  # http_500, hang, disconnect
            msg = f"scripted fault {kind}"
            raise ModelUnavailable(msg, client=self.name, fault=kind)
        turns = action.script.turns
        turn = turns[action.turn_index] if action.turn_index < len(turns) else None
        if _is_tool_turn(turn):
            msg = "scripted malformed tool arguments"
            raise OutputValidationError(msg, client=self.name)
        rendered = RenderedTurn([], _MALFORMED_TEXT, None, "end_turn")
        return self._response(req, rendered, action.call_index)

    def _response(self, req: LLMRequest, rendered: RenderedTurn, call_index: int) -> LLMResponse:
        input_tokens = count_tokens(_ESTIMATE_CFG, req.messages, [], req.system)[0]
        usage = Usage(input_tokens=input_tokens, output_tokens=_text_tokens(rendered.text))
        return LLMResponse(
            text=rendered.text,
            tool_calls=rendered.tool_calls,
            parsed=rendered.parsed,
            reasoning=[],
            stop_reason=rendered.stop_reason,
            raw_stop_reason=rendered.stop_reason,
            refusal_category=None,
            usage=usage,
            cost_usd=Decimal("0"),
            client=self.name,
            model="scripted",
            provider="openai_compat",
            latency_ms=0,
            request_id=f"fake-{call_index}",
            batch=False,
        )


class ScriptedRegistry:
    """`LLMRegistry` interface: every client key gets the scripted client (U11-41)."""

    def __init__(self, inner: LLMRegistry, client: ScriptedLLMClient) -> None:
        self._inner = inner
        self._client = client

    def client(self, name: str) -> LLMClient:
        """The scripted client, whatever `name` is."""
        del name
        return self._client

    def config(self, name: str) -> ClientConfig:
        """Delegates to the inner registry."""
        return self._inner.config(name)

    def model_for(self, model_role: str, depth: str) -> str:
        """Delegates to the inner registry."""
        return self._inner.model_for(model_role, depth)

    def chain_for(self, model_role: str, depth: str) -> list[str]:
        """Delegates to the inner registry."""
        return self._inner.chain_for(model_role, depth)

    def role_params(self, model_role: str) -> RoleParams | None:
        """Delegates to the inner registry (pure config lookup)."""
        return self._inner.role_params(model_role)


def ops_dedup_key_resolver() -> DedupKeyResolver:
    """task_id → the ops `task` row's `spec.dedup_key` (None when unknown) (U11-41).

    Reads through `herness.store.ops.get_task`, which uses the per-thread ops connection
    of the configured store (R-08, R-10), so the resolver is thread-safe.
    """

    def resolve(task_id: str | None) -> str | None:
        if task_id is None:
            return None
        row = ops.get_task(task_id)
        return None if row is None else row.spec.dedup_key

    return resolve
