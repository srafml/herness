"""Phase scheduler and task executor of the swarm (impl 06 U06-92 to U06-95, design 06 §5.3).

`PhaseRunner.run_phase` starts pending tasks in design 06 §5.4 order under the task slots, the
caps (`CapTracker`) and budget admission (`budget_admits`) until none is open (TH06-03).
`execute_task` runs one claimed task and records its end state through the spec 08 task
helpers on the Blackboard writer (design 06 §6.1, R-36 for cancellation).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import sqlite3
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, cast

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import FatalError, HernessError
from herness.core.jobs import claim_task, complete_task, fail_task, release_task
from herness.core.logging import get_logger
from herness.core.resilience import fault_point
from herness.core.resilience.metrics import record_counter, record_histogram
from herness.core.types import Depth, LoopCheckpoint, Role, TaskSpec
from herness.harness.blackboard import Blackboard
from herness.harness.loop import run_agent
from herness.harness.swarm.routing import (
    RunEnv,
    build_hooks,
    build_task_input,
    build_tool_context,
    map_agent_result,
    route_task,
)
from herness.harness.swarm.tools import build_task_tools
from herness.store.ops import count_open, list_task_findings, ready_tasks, select_tasks

if TYPE_CHECKING:
    from herness.core.types import AgentResult
    from herness.harness.budget import RunBudget
    from herness.store.ops.runs import TaskRow

__all__ = [
    "WALL_CLOCK_GRACE_S",
    "CapTracker",
    "PhaseOutcome",
    "PhaseRunner",
    "budget_admits",
    "execute_task",
]

_log = get_logger("harness.swarm")

WALL_CLOCK_GRACE_S: Final = 30  # backstop after the loop's own deadline (R-22)
WAKE_POLL_S: Final = 5.0  # one scheduling pass per wake or per 5 s
TASKS_METRIC: Final = "herness_harness_tasks_total"
DURATION_METRIC: Final = "herness_harness_task_duration_seconds"
_EXEMPT: Final = "verifier"  # no model calls: always admitted, never capped
_EVIDENCE_PACK_ROLES: Final = frozenset({"skeptic", "writer"})

type _Outcome = Literal["done", "partial", "pending", "dead", "released"]
type _Executor = Callable[[RunEnv, "TaskRow", "RunBudget"], Awaitable[None]]

# Skeptic (U06-108) and verifier (U06-114) tasks run in `challenge`, which imports this module:
# it is imported on the first dispatch of such a task (`_executor`), never at the top.
_CHALLENGE_ROLES: Final = frozenset({"skeptic", "verifier"})
_ROLE_EXECUTORS: dict[str, _Executor] = {}


def _executor(role: str) -> _Executor | None:
    if role in _CHALLENGE_ROLES and role not in _ROLE_EXECUTORS:
        from herness.harness.swarm import challenge  # noqa: PLC0415 - import cycle

        _ROLE_EXECUTORS.setdefault("skeptic", challenge.execute_skeptic)
        _ROLE_EXECUTORS.setdefault(
            "verifier", lambda env, row, _: challenge.execute_verifier(env, row)
        )
    return _ROLE_EXECUTORS.get(role)


class _Backstop(Exception):  # noqa: N818 - internal control flow, never escapes this module
    """The `asyncio.timeout` around `run_agent` expired (stdlib `TimeoutError`, R-22)."""


@dataclass(frozen=True, slots=True)
class PhaseOutcome:
    """Result of one phase: tasks refused by budget admission, and whether a stop ended it."""

    blocked: frozenset[str]
    stopped: bool


def budget_admits(ledger: RunBudget, spec: TaskSpec, admit_fraction: float) -> bool:
    """Budget admission (U06-94): enough of the phase ledger left for a fraction of the task.

    Verifier tasks always pass (no model calls).
    """
    if spec.role == _EXEMPT:
        return True
    if ledger.exhausted:
        return False
    remaining = ledger.snapshot()["tokens_remaining"]
    return isinstance(remaining, int) and remaining >= admit_fraction * spec.budget.max_tokens


class CapTracker:
    """Per-parent and per-entity in-flight caps (U06-95, design 06 §5.4); event loop only."""

    def __init__(self, max_children_per_parent: int, max_per_entity: int) -> None:
        self._max_children = max_children_per_parent
        self._max_entity = max_per_entity
        self._children: Counter[str] = Counter()
        self._entities: Counter[tuple[str, str]] = Counter()

    @staticmethod
    def _entity_keys(row: TaskRow) -> list[tuple[str, str]]:
        scope = row.spec.scope
        return [(scope.entity_type, entity_id) for entity_id in scope.entity_ids]

    def can_start(self, row: TaskRow) -> bool:
        """Parent below its children cap and every scope entity below its cap."""
        if row.role == _EXEMPT:
            return True
        parent = row.parent_task_id
        if parent is not None and self._children[parent] >= self._max_children:
            return False
        return all(self._entities[key] < self._max_entity for key in self._entity_keys(row))

    def start(self, row: TaskRow) -> None:
        """Count `row` as running."""
        if row.role == _EXEMPT:
            return
        if row.parent_task_id is not None:
            self._children[row.parent_task_id] += 1
        self._entities.update(self._entity_keys(row))

    def finish(self, row: TaskRow) -> None:
        """Stop counting `row`; counters never go below zero."""
        if row.role == _EXEMPT:
            return
        if row.parent_task_id is not None:
            self._children -= Counter({row.parent_task_id: 1})
        self._entities -= Counter(self._entity_keys(row))


class PhaseRunner:
    """Runs the pending tasks of some roles to quiescence (U06-92)."""

    def __init__(self, env: RunEnv, caps: CapTracker) -> None:
        self._env = env
        self._caps = caps

    async def run_phase(self, roles: frozenset[Role], *, ledger: RunBudget) -> PhaseOutcome:
        """Schedule passes until no task of `roles` is open (blocked ones excluded) or a stop.

        Running tasks end inside the TaskGroup; after a stop they end at their next step
        boundary through the hooks' stop callback.
        """
        env, run_id = self._env, self._env.run.run_id
        blocked: set[str] = set()
        async with asyncio.TaskGroup() as tg:
            while not env.stop():
                await self._pass(tg, roles, ledger, blocked)
                left = await asyncio.to_thread(count_open, run_id, roles, exclude=blocked)
                if left == 0:
                    break
                with contextlib.suppress(TimeoutError):  # normal: re-check stop and changes
                    await asyncio.wait_for(env.wake.wait(), timeout=WAKE_POLL_S)
                env.wake.clear()
        return PhaseOutcome(frozenset(blocked), env.stop())

    async def _pass(
        self,
        tg: asyncio.TaskGroup,
        roles: frozenset[Role],
        ledger: RunBudget,
        blocked: set[str],
    ) -> None:
        """One scheduling pass over the ready tasks in design 06 §5.4 order."""
        env, caps = self._env, self._caps
        aging = env.cfg.swarm.aging_per_min
        rows = await asyncio.to_thread(
            ready_tasks, env.run.run_id, roles, now=env.clock(), aging_per_min=aging
        )
        for row in rows:
            if env.slots.free() <= 0:
                return
            if row.task_id in blocked or not caps.can_start(row):
                continue
            if not budget_admits(ledger, row.spec, env.cfg.swarm.admit_fraction):
                blocked.add(row.task_id)
                continue
            if not await env.bb.run_on_writer(functools.partial(claim_task, row.task_id)):
                continue
            fault_point("swarm.after_task_claim", role=row.role)
            await env.slots.acquire()
            caps.start(row)
            tg.create_task(execute_task(env, row, ledger, caps))


async def execute_task(env: RunEnv, row: TaskRow, ledger: RunBudget, caps: CapTracker) -> None:
    """Execute one claimed task and record its end state (U06-93).

    Slot, caps and wake are always released, and the job heartbeat sent, in `finally`.
    """
    try:
        executor = _executor(row.role)
        if executor is not None:
            await executor(env, row, ledger)
        else:
            await _guarded(env, row, ledger)
    finally:
        env.slots.release()
        caps.finish(row)
        env.wake.set()
        if env.job is not None:
            env.job.heartbeat(f"task {row.task_id}")


async def _guarded(env: RunEnv, row: TaskRow, ledger: RunBudget) -> None:
    """Steps 2 to 6 of U06-93 with the end state of every failure (design 06 §6.1)."""
    started = clock.monotonic()
    outcome: _Outcome
    try:
        outcome = await _run_agent_task(env, row, ledger)
    except _Backstop:  # the wall-clock backstop fired
        outcome = await _complete_timed_out(env, row)
    except asyncio.CancelledError:
        await env.bb.run_on_writer(functools.partial(release_task, row.task_id))  # R-36
        _record(row, "released", started)
        task = asyncio.current_task()
        if task is not None and task.cancelling() > 0:
            raise  # outer cancellation
        return
    except HernessError as err:  # BudgetExceeded is a FatalError: dead
        outcome = await _fail(env, row, err)
    _record(row, outcome, started)


async def _run_agent_task(env: RunEnv, row: TaskRow, ledger: RunBudget) -> _Outcome:
    spec = row.spec
    depth = cast("Depth", env.run.depth)
    route = route_task(spec, llms=env.llms, depth=depth, profile=env.run.profile, ledger=ledger)
    if route.off_network and spec.role in _EVIDENCE_PACK_ROLES:
        # The evidence pack (U06-123) needs the pseudonymizer its owner step builds (U06-108,
        # U06-115); the generic path never sends such a task off-network (TH06-06).
        msg = f"off-network {spec.role} task needs an evidence pack: task_id={spec.task_id}"
        raise FatalError(msg)
    tools = build_task_tools(spec, bb=env.bb, broker=env.broker)
    ctx = build_tool_context(env, spec, route, task_tools=tools, ledger=ledger, now=env.clock())
    hooks = build_hooks(env, spec, route, ctx)
    # JSON in, JSON out: the wrapped copy of the spec's JSON dump (U06-141).
    task_input = cast(
        "dict[str, JsonValue]", build_task_input(spec.role, spec.model_dump(mode="json"))
    )
    resume = _resume_from(row.checkpoint)
    agent = (route.role_spec, task_input, ctx, route.client, route.config, hooks)
    try:
        async with asyncio.timeout(spec.budget.wall_clock_s + WALL_CLOCK_GRACE_S) as backstop:
            res = await run_agent(*agent, resume_from=resume)
    except TimeoutError:
        if backstop.expired():
            raise _Backstop from None
        raise  # any other TimeoutError is not the backstop: it propagates (ENG §3.4)
    _add_usage(env, spec.role, res)
    if res.status == "failed":
        msg = f"agent failed: task_id={spec.task_id} stop_reason={res.stop_reason}"
        return await _fail(env, row, FatalError(msg))
    result = map_agent_result(res, subtasks=await asyncio.to_thread(_child_ids, row))
    writes = None
    if spec.revision_of is not None and not res.finding_ids:
        result["withdrawn"] = True
        writes = functools.partial(_withdraw, spec.revision_of)
    await env.bb.run_on_writer(functools.partial(complete_task, row.task_id, result, writes=writes))
    _completed(env, row, result)
    return "partial" if res.status == "partial" else "done"


async def _complete_timed_out(env: RunEnv, row: TaskRow) -> _Outcome:
    """Backstop timeout: the task is `done` with `partial = true` (design 06 §6.1)."""
    findings = await asyncio.to_thread(list_task_findings, row.task_id)
    result: dict[str, object] = {
        "summary": "",
        "finding_ids": [f.finding_id for f in findings],
        "partial": True,
        "stop_cause": "wall_clock",
        "tokens": {"input": 0, "output": 0},
        "cost_usd": "0",
        "subtasks": await asyncio.to_thread(_child_ids, row),
    }
    await env.bb.run_on_writer(functools.partial(complete_task, row.task_id, result))
    _completed(env, row, result)
    return "partial"


async def _fail(env: RunEnv, row: TaskRow, err: HernessError) -> _Outcome:
    """`fail_task` on the writer; a dead revision task rejects its finding (`revision_dead`)."""
    attempts = env.cfg.swarm.max_task_attempts
    outcome = await env.bb.run_on_writer(
        functools.partial(fail_task, row.task_id, err, max_task_attempts=attempts)
    )
    _log.warning(
        "harness.task.failed",
        run_id=row.run_id,
        task_id=row.task_id,
        role=row.role,
        error_type=type(err).__name__,
        outcome=outcome,
    )
    if outcome == "dead" and row.spec.revision_of is not None:
        await env.bb.reject(row.spec.revision_of, "revision_dead")
    return outcome


def _withdraw(finding_id: str, conn: sqlite3.Connection) -> None:
    """A revision task that committed no finding withdraws the finding it revised."""
    Blackboard.tx_reject(conn, finding_id, "withdrawn", None, None)


def _resume_from(checkpoint: Mapping[str, object] | None) -> LoopCheckpoint | None:
    """The spec 05 loop checkpoint when the envelope has a `loop` key (R-21, R-29)."""
    if checkpoint is None or "loop" not in checkpoint:
        return None
    return LoopCheckpoint.from_envelope(cast("Mapping[str, JsonValue]", checkpoint))


def _child_ids(row: TaskRow) -> list[str]:
    return [t.task_id for t in select_tasks(row.run_id) if t.parent_task_id == row.task_id]


def _add_usage(env: RunEnv, role: str, res: AgentResult) -> None:
    usage = res.usage
    tokens_in = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    with env.by_role_lock:
        acc = env.by_role.setdefault(role, {})
        acc["input"] = acc.get("input", 0) + tokens_in
        acc["output"] = acc.get("output", 0) + usage.output_tokens


def _completed(env: RunEnv, row: TaskRow, result: Mapping[str, object]) -> None:
    _log.info(
        "harness.task.completed",
        run_id=env.run.run_id,
        task_id=row.task_id,
        role=row.role,
        partial=result["partial"],
        stop_cause=result["stop_cause"],
        tokens=result["tokens"],
    )


def _record(row: TaskRow, outcome: _Outcome, started: float) -> None:
    labels = {"role": row.role, "outcome": outcome}
    record_counter(TASKS_METRIC, component="harness", labels=labels)
    elapsed = max(0.0, clock.monotonic() - started)
    record_histogram(DURATION_METRIC, elapsed, component="harness", labels={"role": row.role})
