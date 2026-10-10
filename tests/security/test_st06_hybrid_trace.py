"""ST06-06 (TH06-06) hybrid egress and ST06-09 (TH06-09) decision traceability (T06-26).

Both rows drive full review runs through the real run driver over the T06-22 `drv` set-up
with every role on the real spec 05 loop and spec 11 `FakeLLMClient` scripts
(`tests.support._f06_swarm`); ST06-06 sends the Writer off-network through the real Anthropic
adapter and the real spec 10 egress guard, spied on to see exactly what it decides on.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.support import _f06_swarm as f6

from herness.harness.swarm import _run_env
from herness.store.ops import query_findings, select_tasks

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
test_redactor = e.env_mod.test_redactor  # type: ignore[attr-defined]
__all__ = ["drv", "test_redactor"]


def _trace(drv: Drv, run_id: str) -> list[dict[str, Any]]:
    path = Path(drv.hcfg.paths.data) / "traces" / f"{run_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- ST06-09 -----------------------------------------------------------------------------------

LLM_TRACE_GAP = (
    "product gap (T06-26): herness/harness/swarm/_routing_ctx.py build_tool_context hands the"
    " loop the unbound run tracer (tracer=env.tracer, not env.tracer.bind(task_id=, role=)), so"
    " the spec 05 llm_call and tool_call events of swarm tasks carry task_id null and role null;"
    " no ops row records an analyst task's model either (task.result has none)"
)


def _full_run(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> tuple[str, list[dict[str, Any]]]:
    """A standard run in which one analyst spawns a subtask, with the real review tracer."""
    spawn_from = f6.allow_spawn(drv, monkeypatch)
    f6.use_real_agents(drv, monkeypatch, f6.TaskScripts(spawn_from=spawn_from))
    swarm = drv.swarm(tracer_factory=_run_env._review_tracer)
    res = asyncio.run(swarm.start(drv.request(depth="standard")))
    assert res.status == "done"
    return res.run_id, _trace(drv, res.run_id)


def test_st06_09_findings_verdicts_and_spawns_trace_to_task_and_model(
    drv: Drv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST06-09 a full standard run (one analyst spawns a subtask) with the real review tracer:
    every finding row names its task (an analyst task of the run) and `author_role` analyst;
    every Skeptic verdict (append-only `challenge`) names its Skeptic task, which holds the
    finding, and its model (a client of the task's model-role chain that answered this run's
    `llm_call`s); every spawned task matches an approving `spawn_decision` (parent, child) of
    its parent; every `plan_decision` is the planner task's and names a task of the run unless
    it records a drop."""
    run_id, events = _full_run(drv, monkeypatch)
    tasks = {t.task_id: t for t in select_tasks(run_id)}
    findings = query_findings(run_id, limit=500)
    assert findings
    for f in findings:
        task = tasks[f.task_id]
        assert (task.run_id, task.role, f.author_role) == (run_id, "analyst", "analyst")

    answered = {(ev["client"], ev["model"]) for ev in events if ev["type"] == "llm_call"}
    assert answered
    assert all(client and model for client, model in answered)
    verdicts = [ch for f in findings for ch in f.challenge]
    assert verdicts
    for ch in verdicts:
        skeptic = tasks[ch.skeptic_task_id]
        assert skeptic.role == "skeptic"
        assert ch.finding_id in skeptic.spec.inputs.finding_ids
        assert ch.model in drv.llms.chain_for(skeptic.spec.model_role, "standard")
        assert ch.model in {client for client, _model in answered}

    spawns = [ev for ev in events if ev["type"] == "spawn_decision"]
    children = [t for t in tasks.values() if t.parent_task_id is not None]
    assert len(children) == 1
    approved = {
        (ev["task_id"], ev["parent_task_id"], ev["child_task_id"]) for ev in spawns
        if ev["approved"]
    }  # fmt: skip
    assert approved == {(t.parent_task_id, t.parent_task_id, t.task_id) for t in children}
    assert all(tasks[t.parent_task_id].role == "analyst" for t in children if t.parent_task_id)

    (planner,) = [t for t in tasks.values() if t.role == "planner"]
    plans = [ev for ev in events if ev["type"] == "plan_decision"]
    assert plans
    assert {(ev["task_id"], ev["role"]) for ev in plans} == {(planner.task_id, "planner")}
    keys = {t.spec.dedup_key for t in tasks.values()}
    assert {ev["dedup_key"] for ev in plans if ev["action"] != "dropped"} <= keys


@pytest.mark.xfail(strict=True, raises=(AssertionError, KeyError), reason=LLM_TRACE_GAP)
def test_st06_09_finding_model_traceable_through_llm_call_traces(
    drv: Drv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST06-09 the model behind every finding is traceable: its task has `llm_call` trace
    events bound to that task id, each naming the client and model."""
    run_id, events = _full_run(drv, monkeypatch)
    models: dict[str, set[tuple[str, str]]] = {}
    for ev in events:
        if ev["type"] == "llm_call":
            models.setdefault(ev["task_id"], set()).add((ev["client"], ev["model"]))
    findings = query_findings(run_id, limit=500)
    assert findings
    for f in findings:
        assert models[f.task_id]
        assert all(client and model for client, model in models[f.task_id])
