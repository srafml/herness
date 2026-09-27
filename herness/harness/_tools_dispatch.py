"""Dispatch internals of `herness.harness.tools.dispatch` (impl 05 U05-34); private.

Split out so `tools.py` stays within its 400-line budget. Flow F05-03: the pre-checks run in
call order on the event loop (steps 1-7) and each failing call gets an error result without
being executed; valid calls run in one `asyncio.TaskGroup` under the `max_parallel` semaphore
with the `tool_store` retry policy (8-9); results are filled, the `run_sql` failure counters
updated, `tool_call` traced through `ctx.tracer` and metrics observed (10-12). Tool names are
looked up only in the resolved mapping, never imported or read by attribute (TH05-02).
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import (
    FatalError,
    HernessError,
    QueryError,
    SchemaViolation,
    ToolInputError,
)
from herness.core.ids import canonical_json, normalize_sql
from herness.core.resilience import aretry_call, retry_call
from herness.core.resilience.metrics import record_counter, record_histogram
from herness.core.types import AsyncTool, LoopState, Tool, ToolCall, ToolContext, ToolResult
from herness.harness._tools_schema import schema_hint
from herness.harness.tracing import tool_call_fields

MAX_TOOL_CALLS_PER_MESSAGE: Final = 16
MAX_TOOL_ARGUMENT_CHARS: Final = 32_000
SQL_STOP_HINT: Final = "stop retrying this query; try get_metric or a simpler aggregate"
_RUN_SQL: Final = "run_sql"
_CALLS_TOTAL: Final = "herness_harness_tool_calls_total"
_LATENCY: Final = "herness_harness_tool_latency_seconds"

type AnyTool = Tool | AsyncTool


@dataclass(slots=True)
class _Slot:
    """One call through the round: the tool when it passed every pre-check, then its result."""

    call: ToolCall
    traced: ToolCall
    label: str = "unknown"
    tool: AnyTool | None = None
    sql_key: str | None = None
    result: ToolResult | None = None
    duration_ms: int = 0

    def fail(self, exc: HernessError, *, hint: str | None = None) -> _Slot:
        self.result = ToolResult.from_error(exc, hint=hint)
        return self


def _max_parallel() -> int:
    """`harness.tools.max_parallel` of the process config (step 8)."""
    return get_config().models.harness.tools.max_parallel


def _vet(call: ToolCall) -> tuple[str | None, str | None]:
    """Step 3 on EVERY call: `(signature, None)`, or `(None, error)` when the arguments are
    too large (checked on the raw text first) or cannot be canonicalised (too deep)."""
    raw = call.raw_arguments
    if raw and len(raw) > MAX_TOOL_ARGUMENT_CHARS:
        return None, "tool arguments too large"
    try:
        if not raw and len(canonical_json(call.arguments)) > MAX_TOOL_ARGUMENT_CHARS:
            return None, "tool arguments too large"
        return LoopState.call_signature(call.name, call.arguments), None
    except SchemaViolation:  # too deep or not JSON: never parsed further
        return None, "tool arguments are not valid JSON values"


def _sql_capped(ctx: ToolContext, state: LoopState, slot: _Slot) -> bool:
    """Step 6: a `run_sql` call whose normalized SQL already failed the maximum times."""
    sql = slot.call.arguments.get("sql")
    if slot.call.name != _RUN_SQL or not isinstance(sql, str):
        return False
    slot.sql_key = normalize_sql(sql)
    limit = ctx.sql_limits.max_attempts_per_query
    if state.sql_failures(slot.sql_key) < limit:
        return False
    slot.fail(QueryError(f"query failed {limit} times"), hint=SQL_STOP_HINT)
    return True


def precheck(
    ctx: ToolContext, tools: Mapping[str, AnyTool], state: LoopState, index: int, call: ToolCall
) -> _Slot:
    """Steps 1-7 for one call; the slot carries its tool only when every check passed."""
    tool = tools.get(call.name)  # the resolved mapping is the only source of tools
    sig, bad_args = _vet(call)
    traced = call if sig is not None else call.model_copy(update={"arguments": {}})
    slot = _Slot(call, traced, label="unknown" if tool is None else call.name)  # no blob traced
    if index >= MAX_TOOL_CALLS_PER_MESSAGE:
        msg = f"too many tool calls in one message (max {MAX_TOOL_CALLS_PER_MESSAGE})"
        return slot.fail(ToolInputError(msg))
    if tool is None:
        msg = f"tool {call.name} not allowed; allowed: {', '.join(sorted(tools))}"
        return slot.fail(ToolInputError(msg))
    if sig is None:
        return slot.fail(ToolInputError(bad_args or "invalid arguments"))
    first = state.check_repeat(sig)
    state.remember_call(sig)  # step 7: a later identical call is a repeat even if this fails
    if first is not None:
        return slot.fail(ToolInputError(f"identical call already made at step {first}"))
    hint = schema_hint(tool.input_schema, call.arguments)
    if hint is not None:
        return slot.fail(ToolInputError("invalid arguments"), hint=hint)
    if not _sql_capped(ctx, state, slot):
        slot.tool = tool
    return slot


def _is_async(tool: AnyTool) -> bool:
    return inspect.iscoroutinefunction(tool) or inspect.iscoroutinefunction(type(tool).__call__)


async def _run_one(ctx: ToolContext, slot: _Slot, gate: asyncio.Semaphore) -> None:
    """Step 9: one call under the semaphore; recoverable and exhausted retryable errors become
    error results, `FatalError` propagates (the TaskGroup then cancels the siblings)."""
    tool, args = cast("AnyTool", slot.tool), slot.call.arguments
    async with gate:
        started = clock.monotonic()
        try:
            if _is_async(tool):
                abound = functools.partial(cast("AsyncTool", tool), ctx, **args)
                result = await aretry_call("tool_store", abound)
            else:
                sbound = functools.partial(cast("Tool", tool), ctx, **args)
                result = await asyncio.to_thread(retry_call, "tool_store", sbound)
        except FatalError:
            raise
        except HernessError as exc:  # RecoverableError, RetryableError left after retries
            result = ToolResult.from_error(exc)
        slot.duration_ms = round((clock.monotonic() - started) * 1000)
    slot.result = result


def _first_fatal(group: BaseExceptionGroup[BaseException]) -> BaseException:
    for exc in group.exceptions:
        leaf = _first_fatal(exc) if isinstance(exc, BaseExceptionGroup) else exc
        if isinstance(leaf, FatalError):
            return leaf
    return group


async def execute(ctx: ToolContext, slots: list[_Slot]) -> None:
    """Step 8: run every slot that passed the pre-checks concurrently."""
    runnable = [slot for slot in slots if slot.tool is not None]
    if not runnable:
        return
    gate = asyncio.Semaphore(_max_parallel())
    try:
        async with asyncio.TaskGroup() as group:
            for slot in runnable:
                group.create_task(_run_one(ctx, slot, gate))
    except BaseExceptionGroup as failed:
        raise _first_fatal(failed) from None


def _count_sql_failure(ctx: ToolContext, state: LoopState, slot: _Slot) -> ToolResult:
    """Step 11: an executed `run_sql` call's `QueryError` counts; at the cap, the stop hint."""
    result = cast("ToolResult", slot.result)
    error = result.error
    if slot.tool is None or slot.sql_key is None or error is None or error.type != "QueryError":
        return result
    if state.note_sql_failure(slot.sql_key) < ctx.sql_limits.max_attempts_per_query:
        return result
    return ToolResult.from_error(QueryError(error.message), hint=SQL_STOP_HINT)


def finish(ctx: ToolContext, state: LoopState, slot: _Slot) -> ToolResult:
    """Steps 10-12: fill ids and duration, count `run_sql` failures, trace and observe."""
    result = _count_sql_failure(ctx, state, slot)
    update = {"tool_call_id": slot.call.id, "name": slot.call.name, "duration_ms": slot.duration_ms}
    result = result.model_copy(update=update)
    fields, payload = tool_call_fields(slot.traced, result)
    ctx.tracer.emit("tool_call", step=state.step, payload=payload, **fields)
    labels = {"tool": slot.label, "ok": "true" if result.ok else "false"}
    record_counter(_CALLS_TOTAL, component="harness", labels=labels)
    if slot.tool is not None:
        seconds = slot.duration_ms / 1000
        record_histogram(_LATENCY, seconds, component="harness", labels={"tool": slot.label})
    return result
