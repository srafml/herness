"""Stand-in tools, context and loop state for the registry and dispatch tests (T05-16).

`make_tool_ctx` binds a `ToolContext` to a warehouse-less fake handle (dispatch never touches
the warehouse itself); `SyncTool` / `AsyncFnTool` wrap a body and count its executions so a
test can assert a call was never run; `use_test_config` caches the full test config (the
`tool_store` retry policy and `harness.tools.max_parallel` come from it).
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from pathlib import Path

from pydantic import JsonValue
from tests.support.config_tree import write_full_config
from tests.support.harness_fakes import FakeLedger, FakeOps, FakeVectors, RecordingTracer

from herness.core import config as c
from herness.core.types import (
    Budgets,
    LoopLimits,
    LoopState,
    Message,
    SqlLimits,
    TextPart,
    ToolCall,
    ToolContext,
    ToolResult,
    WarehouseHandle,
)

BUILD_ID = "20260925-101500-ABCDEF"

type SyncBody = Callable[..., ToolResult]
type AsyncBody = Callable[..., Awaitable[ToolResult]]


def strict_schema(props: Mapping[str, JsonValue] | None = None) -> dict[str, JsonValue]:
    """A strict-compatible (R-26) object schema over `props`."""
    properties = dict(props or {})
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


class FakeWarehouse:
    """`WarehouseHandle` without a database; dispatch itself never queries it."""

    def __init__(self, build_id: str = BUILD_ID) -> None:
        self._build_id = build_id

    @property
    def build_id(self) -> str:
        return self._build_id

    @property
    def path(self) -> Path:
        return Path(f"wh-{self._build_id}.duckdb")

    def cursor(self) -> object:
        msg = "FakeWarehouse has no cursor"
        raise AssertionError(msg)

    def schema(self) -> Mapping[str, Mapping[str, Mapping[str, str]]]:
        return {}

    def table_comment(self, qualified: str) -> str:
        del qualified
        return ""


def make_tool_ctx(
    *,
    warehouse: WarehouseHandle | None = None,
    build_id: str = BUILD_ID,
    limits: SqlLimits | None = None,
) -> ToolContext:
    """A `ToolContext` with fakes for every handle and a `RecordingTracer`."""
    return ToolContext(
        run_id="run_1",
        task_id="task_1",
        build_id=build_id,
        role="analyst",
        specialty="general",
        depth="standard",
        profile="local",
        tool_names=[],
        warehouse=warehouse or FakeWarehouse(),
        ops=FakeOps(),
        vectors=FakeVectors(),
        budgets=Budgets(max_steps=10, max_tokens=10_000, max_cost_usd=Decimal(1), wall_clock_s=600),
        ledger=FakeLedger(),
        sql_limits=limits or SqlLimits(timeout_s=30.0),
        tracer=RecordingTracer("run_1", "task_1"),
    )


def make_state() -> LoopState:
    """A fresh loop state at step 0."""
    first = Message(role="user", parts=[TextPart(text="go")])
    limits = LoopLimits(context_budget_tokens=10_000, soft_tokens=5_000, hard_tokens=8_000)
    return LoopState.fresh(first, limits=limits, estimator=lambda _msgs: 0)


def ok_result(text: str = "ok") -> ToolResult:
    """A successful result with `text` as content."""
    return ToolResult(ok=True, content=text)


class SyncTool:
    """A synchronous tool around `body(ctx, **kwargs)`; counts executions."""

    def __init__(
        self,
        name: str,
        body: SyncBody | None = None,
        *,
        schema: dict[str, JsonValue] | None = None,
        description: str = "test tool",
    ) -> None:
        self._name, self._description = name, description
        self._schema = schema if schema is not None else strict_schema()
        self._body = body
        self._lock = threading.Lock()
        self.calls: list[dict[str, JsonValue]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def input_schema(self) -> dict[str, JsonValue]:
        return self._schema

    def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        with self._lock:
            self.calls.append(dict(kwargs))
        return self._body(ctx, **kwargs) if self._body is not None else ok_result(self._name)


class AsyncFnTool:
    """An async tool around `await body(ctx, **kwargs)`; counts executions."""

    def __init__(
        self, name: str, body: AsyncBody, *, schema: dict[str, JsonValue] | None = None
    ) -> None:
        self._name = name
        self._schema = schema if schema is not None else strict_schema()
        self._body = body
        self.calls: list[dict[str, JsonValue]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "async test tool"

    @property
    def input_schema(self) -> dict[str, JsonValue]:
        return self._schema

    async def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        self.calls.append(dict(kwargs))
        return await self._body(ctx, **kwargs)


def call(name: str, call_id: str = "c1", **arguments: JsonValue) -> ToolCall:
    """A `ToolCall` with `arguments`."""
    return ToolCall(id=call_id, name=name, arguments=arguments)


def use_test_config(root: Path, overrides: tuple[str, ...] = ()) -> c.HernessConfig:
    """Cache the full test config for `get_config` (callers reset it afterwards)."""
    c.reset_config()
    return c.init_config("local", overrides, config_dir=write_full_config(root), env={})
