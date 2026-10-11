"""IT07-07 (F07-05; design 07 §10 case 7): kill an agent after compaction; the resumed task
restores the scratchpad and the summary message from its checkpoint (T07-24).

A funding review runs through the T06-22 driver (`drv`) with the real spec 07 `MemoryStore`:
its one analyst task runs on the real spec 05 loop over a `FakeLLMClient` script (long
`post_finding` claims, so the context grows) with the real `HarnessHooks`, whose compactor is
`MemoryStore.compactor` (deterministic notes, `ScratchpadOps` saving checkpoint key
`scratchpad`, R-21). The routed profile has a 12,288-token window (estimate tokenizer) so the
shipped compaction ratios trigger within the task, and the loop checkpoints every step
(`resilience.loop.checkpoint_min_interval_s = 0`, as the T05-27 `loop_kill` child sets it).
The kill is in-process (the T06-26 precedent): a `BaseException` raised by the client on the
second request that carries the compaction summary, i.e. after the compaction step's loop
checkpoint. The run is then resumed through `Swarm.resume`, the path spec 08 takes. The
`loop` checkpoint holds no messages (R-21, R-29): the resumed loop's first message carries the
task, the resume state and the checkpointed scratchpad (spec 05 `render_task`), which is the
summary of the compacted work; the compactor restores the same scratchpad at its next call.
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
from tests.integration.memory._it07_env import memory_store, record_portfolio, swarm_for
from tests.unit.harness._blackboard_env import test_redactor

from herness.core import config as c
from herness.core.ids import canonical_json
from herness.core.types import LLMRequest, LLMResponse
from herness.harness import hooks
from herness.harness.memory import register_memory_components
from herness.harness.memory.render import escape_content
from herness.harness.tools import tool_registry
from herness.store.ops import core, get_run, select_tasks

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
__all__ = ["drv", "test_redactor"]

LONG = " because the restore path was slow and the paging rota was thin" * 20


class SimulatedKill(BaseException):
    """Not an `Exception`: no product handler catches it, as with a killed process."""


def _post(i: int) -> dict[str, Any]:
    nref = {"id": "n1", "value": 7, "unit": "count", "query_id": e.incident_qid("t1"),
            "column": "incidents", "row_key": None, "format": None}  # fmt: skip
    args = {"claim": f"Team t1 had [[n1]] incidents (case {chr(65 + i)}){LONG}",
            "entity_type": "team", "entity_id": "t1", "query_ids": [], "confidence": 0.8,
            "numbers": [nref]}  # fmt: skip
    return {"tool_calls": [{"name": "post_finding", "arguments": args}]}


def _analyst_turns() -> list[dict[str, Any]]:
    out = {"summary": "looked", "unknowns": [], "suggested_followups": []}
    return [*map(_post, range(24)), {"final": {"text": "done"}}, {"final": {"output": out}}]


def _setup(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> None:
    """One analyst task with room for 30 steps, a small window, a checkpoint every step."""
    drv.set_pipeline(t1=1, t2=0)
    det_spec = e.det_spec

    def roomy(*args: Any, **kwargs: Any) -> Any:
        spec = det_spec(*args, **kwargs)
        budget = spec.budget.model_copy(update={"max_steps": 30, "max_tokens": 400_000})
        return spec.model_copy(update={"budget": budget})

    monkeypatch.setattr(e, "det_spec", roomy)
    config = drv.llms.config

    def small(name: str) -> Any:
        return config(name).model_copy(
            update={"context_window": 12_288, "max_output_tokens": 1_000, "tokenizer": "estimate"}
        )

    monkeypatch.setattr(drv.llms, "config", small)
    cfg = c.get_config()
    res, inner = cfg.resilience, cfg.resilience.resilience
    loop_cfg = inner.loop.model_copy(update={"checkpoint_min_interval_s": 0})
    every_step = cfg.model_copy(update={"resilience": res.model_copy(
        update={"resilience": inner.model_copy(update={"loop": loop_cfg})})})  # fmt: skip
    monkeypatch.setattr(hooks, "get_config", lambda: every_step)


def _kill_after_compaction(client: Any, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Raise `SimulatedKill` at the second request carrying a compaction summary (once)."""
    real, seen = client.acomplete, [0]

    async def acomplete(req: LLMRequest) -> LLMResponse:
        if any(m.kind == "compaction_summary" for m in req.messages):
            seen[0] += 1
            if seen[0] == 2:
                raise SimulatedKill
        return await real(req)

    monkeypatch.setattr(client, "acomplete", acomplete)
    return seen


def _checkpoint(task_id: str) -> dict[str, Any]:
    [row] = core.read_all("SELECT checkpoint FROM task WHERE task_id = ?", (task_id,))
    return dict(json.loads(str(row["checkpoint"])))


def _first_text(event: dict[str, Any]) -> str:
    first = event["payload"]["messages"][0]
    return "\n".join(str(part.get("text", "")) for part in first["parts"])


def test_it07_07_kill_after_compaction_resume_restores_scratchpad_and_summary(
    drv: Drv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT07-07 kill the analyst after a compaction; the resumed task continues at the
    checkpointed step with the summary (the saved scratchpad) in its first message, and its
    compactor restores the saved scratchpad (the next compaction is the second one, covering
    the earlier steps)."""
    record_portfolio()
    mem = memory_store(tmp_path, Path(drv.hcfg.paths.data))
    register_memory_components(tool_registry(), mem.store)
    _setup(drv, monkeypatch)
    client = use_real_loop(drv, monkeypatch, [script("analyst_general", _analyst_turns())],
                           {"analyst"})  # fmt: skip
    _kill_after_compaction(client, monkeypatch)

    with pytest.raises(BaseExceptionGroup) as killed:  # through the scheduler's TaskGroup
        asyncio.run(swarm_for(drv, mem.store, tracer_factory=full_tracer).start(
            drv.request(depth="standard")))  # fmt: skip
    assert killed.group_contains(SimulatedKill)
    [task] = [t for t in select_tasks(_run_id()) if t.role == "analyst"]
    saved = _checkpoint(task.task_id)
    pad = saved["scratchpad"]
    assert pad["compactions"] == 1
    step = saved["loop"]["state"]["step"]
    before = llm_calls(Path(drv.hcfg.paths.data), _run_id())
    assert before[-1]["payload"]["messages"][0]["kind"] == "compaction_summary"
    assert before[-1]["step"] + 1 == step  # killed at the call after the compaction step

    res = asyncio.run(swarm_for(drv, mem.store, tracer_factory=full_tracer).resume(_run_id()))

    assert res.status == "done"
    calls = llm_calls(Path(drv.hcfg.paths.data), res.run_id)[len(before) :]
    resumed = calls[0]
    assert resumed["step"] == step  # continued from the checkpoint, not from step 0
    text = _first_text(resumed)  # the resumed task message: task, resume state, scratchpad
    assert "## Resumed task" in text
    assert '<untrusted_data source="scratchpad"' in text
    assert escape_content(canonical_json(pad)) in text  # the checkpointed scratchpad (R-20)
    after = _checkpoint(task.task_id)["scratchpad"]
    assert after["compactions"] == 2  # the restored scratchpad's count, plus one
    assert after["covers_steps"][0] == pad["covers_steps"][0]
    assert all(entry in after["ledger"] for entry in pad["ledger"])
    run = get_run(res.run_id)
    assert run is not None
    assert run.status == "done"


def _run_id() -> str:
    [row] = core.read_all("SELECT run_id FROM run", ())
    return str(row["run_id"])
