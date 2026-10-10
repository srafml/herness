"""Collaborators and per-run setup of the run driver (impl 06 U06-78, U06-82 step 1).

Private sibling of `herness.harness.swarm.run` (T06-22 split): `SwarmDeps` (U06-78) with its
composition default `SwarmDeps.from_config`, `build_env` (the U06-82 step 1 setup: knobs,
tracer, catalog, Blackboard on the Swarm's one writer, restored phase budgets, gates, task
slots, spawn broker, the pipeline over the planner task's recorded reader) and `Stopper` (the
U06-82 step 4 stop rule: own flag, job yield, or the run row `canceled`).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast

from herness.core import time as clock
from herness.core.errors import ConfigError, NotFound
from herness.core.numbers import compile_allowed_patterns
from herness.core.resilience import metrics as process_metrics
from herness.harness.blackboard import Blackboard
from herness.harness.budget import RunBudget, new_phase_budgets
from herness.harness.findings import EntityCatalog
from herness.harness.gates import TaskSlots, build_gates
from herness.harness.pipelines.base import Pipeline, RecordedReader, get_pipeline
from herness.harness.pipelines.settings import DepthKnobs, PipelinesConfig, resolve_knobs
from herness.harness.swarm.planner import planner_dedup_key
from herness.harness.swarm.routing import RunEnv, build_tool_context, route_task
from herness.harness.swarm.spawn import SpawnBroker
from herness.harness.tools import execute_recorded
from herness.store.ops import get_run, get_task_by_dedup

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime

    from herness.core.config import HernessConfig
    from herness.core.jobs import JobContext
    from herness.core.types import (
        Depth,
        OpsHandle,
        ReportDraft,
        TraceEmitter,
        VectorHandle,
        VectorHit,
    )  # fmt: skip
    from herness.harness.llm.registry import LLMRegistry
    from herness.harness.memory import MemoryStore
    from herness.harness.pipelines.settings import ReviewKind
    from herness.harness.tracing import Tracer
    from herness.harness.verifier import Verifier
    from herness.harness.warehouse import WarehousePool
    from herness.store.ops import RunRow

__all__ = [
    "EnvParts", "MetricSink", "Stopper", "SwarmDeps", "build_env", "planner_reader", "run_knobs",
]  # fmt: skip

_FALLBACK_NUMERALS: Final = (  # config/app.yaml `reports.allowed_numeral_patterns` (owner 09)
    r"\b(19|20)\d{2}\b", r"\d{4}-\d{2}-\d{2}", r"Q[1-4] \d{4}", r"(INC|CHG|PRB)\d+",
    r"[A-Z][A-Z0-9]+-\d+",
)  # fmt: skip
STATUS_POLL_S: Final = 1.0  # the run row is re-read for `canceled` at most once per second
GATE_WAIT_METRIC: Final = "herness_harness_gate_wait_seconds"

type StopReason = Literal["cancel", "preempt"]
type SummaryPoster = Callable[[RunRow, ReportDraft | None], str | None]


class MetricSink(Protocol):
    """T06-22: stand-in for spec 08 `MetricSink` (no owner type in the tree yet); structurally
    `herness.core.resilience.metrics` (U08-19, U08-20)."""

    def record_counter(
        self, name: str, value: float = 1.0, *, component: str,
        labels: Mapping[str, str] | None = None,
    ) -> None: ...  # fmt: skip

    def record_histogram(
        self, name: str, value: float, *, component: str, labels: Mapping[str, str] | None = None
    ) -> None: ...


class _UnboundVectors:
    """Fail-closed `VectorHandle` until the composition root binds a ticket index (T06-25
    precedent, T09-27 carry-over): `semantic_search` raises `NotFound`."""

    def search_tickets(
        self, vector: Sequence[float], k: int, *, entity: str | None, service_id: str | None
    ) -> list[VectorHit]:
        del vector, k, entity, service_id
        msg = "ticket vector search is not bound in this process"
        raise NotFound(msg)


def _registry_render(run_id: str, formats: Sequence[str]) -> object:
    """`registry.get("renderer", "report")` resolved at call time (U06-78; L5, O06-12)."""
    from herness.core import registry  # noqa: PLC0415 - resolved per call, after registration

    return registry.get("renderer", "report")(run_id, formats)


def _review_tracer(run_id: str) -> Tracer:
    from herness.harness.tracing import Tracer  # noqa: PLC0415 - default factory only

    run = get_run(run_id)
    build_id = None if run is None else run.build_id
    return Tracer.for_run(run_id, build_id=build_id, run_kind="review")


@dataclass(frozen=True, slots=True, kw_only=True)
class SwarmDeps:
    """Collaborators of `Swarm` (U06-78, D06-25, D06-27: no store handle).

    `ops`, `vectors`, `portfolio_fn` and `post_summary` are additive (spec note): the
    `ToolContext` handles, the U06-87 optimizer and the U06-135 binding. `post_summary` is
    `None` until impl 09 offers an assistant-row append and a lookup by
    `meta.escalation_run_id` (T06-24 carry-over); the driver then logs and skips the summary.
    """

    hcfg: HernessConfig
    verifier: Verifier
    warehouses: WarehousePool
    render: Callable[[str, Sequence[str]], object]
    tracer_factory: Callable[[str], Tracer]
    clock: Callable[[], datetime]
    metrics: MetricSink
    current_build: Callable[[], str | None]
    build_exists: Callable[[str], bool]
    worker_alive: Callable[[], bool]
    ops: OpsHandle
    vectors: VectorHandle
    portfolio_fn: Callable[..., Any]
    post_summary: SummaryPoster | None = None

    @classmethod
    def from_config(cls, hcfg: HernessConfig) -> SwarmDeps:
        """The process collaborators built from `hcfg` (U06-78 default, U06-85)."""
        from herness.core.jobs import worker_alive  # noqa: PLC0415 - lazy package export
        from herness.harness.verifier import Verifier  # noqa: PLC0415 - composition only
        from herness.harness.warehouse import WarehousePool  # noqa: PLC0415 - composition only
        from herness.metrics.portfolio import optimize_portfolio  # noqa: PLC0415 - L3, lazy
        from herness.store.layout import data_layout  # noqa: PLC0415 - composition only
        from herness.store.ops import evidence  # noqa: PLC0415 - composition only
        from herness.store.warehouse import build_exists, read_current  # noqa: PLC0415

        pool = WarehousePool(data_layout(cfg=hcfg).warehouse, hcfg.models.harness.sql)
        return cls(
            hcfg=hcfg, verifier=Verifier(evidence, pool, hcfg.models.harness.verifier),
            warehouses=pool, render=_registry_render, tracer_factory=_review_tracer,
            clock=clock.now, metrics=process_metrics, current_build=read_current,
            build_exists=build_exists, worker_alive=worker_alive, ops=evidence,
            vectors=_UnboundVectors(), portfolio_fn=optimize_portfolio,
        )  # fmt: skip


class Stopper:
    """U06-82 step 4: stop when the own flag is set, the job should yield, or the run row is
    `canceled` (re-read at most once per `STATUS_POLL_S`); `reason` tells cancel from preempt."""

    def __init__(self, run_id: str, flag: threading.Event, job: JobContext | None) -> None:
        self._run_id, self._flag, self._job = run_id, flag, job
        self._row_canceled = False
        self._read_at = -STATUS_POLL_S

    def _canceled_row(self) -> bool:
        now = clock.monotonic()
        if not self._row_canceled and now - self._read_at >= STATUS_POLL_S:
            self._read_at = now
            run = get_run(self._run_id)
            self._row_canceled = run is not None and run.status == "canceled"
        return self._row_canceled

    def __call__(self) -> bool:
        if self._flag.is_set():
            return True
        if self._job is not None and self._job.should_yield():
            return True
        return self._canceled_row()

    def reason(self) -> StopReason:
        """`cancel` for the flag, the row or a job cancel; else `preempt` (or shutdown)."""
        job_cancel = self._job is not None and self._job.stop_reason == "cancel"
        return "cancel" if self._flag.is_set() or self._row_canceled or job_cancel else "preempt"


@dataclass(frozen=True, slots=True, kw_only=True)
class EnvParts:
    """The Swarm-level inputs of `build_env`."""

    cfg: PipelinesConfig
    llms: LLMRegistry
    memory: MemoryStore
    deps: SwarmDeps
    writer: ThreadPoolExecutor


def _allowed_numerals(hcfg: HernessConfig) -> tuple[str, ...]:
    """The report numeral allow-list (as the Verifier reads it) for `Blackboard.post`."""
    reports = getattr(hcfg.app, "reports", None)
    patterns = getattr(reports, "allowed_numeral_patterns", None)
    return _FALLBACK_NUMERALS if patterns is None else tuple(patterns)


def run_knobs(cfg: PipelinesConfig, run: RunRow, *, no_wildcards: bool) -> DepthKnobs:
    """The run's knobs (U06-24) with its stored override; `H_wildcards = 0` after a forced
    resume of a dead planner (design 06 §6.2)."""
    request = run.meta.get("request")
    override = request.get("budget_override") if isinstance(request, Mapping) else None
    kind = cast("ReviewKind", run.kind)
    knobs = resolve_knobs(cfg, kind, cast("Depth", run.depth), override=override)
    return knobs.model_copy(update={"H_wildcards": 0}) if no_wildcards else knobs


def _budgets(parts: EnvParts, run: RunRow, knobs: DepthKnobs) -> tuple[RunBudget, RunBudget]:
    """The phase ledgers (U06-28), restored from `run.token_usage` when present (U06-27)."""
    hybrid = parts.cfg.hybrid.max_cost_usd_per_run if run.profile == "hybrid" else None
    analysis, writer = new_phase_budgets(
        run_id=run.run_id, run_tokens=knobs.run_tokens,
        writer_reserve=parts.cfg.swarm.writer_reserve,
        cost_cap=None if hybrid is None else Decimal(str(hybrid)), cost_cap_raises=False,
    )  # fmt: skip
    for ledger in (analysis, writer):
        snap = run.token_usage.get(ledger.name)
        if isinstance(snap, Mapping):
            ledger.restore(snap)
    return analysis, writer


def _slots(parts: EnvParts, depth: str) -> TaskSlots:
    key = parts.llms.model_for("analyst", depth)
    return TaskSlots.for_run(parts.llms.config(key).max_concurrency, parts.cfg.swarm.oversubscribe)


def _by_role(run: RunRow) -> dict[str, dict[str, int]]:
    """Role token totals of earlier drives (`token_usage.by_role`)."""
    stored = run.token_usage.get("by_role")
    if not isinstance(stored, Mapping):
        return {}
    return {
        str(role): {str(k): v for k, v in counts.items() if type(v) is int}
        for role, counts in stored.items()
        if isinstance(counts, Mapping)
    }


def planner_reader(env: RunEnv) -> RecordedReader:
    """`execute_recorded` bound to the planner task's tool context (design 06 §3.7)."""
    run = env.run
    planner = get_task_by_dedup(run.run_id, planner_dedup_key(run.run_id, run.kind))
    if planner is None:
        msg = f"run {run.run_id} has no planner task"
        raise NotFound(msg)
    spec, depth = planner.spec, cast("Depth", run.depth)
    route = route_task(spec, llms=env.llms, depth=depth, profile=run.profile, ledger=env.analysis)
    ctx = build_tool_context(env, spec, route, task_tools={}, ledger=env.analysis, now=env.clock())
    return partial(execute_recorded, ctx)


def build_env(
    parts: EnvParts,
    run: RunRow,
    job: JobContext | None,
    *,
    force: bool,
    stop: Stopper,
    no_wildcards: bool,
) -> tuple[RunEnv, RecordedReader]:
    """The per-run bundle of one `_drive` and its recorded reader (U06-82 step 1); blocking:
    call it in a thread."""
    deps = parts.deps
    if run.build_id is None:
        msg = f"run {run.run_id} has no build_id"
        raise ConfigError(msg)
    knobs = run_knobs(parts.cfg, run, no_wildcards=no_wildcards)
    tracer = deps.tracer_factory(run.run_id)
    catalog = EntityCatalog(deps.warehouses.get(run.build_id))
    bb = Blackboard(
        run.run_id, build_id=run.build_id, catalog=catalog, tracer=tracer, writer=parts.writer,
        allowed_numerals=compile_allowed_patterns(list(_allowed_numerals(deps.hcfg))),
        metrics=deps.metrics,
    )  # fmt: skip
    analysis, writer = _budgets(parts, run, knobs)
    wake = asyncio.Event()

    def on_wait(client: str, seconds: float) -> None:
        labels = {"client": client}
        deps.metrics.record_histogram(GATE_WAIT_METRIC, seconds, component="harness", labels=labels)

    broker = SpawnBroker(
        run_id=run.run_id, depth_mode=cast("Depth", run.depth), knobs=knobs,
        settings=parts.cfg.swarm, budget=analysis, bb=bb, catalog=catalog,
        tracer=cast("TraceEmitter", tracer),
        wake=wake.set, clock=deps.clock,
    )  # fmt: skip
    env = RunEnv(
        run=run, knobs=knobs, cfg=parts.cfg, hcfg=deps.hcfg, llms=parts.llms,
        memory=parts.memory, verifier=deps.verifier, bb=bb, catalog=catalog,
        gates=build_gates(deps.hcfg.models.models.clients, mode="review", on_wait=on_wait),
        slots=_slots(parts, run.depth), broker=broker,
        pipeline=cast("Pipeline", None),  # set below: its reader needs this env
        analysis=analysis, writer=writer, tracer=tracer, clock=deps.clock,
        metrics=deps.metrics, stop=stop, wake=wake, job=job, force=force,
        warehouses=deps.warehouses, ops=deps.ops, vectors=deps.vectors, by_role=_by_role(run),
    )  # fmt: skip
    reader = planner_reader(env)
    kind = cast("Literal['funding_review', 'org_review']", run.kind)
    env.pipeline = get_pipeline(kind, reader, window_end=run.started_at.date())
    return env, reader
