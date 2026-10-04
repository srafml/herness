"""Run lifecycle helpers of the swarm (impl 06 U06-76, U06-77, U06-83, U06-84, U06-86, U06-137).

`RunRequest` and `RunResult` are the request and outcome models of design 06 §3.1;
`create_run_record` writes a `created` run and its `pending` planner task in one transaction;
`request_config_hash` is the `run.config_hash` that resume compares (TH06-13);
`handle_budget_exhausted` applies design 06 §6.3 when the `analysis` ledger runs out; and
`swarm_health` is the `herness doctor` check (ENG §4). Store access goes through the ops area
functions and the spec 08 task helpers, writes on the Blackboard writer thread. Clocks, the
promoted build and the job queue are injected, so every function here reads no clock itself.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from herness.core.config import config_hash
from herness.core.errors import BudgetExceeded, ConfigError, HernessError, NotFound
from herness.core.ids import IdKind, canonical_json, new_id, new_ulid, sha256_hex
from herness.core.jobs.tasks import claim_task, fail_task
from herness.core.logging import get_logger
from herness.core.types import Coverage, Depth, EntityScope, RunKind, TaskSpec
from herness.harness.findings import compute_dedup_key
from herness.harness.pipelines.settings import DepthKnobs, ReviewKind, resolve_knobs
from herness.harness.swarm.routing import default_tools, role_budget
from herness.metrics.portfolio import Scenario
from herness.store.ops import (
    RunRow,
    insert_run,
    insert_tasks,
    run_write,
    select_runs,
    select_tasks,
    set_run_status,
)

if TYPE_CHECKING:
    import sqlite3

    from herness.core.config import HernessConfig
    from herness.core.jobs import JobRow
    from herness.harness.blackboard import Blackboard

__all__ = [
    "RunRequest",
    "RunResult",
    "create_run_record",
    "handle_budget_exhausted",
    "request_config_hash",
    "swarm_health",
]

REVIEW_KINDS: Final = frozenset({"funding_review", "org_review"})
OPEN_STATUSES: Final = frozenset(
    {"created", "planning", "running", "challenging", "verifying", "writing", "recording"}
)
STALL_AFTER: Final = timedelta(hours=24)  # stalled-run threshold (U06-86)
JOBS_LIMIT: Final = 50
_EXHAUST_ROLES: Final = frozenset({"analyst", "skeptic"})
_EXHAUST_FROM: Final = frozenset({"running", "challenging"})
_HASHED: Final = frozenset(
    {"kind", "depth", "question", "focus", "scenarios", "budget_override"}
)  # plus the resolved profile (U06-83)
_BUILD_ID: Final = r"^\d{8}-\d{6}-[0-9A-HJKMNP-TV-Z]{6}$"

_log = get_logger("harness.swarm")

type _ScenarioName = Annotated[str, Field(min_length=1, max_length=64)]
type Health = dict[str, str]


class RunRequest(BaseModel):
    """A review or chat-escalation request (U06-76, design 06 §3.1)."""

    model_config = ConfigDict(extra="forbid", strict=False)

    kind: RunKind
    depth: Depth = "standard"
    profile: Literal["local", "hybrid", "premium", "synth"] | None = None
    question: str | None = Field(default=None, max_length=2_000)
    focus: EntityScope | None = None
    scenarios: list[Scenario | _ScenarioName] = Field(default=[], max_length=5)
    build_id: str | None = Field(default=None, pattern=_BUILD_ID)
    session_id: str | None = Field(default=None, max_length=64)
    budget_override: dict[str, int] | None = None

    @model_validator(mode="after")
    def _chat_needs_question(self) -> Self:
        if self.kind == "chat" and not self.question:
            msg = "a chat request needs a question"
            raise ValueError(msg)
        return self


class RunResult(BaseModel):
    """Outcome of `Swarm.start` / `Swarm.resume` (U06-77); built by U06-82 `_result`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    status: str
    draft_path: str | None
    coverage: Coverage
    dead_task_ids: list[str]
    token_usage: dict[str, object]
    cost_usd: Decimal


def request_config_hash(cfg: HernessConfig, req: RunRequest, profile: str) -> str:
    """`cfg_` + 16 hex of the effective config plus the behaviour-changing request fields.

    U06-83: `request.profile` is the resolved `profile` argument. Pure and deterministic.
    """
    request = req.model_dump(mode="json", include=set(_HASHED))
    request["profile"] = profile
    payload = canonical_json({"config": config_hash(cfg), "request": request})
    return "cfg_" + sha256_hex(payload)[:16]


def _planner_spec(run_id: str, kind: ReviewKind, depth: Depth, knobs: DepthKnobs) -> TaskSpec:
    """The planner task of a new run (U06-137 step 6)."""
    scope = EntityScope(entity_type="run", entity_ids=[run_id])
    objective = f"Plan the {kind} review."
    return TaskSpec(
        task_id=new_id(IdKind.TASK),
        run_id=run_id,
        role="planner",
        objective=objective,
        scope=scope,
        tools=default_tools("planner", "general", depth, child_depth=0, knobs=knobs),
        budget=role_budget("planner", knobs, writer_tokens=0),
        model_role="planner",
        dedup_key=compute_dedup_key("planner", "general", scope, objective),
    )


def create_run_record(  # noqa: PLR0913 - U06-137 signature
    bb_writer: Blackboard,
    cfg: HernessConfig,
    req: RunRequest,
    *,
    job_id: str | None,
    escalated_from: Mapping[str, object] | None,
    now: datetime,
    current_build: Callable[[], str | None],
) -> RunRow:
    """Create a `created` run and its `pending` planner task in one transaction (U06-137).

    Knobs and the build are resolved first: `ConfigError` (bad kind or override) and
    `NotFound` (no promoted build, R-19) leave the store untouched.
    """
    if req.kind not in REVIEW_KINDS:
        msg = f"create_run_record needs a review kind, got {req.kind}"
        raise ConfigError(msg)
    kind = cast("ReviewKind", req.kind)
    profile = req.profile or cfg.profile
    knobs = resolve_knobs(cfg.pipelines, kind, req.depth, override=req.budget_override)
    build_id = req.build_id or current_build()
    if build_id is None:
        msg = "no promoted warehouse build"
        raise NotFound(msg)
    run_id = "run_" + new_ulid()
    meta: dict[str, object] = {
        "request": req.model_dump(mode="json"),
        "stage": "main",
        "escalated_from": None if escalated_from is None else dict(escalated_from),
        "render_error": None,
        "blocked_reason": None,
        "job_id": job_id,  # D06-26
    }
    run = RunRow(
        run_id=run_id,
        kind=kind,
        depth=req.depth,
        profile=profile,
        build_id=build_id,
        status="created",
        started_at=now,
        finished_at=None,
        token_usage={},
        cost_usd=Decimal(0),
        config_hash=request_config_hash(cfg, req, profile),
        meta=meta,
    )
    planner = _planner_spec(run_id, kind, req.depth, knobs)

    def create(conn: sqlite3.Connection) -> None:
        insert_run(conn, run)  # a fresh ULID never collides; a clash would skip, not raise
        insert_tasks(conn, [planner], now=now)

    bb_writer.run_on_writer_sync(lambda: run_write(create, op="swarm_create_run"))
    return run


def _kill_pending(task_id: str, max_task_attempts: int) -> bool:
    """Claim one pending task and fail it with `BudgetExceeded("run_budget")` (a FatalError)."""
    if not claim_task(task_id):
        return False  # claimed elsewhere since the read: it ends through RunBudget.charge
    err = BudgetExceeded("run_budget")
    return fail_task(task_id, err, max_task_attempts=max_task_attempts) == "dead"


async def handle_budget_exhausted(
    bb: Blackboard, run_id: str, *, max_task_attempts: int, now: datetime
) -> int:
    """Kill the run's pending analyst and skeptic tasks and move it to `verifying` (U06-84).

    Running tasks are untouched: they end through `BudgetExceeded` from `RunBudget.charge`.
    Returns the number of tasks set `dead`.
    """
    pending = select_tasks(run_id, roles=_EXHAUST_ROLES, statuses={"pending"})
    dead = 0
    for task in pending:
        if await bb.run_on_writer(partial(_kill_pending, task.task_id, max_task_attempts)):
            dead += 1

    def to_verifying(conn: sqlite3.Connection) -> bool:
        return set_run_status(conn, run_id, "verifying", _EXHAUST_FROM, now=now)

    await bb.run_on_writer(lambda: run_write(to_verifying, op="swarm_budget_exhausted"))
    _log.warning("harness.budget.exhausted", run_id=run_id, phase="analysis", tasks_dead=dead)
    return dead


def _stalled(runs: Collection[RunRow], jobs: Collection[JobRow], now: datetime) -> str | None:
    """The first open run older than 24 h that no queued or running review job references."""
    job_ids = {job.job_id for job in jobs}
    run_ids = {job.payload.get("run_id") for job in jobs}
    for run in runs:
        if now - run.started_at <= STALL_AFTER or run.run_id in run_ids:
            continue
        if run.meta.get("job_id") not in job_ids:
            return run.run_id
    return None


def swarm_health(
    *,
    list_jobs: Callable[..., list[JobRow]],
    worker_alive: Callable[[], bool],
    now: datetime,
) -> Health:
    """`herness doctor` swarm check: `ok`, `degraded` or `down` with a reason (U06-86)."""
    try:
        runs = select_runs(statuses=OPEN_STATUSES, kinds=REVIEW_KINDS)
    except HernessError as exc:
        return {"status": "down", "reason": type(exc).__name__}
    if runs and not worker_alive():
        return {"status": "degraded", "reason": f"no live worker for {len(runs)} open runs"}
    try:
        jobs = [
            *list_jobs(status="queued", kind="review", limit=JOBS_LIMIT),
            *list_jobs(status="running", kind="review", limit=JOBS_LIMIT),
        ]
    except HernessError as exc:
        return {"status": "down", "reason": type(exc).__name__}
    if (stalled := _stalled(runs, jobs, now)) is not None:
        return {"status": "degraded", "reason": f"stalled run {stalled}"}
    return {"status": "ok", "reason": ""}
