"""Stand-ins for the agent loop tests (T05-23: UT05-103..113, IT05-*, ST05-08, BT05-01).

`ListClient` answers from a list of `LLMResponse`s (or raises the listed exceptions);
`FakeHooks` is a recording `LoopHooks` with switchable compaction, loop-signal policy and
checkpoint behaviour; `demo_role` builds a `RoleSpec` over a tmp prompt directory (the T05-19
test seam `_prompts_root`); `loop_ctx` builds a `ToolContext` with the T05-02 fakes. Spec 11's
`tiny_build` does not exist yet: the integration tests use `warehouse_tools_build` instead.
"""

from __future__ import annotations

import asyncio
import string
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue
from tests.support.dispatch_standin import FakeWarehouse
from tests.support.harness_fakes import FakeLedger, FakeOps, FakeVectors, RecordingTracer

from herness.core.config import get_config
from herness.core.types import (
    Budgets,
    LLMRequest,
    LLMResponse,
    LoopSignal,
    LoopState,
    Message,
    SqlLimits,
    ToolCall,
    ToolContext,
    Usage,
)
from herness.harness.llm.settings import ClientConfig
from herness.harness.roles import base
from herness.harness.roles.base import RoleSpec

QIDS = [f"q_{i:016x}" for i in range(1, 40)]
_CROCKFORD = "".join(ch for ch in string.digits + string.ascii_uppercase if ch not in "ILOU")
FIDS = [f"fnd_{'0' * 25}{ch}" for ch in _CROCKFORD]


class Out(BaseModel):
    """The demo role's output model."""

    model_config = ConfigDict(extra="forbid")
    answer: str


def write_prompts(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp prompt directory for the role resolver (T05-19 seam)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "_common.md").write_text("# Common rules\nCite every number.\n", encoding="utf-8")
    (root / "demo.md").write_text("# Demo analyst\nAnswer the task.\n", encoding="utf-8")
    monkeypatch.setattr(base, "_prompts_root", lambda: root)
    return root


def demo_role(
    *,
    tools: Iterable[str] = ("run_sql", "list_tables", "post_finding"),
    output_model: type[BaseModel] | None = Out,
    model_role: str = "analyst",
    thinking: Literal["off", "on", "auto"] = "auto",
) -> RoleSpec:
    """A `RoleSpec` named `analyst_general` over the tmp prompts."""
    return RoleSpec(
        name="analyst_general",
        specialty="general",
        prompt_files=("_common.md", "demo.md"),
        allowed_tools=frozenset(tools),
        output_model=output_model,
        temperature=0.2,
        effort="medium",
        thinking=thinking,
        model_role=model_role,
    )


def profile(name: str = "fake", *, key: str = "local-small-cpu", **update: Any) -> ClientConfig:
    """A configured client entry renamed to the fake client's name."""
    return get_config().models.models.clients[key].model_copy(update={"name": name, **update})


def loop_ctx(  # noqa: PLR0913 - test builder
    *,
    tool_names: Sequence[str] = ("run_sql",),
    task_tools: Mapping[str, Any] | None = None,
    budgets: Budgets | None = None,
    ledger: Any = None,
    tracer: Any = None,
    ops: Any = None,
    warehouse: Any = None,
    egress_purpose: Literal["reasoning", "reasoning_final"] | None = None,
    task_id: str = "task_1",
) -> ToolContext:
    """A `ToolContext` with the T05-02 fakes and a `RecordingTracer`."""
    wh = warehouse or FakeWarehouse()
    return ToolContext(
        run_id="run_1",
        task_id=task_id,
        build_id=wh.build_id,
        role="analyst_general",
        specialty="general",
        depth="standard",
        profile="local",
        tool_names=list(tool_names),
        task_tools=dict(task_tools or {}),
        warehouse=wh,
        ops=ops or FakeOps(),
        vectors=FakeVectors(),
        budgets=budgets
        or Budgets(max_steps=20, max_tokens=200_000, max_cost_usd=Decimal(5), wall_clock_s=600),
        ledger=ledger or FakeLedger(),
        sql_limits=SqlLimits(timeout_s=30.0),
        egress_purpose=egress_purpose,
        tracer=tracer or RecordingTracer("run_1", task_id),
    )


def budgets(**changes: Any) -> Budgets:
    """Budgets with test defaults, overridden by `changes`."""
    values: dict[str, Any] = {
        "max_steps": 20,
        "max_tokens": 200_000,
        "max_cost_usd": Decimal(5),
        "wall_clock_s": 600,
    }
    return Budgets(**(values | changes))


def resp(  # noqa: PLR0913 - test builder
    text: str = "",
    calls: Sequence[ToolCall] = (),
    *,
    stop: str | None = None,
    parsed: dict[str, JsonValue] | None = None,
    tin: int = 10,
    tout: int = 5,
    cost: str = "0",
    client: str = "fake",
    provider: str = "openai_compat",
) -> LLMResponse:
    """An `LLMResponse`; `stop` defaults to `tool_use` with calls, else `end_turn`."""
    return LLMResponse.model_validate(
        {
            "text": text,
            "tool_calls": list(calls),
            "parsed": parsed,
            "reasoning": [],
            "stop_reason": stop or ("tool_use" if calls else "end_turn"),
            "raw_stop_reason": stop or "x",
            "refusal_category": "policy" if stop == "refusal" else None,
            "usage": Usage(input_tokens=tin, output_tokens=tout),
            "cost_usd": Decimal(cost),
            "client": client,
            "model": "m",
            "provider": provider,
            "latency_ms": 1,
            "request_id": None,
        }
    )


def final(answer: str = "done", **kw: Any) -> LLMResponse:
    """A final structured response for `Out`."""
    return resp(f'{{"answer": "{answer}"}}', parsed={"answer": answer}, **kw)


class ListClient:
    """`LLMClient` answering from `replies` in order (an Exception is raised); records calls.

    `replies` may be a callable `(n, req) -> LLMResponse` for unbounded scripts.
    """

    def __init__(
        self,
        replies: Sequence[LLMResponse | Exception] | Callable[[int, LLMRequest], LLMResponse],
        name: str = "fake",
    ) -> None:
        self.name = name
        self.replies = replies
        self.requests: list[LLMRequest] = []

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        n = len(self.requests) - 1
        if callable(self.replies):
            return self.replies(n, req)
        reply = self.replies[n]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def complete(self, req: LLMRequest) -> LLMResponse:
        raise NotImplementedError


class FakeHooks:
    """A recording `LoopHooks`; defaults: no compaction, policy nudges, no-op checkpoint."""

    def __init__(
        self,
        *,
        policy: Callable[[LoopState, LoopSignal], Literal["nudge", "stop"]] | None = None,
        compact_at: Iterable[int] = (),
        pressure: Callable[[LoopState], list[Message]] | None = None,
        cancel_at_step: int | None = None,
    ) -> None:
        self.on_text_delta = None
        self.policy = policy
        self.compact_at = set(compact_at)
        self.pressure = pressure
        self.cancel_at_step = cancel_at_step
        self.before: list[LLMRequest] = []
        self.signals: list[tuple[str, int, int]] = []  # (cause, loop_signals, nudges)
        self.after: list[tuple[int, bool, int]] = []  # (step, stopping, nudges)
        self.checkpoints: list[dict[str, JsonValue]] = []
        self.messages_seen: list[list[Message]] = []

    async def before_call(self, state: LoopState, req: LLMRequest) -> LLMRequest:
        self.before.append(req)
        return req

    async def call(
        self,
        client: Any,
        req: LLMRequest,
        state: LoopState,
        schema: type[BaseModel] | None,
    ) -> tuple[LLMResponse, BaseModel | None]:
        response = await client.acomplete(req)
        if schema is None or response.parsed is None:
            return response, None
        return response, schema.model_validate(response.parsed)

    async def needs_compaction(self, state: LoopState) -> bool:
        return state.step in self.compact_at

    async def on_context_pressure(self, state: LoopState) -> list[Message]:
        self.compact_at.discard(state.step)
        assert self.pressure is not None
        return self.pressure(state)

    async def on_loop_signal(
        self, state: LoopState, signal: LoopSignal
    ) -> Literal["nudge", "stop"]:
        self.signals.append((signal.cause, state.loop_signals, state.nudges))
        return "nudge" if self.policy is None else self.policy(state, signal)

    async def after_step(self, state: LoopState) -> None:
        self.after.append((state.step, state._stopping, state.nudges))
        self.checkpoints.append(state.to_checkpoint())
        if self.cancel_at_step is not None and state.step >= self.cancel_at_step:
            raise asyncio.CancelledError


def stop_on(n: int, tracer: RecordingTracer | None = None) -> Callable[..., Any]:
    """A policy fake: nudge until the `n`-th signal, then stop, emitting `guard_stop` as the
    spec 08 policy does."""

    def policy(state: LoopState, signal: LoopSignal) -> Literal["nudge", "stop"]:
        if state.loop_signals < n:
            return "nudge"
        if tracer is not None:
            tracer.emit("guard_stop", cause=signal.cause, step=state.step)
        return "stop"

    return policy


def events(tracer: RecordingTracer, kind: str) -> list[dict[str, object]]:
    """The fields of every recorded event of `kind`."""
    return [fields for t, _span, fields in tracer.events if t == kind]


def run(coro: Any) -> Any:
    """`asyncio.run` shorthand."""
    return asyncio.run(coro)


def iter_ids(ids: Sequence[str]) -> Iterator[str]:
    """Cycle through `ids` forever."""
    while True:
        yield from ids
