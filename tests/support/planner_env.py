"""Planner-step environment over the real ops store and the scripted fake LLM (impl 06 T06-15).

Shared by IT06-26 and its child process `tests.support.planner_kill`: `configure` loads the
full test config, binds the migrated ops store as the jobs and resilience backend and
registers the planner's tools; `create_run` writes a deep `funding_review` run in `planning`
with its pending planner task (the U06-137 spec); `make_env` builds a `RunEnv` whose registry
routes every client key to one `CountingClient` (spec 11 `FakeLLMClient` that counts calls per
role) and whose pipeline is a stub with one deterministic analyst task. Global state is patched
through the `setattr` the caller passes (`monkeypatch.setattr` in tests, `setattr` in a child).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from tests.support.dispatch_standin import SyncTool, use_test_config
from tests.support.fake_llm import FakeLLMClient
from tests.support.harness_fakes import FakeOps, FakeVectors, RecordingTracer
from tests.unit.harness._blackboard_env import BUILD_ID, FakeWarehouse

from herness.core import redact as r
from herness.core import time as clock
from herness.core.config import HernessConfig
from herness.core.ids import IdKind, new_id
from herness.core.jobs.ports import bind_jobs_backend
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import bind_ops_backend
from herness.core.settings import RedactionConfig
from herness.core.types import EntityScope, LLMRequest, LLMResponse, ServiceName, TaskSpec
from herness.harness import tools as tools_mod
from herness.harness import warehouse_tools as wt
from herness.harness.blackboard import Blackboard
from herness.harness.budget import RunBudget
from herness.harness.findings import EntityCatalog, compute_dedup_key
from herness.harness.gates import CallGate, TaskSlots
from herness.harness.llm.registry import LLMRegistry
from herness.harness.pipelines.base import PlanContext
from herness.harness.pipelines.settings import PipelinesConfig, resolve_knobs
from herness.harness.roles import base
from herness.harness.swarm import _routing_ctx
from herness.harness.swarm.lifecycle import _planner_spec
from herness.harness.swarm.routing import RunEnv
from herness.store.ops import get_run, insert_tasks, run_write
from herness.store.ops.jobs import SqliteJobsBackend
from herness.store.ops.resilience import SqliteResilienceBackend

ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = ROOT / "tests" / "fixtures" / "llm_scripts" / "it06_26_planner.yaml"
T0: Final = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
DET_OBJECTIVE: Final = "Review the incident load of team t1 in core.incident."
DET_SCOPE: Final = EntityScope(entity_type="team", entity_ids=["t1"])
DET_KEY: Final = compute_dedup_key("analyst", "general", DET_SCOPE, DET_OBJECTIVE)
DET_PRIORITY: Final = 120.0

type Setter = Callable[[object, str, object], None]


class CountingClient(FakeLLMClient):
    """`FakeLLMClient` counting calls per request role; answers under any client key."""

    def __init__(self, scripts: Path) -> None:
        super().__init__(scripts)
        self.roles: Counter[str] = Counter()

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        self.roles[req.metadata.role] += 1
        return await super().acomplete(req)


class _Gpu:
    def loaded_class(self) -> Literal["reasoning"]:
        return "reasoning"

    def service_healthy(self, name: ServiceName) -> bool:
        del name
        return True


class _Memory:
    def compactor(self, profile: object, *, ctx: object) -> None:
        del profile, ctx


class _Warehouses:
    def __init__(self, warehouse: FakeWarehouse) -> None:
        self._warehouse = warehouse

    def get(self, build_id: str) -> object:
        del build_id
        return self._warehouse


class StubPipeline:
    """One deterministic analyst task on team t1; the planner input names its dedup key."""

    kind = "funding_review"

    def deterministic_tasks(self, ctx: PlanContext) -> list[TaskSpec]:
        return [
            TaskSpec(
                task_id=new_id(IdKind.TASK), run_id=ctx.run_id, role="analyst",
                objective=DET_OBJECTIVE, scope=DET_SCOPE, tools=["run_sql"],
                budget=ctx.knobs.analyst_budget, priority=DET_PRIORITY, model_role="analyst",
                must_cover=True, dedup_key=DET_KEY,
            )
        ]  # fmt: skip

    def planner_input(self, ctx: PlanContext, tasks: list[TaskSpec]) -> dict[str, object]:
        del ctx
        return {"deterministic": [t.dedup_key for t in tasks], "H_wildcards": 8}


def configure(cfg_root: Path, setattr_: Setter) -> HernessConfig:
    """Test config, ops backends, a test redactor and the planner tools (no warehouse calls)."""
    hcfg = use_test_config(cfg_root)
    bind_jobs_backend(SqliteJobsBackend())
    bind_ops_backend(SqliteResilienceBackend())
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    setattr_(r._State, "redactor", redactor)
    setattr_(base, "_catalog_describe", list)
    setattr_(_routing_ctx, "gpu_state", _Gpu)
    setattr_(tools_mod._Process, "registry", tools_mod.ToolRegistry())
    wt.register_warehouse_tools()
    tools_mod.tool_registry().register(SyncTool("recall_memory"), owner="07")
    return hcfg


def create_run() -> str:
    """A deep `funding_review` run in `planning` with its pending planner task."""
    run_id = new_id(IdKind.RUN)
    knobs = resolve_knobs(PipelinesConfig(), "funding_review", "deep")
    planner = _planner_spec(run_id, "funding_review", "deep", knobs)

    def write(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO run (run_id, kind, depth, profile, config_hash, build_id, status,"
            " started_at, meta) VALUES (?, 'funding_review', 'deep', 'local', 'h', ?,"
            " 'planning', ?, '{}')",
            (run_id, BUILD_ID, clock.format_utc(T0)),
        )
        insert_tasks(conn, [planner], now=T0)

    run_write(write, op="test_planner_run")
    return run_id


def plan_context(run_id: str) -> PlanContext:
    knobs = resolve_knobs(PipelinesConfig(), "funding_review", "deep")
    return PlanContext.model_validate(
        {
            "run_id": run_id, "kind": "funding_review", "depth": "deep", "profile": "local",
            "build_id": BUILD_ID, "question": None, "focus": None, "knobs": knobs,
            "dq_warnings": [], "unconfirmed_weights": [], "prior_context": "",
            "prior_recs": [],
            "portfolio": {"scenario": "", "rows": [], "selected": [], "query_ids": [],
                          "custom": []},
        }
    )  # fmt: skip


def make_env(
    run_id: str, hcfg: HernessConfig, client: CountingClient, setattr_: Setter
) -> tuple[RunEnv, Blackboard]:
    """A `RunEnv` for the planner step; the caller closes the returned Blackboard."""
    run = get_run(run_id)
    assert run is not None
    knobs = resolve_knobs(PipelinesConfig(), "funding_review", "deep")
    llms = LLMRegistry(hcfg.models, profile="test", egress_enabled=True)

    def route(name: str) -> CountingClient:
        client.name = name  # run_agent checks that the routed profile names the client
        return client

    setattr_(llms, "client", route)
    warehouse = FakeWarehouse()
    catalog = EntityCatalog(warehouse)
    bb = Blackboard(run_id, build_id=BUILD_ID, catalog=catalog, allowed_numerals=())
    gates = {key: CallGate(key, 4) for key in ("planner", *llms.chain_for("planner", "deep"))}
    env_fields: dict[str, Any] = {
        "run": run, "knobs": knobs, "cfg": PipelinesConfig(), "hcfg": hcfg, "llms": llms,
        "memory": _Memory(), "verifier": None, "bb": bb, "catalog": catalog, "gates": gates,
        "slots": TaskSlots(4), "broker": None, "pipeline": StubPipeline(),
        "analysis": RunBudget("analysis", 10_000_000, run_id=run_id),
        "writer": RunBudget("writer", 1_000_000, run_id=run_id),
        "tracer": RecordingTracer(run_id), "clock": clock.now, "metrics": None,
        "stop": lambda: False, "wake": asyncio.Event(), "job": None, "force": False,
        "warehouses": _Warehouses(warehouse), "ops": FakeOps(), "vectors": FakeVectors(),
        "plan_ctx": plan_context(run_id),
    }  # fmt: skip
    return RunEnv(**env_fields), bb
