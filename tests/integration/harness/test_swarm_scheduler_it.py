"""IT06-08, IT06-24, IT06-25: the phase scheduler over the real store, writer and agent loop.

A migrated ops store with the SQLite jobs backend and one `running` review run (`bb_env`), the
real `Blackboard` writer, `TaskSlots`, `CallGate`, `RunBudget`, `SpawnBroker` and spec 08 task
helpers. IT06-08 and IT06-25 run the real `run_agent` with real `HarnessHooks` (T06-13
`build_hooks`) over an in-process client that counts concurrent calls; IT06-24 replaces only
`run_agent` with scripted agents (partial, failed, raising, hanging, cancelled).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from pydantic import JsonValue
from tests.support import loop_standin as ls
from tests.support.dispatch_standin import SyncTool, use_test_config
from tests.support.harness_fakes import FakeOps, FakeVectors, RecordingTracer
from tests.unit.harness import _blackboard_env as env_mod
from tests.unit.harness._blackboard_env import BbEnv

from herness.core import config as c
from herness.core import redact as r
from herness.core import time as clock
from herness.core.errors import BudgetExceeded, FatalError, ModelUnavailable
from herness.core.ids import IdKind, new_id
from herness.core.jobs import claim_task, complete_task
from herness.core.resilience import ProcessState, bind_ops_backend, process_state
from herness.core.types import (
    AgentResult,
    EntityScope,
    LLMRequest,
    LLMResponse,
    ServiceName,
    TaskBudget,
    TaskInputs,
    TaskSpec,
    ToolCall,
    Usage,
)
from herness.harness import tools as tools_mod
from herness.harness import warehouse_tools as wt
from herness.harness.budget import RunBudget
from herness.harness.findings import EntityCatalog, compute_dedup_key
from herness.harness.gates import CallGate, TaskSlots
from herness.harness.llm.registry import LLMRegistry
from herness.harness.pipelines.settings import PipelinesConfig, SwarmSettings, resolve_knobs
from herness.harness.roles import base
from herness.harness.roles.base import get_role
from herness.harness.swarm import _routing_ctx, scheduler
from herness.harness.swarm.routing import RunEnv, default_tools
from herness.harness.swarm.scheduler import CapTracker, PhaseRunner, execute_task
from herness.harness.swarm.spawn import SpawnBroker
from herness.store.ops import get_findings, get_run, get_task, insert_tasks, run_write
from herness.store.ops.resilience import SqliteResilienceBackend
from herness.store.ops.runs import TaskRow

pytestmark = pytest.mark.integration

bb_env = env_mod.bb_env  # fixtures
test_redactor = env_mod.test_redactor
CLIENT = "local-30b"
ANALYSTS = frozenset({"analyst"})
FINAL: dict[str, JsonValue] = {"summary": "looked", "unknowns": [], "suggested_followups": []}
SPAWN_MARK = "SPAWN-A-CHILD"
SWARM_TOOLS = ["list_findings", "post_finding", "request_subtask"]


# --- fakes


class CountingClient:
    """In-process `LLMClient` answering every call with the analyst final; it counts the calls
    in flight (peak) and can ask for one `request_subtask` call from the marked parent."""

    def __init__(self, delay: float = 0.02) -> None:
        self.name = CLIENT
        self.delay = delay
        self.in_flight = self.peak = self.calls = 0
        self.spawned = False
        self.requests: list[LLMRequest] = []

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        self.in_flight += 1
        self.calls += 1
        self.requests.append(req)
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        if not self.spawned and req.tools and SPAWN_MARK in req.model_dump_json():
            self.spawned = True
            args: dict[str, JsonValue] = {
                "objective": "incidents of team t2", "specialty": "ops", "entity_type": "team",
                "entity_ids": ["t2"], "reason": "follow the incident trail",
            }  # fmt: skip
            call = ToolCall(id="call_1", name="request_subtask", arguments=args)
            return ls.resp(calls=[call], client=CLIENT)
        return ls.resp(json.dumps(FINAL), parsed=FINAL, client=CLIENT)

    def complete(self, req: LLMRequest) -> LLMResponse:
        raise NotImplementedError


class _Gpu:
    def loaded_class(self) -> Literal["reasoning"]:
        return "reasoning"

    def service_healthy(self, name: ServiceName) -> bool:
        del name
        return True


class _Memory:
    def compactor(self, profile: object, *, ctx: object) -> None:
        del profile, ctx


@dataclass
class _Job:
    notes: list[str | None] = field(default_factory=list)

    def heartbeat(self, note: str | None = None) -> None:
        self.notes.append(note)


class RecordingCaps(CapTracker):
    """`CapTracker` recording the start order and the peak of running agents."""

    def __init__(self, children: int = 2, entity: int = 2) -> None:
        super().__init__(children, entity)
        self.started: list[str] = []
        self.running = self.peak = 0

    def start(self, row: TaskRow) -> None:
        super().start(row)
        self.started.append(row.task_id)
        self.running += 1
        self.peak = max(self.peak, self.running)

    def finish(self, row: TaskRow) -> None:
        super().finish(row)
        self.running -= 1


@dataclass
class Sched:
    """One scheduler setup: the env, its client, its stop flag and the Blackboard env."""

    bb: BbEnv
    env: RunEnv
    client: CountingClient
    stop: list[bool]
    job: _Job

    def runner(self, caps: CapTracker) -> PhaseRunner:
        return PhaseRunner(self.env, caps)


def _spec(  # noqa: PLR0913 - test builder
    run_id: str,
    *,
    objective: str = "look",
    role: str = "analyst",
    ids: tuple[str, ...] = ("t1",),
    priority: float = 0.0,
    max_tokens: int = 20_000,
    wall_clock_s: int = 600,
    **kw: Any,
) -> TaskSpec:
    scope = EntityScope(entity_type="team", entity_ids=list(ids))
    task_id = new_id(IdKind.TASK)
    tools = kw.pop("tools", SWARM_TOOLS)  # the swarm task tools only: no warehouse
    return TaskSpec(
        task_id=task_id,
        run_id=run_id,
        role=role,  # type: ignore[arg-type]
        objective=objective,
        scope=scope,
        tools=tools if role == "analyst" else [],
        budget=TaskBudget(max_steps=10, max_tokens=max_tokens, wall_clock_s=wall_clock_s),
        priority=priority,
        model_role=role,
        dedup_key=compute_dedup_key(role, "general", scope, task_id),
        **kw,
    )


def _insert(*specs: TaskSpec) -> list[str]:
    return run_write(lambda conn: insert_tasks(conn, list(specs), now=clock.now()), op="test")


def _task(task_id: str) -> TaskRow:
    row = get_task(task_id)
    assert row is not None
    return row


@pytest.fixture
def sched(
    reset_process_state: ProcessState,
    bb_env: BbEnv,
    test_redactor: r.Redactor,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Sched]:
    """Test config, tmp prompts, a real registry routed to `CountingClient`, a `RunEnv` with
    gate size 2 and 4 task slots; the fixture's own claimed task is completed first."""
    del reset_process_state
    hcfg = use_test_config(tmp_path / "cfg")
    bind_ops_backend(SqliteResilienceBackend())
    monkeypatch.setattr(r._State, "redactor", test_redactor)  # the config reset dropped it
    prompts = ls.write_prompts(tmp_path / "prompts", monkeypatch)
    (prompts / "analyst_general.md").write_text("# Analyst\nInvestigate.\n", encoding="utf-8")
    (prompts / "analyst_ops.md").write_text("# Ops analyst\nInvestigate.\n", encoding="utf-8")
    monkeypatch.setattr(base, "_catalog_describe", list)
    monkeypatch.setattr(_routing_ctx, "gpu_state", _Gpu)
    # A fresh tool registry with every analyst tool (never called by the fake model).
    monkeypatch.setattr(tools_mod._Process, "registry", tools_mod.ToolRegistry())
    wt.register_warehouse_tools()
    for name in ("recall_memory", "propose_memory"):
        tools_mod.tool_registry().register(SyncTool(name), owner="07")
    complete_task(bb_env.task_id, {"summary": "fixture"})
    client = CountingClient()
    llms = LLMRegistry(hcfg.models, profile="test", egress_enabled=True)
    monkeypatch.setattr(llms, "client", lambda name: client)
    run = get_run(bb_env.run_id)
    assert run is not None
    knobs = resolve_knobs(PipelinesConfig(), "org_review", "standard")
    analysis = RunBudget("analysis", 10_000_000, run_id=run.run_id)
    writer = RunBudget("writer", 1_000_000, run_id=run.run_id)
    tracer = RecordingTracer(run.run_id)
    stop: list[bool] = [False]
    wake = asyncio.Event()
    catalog = EntityCatalog(bb_env.warehouse)
    job = _Job()
    env = RunEnv(
        run=run, knobs=knobs, cfg=PipelinesConfig(), hcfg=hcfg, llms=llms,
        memory=_Memory(),  # type: ignore[arg-type]
        verifier=None,  # type: ignore[arg-type]
        bb=bb_env.bb, catalog=catalog, gates={CLIENT: CallGate(CLIENT, 2)}, slots=TaskSlots(4),
        broker=None, pipeline=None,  # type: ignore[arg-type]
        analysis=analysis, writer=writer, tracer=tracer,  # type: ignore[arg-type]
        clock=clock.now, metrics=None,  # type: ignore[arg-type]
        stop=lambda: stop[0], wake=wake, job=job,  # type: ignore[arg-type]
        force=False, warehouses=_Warehouses(bb_env),  # type: ignore[arg-type]
        ops=FakeOps(), vectors=FakeVectors(),
    )  # fmt: skip
    env.broker = SpawnBroker(
        run_id=run.run_id, depth_mode="standard", knobs=knobs, settings=SwarmSettings(),
        budget=analysis, bb=bb_env.bb, catalog=catalog, tracer=tracer,
        wake=lambda: env.wake.set(),  # noqa: PLW0108 - env.wake is replaced per event loop
        clock=clock.now,
    )  # fmt: skip
    yield Sched(bb_env, env, client, stop, job)
    c.reset_config()
    for name in ("analyst_general", "analyst_ops"):
        get_role(name).__dict__.pop("_prompts", None)


class _Warehouses:
    def __init__(self, bb: BbEnv) -> None:
        self._bb = bb

    def get(self, build_id: str) -> object:
        del build_id
        return self._bb.warehouse


def _phase(s: Sched, caps: CapTracker, roles: frozenset[str] = ANALYSTS) -> Any:
    async def go() -> Any:
        s.env.wake = asyncio.Event()  # one event loop per asyncio.run
        return await s.runner(caps).run_phase(roles, ledger=s.env.analysis)  # type: ignore[arg-type]

    return asyncio.run(go())


# --- IT06-08


def test_it06_08_gate_two_bounds_concurrent_llm_calls(sched: Sched) -> None:
    """IT06-08 gate 2, 10 tasks, 4 slots: at most 2 concurrent LLM calls (counted in the
    client) while up to 4 agents run; tasks start in design 06 §5.4 order (priority desc)."""
    specs = [
        _spec(sched.bb.run_id, objective=f"team {i}", ids=(f"t{i}",), priority=float(i * 10))
        for i in range(10)
    ]
    _insert(*specs)
    caps = RecordingCaps()
    out = _phase(sched, caps)
    assert out.blocked == frozenset()
    assert out.stopped is False
    assert sched.client.peak == 2  # measured inside the client
    assert sched.client.calls == 20  # one turn + one structured final per task
    assert caps.peak > 2  # more agents than the gate allowed calls
    expected = [s.task_id for s in sorted(specs, key=lambda s: -s.priority)]
    assert caps.started == expected
    for spec in specs:
        row = _task(spec.task_id)
        assert row.status == "done"
        assert row.result is not None
        assert row.result["partial"] is False
        assert row.attempts == 1
    assert sched.env.by_role["analyst"]["input"] > 0
    assert len(sched.job.notes) == 10


def test_it06_08_entity_cap_serializes_one_entity(sched: Sched) -> None:
    """IT06-08 per-entity cap 1: tasks on one entity run one at a time, still in order."""
    specs = [_spec(sched.bb.run_id, objective=f"t1 {i}", priority=float(i)) for i in range(3)]
    _insert(*specs)
    caps = RecordingCaps(entity=1)
    _phase(sched, caps)
    assert caps.peak == 1
    assert caps.started == [s.task_id for s in reversed(specs)]


def test_it06_08_admission_blocks_and_phase_ends(sched: Sched) -> None:
    """IT06-08 a task refused by budget admission is reported blocked and stays pending."""
    big = _spec(sched.bb.run_id, objective="big", max_tokens=80_000_000)
    small = _spec(sched.bb.run_id, objective="small", ids=("t2",))
    _insert(big, small)
    out = _phase(sched, RecordingCaps())
    assert out.blocked == frozenset({big.task_id})
    assert _task(big.task_id).status == "pending"
    assert _task(small.task_id).status == "done"


# --- IT06-25


def test_it06_25_child_spawned_during_phase_runs_in_same_phase(sched: Sched) -> None:
    """IT06-25 a parent requests a sub-task through the real tool and broker; the child runs
    in the same phase and the phase ends only when no task is open."""
    knobs = sched.env.knobs
    tools = default_tools("analyst", "general", "standard", child_depth=0, knobs=knobs)
    parent = _spec(sched.bb.run_id, objective=f"{SPAWN_MARK} team t1", tools=tools)
    _insert(parent)
    caps = RecordingCaps()
    out = _phase(sched, caps)
    assert sched.client.spawned
    assert out == scheduler.PhaseOutcome(frozenset(), False)
    assert len(caps.started) == 2
    child = _task(caps.started[1])
    assert child.parent_task_id == parent.task_id
    assert child.status == "done"
    done = _task(parent.task_id)
    assert done.status == "done"
    assert done.result is not None
    assert done.result["subtasks"] == [child.task_id]


def test_it06_25_stop_ends_the_phase(sched: Sched) -> None:
    """IT06-25 a stop before the first pass returns `stopped` without starting a task."""
    spec = _spec(sched.bb.run_id)
    _insert(spec)
    sched.stop[0] = True
    out = _phase(sched, RecordingCaps())
    assert out.stopped is True
    assert _task(spec.task_id).status == "pending"


# --- IT06-24 (scripted agents)

type _Agent = Callable[..., Any]


def _result(status: str, stop: str, finding_ids: list[str] | None = None) -> AgentResult:
    return AgentResult(
        status=status,  # type: ignore[arg-type]
        stop_reason=stop,  # type: ignore[arg-type]
        output={"summary": status},
        steps=2,
        usage=Usage(input_tokens=10, output_tokens=5),
        cost_usd=Decimal(0),
        query_ids=[],
        finding_ids=finding_ids or [],
    )


def _scripted(
    monkeypatch: pytest.MonkeyPatch,
    behaviours: dict[str, _Agent],
    ctxs: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Replace `run_agent` with the behaviour named by the task objective's first word; the
    task's `ToolContext` is kept in `ctxs` under that word."""
    calls: dict[str, int] = {}

    async def fake(role: Any, task_input: Any, ctx: Any, *args: Any, **kw: Any) -> AgentResult:
        del role, task_input, args, kw
        row = _task(ctx.task_id)
        key = row.spec.objective.split()[0]
        calls[key] = calls.get(key, 0) + 1
        if ctxs is not None:
            ctxs[key] = ctx
        result: AgentResult = await behaviours[key]()
        return result

    monkeypatch.setattr(scheduler, "run_agent", fake)
    return calls


async def _hang() -> AgentResult:
    await asyncio.sleep(3600)
    raise AssertionError


def test_it06_24_end_states_per_section_6(sched: Sched, monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-24 partial → done partial; failed → dead; RetryableError → pending, retried, dead
    after 3 attempts; FatalError and BudgetExceeded → dead; hanging agent → wall-clock
    backstop, done with partial and stop_cause wall_clock."""

    async def partial() -> AgentResult:
        return _result("partial", "task_budget")

    async def failed() -> AgentResult:
        return _result("failed", "error")

    async def retryable() -> AgentResult:
        msg = "model down"
        raise ModelUnavailable(msg)

    async def fatal() -> AgentResult:
        msg = "broken"
        raise FatalError(msg)

    async def budget() -> AgentResult:
        msg = "run budget exhausted"
        raise BudgetExceeded(msg)

    ctxs: dict[str, Any] = {}
    posted: list[str] = []

    async def hang_after_posting() -> AgentResult:
        posted.append(sched.bb.bb.post(ctxs["hang"], **env_mod.args(sched.bb.ops_qid)))
        return await _hang()

    calls = _scripted(
        monkeypatch,
        {"partial": partial, "failed": failed, "retry": retryable, "fatal": fatal,
         "budget": budget, "hang": hang_after_posting},
        ctxs,
    )  # fmt: skip
    monkeypatch.setattr(scheduler, "WALL_CLOCK_GRACE_S", 0)
    run_id = sched.bb.run_id
    specs = {
        name: _spec(run_id, objective=f"{name} task", ids=(f"t{i}",), wall_clock_s=1)
        for i, name in enumerate(("partial", "failed", "retry", "fatal", "budget", "hang"))
    }
    _insert(*specs.values())
    out = _phase(sched, RecordingCaps())
    assert out.blocked == frozenset()
    rows = {name: _task(spec.task_id) for name, spec in specs.items()}
    assert rows["partial"].status == "done"
    assert rows["partial"].result is not None
    assert rows["partial"].result["partial"] is True
    assert rows["failed"].status == "dead"
    assert rows["failed"].last_error is not None
    assert rows["failed"].last_error["class"] == "FatalError"
    assert (rows["retry"].status, rows["retry"].attempts, calls["retry"]) == ("dead", 3, 3)
    assert rows["fatal"].status == "dead"
    assert rows["budget"].status == "dead"
    assert rows["budget"].last_error is not None
    assert rows["budget"].last_error["class"] == "BudgetExceeded"
    hang = rows["hang"]
    assert hang.status == "done"
    assert hang.result is not None
    assert (hang.result["partial"], hang.result["stop_cause"]) == (True, "wall_clock")
    assert len(posted) == 1
    assert hang.result["finding_ids"] == posted  # read back from the database
    assert sched.env.slots.free() == 4


def test_it06_24_cancelled_agent_released_without_attempt(
    sched: Sched, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-24 a stop raises CancelledError at a step boundary: the task is released to
    pending with no attempt charged (R-36) and the phase returns stopped."""

    async def cancelled() -> AgentResult:
        sched.stop[0] = True
        raise asyncio.CancelledError

    _scripted(monkeypatch, {"cancel": cancelled})
    spec = _spec(sched.bb.run_id, objective="cancel me")
    _insert(spec)
    out = _phase(sched, RecordingCaps())
    assert out.stopped is True
    row = _task(spec.task_id)
    assert (row.status, row.attempts) == ("pending", 0)
    assert sched.env.slots.free() == 4


def test_it06_24_outer_cancellation_releases_and_reraises(
    sched: Sched, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-24 an outer `task.cancel()` releases the task and re-raises the cancellation."""
    _scripted(monkeypatch, {"hang": _hang})
    spec = _spec(sched.bb.run_id, objective="hang forever")
    _insert(spec)
    caps = RecordingCaps()

    async def go() -> None:
        assert await sched.env.bb.run_on_writer(lambda: claim_task(spec.task_id))
        await sched.env.slots.acquire()
        row = _task(spec.task_id)
        caps.start(row)
        task = asyncio.create_task(execute_task(sched.env, row, sched.env.analysis, caps))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    row = _task(spec.task_id)
    assert (row.status, row.attempts) == ("pending", 0)
    assert caps.running == 0


def _finding(env: BbEnv) -> str:
    return env.bb.post(env.ctx(), **env_mod.args(env.ops_qid))


def test_it06_24_revision_withdrawn_and_dead(sched: Sched, monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-24 a revision task that posts nothing withdraws its finding in the completing
    transaction; a dead revision task rejects its finding with `revision_dead`."""
    bbe = sched.bb
    bbe.task_id = env_mod.add_running_task(bbe.run_id, n=7)
    withdrawn = _finding(bbe)
    killed = bbe.bb.post(bbe.ctx(), **env_mod.args(bbe.ops_qid, entity_id="t2", claim="T2 [[n1]]"))
    assert withdrawn != killed  # two distinct findings: each rejection is asserted on its own
    complete_task(bbe.task_id, {"summary": "poster"})

    async def done() -> AgentResult:
        return _result("completed", "final")

    async def fatal() -> AgentResult:
        msg = "broken"
        raise FatalError(msg)

    _scripted(monkeypatch, {"revise": done, "doomed": fatal})
    a = _spec(bbe.run_id, objective="revise it", revision_of=withdrawn, round=1)
    b = _spec(bbe.run_id, objective="doomed revision", ids=("t2",), revision_of=killed, round=1)
    _insert(a, b)
    _phase(sched, RecordingCaps())
    row = _task(a.task_id)
    assert row.status == "done"
    assert row.result is not None
    assert row.result["withdrawn"] is True
    assert _task(b.task_id).status == "dead"
    assert get_findings([withdrawn])[withdrawn].status == "rejected"  # withdrawn, same tx
    assert get_findings([killed])[killed].status == "rejected"  # revision_dead after dead


def test_it06_24_seam_roles_and_invalid_checkpoint(
    sched: Sched, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-24 a skeptic task without its T06-17 executor ends dead; a registered executor is
    called; an invalid loop checkpoint fails the task; resources are released every time."""
    run_id = sched.bb.run_id
    fid = "fnd_01HZX3K7M20000000000000000"
    skeptic = _spec(
        run_id, role="skeptic", objective="skeptic check", round=1,
        inputs=TaskInputs(finding_ids=[fid]),
    )  # fmt: skip
    _insert(skeptic)
    out = _phase(sched, RecordingCaps(), frozenset({"skeptic"}))
    assert out.blocked == frozenset()
    assert _task(skeptic.task_id).status == "dead"
    key = (scheduler.TASKS_METRIC, (("outcome", "dead"), ("role", "skeptic")), "harness")
    assert process_state().metric_buffer.counters.get(key) == 1.0

    seen: list[str] = []

    async def executor(env: RunEnv, row: TaskRow, ledger: RunBudget) -> None:
        del ledger
        seen.append(row.task_id)
        await env.bb.run_on_writer(lambda: complete_task(row.task_id, {"summary": "ok"}))

    monkeypatch.setitem(scheduler._ROLE_EXECUTORS, "verifier", executor)
    verifier = _spec(run_id, role="verifier", objective="verify batch", max_tokens=1_000)
    _insert(verifier)
    _phase(sched, RecordingCaps(), frozenset({"verifier"}))
    assert seen == [verifier.task_id]
    assert _task(verifier.task_id).status == "done"

    spec = _spec(run_id, objective="resume me")
    _insert(spec)
    run_write(
        lambda conn: conn.execute(
            "UPDATE task SET checkpoint = ? WHERE task_id = ?",
            ('{"schema_version": 1, "loop": {"bad": true}}', spec.task_id),
        ),
        op="test_checkpoint",
    )
    _phase(sched, RecordingCaps())
    row = _task(spec.task_id)
    assert row.status == "dead"
    assert row.last_error is not None
    assert row.last_error["class"] == "SchemaViolation"
    assert sched.env.slots.free() == 4


def test_it06_24_off_network_writer_never_runs_generic_path(
    sched: Sched, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-24 an off-network writer task on the generic path ends dead before any model call
    (TH06-06: its evidence pack is built by its owner step, U06-115)."""
    monkeypatch.setattr(scheduler, "route_task", lambda *a, **kw: SimpleNamespace(off_network=True))
    spec = _spec(sched.bb.run_id, role="writer", objective="write the report")
    _insert(spec)
    _phase(sched, RecordingCaps(), frozenset({"writer"}))
    row = _task(spec.task_id)
    assert row.status == "dead"
    assert sched.client.calls == 0


def test_it06_24_foreign_timeout_is_not_the_backstop(
    sched: Sched, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-24 a TimeoutError raised by the agent itself (the backstop did not expire) is not
    recorded as a wall-clock partial: it propagates out of the phase; resources are released."""

    async def foreign() -> AgentResult:
        raise TimeoutError

    _scripted(monkeypatch, {"foreign": foreign})
    spec = _spec(sched.bb.run_id, objective="foreign timeout")
    _insert(spec)
    with pytest.raises(ExceptionGroup) as info:
        _phase(sched, RecordingCaps())
    assert info.group_contains(TimeoutError)
    row = _task(spec.task_id)
    assert (row.status, row.result) == ("running", None)
    assert sched.env.slots.free() == 4
