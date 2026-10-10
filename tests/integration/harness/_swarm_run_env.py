"""Run-driver test environment (impl 06 T06-22): fixture `drv`, scripted agents, fakes.

`drv` gives a migrated ops store with the SQLite jobs and resilience backends, the full test
config (`paths.data` under `tmp_path`), a test redactor, a fake warehouse holding `core.team`
(t1, t2), two ops `evidence` rows of the build (an incident count per team and a
`score.funding` read for the Writer's recommendation), and a `SwarmDeps` of fakes (Verifier
that passes, recording renderer, metrics and tracer). Every swarm step runs for real except
`run_agent`, which `Agents` scripts per role: the Planner frames the deterministic tasks, each
Analyst records a real finding through the real `post_finding` tool and Blackboard, each
Skeptic upholds, and the Writer writes a draft whose single recommendation passes the U06-117
funding rules. The pipeline is a stub with deterministic analyst tasks per team (team t1 is
must-cover), and the plan context is fixed (its portfolio query id is the recommendation's
expected-USD source). Dedup runs in `query_set` mode (no embedding model).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

import pytest
from tests.support.dispatch_standin import use_test_config
from tests.support.harness_fakes import FakeOps, FakeVectors, RecordingTracer
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness import _blackboard_env as env_mod
from tests.unit.harness._blackboard_env import BUILD_ID, FakeWarehouse

from herness.core import config as c
from herness.core import redact as r
from herness.core.errors import FatalError, ModelUnavailable
from herness.core.ids import IdKind, new_id
from herness.core.ids import query_id as compute_query_id
from herness.core.jobs.ports import JobRow, bind_jobs_backend
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.types import (
    SKEPTIC_CHECKS,
    AgentResult,
    EntityScope,
    Evidence,
    Finding,
    PriorContext,
    RankedEntity,
    RecommendationDraft,
    ReportDraft,
    ServiceName,
    TaskSpec,
    Usage,
    VerificationResult,
)
from herness.harness.findings import compute_dedup_key
from herness.harness.llm.registry import LLMRegistry
from herness.harness.pipelines.base import (
    PlanContext,
    build_ranked_entities,
    to_recommendation_drafts,
)
from herness.harness.pipelines.settings import resolve_knobs
from herness.harness.roles import base
from herness.harness.swarm import (
    _routing_ctx,
    _run_env,
    _run_steps,
    challenge,
    dedup,
    planner,
    scheduler,
    writer,
)
from herness.harness.swarm.lifecycle import RunRequest
from herness.harness.swarm.run import Swarm, SwarmDeps
from herness.store.ops import query_findings, record_evidence
from herness.store.ops.jobs import SqliteJobsBackend
from herness.store.ops.resilience import SqliteResilienceBackend

bb_fixtures = (env_mod.test_redactor,)
T0: Final = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
INCIDENT_SQL: Final = "SELECT count(*) AS incidents FROM core.incident WHERE team_id = $team"
FUNDING_SQL: Final = (
    "SELECT effort_cost_usd, confidence FROM score.funding WHERE candidate_id = $candidate"
)
PORTFOLIO_QID: Final = "q_" + "a" * 16
ALL_STATUSES: Final = (
    "created", "planning", "running", "challenging", "verifying", "writing", "recording", "done",
)  # fmt: skip

type Mode = Literal["ok", "block", "fail", "budget"]


def incident_qid(team: str) -> str:
    return compute_query_id(INCIDENT_SQL, {"team": team}, BUILD_ID)


def funding_qid() -> str:
    return compute_query_id(FUNDING_SQL, {"candidate": "c1"}, BUILD_ID)


def _evidence(sql: str, params: dict[str, Any], sample: dict[str, Any]) -> Evidence:
    return Evidence(
        query_id=compute_query_id(sql, params, BUILD_ID), run_id=None, build_id=BUILD_ID,
        sql=sql, params=params, result_hash="0" * 64, row_count=1, result_sample=[sample],
        executed_at=T0, duration_ms=1,
    )  # fmt: skip


def det_spec(run_id: str, team: str, n: int, *, must: bool) -> TaskSpec:
    """Deterministic analyst task `n` on `team` (dedup keys do not depend on the run)."""
    scope = EntityScope(entity_type="team", entity_ids=[team])
    objective = f"Review the incident load of team {team} ({n})."
    return TaskSpec(
        task_id=new_id(IdKind.TASK), run_id=run_id, role="analyst", objective=objective,
        scope=scope, tools=["post_finding"],
        budget={"max_steps": 5, "max_tokens": 40_000, "wall_clock_s": 300},  # type: ignore[arg-type]
        priority=100.0 - n, model_role="analyst", must_cover=must,
        dedup_key=compute_dedup_key("analyst", "general", scope, objective),
    )  # fmt: skip


class StubPipeline:
    """Deterministic analyst tasks: `t1_tasks` on must-cover team t1 and `t2_tasks` on t2."""

    def __init__(self, kind: str, t1_tasks: int, t2_tasks: int, *, t2_must: bool) -> None:
        self.kind = kind
        self._t1, self._t2, self._t2_must = t1_tasks, t2_tasks, t2_must

    def deterministic_tasks(self, ctx: PlanContext) -> list[TaskSpec]:
        t1 = [det_spec(ctx.run_id, "t1", i, must=True) for i in range(self._t1)]
        t2 = [det_spec(ctx.run_id, "t2", 50 + i, must=self._t2_must) for i in range(self._t2)]
        return t1 + t2

    def must_cover(self, ctx: PlanContext) -> set[str]:
        del ctx
        return {"team:t1"} | ({"team:t2"} if self._t2_must and self._t2 else set())

    def planner_input(self, ctx: PlanContext, tasks: list[TaskSpec]) -> dict[str, object]:
        del ctx
        return {"deterministic": [t.dedup_key for t in tasks]}

    def challenge_priority(self, f: Finding, ctx: PlanContext) -> float:
        del ctx
        return f.confidence

    def writer_input(self, ctx: PlanContext, verified: list[Finding]) -> dict[str, object]:
        del ctx
        return {"findings": [f.finding_id for f in verified]}

    def ranked_entities(self, draft: ReportDraft) -> list[RankedEntity]:
        return build_ranked_entities(draft, "candidate", [])

    def recommendation_drafts(
        self, draft: ReportDraft, findings: dict[str, Finding]
    ) -> list[RecommendationDraft]:
        # Spec 07 `RecommendationDraft.target_type` has no `candidate` (a funding item's
        # target): the stub hands the candidate over as a work item (T06-22 report note).
        recs = [r.model_copy(update={"target_type": "work_item"}) for r in draft.recommendations]
        return to_recommendation_drafts(
            draft.model_copy(update={"recommendations": recs}), findings
        )


def plan_ctx_for(run: Any) -> PlanContext:
    knobs = resolve_knobs(c.get_config().pipelines, run.kind, run.depth)
    return PlanContext.model_validate(
        {
            "run_id": run.run_id, "kind": run.kind, "depth": run.depth, "profile": run.profile,
            "build_id": BUILD_ID, "question": None, "focus": None, "knobs": knobs,
            "dq_warnings": [], "unconfirmed_weights": [], "prior_context": "",
            "prior_recs": [],
            "portfolio": {"scenario": "base", "rows": [], "selected": [],
                          "query_ids": [PORTFOLIO_QID], "custom": []},
        }
    )  # fmt: skip


def _usage(tokens: int = 10) -> Usage:
    return Usage(input_tokens=tokens, output_tokens=5)


def result(output: dict[str, Any] | None, finding_ids: list[str] | None = None) -> AgentResult:
    return AgentResult(
        status="completed", stop_reason="final", output=output, steps=1, usage=_usage(),
        cost_usd=Decimal(0), query_ids=[], finding_ids=finding_ids or [],
    )  # fmt: skip


def _nref(n: int, qid: str, unit: str, column: str, value: Any) -> dict[str, Any]:
    return {"id": f"n{n}", "value": value, "unit": unit, "query_id": qid, "column": column,
            "row_key": None}  # fmt: skip


def writer_output(run_id: str) -> dict[str, Any]:
    """A Writer output citing the run's verified t1 finding and one passing fund item."""
    verified = query_findings(run_id, statuses={"verified"}, limit=500)
    f = next(x for x in verified if x.entity_id == "t1")
    para = {"text": "Team t1 had [[n1]] incidents", "numbers": [n.model_dump(mode="json")
            for n in f.numbers], "finding_ids": [f.finding_id]}  # fmt: skip
    rec = {
        "kind": "fund", "target_type": "candidate", "target_id": "c1",
        "headline": "Fund the cache rewrite", "summary": "It saves [[n1]] a year",
        "numbers": [_nref(1, PORTFOLIO_QID, "usd", "expected_impact_usd", "1200.50"),
                    _nref(2, funding_qid(), "usd", "effort_cost_usd", "300.00"),
                    _nref(3, funding_qid(), "score", "confidence", 0.7)],
        "expected_usd_ref": "n1", "effort_usd_ref": "n2", "confidence_ref": "n3",
        "finding_ids": [f.finding_id], "query_ids": [],
    }  # fmt: skip
    return {
        "title": "Funding review",
        "recommendations": [rec],
        "caveats": [],
        "sections": [{"id": "executive_summary", "title": "Summary", "paragraphs": [para]}],
        "prior_outcomes_commentary": None,
    }


@dataclass
class Agents:
    """Scripted `run_agent` per role; counts calls; modes per analyst team or role."""

    planner: Literal["ok", "fail"] = "ok"
    writer: Literal["ok", "fail"] = "ok"
    analyst: dict[str, Mode] = field(default_factory=dict)
    charge: int = 0  # tokens charged to the ledger per analyst call (IT06-03)
    usd: bool = False  # findings carry a usd number (cross-check planning, U06-110)
    calls: dict[str, int] = field(default_factory=dict)
    running: asyncio.Event | None = None

    async def __call__(  # noqa: PLR0913 - spec 05 run_agent signature
        self, role_spec: Any, task_input: Any, ctx: Any, client: Any, config: Any, hooks: Any,
        *, resume_from: Any = None,
    ) -> AgentResult:  # fmt: skip
        del role_spec, client, config, resume_from
        role = ctx.role
        self.calls[role] = self.calls.get(role, 0) + 1
        if role == "planner":
            return self._planner(task_input)
        if role == "analyst":
            return await self._analyst(ctx, hooks)
        if role == "judge":
            return result({"choice": 0, "scores": [4.0, 3.0, 2.0], "reasons": ["a", "b", "c"]})
        if role == "skeptic":
            return result({"checks": [{"check": k, "result": "pass", "note": ""}
                                      for k in SKEPTIC_CHECKS],
                           "verdict": "uphold", "required_actions": []})  # fmt: skip
        if role == "writer":
            if self.writer == "fail":
                msg = "writer failed"
                raise FatalError(msg)
            return result(writer_output(ctx.run_id))
        msg = f"unscripted role {role}"
        raise AssertionError(msg)

    def _planner(self, task_input: Any) -> AgentResult:
        if self.planner == "fail":
            msg = "planner model unavailable"
            raise ModelUnavailable(msg)
        keys = str(task_input)
        key = next(k for k in _det_keys() if k in keys)
        task = {"dedup_key": key, "specialty": "general", "objective": "Frame it.",
                "entity_type": "team", "entity_ids": ["t1"], "notes": "framed"}  # fmt: skip
        return result({"tasks": [task], "rationale": "deterministic", "unknowns": []})

    async def _analyst(self, ctx: Any, hooks: Any) -> AgentResult:
        team = ctx.task_id and _team_of(ctx.task_id)
        mode = self.analyst.get(team, "ok")
        if mode == "fail":
            msg = "analyst failed"
            raise FatalError(msg)
        if mode == "block":
            if self.running is not None:
                self.running.set()
            while not hooks._stop():  # noqa: ASYNC110 - polls stop like after_step
                await asyncio.sleep(0.01)
            raise asyncio.CancelledError  # spec 05 `after_step` on a stop
        if self.charge:
            ctx.ledger.charge(self.charge, 0, Decimal(0))
        args = {
            "claim": f"Team {team} had [[n1]] incidents", "entity_type": "team",
            "entity_id": team, "query_ids": [], "confidence": 0.8,
            "numbers": [_nref(1, incident_qid(team), "usd", "incidents", "1200.50") if self.usd
                        else _nref(1, incident_qid(team), "count", "incidents", 7)],
        }  # fmt: skip
        out = await asyncio.to_thread(lambda: ctx.task_tools["post_finding"](ctx, **args))
        assert out.ok, out
        posted = out.data["finding_id"] if isinstance(out.data, dict) else None
        return result({"summary": "looked", "unknowns": [], "suggested_followups": []},
                      [posted] if posted else [])  # fmt: skip


def _det_keys() -> list[str]:
    return [det_spec("run_" + "0" * 26, "t1", 0, must=True).dedup_key]


def _team_of(task_id: str) -> str:
    from herness.store.ops import get_task  # noqa: PLC0415

    row = get_task(task_id)
    assert row is not None
    return row.spec.scope.entity_ids[0]


class Tracer(RecordingTracer):
    """`RecordingTracer` with the `close` the driver calls."""

    closed = False

    def close(self, timeout_s: float = 5.0) -> None:
        del timeout_s
        self.closed = True


class Metrics:
    """Records `record_counter` / `record_histogram` calls."""

    def __init__(self) -> None:
        self.counters: list[tuple[str, dict[str, str]]] = []
        self.histograms: list[tuple[str, float, dict[str, str]]] = []

    def record_counter(
        self, name: str, value: float = 1.0, *, component: str,
        labels: dict[str, str] | None = None,
    ) -> None:  # fmt: skip
        del value, component
        self.counters.append((name, dict(labels or {})))

    def record_histogram(
        self, name: str, value: float, *, component: str, labels: dict[str, str] | None = None
    ) -> None:
        del component
        self.histograms.append((name, value, dict(labels or {})))


class Verifier:
    """Gate 1 and gate 2 pass every item."""

    def _ok(self) -> VerificationResult:
        return VerificationResult.model_validate(
            {"build_id": BUILD_ID, "passed": True, "items": [], "n_numbers": 0, "n_failed": 0,
             "verified_at": T0, "duration_ms": 1}
        )  # fmt: skip

    def verify_findings(self, findings: list[Finding], build_id: str) -> list[VerificationResult]:
        del build_id
        return [self._ok() for _ in findings]

    def verify_draft(self, draft: ReportDraft, build_id: str) -> VerificationResult:
        del draft, build_id
        return self._ok()


class Memory:
    """Spec 07 surface of the steps: compactor, recommendations (idempotent), promotion."""

    def __init__(self) -> None:
        self.recs: dict[str, list[str]] = {}
        self.writes = 0
        self.promoted: list[str] = []

    def compactor(self, profile: object, *, ctx: object) -> None:
        del profile, ctx

    def write_recommendations(self, run_id: str, drafts: list[Any]) -> list[str]:
        self.writes += 1
        if run_id not in self.recs:
            self.recs[run_id] = [new_id(IdKind.REC) for _ in drafts]
        return list(self.recs[run_id])

    def promote_procedural(self, run_id: str) -> None:
        self.promoted.append(run_id)

    def prior_context(self, run_ctx: object) -> PriorContext:
        del run_ctx
        return PriorContext(items=[], rendered="", memory_ids=[], tally={})


class _Gpu:
    def loaded_class(self) -> Literal["reasoning"]:
        return "reasoning"

    def service_healthy(self, name: ServiceName) -> bool:
        del name
        return True


class _Pool:
    def __init__(self) -> None:
        self.warehouse = FakeWarehouse()
        self.closed = False

    def get(self, build_id: str) -> FakeWarehouse:
        del build_id
        return self.warehouse

    def close_all(self) -> None:
        self.closed = True


class FakeJob:
    """A `JobContext` whose yield and stop reason the test sets."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.job_id = new_id(IdKind.JOB)
        self.yield_when: Callable[[], bool] = lambda: False
        self.stop_reason: str | None = None
        self.notes: list[str | None] = []
        self.scopes: list[str] = []
        self.job = JobRow.model_validate(
            {"job_id": self.job_id, "kind": "review", "payload": payload or {},
             "gpu_class": "reasoning", "priority": 40, "status": "running", "attempts": 1,
             "max_attempts": 3}
        )  # fmt: skip

    def should_yield(self) -> bool:
        if self.yield_when():
            self.stop_reason = self.stop_reason or "preempt"
            return True
        return False

    def heartbeat(self, note: str | None = None) -> None:
        self.notes.append(note)

    @contextlib.contextmanager
    def gpu_scope(self, cls: str) -> Iterator[None]:
        self.scopes.append(f"enter:{cls}")
        try:
            yield
        finally:
            self.scopes.append(f"exit:{cls}")


@dataclass
class Drv:
    """One driver setup: config, deps, fakes and a Swarm factory."""

    hcfg: c.HernessConfig
    deps: SwarmDeps
    agents: Agents
    memory: Memory
    metrics: Metrics
    renders: list[tuple[str, list[str]]]
    pool: _Pool
    llms: LLMRegistry
    pipeline: dict[str, Any]
    cfg_root: Path
    swarms: list[Swarm] = field(default_factory=list)

    def swarm(self, hcfg: c.HernessConfig | None = None, **deps: Any) -> Swarm:
        cfg = hcfg or self.hcfg
        d = self.deps if hcfg is None else _replace(self.deps, hcfg=cfg)
        if deps:
            d = _replace(d, **deps)
        s = Swarm(cfg.pipelines, self.llms, self.memory, deps=d)  # type: ignore[arg-type]
        self.swarms.append(s)
        return s

    def request(self, kind: str = "funding_review", depth: str = "fast", **kw: Any) -> RunRequest:
        return RunRequest.model_validate({"kind": kind, "depth": depth} | kw)

    def set_pipeline(self, *, t1: int = 2, t2: int = 1, t2_must: bool = False) -> None:
        self.pipeline.update(t1=t1, t2=t2, t2_must=t2_must)


def _replace(deps: SwarmDeps, **kw: Any) -> SwarmDeps:
    import dataclasses  # noqa: PLC0415

    return dataclasses.replace(deps, **kw)


@pytest.fixture
def drv(
    reset_process_state: ProcessState,
    ops_store: OpsStoreHandle,
    test_redactor: r.Redactor,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Drv]:
    del reset_process_state
    cfg_root = tmp_path / "cfg"
    hcfg = use_test_config(cfg_root, (f"paths.data={ops_store.data_root.as_posix()}",))
    bind_jobs_backend(SqliteJobsBackend())
    bind_ops_backend(SqliteResilienceBackend())
    monkeypatch.setattr(r._State, "redactor", test_redactor)  # the config reset dropped it
    monkeypatch.setattr(base, "_catalog_describe", list)
    monkeypatch.setattr(_routing_ctx, "gpu_state", _Gpu)
    for team in ("t1", "t2"):
        assert record_evidence(_evidence(INCIDENT_SQL, {"team": team}, {"incidents": 7}))
    sample = {"effort_cost_usd": 300, "confidence": 0.7}
    assert record_evidence(_evidence(FUNDING_SQL, {"candidate": "c1"}, sample))
    llms = LLMRegistry(hcfg.models, profile="test", egress_enabled=True)
    monkeypatch.setattr(llms, "client", _Client)
    agents, memory, metrics, pool = Agents(), Memory(), Metrics(), _Pool()
    for module in (planner, scheduler, challenge, writer):
        monkeypatch.setattr(module, "run_agent", agents)

    def no_embedding(findings: Any) -> Any:
        msg = "no embedding model in tests"
        raise ModelUnavailable(msg)

    monkeypatch.setattr(dedup, "_embed_all", no_embedding)
    shape: dict[str, Any] = {"t1": 2, "t2": 1, "t2_must": False}
    monkeypatch.setattr(
        _run_env, "get_pipeline",
        lambda kind, reader, window_end: StubPipeline(
            kind, shape["t1"], shape["t2"], t2_must=shape["t2_must"]
        ),
    )  # fmt: skip

    async def plan_context(run: Any, knobs: Any, **kw: Any) -> PlanContext:
        del knobs, kw
        return plan_ctx_for(run)

    monkeypatch.setattr(_run_steps, "build_plan_context", plan_context)
    renders: list[tuple[str, list[str]]] = []

    def render(run_id: str, formats: Any) -> None:
        renders.append((run_id, list(formats)))

    deps = SwarmDeps(
        hcfg=hcfg, verifier=Verifier(), warehouses=pool, render=render,  # type: ignore[arg-type]
        tracer_factory=Tracer, clock=_now, metrics=metrics, current_build=lambda: BUILD_ID,
        build_exists=lambda b: b == BUILD_ID, worker_alive=lambda: True, ops=FakeOps(),
        vectors=FakeVectors(), portfolio_fn=_no_portfolio,
    )  # fmt: skip
    d = Drv(hcfg, deps, agents, memory, metrics, renders, pool, llms, shape, cfg_root)
    yield d
    for s in d.swarms:
        s.close()
    c.reset_config()


def _now() -> datetime:
    from herness.core import time as clock  # noqa: PLC0415

    return clock.now()


def _no_portfolio(*args: object, **kwargs: object) -> Any:
    msg = "the portfolio optimizer is not called with a fixed plan context"
    raise AssertionError(msg)


@dataclass
class _Client:
    name: str
