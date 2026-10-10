"""The T06-22 run-driver test set-up outside pytest, for the FT08-06 child process (T08-24).

`build_drv` repeats the body of the `drv` fixture of `tests.integration.harness._swarm_run_env`
(scripted `run_agent` per role, stub pipeline, fake Verifier, memory, renderer and metrics) with
a plain `pytest.MonkeyPatch`, on the parent's config tree and migrated ops store, so a child
process drives the same swarm as the parent and the parent can resume the run it left behind.
The evidence rows are the parent's (the fixture already recorded them).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import keyring
import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.support.fake_keyring import MemoryKeyring
from tests.support.harness_fakes import FakeOps, FakeVectors
from tests.unit.harness._blackboard_env import PLANTED_NAME

from herness.core import config as c
from herness.core import redact as r
from herness.core import time as clock
from herness.core.errors import ModelUnavailable
from herness.core.jobs.ports import bind_jobs_backend
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import bind_ops_backend
from herness.core.settings import RedactionConfig
from herness.harness.llm.registry import LLMRegistry
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
from herness.harness.swarm.run import SwarmDeps
from herness.store.ops import record_evidence_use, reset_connections
from herness.store.ops.jobs import SqliteJobsBackend
from herness.store.ops.resilience import SqliteResilienceBackend


def link_evidence(mp: pytest.MonkeyPatch, agents: e.Agents) -> None:
    """Make each scripted analyst record the use of its team's incident evidence (the
    `evidence_use` row a real `run_sql` call writes, through the real ops store) before it
    posts its finding, so a run has per-task evidence links (FT08-06)."""
    analyst = agents._analyst

    async def linked(ctx: Any, hooks: Any) -> Any:
        qid = e.incident_qid(e._team_of(ctx.task_id))
        use = (qid, ctx.run_id, ctx.task_id, clock.now())
        await asyncio.to_thread(record_evidence_use, *use)
        return await analyst(ctx, hooks)

    mp.setattr(agents, "_analyst", linked)


def _no_embedding(findings: Any) -> Any:
    del findings
    msg = "no embedding model in tests"
    raise ModelUnavailable(msg)


async def _plan_context(run: Any, knobs: Any, **kw: Any) -> Any:
    del knobs, kw
    return e.plan_ctx_for(run)


def _pipeline(kind: str, reader: Any, window_end: Any) -> e.StubPipeline:
    del reader, window_end
    return e.StubPipeline(kind, 2, 1, t2_must=False)


def build_drv(mp: pytest.MonkeyPatch, cfg_root: Path, data_root: Path) -> e.Drv:
    """The `drv` fixture's set-up on an existing store (`data_root/ops.sqlite`)."""
    keyring.set_keyring(MemoryKeyring())
    overrides = (f"paths.data={data_root.as_posix()}",)  # the parent's tree (use_test_config)
    hcfg = c.init_config("local", overrides, config_dir=cfg_root / "config", env={})
    reset_connections(path=data_root / "ops.sqlite")
    bind_jobs_backend(SqliteJobsBackend())
    bind_ops_backend(SqliteResilienceBackend())
    directory = NameDirectory.from_files(None, (PLANTED_NAME,), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    mp.setattr(r._State, "redactor", redactor)
    mp.setattr(base, "_catalog_describe", list)
    mp.setattr(_routing_ctx, "gpu_state", e._Gpu)
    llms = LLMRegistry(hcfg.models, profile="test", egress_enabled=True)
    mp.setattr(llms, "client", e._Client)
    agents, memory, metrics, pool = e.Agents(), e.Memory(), e.Metrics(), e._Pool()
    for module in (planner, scheduler, challenge, writer):
        mp.setattr(module, "run_agent", agents)
    link_evidence(mp, agents)
    mp.setattr(dedup, "_embed_all", _no_embedding)
    mp.setattr(_run_env, "get_pipeline", _pipeline)
    mp.setattr(_run_steps, "build_plan_context", _plan_context)
    renders: list[tuple[str, list[str]]] = []

    def render(run_id: str, formats: Any) -> None:
        renders.append((run_id, list(formats)))

    deps = SwarmDeps(
        hcfg=hcfg, verifier=e.Verifier(), warehouses=pool, render=render,  # type: ignore[arg-type]
        tracer_factory=e.Tracer, clock=e._now, metrics=metrics, current_build=lambda: e.BUILD_ID,
        build_exists=lambda b: b == e.BUILD_ID, worker_alive=lambda: True, ops=FakeOps(),
        vectors=FakeVectors(), portfolio_fn=e._no_portfolio,
    )  # fmt: skip
    shape: dict[str, Any] = {"t1": 2, "t2": 1, "t2_must": False}
    return e.Drv(hcfg, deps, agents, memory, metrics, renders, pool, llms, shape, cfg_root)
