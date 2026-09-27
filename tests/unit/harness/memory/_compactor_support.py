"""Shared fakes and builders for the ``ContextCompactor`` tests (T07-14).

``InMemoryOps`` is the ``CompactorOps`` fake (UT07-62); model calls go through the T11-23
``FakeLLMClient`` over an in-code ``ScriptBook``. The ``ToolContext`` is built with
``model_construct``: the compactor reads only its ids, role, ledger and tracer.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from pydantic import JsonValue
from tests.support.fake_llm import FakeLLMClient
from tests.support.harness_fakes import FakeLedger, RecordingTracer

from herness.core.errors import StoreBusy
from herness.core.resilience import bind_ops_backend
from herness.core.types import (
    LLMRequest,
    LLMResponse,
    LoopLimits,
    LoopState,
    Message,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolContext,
    ToolResultPart,
)
from herness.eval.scripted import LLMScript, ScriptBook, ScriptFault, ScriptTurn
from herness.harness.llm import tokens as llm_tokens
from herness.harness.llm.settings import ClientConfig
from herness.harness.memory.compactor import ContextCompactor
from herness.harness.memory.settings import CompactionConfig
from herness.harness.memory.tokens import TokenCounter

TASK_ID = "task_1"
BUILD_ID = "20260925-101500-ABCDEF"
_PRICE = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}
_LIMITS = LoopLimits(context_budget_tokens=100_000, soft_tokens=50_000, hard_tokens=80_000)
VALID_NOTES: dict[str, Any] = {
    "progress": "checked volumes",
    "hypotheses": [],
    "dead_ends": [],
    "next_steps": ["look at teams"],
}


def qid(seed: int) -> str:
    return f"q_{seed:016x}"


def profile(kind: str = "local", *, window: int = 32_768, timeout_s: float = 30.0) -> ClientConfig:
    fields: dict[str, Any] = {
        "name": "local-30b",
        "kind": "openai_compat",
        "base_url": "http://localhost:8000/v1",
        "model": "m",
        "context_window": window,
        "max_output_tokens": 1_000,
        "tokenizer": "estimate",
        "max_concurrency": 4,
        "price_per_mtok": _PRICE,
        "timeout_s": timeout_s,
    }
    if kind == "anthropic":
        fields |= {
            "name": "claude",
            "kind": "anthropic",
            "base_url": None,
            "tokenizer": "anthropic",
            "thinking_mode": "adaptive_always",
            "price_per_mtok": {"input": "3", "output": "15", "cache_read": "0", "cache_write": "0"},
        }
    return ClientConfig.model_validate(fields)


def make_ctx(ledger: FakeLedger | None = None) -> ToolContext:
    return ToolContext.model_construct(
        run_id="run_1",
        task_id=TASK_ID,
        build_id=BUILD_ID,
        role="analyst",
        ledger=ledger or FakeLedger(),
        tracer=RecordingTracer("run_1", TASK_ID),
    )


class InMemoryOps:
    """``CompactorOps`` over a dict; ``busy`` makes ``save_scratchpad`` raise ``StoreBusy``."""

    def __init__(self) -> None:
        self.saved: dict[str, dict[str, JsonValue]] = {}
        self.saves = 0
        self.busy = False

    def get_task_scratchpad(self, task_id: str) -> str | None:
        value = self.saved.get(task_id)
        return None if value is None else json.dumps(value)

    def save_scratchpad(self, task_id: str, value: dict[str, JsonValue]) -> None:
        if self.busy:
            msg = "database is locked"
            raise StoreBusy(msg)
        self.saves += 1
        self.saved[task_id] = json.loads(json.dumps(value))


class FakeBackend:
    """Minimal resilience backend: ``complete_validated`` records a repair event."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def insert_event(self, row: object) -> None:
        self.events.append(row)

    def insert_metric_samples(self, rows: Sequence[object]) -> int:
        return len(rows)

    def write_open(self) -> bool:
        return False


def bind_backend() -> FakeBackend:
    backend = FakeBackend()
    bind_ops_backend(backend)  # type: ignore[arg-type]
    return backend


def book(*turns: dict[str, Any], faults: Sequence[ScriptFault] = ()) -> ScriptBook:
    script = LLMScript(
        turns=[ScriptTurn(final=turn) for turn in turns], faults=list(faults), source="t#0"
    )
    return ScriptBook([script])


def notes_llm(*outputs: dict[str, Any]) -> FakeLLMClient:
    """A fake whose replies are the given JSON objects, in order."""
    return FakeLLMClient(book(*({"output": out} for out in outputs)))


class RefusingLLM(FakeLLMClient):
    """The fake's reply with ``stop_reason="refusal"``."""

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        resp = await super().acomplete(req)
        return resp.model_copy(update={"stop_reason": "refusal", "refusal_category": "cyber"})


class SlowLLM(FakeLLMClient):
    """The fake, answering only after ``delay_s`` (a hung summarizer server)."""

    delay_s = 5.0

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        await asyncio.sleep(self.delay_s)
        return await super().acomplete(req)


def call_group(
    call_id: str, seed: int, cells: Sequence[int] = (5, 7), *, say: str | None = None
) -> list[Message]:
    """One tool group: run_sql, a result table with ``qid(seed)``, optional assistant text."""
    rows = "\n".join(f"r{i} | {v}" for i, v in enumerate(cells))
    table = f"query_id={qid(seed)} rows={len(cells)}\nk | v\nVARCHAR | BIGINT\n{rows}"
    call = ToolCall(id=call_id, name="run_sql", arguments={"sql": f"SELECT {seed}"})
    out = [
        Message(role="assistant", parts=[ToolCallPart(call=call)]),
        Message(role="tool", parts=[ToolResultPart(tool_call_id=call_id, content=table)]),
    ]
    if say is not None:
        out.append(Message(role="assistant", parts=[TextPart(text=say)]))
    return out


def history(n: int, *, task: str = "find the drivers") -> list[Message]:
    msgs = [Message(role="user", parts=[TextPart(text=task)])]
    for i in range(n):
        msgs += call_group(f"c{i}", i + 1, say=f"saw {5 + i} rows")
    return msgs


def state_of(messages: Sequence[Message], query_ids: Sequence[str] = ()) -> LoopState:
    state = LoopState.fresh(messages[0], limits=_LIMITS, estimator=llm_tokens.estimate_tokens)
    state.messages = list(messages)
    state.query_ids = list(query_ids)
    return state


def compactor(  # noqa: PLR0913 - test builder
    *,
    kind: str = "local",
    client: Any = None,
    ops: InMemoryOps | None = None,
    ctx: ToolContext | None = None,
    estimate: Callable[..., int] | None = None,
    cfg: CompactionConfig | None = None,
    window: int = 32_768,
    timeout_s: float = 30.0,
) -> ContextCompactor:
    prof = profile(kind, window=window, timeout_s=timeout_s)
    est = estimate or llm_tokens.estimate_tokens
    # Offline: the "exact" Anthropic count is the same estimate, never a network call.
    counter = TokenCounter(prof, estimate=est, exact=lambda _c, ms, _t, _s: (est(ms), False))
    return ContextCompactor(
        prof,
        ctx=ctx or make_ctx(),
        cfg=cfg or CompactionConfig(),
        counter=counter,
        client=client,
        allowed=[],
        ops=ops or InMemoryOps(),
    )


def all_text(messages: Sequence[Message]) -> str:
    out: list[str] = []
    for part in (p for m in messages for p in m.parts):
        if isinstance(part, TextPart):
            out.append(part.text)
        elif isinstance(part, ToolResultPart):
            out.append(part.content)
        elif isinstance(part, ToolCallPart):
            out.append(json.dumps(part.call.arguments, sort_keys=True, separators=(",", ":")))
    return "\n".join(out)


ZERO = Decimal(0)
