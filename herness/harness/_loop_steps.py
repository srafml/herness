"""Private step helpers of the agent loop (impl 05 U05-58, U05-59; design 05 §5.2.1).

`herness.harness.loop` keeps the public names and re-exports `build_request` as the spec's
`loop._build_request`; this module exists only to keep `loop.py` within its 220-line budget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import JsonValue, ValidationError

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import BudgetExceeded, ConfigError, ModelRefused
from herness.core.logging import get_logger
from herness.core.types import (
    AgentResult,
    LLMRequest,
    LLMResponse,
    LoopCheckpoint,
    LoopLimits,
    LoopState,
    SystemBlock,
    ToolContext,
    ToolSpec,
)
from herness.harness.llm.base import BASE_ROLE, resolve_request_params
from herness.harness.llm.tokens import estimate_tokens
from herness.harness.tracing import llm_call_fields

if TYPE_CHECKING:
    from herness.harness.llm.settings import ClientConfig
    from herness.harness.loop import LoopHooks
    from herness.harness.roles.base import RoleSpec

__all__ = ["account", "build_request", "compact", "fresh_state", "limit_hit", "result"]

_log = get_logger("harness.loop")


def fresh_state(
    role: RoleSpec,
    task_input: dict[str, JsonValue],
    profile: ClientConfig,
    specs: list[ToolSpec],
    system: list[SystemBlock],
    cp: LoopCheckpoint | None,
) -> LoopState:
    """Setup steps 4-6: the client's limits, then a fresh state restored from `cp` (R-17)."""
    ls = get_config().models.harness.loop
    limits = LoopLimits.for_client(
        profile.context_window,
        profile.max_effective_context,
        profile.max_output_tokens,
        no_progress_steps=ls.no_progress_steps,
        error_streak=ls.error_streak,
        fixed_tokens=estimate_tokens((), specs, system),
    )
    first = role.render_task(task_input, cp)
    state = LoopState.fresh(first, limits=limits, estimator=estimate_tokens)
    if cp is not None:
        state.restore(cp)
    return state


async def compact(
    state: LoopState, hooks: LoopHooks, ctx: ToolContext, profile: ClientConfig
) -> tuple[str, str] | None:
    """Step 7: replace the messages when needed; `(reason, cause)` when still over hard."""
    if not await hooks.needs_compaction(state):
        return None
    before, old = state.est_input_tokens(), state.messages
    new = await hooks.on_context_pressure(state)
    state.replace_messages(new)
    kept, after = {id(m) for m in new}, state.est_input_tokens()
    ctx.tracer.emit(
        "compaction",
        step=state.step,
        before_tokens=before,
        after_tokens=after,
        n_messages_removed=sum(id(m) not in kept for m in old),
        fresh_conversation=profile.kind == "anthropic",
    )
    return ("task_tokens", "context") if after >= state.limits().hard_tokens else None


def limit_hit(state: LoopState, ctx: ToolContext, t0: float) -> tuple[str, str] | None:
    """Step 8: the hard limits in order, as `(stop_reason, cause)` (R-22)."""
    b = ctx.budgets
    if state.step >= b.max_steps:
        return ("max_steps", "max_steps")
    if state.tokens_used() >= b.max_tokens:
        return ("task_tokens", "task_tokens")
    late = b.deadline is not None and clock.now() >= b.deadline
    if clock.monotonic() - t0 >= b.wall_clock_s or late:
        return ("wall_clock", "wall_clock")
    return None


def account(
    role: RoleSpec, state: LoopState, ctx: ToolContext, req: LLMRequest, resp: LLMResponse
) -> None:
    """Steps 12-13: charge the state and the run ledger, trace `llm_call`, check refusal."""
    tin, tout = state.charge(resp)
    try:
        ctx.ledger.charge(tin, tout, resp.cost_usd)  # only the run ledger raises (R-25)
    except BudgetExceeded:
        _log.info("harness.loop.budget_exceeded", task_id=ctx.task_id, step=state.step)
        raise
    wait = state.gate_wait_ms()
    fields, payload = llm_call_fields(req, resp, prompt_hash=role.prompt_hash, gate_wait_ms=wait)
    ctx.tracer.emit("llm_call", step=state.step, payload=payload, **fields)
    _log.debug(
        "harness.llm.call_completed", client=resp.client, model=resp.model, role=role.name,
        latency_ms=resp.latency_ms, stop_reason=resp.stop_reason, input_tokens=tin,
        output_tokens=tout,
    )  # fmt: skip
    if resp.stop_reason == "refusal":  # defensive: `GatedClient` raises first
        category = resp.refusal_category
        _log.warning(
            "harness.llm.refused",
            client=resp.client,
            role=role.name,
            task_id=ctx.task_id,
            refusal_category=category,
        )
        msg = "model refused the request"
        raise ModelRefused(msg, category=category, client=resp.client)


def build_request(  # noqa: PLR0913 - signature fixed by U05-59
    role: RoleSpec,
    profile: ClientConfig,
    system: list[SystemBlock],
    specs: list[ToolSpec],
    state: LoopState,
    ctx: ToolContext,
    *,
    wrap_up: bool,
    final: bool,
) -> LLMRequest:
    """The request of one step, or of the final structured call (U05-59)."""
    table = get_config().models.models.role_params
    rp = table.get(role.model_role) or table.get(BASE_ROLE.get(role.model_role, role.model_role))
    params = resolve_request_params(
        model_role=role.model_role,
        depth=ctx.depth,
        client=profile,
        role_params=rp,
        fallback=role.fallback_params(),
    )
    if params.downgraded:
        _log.warning("harness.llm.thinking_downgraded", client=profile.name, role=role.name)
    tools, choice = specs, "auto"
    if wrap_up or final:  # Anthropic needs the definitions while the history has tool blocks
        tools, choice = (specs, "none") if profile.kind == "anthropic" and specs else ([], "auto")
    model = role.output_model if final else None
    meta: dict[str, object] = {"run_id": ctx.run_id, "task_id": ctx.task_id, "role": role.name}
    meta |= {"model_role": role.model_role, "step": state.step}
    meta["request_key"] = f"{ctx.task_id}:{state.step}:{'final' if final else 'step'}"
    try:
        return LLMRequest.model_validate(
            {
                "client": profile.name,
                "system": system,
                "messages": list(state.messages),
                "tools": tools,
                "tool_choice": choice,
                "parallel_tool_calls": True,
                "response_schema": None if model is None else model.model_json_schema(),
                "response_schema_name": None if model is None else model.__name__,
                "max_output_tokens": profile.max_output_tokens,
                "temperature": params.temperature,
                "effort": params.effort,
                "thinking": params.thinking,
                "timeout_s": profile.timeout_s,
                "metadata": meta,
            }
        )
    except ValidationError:
        msg = "invalid model request"  # the pydantic message may echo prompt text
        raise ConfigError(msg) from None


def result(
    state: LoopState, status: str, reason: str, output: dict[str, JsonValue] | None
) -> AgentResult:
    """The `AgentResult` of the state's counters, usage, cost and ids."""
    return AgentResult.model_validate(
        {
            "status": status, "stop_reason": reason, "output": output, "steps": state.step,
            "usage": state.total_usage(), "cost_usd": state.cost_usd,
            "query_ids": list(state.query_ids), "finding_ids": list(state.finding_ids),
        }
    )  # fmt: skip
