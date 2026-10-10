"""Review run driver: `Swarm` start, resume, cancel and the run state machine (impl 06 §3.8).

`Swarm.start` (U06-79) creates the run of a request, or continues the one its job already
created; `resume` (U06-80) applies the TH06-13 config-hash guard and the build guard, recovers
interrupted tasks and drives on; `cancel` (U06-81) is idempotent from any thread. `_drive`
(U06-82) advances the run through design 06 §5.1, reading the status from the database before
every step, so a kill at any step boundary resumes where it stopped. Every status change is a
compare-and-set (`set_run_status` with `allowed_from`). The per-run setup lives in the private
sibling `_run_env`, the status handlers in `_run_steps`.
"""

from __future__ import annotations

import asyncio
import functools
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from pydantic import ValidationError

from herness.core.errors import BudgetExceeded, ConfigError, HernessError, NotFound, RetryableError
from herness.core.jobs import cancel as cancel_job
from herness.core.jobs import recover_run_tasks
from herness.core.logging import get_logger
from herness.harness.blackboard import Blackboard
from herness.harness.swarm._run_env import EnvParts, Stopper, SwarmDeps, build_env
from herness.harness.swarm._run_steps import (
    Steps,
    build_result,
    cas,
    exit_large_scope,
    load_draft,
    persist_usage,
    release_running,
    retire_build,
    review_jobs,
)
from herness.harness.swarm.lifecycle import (
    OPEN_STATUSES,
    RunRequest,
    RunResult,
    create_run_record,
    handle_budget_exhausted,
    request_config_hash,
)
from herness.harness.swarm.planner import planner_dedup_key
from herness.harness.swarm.scheduler import CapTracker, PhaseRunner
from herness.store.ops import find_run_by_job, get_run, get_task_by_dedup

if TYPE_CHECKING:
    from herness.core.jobs import JobContext
    from herness.harness.findings import EntityCatalog
    from herness.harness.llm.registry import LLMRegistry
    from herness.harness.memory import MemoryStore
    from herness.harness.pipelines.settings import PipelinesConfig
    from herness.harness.swarm.routing import RunEnv
    from herness.store.ops import RunRow

__all__ = ["Swarm", "SwarmDeps"]

_log = get_logger("harness.swarm")

TERMINAL: Final = frozenset({"done", "partial", "failed", "canceled"})
_STAGED: Final = frozenset({"challenging", "verifying", "writing"})  # F06-14 re-entry
RUNS_METRIC: Final = "herness_harness_runs_total"
DURATION_METRIC: Final = "herness_harness_run_duration_seconds"


class Swarm:
    """Review run driver (U06-78, design 06 §3.1); no store handle (R-10, D06-27)."""

    def __init__(
        self,
        cfg: PipelinesConfig,
        llms: LLMRegistry,
        memory: MemoryStore,
        *,
        deps: SwarmDeps | None = None,
    ) -> None:
        if deps is None:
            from herness.core.config import get_config  # noqa: PLC0415 - default deps only

            deps = SwarmDeps.from_config(get_config())
        self._cfg, self._llms, self._memory, self._deps = cfg, llms, memory, deps
        self._writer = ThreadPoolExecutor(1, "herness-swarm-writer")  # one writer per Swarm
        self._cancel_flags: dict[str, threading.Event] = {}
        self._no_wildcards: set[str] = set()
        self._reports = Path(deps.hcfg.paths.data) / "reports"

    def close(self) -> None:
        """Shut down the writer thread and the warehouse handles of this Swarm."""
        self._writer.shutdown(wait=True)
        close_all = getattr(self._deps.warehouses, "close_all", None)
        if callable(close_all):
            close_all()

    def _board(self) -> Blackboard:
        """A Blackboard used only for its writer (run creation, resume and cancel writes)."""
        return Blackboard(
            "", build_id="", catalog=cast("EntityCatalog", None), allowed_numerals=(),
            writer=self._writer,
        )  # fmt: skip

    async def start(self, req: RunRequest, job: JobContext | None = None) -> RunResult:
        """Start a new review run, or continue the one this job created (U06-79)."""
        if req.kind == "chat":
            msg = "Swarm.start takes review requests only; chat runs use ChatService"
            raise ConfigError(msg)
        if job is not None:
            existing = await asyncio.to_thread(find_run_by_job, job.job_id)
            if existing is not None:
                return await self.resume(existing.run_id, job)
        run = await asyncio.to_thread(
            functools.partial(
                create_run_record, self._board(), self._deps.hcfg, req,
                job_id=None if job is None else job.job_id, escalated_from=None,
                now=self._deps.clock(), current_build=self._deps.current_build,
            )
        )  # fmt: skip
        _log.info(
            "harness.run.created", run_id=run.run_id, kind=run.kind, depth=run.depth,
            profile=run.profile, build_id=run.build_id, job_id=run.meta.get("job_id"),
        )  # fmt: skip
        return await self._drive(run, job, force=False)

    async def resume(
        self, run_id: str, job: JobContext | None = None, *, force: bool = False
    ) -> RunResult:
        """Continue a run from database state (U06-80, design 06 §5.1 Resume, §6.2; TH06-13)."""
        run = await asyncio.to_thread(get_run, run_id)
        if run is None:
            msg = f"run not found: run_id={run_id}"
            raise NotFound(msg)
        if run.status in TERMINAL and not (force and run.status == "canceled"):
            return await asyncio.to_thread(build_result, run, self._reports)
        if self._config_hash(run) != run.config_hash and not force:
            _log.warning("harness.run.resume_refused", run_id=run_id, reason="config_changed")
            msg = f"config changed since run start: run_id={run_id}"
            raise ConfigError(msg)
        if run.build_id is None or not self._deps.build_exists(run.build_id):
            await retire_build(self._board(), run, self._deps.clock())
            msg = f"build retired; start a new run: run_id={run_id}"
            raise ConfigError(msg)
        board = self._board()
        if force and run.status == "canceled":
            await cas(board, run_id, "canceled", "planning", {"canceled"}, self._deps.clock())
        key = planner_dedup_key(run_id, run.kind)
        planner = await asyncio.to_thread(get_task_by_dedup, run_id, key)
        retry_dead = force and planner is not None and planner.status == "dead"
        recover = functools.partial(
            recover_run_tasks, run_id, max_task_attempts=self._cfg.swarm.max_task_attempts,
            retry_dead=retry_dead,
        )  # fmt: skip
        summary = await board.run_on_writer(recover)
        if retry_dead:
            self._no_wildcards.add(run_id)
        run = await asyncio.to_thread(get_run, run_id) or run
        _log.info(
            "harness.run.resumed", run_id=run_id, status=run.status,
            recovered={"running": summary.running_reset, "failed": summary.failed_reset,
                       "dead": summary.dead_reset},
        )  # fmt: skip
        return await self._drive(run, job, force=force)

    def _config_hash(self, run: RunRow) -> str:
        """`request_config_hash` of the stored request; a malformed request is a ConfigError."""
        try:
            req = RunRequest.model_validate(run.meta.get("request"))
        except ValidationError as exc:
            msg = f"stored run request invalid: run_id={run.run_id}"
            raise ConfigError(msg) from exc
        return request_config_hash(self._deps.hcfg, req, run.profile)

    async def cancel(self, run_id: str) -> None:
        """Cancel a run; idempotent and callable from any thread (U06-81, design 06 §6.4)."""
        flag = self._cancel_flags.get(run_id)
        if flag is not None:
            flag.set()
        run = await asyncio.to_thread(get_run, run_id)
        if run is None:
            msg = f"run not found: run_id={run_id}"
            raise NotFound(msg)
        for job_id in await asyncio.to_thread(review_jobs, run):
            await asyncio.to_thread(cancel_job, job_id)
        board = self._board()
        if run.status not in TERMINAL:
            now = self._deps.clock()
            await cas(board, run_id, run.status, "canceled", OPEN_STATUSES, now)
        _log.info("harness.run.canceled", run_id=run_id)

    async def _drive(self, run: RunRow, job: JobContext | None, *, force: bool) -> RunResult:
        """Advance `run` through design 06 §5.1 until terminal or stopped (U06-82)."""
        flag = self._cancel_flags.setdefault(run.run_id, threading.Event())
        stop = Stopper(run.run_id, flag, job)
        parts = EnvParts(
            cfg=self._cfg, llms=self._llms, memory=self._memory, deps=self._deps,
            writer=self._writer,
        )  # fmt: skip
        no_wild = force and run.run_id in self._no_wildcards
        env, reader = await asyncio.to_thread(
            functools.partial(build_env, parts, run, job, force=force, stop=stop,
                              no_wildcards=no_wild)
        )  # fmt: skip
        caps = CapTracker(
            self._cfg.swarm.max_inflight_children_per_parent,
            self._cfg.swarm.max_inflight_per_entity,
        )
        steps = Steps(
            env, PhaseRunner(env, caps), self._deps.render, self._deps.portfolio_fn, reader
        )
        try:
            return await self._loop(steps, stop)
        finally:
            await exit_large_scope(env)
            env.tracer.close()
            self._cancel_flags.pop(run.run_id, None)
            self._no_wildcards.discard(run.run_id)

    async def _loop(self, steps: Steps, stop: Stopper) -> RunResult:
        """Read the status, run its step, repeat (U06-82 steps 2-4 and 8)."""
        env, last = steps.env, steps.env.run.status
        while True:
            run = await asyncio.to_thread(get_run, env.run.run_id)
            if run is None:
                msg = f"run not found: run_id={env.run.run_id}"
                raise NotFound(msg)
            env.run, last = run, self._observe(run, last)
            if run.status in TERMINAL:
                return await self._finish(env)
            if stop():
                return await self._stopped(env, stop)
            await self._enter_stage(env)
            await self._guarded(steps, run.status, stop)

    async def _guarded(self, steps: Steps, status: str, stop: Stopper) -> None:
        """One step with the U06-82 step 8 error rules; a stop-induced cancel just returns."""
        env = steps.env
        try:
            await self._step(steps, status)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if (task is not None and task.cancelling()) or not stop():
                raise
        except RetryableError:
            raise  # spec 08 requeues; resume continues (no status change)
        except BudgetExceeded:
            if status not in {"running", "challenging"}:
                await self._fail(env, status, "BudgetExceeded")
                raise
            await handle_budget_exhausted(
                env.bb, env.run.run_id, max_task_attempts=self._cfg.swarm.max_task_attempts,
                now=env.clock(),
            )  # fmt: skip
        except HernessError as err:
            await self._fail(env, status, type(err).__name__)
            raise

    @staticmethod
    def _observe(run: RunRow, last: str) -> str:
        """Log a status change made inside a step (planner commit, budget, record)."""
        if run.status not in {last, "canceled"}:
            _log.info(
                "harness.run.status_changed", run_id=run.run_id, **{"from": last, "to": run.status}
            )
        return run.status

    async def _step(self, steps: Steps, status: str) -> None:
        handlers = {
            "created": steps.created, "planning": steps.planning, "running": steps.running,
            "challenging": steps.challenging, "verifying": steps.verifying,
            "writing": steps.writing, "recording": steps.recording,
        }  # fmt: skip
        handler = handlers.get(status)
        if handler is None:
            msg = f"unknown run status {status}: run_id={steps.env.run.run_id}"
            raise ConfigError(msg)
        if status in {"running", "challenging", "writing"} and not await steps.ensure_plan_ctx():
            msg = f"plan context unavailable: run_id={steps.env.run.run_id}"
            raise ConfigError(msg)
        await handler()

    async def _enter_stage(self, env: RunEnv) -> None:
        """F06-14 step 3: a resumed large stage re-enters the GPU scope before continuing."""
        run = env.run
        if env.large_scope is not None or env.job is None or run.status not in _STAGED:
            return
        if run.meta.get("stage") != "large" or run.profile != "local" or not env.knobs.large_stage:
            return
        scope = env.job.gpu_scope("large")
        await asyncio.to_thread(scope.__enter__)
        env.large_scope = scope

    async def _fail(self, env: RunEnv, status: str, error_type: str) -> None:
        """U06-82 step 8: CAS to `failed` from any open status (a terminal run never flips)."""
        if await cas(env.bb, env.run.run_id, status, "failed", OPEN_STATUSES, env.clock()):
            _log.error("harness.run.failed", run_id=env.run.run_id, error_type=error_type)
        await persist_usage(env)

    async def _stopped(self, env: RunEnv, stop: Stopper) -> RunResult:
        """U06-82 step 4: cancel → `canceled`; preempt or shutdown → no status change."""
        await release_running(env)
        await persist_usage(env)
        run = env.run
        if stop.reason() == "cancel":
            if await cas(env.bb, run.run_id, run.status, "canceled", OPEN_STATUSES, env.clock()):
                _log.info("harness.run.canceled", run_id=run.run_id)
            return await self._finish(env)
        return await asyncio.to_thread(build_result, run, self._reports)

    async def _finish(self, env: RunEnv) -> RunResult:
        """U06-82 steps 6-7: usage, finished log, metrics, escalation summary, result."""
        await persist_usage(env)
        run = await asyncio.to_thread(get_run, env.run.run_id) or env.run
        result = await asyncio.to_thread(build_result, run, self._reports)
        cov = result.coverage
        _log.info(
            "harness.run.finished", run_id=run.run_id, status=run.status,
            planned=cov.planned_tasks, done=cov.done_tasks, dead=cov.dead_tasks,
            verified=cov.verified_findings, rejected=cov.rejected_findings,
            cost_usd=str(run.cost_usd), tokens=run.token_usage.get("by_role", {}),
        )  # fmt: skip
        metrics = self._deps.metrics
        metrics.record_counter(
            RUNS_METRIC, component="harness", labels={"kind": run.kind, "status": run.status}
        )
        if run.finished_at is not None:
            seconds = max(0.0, (run.finished_at - run.started_at).total_seconds())
            labels = {"kind": run.kind, "depth": run.depth}
            metrics.record_histogram(DURATION_METRIC, seconds, component="harness", labels=labels)
        escalated = isinstance(run.meta.get("escalated_from"), Mapping)
        if escalated and run.status in {"done", "partial"}:
            await self._post_summary(run)
        return result

    async def _post_summary(self, run: RunRow) -> None:
        """U06-135 for an escalated run; skipped (logged) while no binding exists (T06-24)."""
        poster = self._deps.post_summary
        if poster is None:
            _log.warning("harness.escalation.summary_unbound", run_id=run.run_id)
            return
        draft = await asyncio.to_thread(load_draft, run.run_id, self._reports)
        await asyncio.to_thread(poster, run, draft)
