"""IT07-06 (F07-10, F07-11; design 07 §10 case 6): procedural promotion over real runs (T07-24).

Four funding reviews run through the T06-22 driver (`drv`, pinned to `tiny_build`) with the
real spec 07 `MemoryStore`: each run's record step calls the real `promote_procedural`
(`_it07_procedural`: per team three distinct verified findings citing one SQL template). In
the fourth run the Planner runs on the real spec 05 loop (`FakeLLMClient` script, real tracer)
and calls the real `recall_memory` tool (R-27: templates reach a prompt only through a tool
call; few-shot fetching is removed). The LoRA export goes through `MemoryStore.export_lora`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.integration.memory._it07_agents import full_tracer, llm_calls, script, use_real_loop
from tests.integration.memory._it07_env import (
    memory_store,
    pin_tiny,
    record_portfolio,
    run_review,
    swarm_for,
)
from tests.integration.memory._it07_procedural import cite_literal_sql, template_rows
from tests.support.builds import BuildHandle
from tests.unit.harness._blackboard_env import test_redactor

from herness.harness import warehouse_tools as wt
from herness.harness.memory import register_memory_components
from herness.harness.tools import tool_registry

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
__all__ = ["drv", "test_redactor"]


def _plan_turns(query: str) -> list[dict[str, Any]]:
    task = {"dedup_key": e._det_keys()[0], "specialty": "general", "objective": "Frame it.",
            "entity_type": "team", "entity_ids": ["t1"], "notes": "framed"}  # fmt: skip
    args = {"query": query, "layers": None, "k": None, "kinds": ["sql_template"], "entity": None}
    call = {"name": "recall_memory", "arguments": args}  # every key: the strict tool schema
    return [
        {"tool_calls": [call]},
        {"final": {"text": "Plan drafted with the stored template."}},
        {"final": {"output": {"tasks": [task], "rationale": "template", "unknowns": []}}},
    ]


def _events(data_root: Path, run_id: str, kind: str) -> list[dict[str, Any]]:
    path = data_root / "traces" / f"{run_id}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ev for ev in map(json.loads, lines) if ev.get("type") == kind]


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def test_it07_06_template_verified_in_three_runs_active_recalled_by_tool_export_disjoint(
    drv: Drv, tiny_build: BuildHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT07-06 the same template verified in 3 runs is `active` (not after 2); a 4th run's
    Planner gets it from `recall_memory`, and no prompt or task input holds it without that
    tool call (R-27); the LoRA export has no fingerprint in both splits."""
    data_root = Path(drv.hcfg.paths.data)
    pin_tiny(drv, data_root, tiny_build, monkeypatch)
    record_portfolio()
    cite_literal_sql(drv, monkeypatch)
    mem = memory_store(tmp_path, data_root)
    register_memory_components(tool_registry(), mem.store)
    wt.register_warehouse_tools()
    seen: list[tuple[str, Any]] = []
    use_real_loop(drv, monkeypatch, [], set(), seen=seen)
    statuses = []
    for _ in range(3):
        assert run_review(drv, mem.store).status == "done"
        statuses.append([t["status"] for t in template_rows()])
    assert statuses == [["candidate"] * 2, ["candidate"] * 2, ["active"] * 2]
    templates = template_rows()
    assert [(t["data"]["passes"], t["data"]["fails"]) for t in templates] == [(9, 0)] * 2
    assert all(len(set(t["data"]["run_ids"])) == 3 for t in templates)
    target = templates[0]
    secrets = [t["memory_id"] for t in templates] + [t["data"]["fingerprint"] for t in templates]
    secrets += [str(t["data"]["sql_template"]) for t in templates]

    turns = _plan_turns(f"sql_template: {target['content']}")
    use_real_loop(drv, monkeypatch, [script("planner", turns)], {"planner"}, seen=seen)
    swarm = swarm_for(drv, mem.store, tracer_factory=full_tracer)
    res = asyncio.run(swarm.start(drv.request(depth="standard")))

    assert res.status == "done"
    [tool] = _events(data_root, res.run_id, "tool_call")
    assert (tool["tool"], tool["ok"]) == ("recall_memory", True)
    first, second, *_ = llm_calls(data_root, res.run_id)
    before = json.dumps(first["payload"])
    assert not [s for s in secrets if s in before]  # the prompt before the tool call
    after = json.dumps(second["payload"]["messages"][-1])  # the tool result message
    assert target["memory_id"] in after
    assert {role for role, _ in seen} >= {"planner", "analyst", "skeptic", "writer"}
    for role, task_input in seen:  # every task input of the four runs
        text = json.dumps(task_input, default=str)
        assert not [s for s in secrets if s in text], role

    out = data_root / "models" / "lora_data"
    report = mem.store.export_lora(out, min_pass_lb=0.7, golden_questions=[])
    splits = {name: _lines(report.out_dir / f"{name}.jsonl") for name in ("train", "val")}
    fps = {name: {x["meta"]["fingerprint"] for x in lines} for name, lines in splits.items()}
    assert fps["train"]
    assert fps["val"]
    assert fps["train"].isdisjoint(fps["val"])  # export disjoint
    assert fps["train"] | fps["val"] == {t["data"]["fingerprint"] for t in templates}
    assert report.train_count + report.val_count == len(splits["train"]) + len(splits["val"])
