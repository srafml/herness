"""Per-run bundle, tool context and hooks of one swarm task (impl 06 U06-139, U06-100, U06-99).

Private sibling of `herness.harness.swarm.routing`, which re-exports `RunEnv`,
`build_tool_context` and `build_hooks`. `Route` is only an annotation here, so this module does
not import `routing` at run time (no cycle).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, Protocol, cast

from herness.core.errors import ConfigError
from herness.core.jobs import gpu_state
from herness.core.resilience import ModelChain
from herness.core.types import Depth, SqlLimits, TaskSpec, ToolContext
from herness.harness.loop import HarnessHooks

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from datetime import datetime

    from herness.core.config import HernessConfig
    from herness.core.jobs import JobContext
    from herness.core.types import AsyncTool, OpsHandle, Tool, TraceEmitter, VectorHandle
    from herness.harness.blackboard import Blackboard
    from herness.harness.budget import RunBudget
    from herness.harness.findings import EntityCatalog
    from herness.harness.gates import CallGate, TaskSlots
    from herness.harness.llm.registry import LLMRegistry
    from herness.harness.memory import MemoryStore
    from herness.harness.pipelines.base import Pipeline, PlanContext
    from herness.harness.pipelines.settings import DepthKnobs, PipelinesConfig
    from herness.harness.swarm.routing import Route
    from herness.harness.swarm.spawn import SpawnBroker
    from herness.harness.tracing import Tracer
    from herness.harness.verifier import Verifier
    from herness.harness.warehouse import WarehousePool
    from herness.store.ops import RunRow

__all__ = ["RunEnv", "build_hooks", "build_tool_context"]

_OFF_NETWORK_TOOLS: Final = frozenset({"get_metric", "get_scores", "list_findings"})


class _MetricSink(Protocol):
    """T06-13: stand-in for spec 08 `MetricSink` (no owner type in the tree yet); the same
    structural view as `herness.harness.blackboard._MetricSink`."""

    def record_histogram(self, name: str, value: float, *, component: str) -> None: ...


@dataclass(slots=True, kw_only=True)
class RunEnv:
    """Per-run bundle passed to every step, so step functions keep ≤ 6 parameters (U06-139).

    Built once per `_drive`; only `plan_ctx`, `by_role` (under `by_role_lock`) and
    `large_scope` change after construction. `warehouses`, `ops` and `vectors` are the handles
    `build_tool_context` reads (U06-100), which the U06-139 field list does not name (spec note).
    """

    run: RunRow
    knobs: DepthKnobs
    cfg: PipelinesConfig
    hcfg: HernessConfig
    llms: LLMRegistry
    memory: MemoryStore
    verifier: Verifier
    bb: Blackboard
    catalog: EntityCatalog
    gates: dict[str, CallGate]
    slots: TaskSlots
    broker: SpawnBroker
    pipeline: Pipeline
    analysis: RunBudget
    writer: RunBudget
    tracer: Tracer
    clock: Callable[[], datetime]
    metrics: _MetricSink
    stop: Callable[[], bool]
    wake: asyncio.Event
    job: JobContext | None
    force: bool
    warehouses: WarehousePool
    ops: OpsHandle
    vectors: VectorHandle
    plan_ctx: PlanContext | None = None
    by_role: dict[str, dict[str, int]] = field(default_factory=dict)
    by_role_lock: threading.Lock = field(default_factory=threading.Lock)
    large_scope: AbstractContextManager[None] | None = None


def _egress_purpose(spec: TaskSpec, route: Route) -> Literal["reasoning", "reasoning_final"] | None:
    if not route.off_network:
        return None
    final = spec.role == "writer" or route.model_role == "skeptic_final"
    return "reasoning_final" if final else "reasoning"


def build_tool_context(
    env: RunEnv,
    spec: TaskSpec,
    route: Route,
    *,
    task_tools: dict[str, Tool | AsyncTool],
    ledger: RunBudget,
    now: datetime,
) -> ToolContext:
    """The spec 05 `ToolContext` of one task (U06-100, TH06-02, TH06-06).

    A `hybrid` off-network route keeps only `get_metric`, `get_scores` and `list_findings`;
    task tools outside the resulting tool names are dropped too (least privilege).
    """
    run = env.run
    if run.build_id is None:
        msg = f"run {run.run_id} has no build_id"
        raise ConfigError(msg)
    names = list(spec.tools)
    if run.profile == "hybrid" and route.off_network:
        names = [name for name in names if name in _OFF_NETWORK_TOOLS]
    sql = env.hcfg.models.harness.sql
    depth = cast("Depth", run.depth)
    return ToolContext(
        run_id=run.run_id,
        task_id=spec.task_id,
        build_id=run.build_id,
        role=spec.role,
        specialty=spec.specialty,
        depth=depth,
        profile=run.profile,
        tool_names=names,
        task_tools={name: tool for name, tool in task_tools.items() if name in names},
        warehouse=env.warehouses.get(run.build_id),
        ops=env.ops,
        vectors=env.vectors,
        budgets=spec.budget.to_budgets(now),
        ledger=ledger,
        sql_limits=SqlLimits(
            return_rows=sql.return_rows, scan_rows=sql.scan_rows, timeout_s=sql.timeout_s[depth]
        ),
        text_access="redacted_only",
        egress_purpose=_egress_purpose(spec, route),
        # Tracer.emit adds typed keyword-only fields; structurally a TraceEmitter (U05-69).
        tracer=cast("TraceEmitter", env.tracer),
    )


def build_hooks(
    env: RunEnv,
    spec: TaskSpec,
    route: Route,
    ctx: ToolContext,
    *,
    on_text_delta: Callable[[str | None], Awaitable[None]] | None = None,
) -> HarnessHooks:
    """The spec 05 `HarnessHooks` of one task (U06-99, design 06 §3.3).

    No model chain after a hybrid local fallback (the routed local client is called directly);
    the phase is the run's current status.
    """
    chain = (
        None
        if route.fallback_local
        else ModelChain(route.model_role, registry=env.llms, depth=env.run.depth, gpu=gpu_state())
    )
    return HarnessHooks(
        registry=env.llms,
        gates=env.gates,
        chain=chain,
        compactor=env.memory.compactor(route.config, ctx=ctx),
        task_id=spec.task_id,
        phase=env.run.status,
        stop=env.stop,
        on_text_delta=on_text_delta,
        tracer=env.tracer,
    )
