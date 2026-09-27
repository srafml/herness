"""The agent loop `run_agent` and its `LoopHooks` protocol (impl 05 U05-57-U05-60, U05-74).

Design 05 §3.3, §5.2.1 (flows F05-01, F05-07). One bounded task: every step checks the context,
the hard limits (steps, task tokens, wall clock, deadline, task cost) and the loop signals
(TH05-08); tools come only from the registry (TH05-07). Messages are only appended, or the whole
list is replaced by compaction. The step helpers live in the private `_loop_steps`
(`_build_request` is its `build_request`); `HarnessHooks` and `GatedClient` are re-exported.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Final, Literal, Protocol

from pydantic import BaseModel, JsonValue

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError, OutputValidationError
from herness.core.logging import get_logger
from herness.core.types import (
    AgentResult,
    LLMRequest,
    LLMResponse,
    LoopCheckpoint,
    LoopSignal,
    LoopState,
    Message,
    SystemBlock,
    TextPart,
    ToolContext,
    ToolSpec,
)
from herness.harness import _loop_steps as steps
from herness.harness._loop_steps import build_request as _build_request
from herness.harness.hooks import GatedClient, HarnessHooks
from herness.harness.llm.base import LLMClient, egress_purpose_for
from herness.harness.tools import dispatch, tool_registry

if TYPE_CHECKING:
    from herness.harness.llm.settings import ClientConfig
    from herness.harness.roles.base import RoleSpec

__all__ = [
    "BUDGET_WARN_RATIO",
    "CONTINUE_NUDGE",
    "FINAL_JSON_INSTRUCTION",
    "STOP_REASON_BY_CAUSE",
    "WRAP_UP_NUDGE",
    "GatedClient",
    "HarnessHooks",
    "LoopHooks",
    "run_agent",
]

BUDGET_WARN_RATIO: Final = 0.75
WRAP_UP_NUDGE: Final = (
    "You have reached the budget limit for this task. Do not call tools. "
    "Give your final answer now."
)
CONTINUE_NUDGE: Final = "Your reply was cut off. Continue more concisely."
FINAL_JSON_INSTRUCTION: Final = "Return your final result as JSON only."
STOP_REASON_BY_CAUSE: Final[Mapping[str, str]] = {
    "repeat": "repeat_call",
    "no_progress": "no_progress",
    "error_streak": "error_streak",
}

_log = get_logger("harness.loop")

type _Finish = Callable[..., Awaitable[AgentResult]]


class LoopHooks(Protocol):
    """The loop's extension points (design §3.3); `HarnessHooks` is the only production one."""

    on_text_delta: Callable[[str | None], Awaitable[None]] | None

    async def before_call(self, state: LoopState, req: LLMRequest) -> LLMRequest: ...
    async def call(
        self, client: LLMClient, req: LLMRequest, state: LoopState, schema: type[BaseModel] | None
    ) -> tuple[LLMResponse, BaseModel | None]: ...
    async def needs_compaction(self, state: LoopState) -> bool: ...
    async def on_context_pressure(self, state: LoopState) -> list[Message]: ...
    async def on_loop_signal(
        self, state: LoopState, signal: LoopSignal
    ) -> Literal["nudge", "stop"]: ...
    async def after_step(self, state: LoopState) -> None: ...


async def run_agent(  # noqa: PLR0913, PLR0917 - seven parameters fixed by spec 00 §12.3
    role: RoleSpec,
    task_input: dict[str, JsonValue],
    ctx: ToolContext,
    client: LLMClient,
    profile: ClientConfig,
    hooks: LoopHooks,
    resume_from: LoopCheckpoint | None = None,
) -> AgentResult:
    """Run one bounded agent task: `completed` on a validated final, else `partial` (U05-58)."""
    if profile.name != client.name:
        msg = "client profile does not match the client"
        raise ConfigError(msg)
    if profile.off_network and ctx.egress_purpose != egress_purpose_for(role.model_role):
        msg = "egress purpose mismatch"
        raise ConfigError(msg)
    t0, ratio = clock.monotonic(), get_config().models.harness.loop.wrap_up_ratio
    tool_list = tool_registry().resolve(role, ctx.tool_names, ctx.task_tools)
    tools, specs = {t.name: t for t in tool_list}, tool_registry().tool_specs(tool_list)
    system = role.system_blocks(ctx)
    state = steps.fresh_state(role, task_input, profile, specs, system, resume_from)
    finish: _Finish = functools.partial(_finish, ctx=ctx, hooks=hooks)
    while True:
        stop = await steps.compact(state, hooks, ctx, profile) or steps.limit_hit(state, ctx, t0)
        if stop is not None:
            return await finish(state, stop[0], cause=stop[1])
        wrap_up = _wrap_up(state, ctx, ratio)
        req = _build_request(role, profile, system, specs, state, ctx, wrap_up=wrap_up, final=False)
        req = await hooks.before_call(state, req)
        resp, _ = await hooks.call(client, req, state, None)
        steps.account(role, state, ctx, req, resp)
        state.append_assistant(resp)
        if state.cost_usd > ctx.budgets.max_cost_usd:
            return await finish(state, "task_budget", cause="task_cost")
        if resp.tool_calls and not wrap_up:
            state.append_tool_results(await dispatch(ctx, tools, resp.tool_calls, hooks, state))
        elif resp.stop_reason == "max_tokens":
            state.add_nudge(CONTINUE_NUDGE, counts=False)
        else:
            return await _finalize(
                role, state, client=client, profile=profile, system=system, specs=specs,
                hooks=hooks, ctx=ctx,
            )  # fmt: skip
        state.step += 1
        stopped = await _on_signal(state, hooks, finish)
        if stopped is not None:
            return stopped
        await hooks.after_step(state)  # checkpoint; a CancelledError propagates


def _wrap_up(state: LoopState, ctx: ToolContext, ratio: float) -> bool:
    """Step 9: the wrap-up decision; the nudge and each `budget` event only the first time."""
    b, used = ctx.budgets, state.tokens_used()
    wrap_up = used >= ratio * b.max_tokens or state.step == b.max_steps - 1
    if wrap_up and not state._wrap_up_sent:
        state._wrap_up_sent = True
        state.add_nudge(WRAP_UP_NUDGE, counts=False)
        _budget_event(state, ctx, "wrap_up", WRAP_UP_NUDGE)
    if used >= BUDGET_WARN_RATIO * b.max_tokens and not state._budget_warned:
        state._budget_warned = True
        _budget_event(state, ctx, "warn", None)
    return wrap_up


def _budget_event(state: LoopState, ctx: ToolContext, kind: str, message: str | None) -> None:
    b = ctx.budgets
    used = {"tokens": state.tokens_used(), "cost_usd": str(state.cost_usd), "steps": state.step}
    limit = {"tokens": b.max_tokens, "cost_usd": str(b.max_cost_usd), "steps": b.max_steps}
    ctx.tracer.emit("budget", step=state.step, kind=kind, used=used, limit=limit, message=message)


async def _on_signal(state: LoopState, hooks: LoopHooks, finish: _Finish) -> AgentResult | None:
    """Step 18: a loop signal goes to the policy; `stop` ends the task (`guard_stop` emitted)."""
    signal = state.loop_signal()  # increments `loop_signals` when it returns one (R-66)
    if signal is None:
        return None
    if await hooks.on_loop_signal(state, signal) == "stop":
        reason = STOP_REASON_BY_CAUSE[signal.cause]
        return await finish(state, reason, cause=signal.cause, guard_emitted=True)
    state.add_nudge(signal.message)
    return None


async def _finalize(  # noqa: PLR0913 - signature fixed by U05-60
    role: RoleSpec,
    state: LoopState,
    *,
    client: LLMClient,
    profile: ClientConfig,
    system: list[SystemBlock],
    specs: list[ToolSpec],
    hooks: LoopHooks,
    ctx: ToolContext,
) -> AgentResult:
    """The separate structured-output call; `completed` with the validated output (U05-60)."""
    if role.output_model is None:
        last = next(m for m in reversed(state.messages) if m.role == "assistant")
        text = "".join(p.text for p in last.parts if isinstance(p, TextPart))
        return steps.result(state, "completed", "final", {"text": text})
    state.add_nudge(FINAL_JSON_INSTRUCTION, counts=False)
    req = _build_request(role, profile, system, specs, state, ctx, wrap_up=False, final=True)
    req = await hooks.before_call(state, req)
    resp, parsed = await hooks.call(client, req, state, role.output_model)
    steps.account(role, state, ctx, req, resp)
    if parsed is None:
        msg = "final output missing"
        raise OutputValidationError(msg, client=resp.client)
    return steps.result(state, "completed", "final", parsed.model_dump(mode="json"))


async def _finish(
    state: LoopState,
    reason: str,
    *,
    cause: str,
    ctx: ToolContext,
    hooks: LoopHooks,
    guard_emitted: bool = False,
) -> AgentResult:
    """End with `partial`: `guard_stop`, then a forced checkpoint, then the log (U05-60)."""
    if not guard_emitted:
        ctx.tracer.emit("guard_stop", step=state.step, cause=cause)
    state._stopping = True  # `HarnessHooks.after_step` then saves regardless of the interval
    await hooks.after_step(state)  # a CancelledError propagates
    _log.info("harness.loop.stopped", task_id=ctx.task_id, cause=cause, step=state.step)
    return steps.result(state, "partial", reason, None)
