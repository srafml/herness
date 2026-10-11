"""Swarm fault and hybrid security helpers (impl 06 T06-26; FT06-01..05, ST06-06, ST06-09).

`use_real_agents` turns the T06-22 `drv` set-up (`tests.integration.harness._swarm_run_env`:
real driver, steps, Blackboard and ops store; planted evidence, fake Verifier) into a run of
the real spec 05 loop on spec 11 scripts: every client key of the run's registry answers
through a `TaskClient`, a `FakeLLMClient` whose `ScriptBook` (R-65) is built per task
execution `(task_id, attempts)` from the task row, so a task re-executed after a kill starts
its script again (and would post its finding again). The turns that must name ids existing
only at run time (the Skeptic's finding id, the Writer's verified finding) are rendered when
the execution starts.

`SimulatedKill` is the in-process kill: `install_kill` makes the plan action `kill` raise it
(a `BaseException`, so no product handler catches it; it unwinds the event loop like a crash,
leaving claimed tasks `running` and the run status unchanged). `record_points` logs every
`fault_point` call with the run status the driver was stepping, so a test can aim a
count-based rule (`nth`) at one phase of an identical run.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.support.dispatch_standin import SyncTool
from tests.support.fake_llm import FakeLLMClient
from tests.unit.harness.memory._write_env import PATTERNS, make_writer

from herness.core.resilience import faults
from herness.core.types import SKEPTIC_CHECKS, LLMRequest, LLMResponse, TaskSpec
from herness.eval.scripted import LLMScript, ScriptBook
from herness.harness import loop
from herness.harness import tools as tools_mod
from herness.harness import warehouse_tools as wt
from herness.harness.memory import MemoryStore
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder
from herness.harness.swarm import _run_env, challenge, planner, scheduler, writer
from herness.harness.swarm import run as run_mod
from herness.harness.swarm.routing import default_tools
from herness.store.ops import core, get_task, query_findings, record_evidence, select_tasks

__all__ = [
    "PointLog",
    "SimulatedKill",
    "TaskScripts",
    "allow_spawn",
    "draft_view",
    "expect_kill",
    "finding_view",
    "install_kill",
    "real_memory",
    "record_points",
    "reports",
    "run_killed",
    "task_view",
    "use_real_agents",
]

_MEMORY_TOOLS: Final = ("propose_memory", "recall_memory")
REPEAT: Final = 5  # agent runs per task execution: best-of plans, judge and skeptic samples
SPAWN_ARGS: Final = {
    "objective": "Follow up the incident load of team t2.", "specialty": "ops",
    "entity_type": "team", "entity_ids": ["t2"], "reason": "t1 load may come from t2",
}  # fmt: skip


class SimulatedKill(BaseException):
    """Raised by the plan action `kill` instead of SIGKILL (in-process kill)."""


def install_kill(mp: pytest.MonkeyPatch, on_kill: Callable[[], None] | None = None) -> None:
    """Make the fault plan's `kill` action raise `SimulatedKill` in this process; `on_kill`
    runs first (to observe the state at the moment of the kill)."""

    def kill() -> None:
        if on_kill is not None:
            on_kill()
        msg = "simulated kill"
        raise SimulatedKill(msg)

    mp.setattr(faults, "_kill_process", kill)


def _has_kill(exc: BaseException) -> bool:
    if isinstance(exc, SimulatedKill):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_has_kill(inner) for inner in exc.exceptions)
    return False


def run_killed(make: Callable[[], Coroutine[Any, Any, object]]) -> None:
    """Run `make()` to completion and require it to end by `SimulatedKill`."""
    expect_kill(lambda: asyncio.run(make()))


def expect_kill(fn: Callable[[], object]) -> None:
    """Call `fn` and require it to end by `SimulatedKill`."""
    try:
        fn()
    except BaseException as exc:
        if not _has_kill(exc):
            raise
        return
    msg = "the run was not killed"
    raise AssertionError(msg)


# --- scripted agents ---------------------------------------------------------------------------


def _text(text: str) -> dict[str, Any]:
    return {"final": {"text": text}}


def _output(value: Mapping[str, Any]) -> dict[str, Any]:
    return {"final": {"output": dict(value)}}


def _post(team: str) -> dict[str, Any]:
    number = e._nref(1, e.incident_qid(team), "count", "incidents", 7) | {"format": None}
    args = {
        "claim": f"Team {team} had [[n1]] incidents", "entity_type": "team",
        "entity_id": team, "query_ids": [], "confidence": 0.8, "numbers": [number],
    }  # fmt: skip
    return {"tool_calls": [{"name": "post_finding", "arguments": args}]}


@dataclass
class TaskScripts:
    """Script books per task execution; counts the executions (books built) per task."""

    spawn_from: str | None = None  # dedup key of the analyst that requests one subtask
    skeptic_note: str = ""  # check note of every Skeptic verdict (planted text, ST06-06)
    executions: Counter[str] = field(default_factory=Counter)
    _books: dict[tuple[str, int], ScriptBook] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def book(self, task_id: str) -> ScriptBook:
        row = get_task(task_id)
        assert row is not None, task_id
        key = (task_id, row.attempts)
        with self._lock:
            found = self._books.get(key)
            if found is None:
                script = {"turns": self._turns(row.spec), "source": f"f06:{row.role}"}
                found = self._books[key] = ScriptBook([LLMScript.model_validate(script)])
                self.executions[task_id] += 1
            return found

    def _turns(self, spec: TaskSpec) -> list[dict[str, Any]]:
        if spec.role == "analyst":
            return self._analyst(spec)
        one = {
            "planner": self._planner, "judge": self._judge, "skeptic": self._skeptic,
            "writer": self._writer,
        }[spec.role](spec)  # fmt: skip
        return one * REPEAT

    def _analyst(self, spec: TaskSpec) -> list[dict[str, Any]]:
        done = _output({"summary": "looked", "unknowns": [], "suggested_followups": []})
        steps = [_post(spec.scope.entity_ids[0]), _text("posted"), done]
        if self.spawn_from is not None and spec.dedup_key == self.spawn_from:
            steps.insert(0, {"tool_calls": [{"name": "request_subtask", "arguments": SPAWN_ARGS}]})
        return steps

    @staticmethod
    def _planner(spec: TaskSpec) -> list[dict[str, Any]]:
        del spec
        task = {
            "dedup_key": e._det_keys()[0], "specialty": "general", "objective": "Frame it.",
            "entity_type": "team", "entity_ids": ["t1"], "notes": "framed",
        }  # fmt: skip
        plan = {"tasks": [task], "rationale": "deterministic", "unknowns": []}
        return [_text("plan"), _output(plan)]

    @staticmethod
    def _judge(spec: TaskSpec) -> list[dict[str, Any]]:
        del spec
        verdict = {"choice": 0, "scores": [4.0, 3.0, 2.0], "reasons": ["a", "b", "c"]}
        return [_text("judged"), _output(verdict)]

    def _skeptic(self, spec: TaskSpec) -> list[dict[str, Any]]:
        checks = [{"check": k, "result": "pass", "note": self.skeptic_note} for k in SKEPTIC_CHECKS]
        verdict = {
            "finding_id": spec.inputs.finding_ids[0], "checks": checks, "verdict": "uphold",
            "required_actions": [],
        }  # fmt: skip
        return [_text("checked"), _output(verdict)]

    @staticmethod
    def _writer(spec: TaskSpec) -> list[dict[str, Any]]:
        return [_text("draft"), _output(e.writer_output(spec.run_id))]


class TaskClient(FakeLLMClient):
    """`FakeLLMClient` under client key `name`, answering from the call's task execution."""

    def __init__(self, scripts: TaskScripts, name: str) -> None:
        unused = LLMScript.model_validate({"turns": [_text("-")], "source": "f06:none"})
        super().__init__(ScriptBook([unused]))
        self.name, self._scripts = name, scripts

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        task_id = req.metadata.task_id
        assert task_id is not None
        inner = FakeLLMClient(await asyncio.to_thread(self._scripts.book, task_id))
        inner.name = self.name
        return await inner.acomplete(req)


def use_real_agents(
    drv: e.Drv,
    mp: pytest.MonkeyPatch,
    scripts: TaskScripts | None = None,
    *,
    llms: Any = None,
    real: Mapping[str, object] | None = None,
) -> TaskScripts:
    """Run every role through the real spec 05 loop on `scripts` (see the module doc).

    `llms` is the registry whose `client` is routed (the `drv` registry by default); the
    client keys in `real` keep the given (real) client.
    """
    scripts = scripts or TaskScripts()
    for module in (planner, scheduler, challenge, writer):
        mp.setattr(module, "run_agent", loop.run_agent)
    mp.setattr(tools_mod._Process, "registry", tools_mod.ToolRegistry())
    wt.register_warehouse_tools()
    registry_ = tools_mod.tool_registry()
    for name in _MEMORY_TOOLS:  # spec 07 tools: never called by the scripts
        registry_.register(SyncTool(name), owner="07")
    registry = drv.llms if llms is None else llms
    clients: dict[str, object] = dict(real or {})

    def client(name: str) -> object:
        if name not in clients:
            clients[name] = TaskClient(scripts, name)
        return clients[name]

    mp.setattr(registry, "client", client)
    return scripts


def allow_spawn(drv: e.Drv, mp: pytest.MonkeyPatch) -> str:
    """Give the first planned t1 analyst task the default analyst tools (the stub pipeline's
    tasks carry `post_finding` only), so it may spawn a child whose tools are no wider
    (U06-101, TH06-02); returns that task's dedup key."""

    class Spawning(e.StubPipeline):
        def deterministic_tasks(self, ctx: Any) -> list[TaskSpec]:
            tasks = super().deterministic_tasks(ctx)
            tools = default_tools("analyst", "general", ctx.depth, child_depth=0, knobs=ctx.knobs)
            return [tasks[0].model_copy(update={"tools": tools}), *tasks[1:]]

    shape = drv.pipeline
    mp.setattr(
        _run_env, "get_pipeline",
        lambda kind, reader, window_end: Spawning(
            kind, shape["t1"], shape["t2"], t2_must=shape["t2_must"]
        ),
    )  # fmt: skip
    return e.det_spec("run_" + "0" * 26, "t1", 0, must=True).dedup_key


# --- fault point probe -------------------------------------------------------------------------


@dataclass
class PointLog:
    """`fault_point` calls as (run status being stepped, point, labels)."""

    calls: list[tuple[str | None, str, dict[str, str]]] = field(default_factory=list)

    def nth(self, point: str, phase: str, **labels: str) -> int:
        """1-based index, among the calls a rule on `point` + `labels` counts, of the first
        call made while the driver stepped `phase`."""
        wanted = labels.items()
        counted = [c for c in self.calls if c[1] == point and wanted <= c[2].items()]
        return 1 + next(i for i, c in enumerate(counted) if c[0] == phase)


def record_points(mp: pytest.MonkeyPatch) -> PointLog:
    """Log every `fault_point` call of an enabled plan (`fault_env`) with the stepped status."""
    log = PointLog()
    phase: list[str | None] = [None]
    select = faults._select

    def spy(plan: Any, name: str, labels: Mapping[str, str]) -> Any:
        log.calls.append((phase[0], name, dict(labels)))
        return select(plan, name, labels)

    step = run_mod.Swarm._step

    async def stepped(self: run_mod.Swarm, steps: Any, status: str) -> None:
        phase[0] = status
        await step(self, steps, status)

    mp.setattr(faults, "_select", spy)
    mp.setattr(run_mod.Swarm, "_step", stepped)
    return log


# --- comparisons -------------------------------------------------------------------------------


def finding_view(run_id: str) -> Counter[tuple[str | None, str, str, str]]:
    """The run's findings as a multiset of (author role, entity, claim, status)."""
    rows = query_findings(run_id, limit=500)
    return Counter((f.author_role, f.entity_id, f.claim, f.status) for f in rows)


def task_view(run_id: str) -> Counter[tuple[str, str, int, str, int]]:
    """The run's tasks as a multiset of (role, specialty, round, status, dedup-key count)."""
    rows = select_tasks(run_id)
    keys = Counter(t.spec.dedup_key for t in rows)
    return Counter(
        (t.role, t.spec.specialty, t.spec.round, t.status, keys[t.spec.dedup_key]) for t in rows
    )


def _norm(value: object, names: Mapping[str, str], run_id: str) -> object:
    if isinstance(value, dict):
        return {k: _norm(v, names, run_id) for k, v in value.items() if not k.endswith("_at")}
    if isinstance(value, list):
        return [_norm(v, names, run_id) for v in value]
    if isinstance(value, str):
        if value == run_id:
            return "<run>"
        if value.startswith("rec_"):
            return "<rec>"
        return names.get(value, value)
    return value


def draft_view(run_id: str, reports: Path) -> object:
    """`draft.json` with timestamps dropped, the run id and `rec_id`s masked and every
    finding id replaced by its finding's (author role, entity, claim)."""
    names = {
        f.finding_id: f"<{f.author_role}|{f.entity_id}|{f.claim}>"
        for f in query_findings(run_id, limit=500)
    }
    data = json.loads((reports / run_id / "draft.json").read_text(encoding="utf-8"))
    return _norm(data, names, run_id)


# --- real memory -------------------------------------------------------------------------------


def real_memory(drv: e.Drv, tmp_path: Path) -> MemoryStore:
    """The real spec 07 `MemoryStore` on the test's ops store (IT06-37 set-up), with the
    portfolio evidence the funding recommendation cites."""
    portfolio = e._evidence("SELECT 1 AS expected_impact_usd", {}, {"expected_impact_usd": 1200})
    assert record_evidence(portfolio.model_copy(update={"query_id": e.PORTFOLIO_QID}))
    env = make_writer(tmp_path)
    return MemoryStore(
        MemoryConfig(injection_patterns=PATTERNS), conn_factory=core.connection,
        vectors=env.vectors, embedder=Embedder(env.embed, model_name="bge-m3"),
        redactor=env.redactor, llms=None,
        allowed_numeral_patterns=drv.hcfg.app.reports.allowed_numeral_patterns,
        data_root=Path(drv.hcfg.paths.data),
    )  # fmt: skip


def reports(drv: e.Drv) -> Path:
    """The reports root of the `drv` set-up (`<paths.data>/reports`)."""
    return Path(drv.hcfg.paths.data) / "reports"
