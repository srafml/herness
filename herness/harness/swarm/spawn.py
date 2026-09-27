"""Spawn broker: approve or deny Analyst sub-task requests (impl 06 U06-65, design 06 §5.5).

Rules run in design order; the first failure is the denial reason (TH06-02, TH06-03). Approval
inserts one `pending` task row on the Blackboard writer and wakes the scheduler (no subprocess;
the parent never waits). Every decision emits a `spawn_decision` trace event of structured values
only (never model text, TH06-09) and increments `herness_harness_spawn_decisions_total`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast, get_args

from pydantic import ValidationError

from herness.core.ids import IdKind, new_id
from herness.core.resilience.metrics import record_counter
from herness.core.types import (
    Depth,
    EntityScope,
    ScopeEntityType,
    Specialty,
    TaskInputs,
    TaskSpec,
    TraceEmitter,
)
from herness.harness.blackboard import Blackboard
from herness.harness.budget import RunBudget
from herness.harness.findings import EntityCatalog, compute_dedup_key
from herness.harness.pipelines.settings import DepthKnobs, SwarmSettings
from herness.harness.swarm.routing import default_tools
from herness.store.ops import count_tasks, get_task_by_dedup, insert_tasks, run_write

__all__ = ["SpawnBroker", "SpawnDecision"]

METRIC: Final = "herness_harness_spawn_decisions_total"
MAX_SCOPE_ENTITIES: Final = 50
PRIORITY_FACTOR: Final = 0.9
OBJECTIVE_MAX: Final = 2_000
_SPECIALTIES: Final = frozenset(get_args(Specialty.__value__))
_ENTITY_TYPES: Final = frozenset(get_args(ScopeEntityType.__value__))


@dataclass(frozen=True, slots=True)
class SpawnDecision:
    """Outcome of one `request_subtask` call; `task_id` is set exactly when approved."""

    approved: bool
    reason: str | None
    task_id: str | None


@dataclass(frozen=True, slots=True)
class _Request:
    """The checked `request_subtask` arguments."""

    objective: str
    specialty: Specialty
    scope: EntityScope
    reason: str


@dataclass(frozen=True, slots=True)
class _Outcome:
    """A denial `reason` (with the `dedup_key` once computed) or the approved `child`."""

    reason: str | None = None
    key: str | None = None
    child: TaskSpec | None = None


def _parse(parent: TaskSpec, args: Mapping[str, object]) -> _Request | None:
    """Rule 6 shape check of the arguments (already schema-checked by dispatch)."""
    ids, entity_type = args.get("entity_ids"), args.get("entity_type")
    objective, specialty, reason = args.get("objective"), args.get("specialty"), args.get("reason")
    if not isinstance(ids, list) or len(ids) > MAX_SCOPE_ENTITIES or not isinstance(objective, str):
        return None
    if not 1 <= len(objective) <= OBJECTIVE_MAX:
        return None
    if entity_type not in _ENTITY_TYPES or specialty not in _SPECIALTIES:
        return None
    period = {"period_start": parent.scope.period_start, "period_end": parent.scope.period_end}
    try:
        scope = EntityScope.model_validate(
            {"entity_type": entity_type, "entity_ids": ids, **period}
        )
    except ValidationError:
        return None
    text = reason if isinstance(reason, str) else ""
    return _Request(objective, cast("Specialty", specialty), scope, text)


class SpawnBroker:
    """Approves Analyst sub-tasks for one run, one decision at a time (U06-65)."""

    def __init__(  # noqa: PLR0913 - U06-65 signature
        self,
        *,
        run_id: str,
        depth_mode: Depth,
        knobs: DepthKnobs,
        settings: SwarmSettings,
        budget: RunBudget,
        bb: Blackboard,
        catalog: EntityCatalog,
        tracer: TraceEmitter,
        wake: Callable[[], None],
        clock: Callable[[], datetime],
    ) -> None:
        self.run_id = run_id
        self._mode, self._knobs, self._settings = depth_mode, knobs, settings
        self._budget, self._bb, self._catalog = budget, bb, catalog
        self._tracer, self._wake, self._clock = tracer, wake, clock
        self._lock = asyncio.Lock()

    async def request(self, parent: TaskSpec, args: Mapping[str, object]) -> SpawnDecision:
        """Check the rules in order; approve by inserting a `pending` child and waking."""
        async with self._lock:
            out = await self._decide(parent, args)
            if out.child is not None:
                self._wake()
                decision = SpawnDecision(approved=True, reason=None, task_id=out.child.task_id)
            else:
                decision = SpawnDecision(approved=False, reason=out.reason, task_id=None)
            self._record(parent, args, decision, out.key)
            return decision

    def _check_caps(self, parent: TaskSpec) -> str | None:
        """Rules 1-5: spawn enabled, depth, children of the parent, tasks in the run, budget."""
        knobs, run_id = self._knobs, self.run_id
        if knobs.max_spawn_depth == 0:
            return "spawn_disabled"
        if parent.depth + 1 > knobs.max_spawn_depth:
            return "max_depth"
        children = count_tasks(run_id, parent_task_id=parent.task_id, roles={"analyst"})
        if children >= self._settings.max_children_per_task:
            return "max_children"
        if count_tasks(run_id, roles={"analyst", "skeptic"}) >= knobs.max_tasks_per_run:
            return "max_tasks"
        child_tokens = parent.budget.scaled(self._settings.child_budget_factor).max_tokens
        remaining = self._budget.snapshot()["tokens_remaining"]
        if not isinstance(remaining, int) or remaining < child_tokens:
            return "budget"  # the writer ledger is a separate RunBudget: never touched here
        return None

    async def _decide(self, parent: TaskSpec, args: Mapping[str, object]) -> _Outcome:
        if (cap := self._check_caps(parent)) is not None:
            return _Outcome(reason=cap)
        req = _parse(parent, args)
        if req is None or self._catalog.missing(req.scope.entity_type, req.scope.entity_ids):
            return _Outcome(reason="bad_scope")
        key = compute_dedup_key("analyst", req.specialty, req.scope, req.objective)
        if (existing := get_task_by_dedup(self.run_id, key)) is not None:
            return _Outcome(reason=f"duplicate:{existing.task_id}", key=key)
        tools = default_tools(
            "analyst", req.specialty, self._mode, child_depth=parent.depth + 1, knobs=self._knobs
        )
        if not set(tools) <= set(parent.tools):
            return _Outcome(reason="tool_escalation", key=key)
        child = TaskSpec(
            task_id=new_id(IdKind.TASK),
            run_id=self.run_id,
            role="analyst",
            specialty=req.specialty,
            objective=req.objective,
            scope=req.scope,
            inputs=TaskInputs(notes=req.reason[:1_500] or None),
            tools=tools,
            budget=parent.budget.scaled(self._settings.child_budget_factor),
            depth=parent.depth + 1,
            parent_task_id=parent.task_id,
            priority=parent.priority * PRIORITY_FACTOR,
            model_role="analyst",
            dedup_key=key,
            round=parent.round,
            k_samples=1,
        )
        now = self._clock()

        def insert() -> list[str]:
            return run_write(lambda conn: insert_tasks(conn, [child], now=now), op="spawn_task")

        if not await self._bb.run_on_writer(insert):  # lost race on (run_id, dedup_key)
            existing = get_task_by_dedup(self.run_id, key)
            lost = "duplicate" if existing is None else f"duplicate:{existing.task_id}"
            return _Outcome(reason=lost, key=key)
        return _Outcome(key=key, child=child)

    def _record(
        self,
        parent: TaskSpec,
        args: Mapping[str, object],
        decision: SpawnDecision,
        dedup_key: str | None,
    ) -> None:
        """Trace event and counter of one decision (enums only when they are valid values)."""
        specialty, entity_type = args.get("specialty"), args.get("entity_type")
        ids = args.get("entity_ids")
        self._tracer.emit(
            "spawn_decision",
            task_id=parent.task_id,
            parent_task_id=parent.task_id,
            approved=decision.approved,
            reason=decision.reason,
            child_task_id=decision.task_id,
            depth=parent.depth + 1,
            dedup_key=dedup_key,
            specialty=specialty if specialty in _SPECIALTIES else None,
            entity_type=entity_type if entity_type in _ENTITY_TYPES else None,
            n_entities=len(ids) if isinstance(ids, list) else 0,
        )
        rule = "none" if decision.reason is None else decision.reason.partition(":")[0]
        labels = {"approved": "true" if decision.approved else "false", "reason": rule}
        record_counter(METRIC, component="harness", labels=labels)
