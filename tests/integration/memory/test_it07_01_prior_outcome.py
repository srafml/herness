"""IT07-01 (F07-09, F07-06; design 07 §10 case 1): a prior `no_effect` outcome surfaces at run
start and lowers a new recommendation's confidence (T07-24).

Set-up: the T06-22 run driver (`drv`) pinned to the `tiny_build` warehouse; the real spec 07
`MemoryStore`; `tests.support.seed_ops.seed_prior_run` plants the T6 prior funding run (two
accepted `fund` recommendations on the T6 epics, through `write_recommendations` and `decide`).
The T6 `unpaid` side (planted effect 0.0) gets its measured `no_effect` outcome row through the
ops closed-loop writer (`insert_outcome`; F07-08 itself is IT07-05). An `org_review` then runs
through the real driver: its plan context takes the real `MemoryStore.prior_context`, the
Planner runs on the real spec 05 loop with a `FakeLLMClient` script and the real tracer, and the
scripted Writer recommends funding the same epic, which the real record step hands to
`write_recommendations` (U07-80 feedback).

Deviation (recorded in the report): the spec names the target `grp_db_ops`; the tiny build's T6
plant has no such target, so the seeded `unpaid` epic (a `work_item`) is the target.
The row runs on `tiny_build` (not a scored build) because `seed_prior_run` needs the tiny
build's T6 plant (truth and warehouse), and the driver's stub pipeline reads no `score.*`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import duckdb
import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.integration.memory._it07_agents import full_tracer, llm_calls, script, use_real_loop
from tests.integration.memory._it07_env import (
    memory_store,
    pin_tiny,
    real_prior_context,
    record_portfolio,
    swarm_for,
)
from tests.support import seed_ops
from tests.support.builds import BuildHandle
from tests.unit.harness._blackboard_env import test_redactor

from herness.core import time as clock
from herness.core.ids import IdKind, new_id
from herness.core.ids import query_id as compute_query_id
from herness.harness import warehouse_tools as wt
from herness.harness.memory import register_memory_components
from herness.harness.roles.base import get_role
from herness.harness.tools import tool_registry
from herness.store import ops
from herness.store.ops import run_write

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
__all__ = ["drv", "test_redactor"]


def _plan() -> dict[str, Any]:
    key = e._det_keys()[0]  # the deterministic t1 task of the driver's stub pipeline
    task = {"dedup_key": key, "specialty": "general", "objective": "Frame it.",
            "entity_type": "team", "entity_ids": ["t1"], "notes": "framed"}  # fmt: skip
    return {"tasks": [task], "rationale": "deterministic", "unknowns": []}


def _same_target_output(epic_id: str) -> Any:
    """The driver's Writer output with its `fund` recommendation on `epic_id`."""
    base = e.writer_output

    def output(run_id: str) -> dict[str, Any]:
        out = base(run_id)
        rec = out["recommendations"][0]
        rec.update(target_type="work_item", target_id=epic_id, expected_metric="incident_count",
                   summary="Fund the epic to cut [[n1]] of incidents")  # fmt: skip
        return out

    return output


def _no_effect(rec_id: str, query_id: str) -> None:
    """The measured outcome of `rec_id` (verdict `no_effect`) through the ops writer."""
    row = ops.OutcomeRow(
        outcome_id=new_id(IdKind.OUTCOME), rec_id=rec_id, measurement=1,
        measured_at=clock.format_utc(clock.now() - timedelta(days=1)), metric="incident_count",
        baseline=4.0, actual=4.0, delta=0.0, query_id=query_id, verdict="no_effect",
        details={"rel": 0.0, "method": "did_peer_median"},
    )  # fmt: skip
    run_write(lambda c: ops.insert_outcome(row, conn=c), op="test_seed")


def _prior_context(event: dict[str, Any]) -> str:
    """`planner_input.prior_context` of the task JSON in the first message of one `llm_call`
    payload (the loop sends the task input as `## Task` and its JSON)."""
    text = str(event["payload"]["messages"][0]["parts"][0]["text"])
    task, _ = json.JSONDecoder().raw_decode(text, text.index("{", text.index("## Task")))
    return str(task["planner_input"]["prior_context"])


def test_it07_01_prior_no_effect_reaches_planner_and_lowers_new_rec_confidence(
    drv: Drv, tiny_build: BuildHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT07-01 seed an accepted rec with a `no_effect` outcome; start an `org_review`: the
    Planner's first `llm_call` payload has the `rec_id`, the verdict and the outcome
    `query_id`; the new rec on the same target gets a confidence below its base."""
    data_root = Path(drv.hcfg.paths.data)
    copy = pin_tiny(drv, data_root, tiny_build, monkeypatch)
    record_portfolio()
    mem = memory_store(tmp_path, data_root,
                       allowed=drv.hcfg.app.reports.allowed_numeral_patterns)  # fmt: skip
    register_memory_components(tool_registry(), mem.store)  # the real recall_memory tool
    wt.register_warehouse_tools()
    con = duckdb.connect(str(copy), read_only=True)
    try:
        prior = seed_ops.seed_prior_run(mem.store, tiny_build.truth, warehouse=con,
                                        build_id=tiny_build.build_id)  # fmt: skip
    finally:
        con.close()
    t6 = tiny_build.truth.plants.T6_outcomes
    params = {"service_id": t6.unpaid.service_id, "effective_at": clock.format_utc(t6.effective_at)}
    outcome_qid = compute_query_id(seed_ops.INCIDENT_SQL, params, tiny_build.build_id)
    rec_id = prior.rec_ids["unpaid"]
    _no_effect(rec_id, outcome_qid)
    real_prior_context(monkeypatch)
    monkeypatch.setattr(e, "writer_output", _same_target_output(t6.unpaid.epic_record_id))
    turns: list[dict[str, Any]] = [{"final": {"text": "Plan drafted."}},
                                   {"final": {"output": _plan()}}]  # fmt: skip
    use_real_loop(drv, monkeypatch, [script("planner", turns)], {"planner"})

    swarm = swarm_for(drv, mem.store, tracer_factory=full_tracer)
    res = asyncio.run(swarm.start(drv.request(kind="org_review", depth="standard")))

    assert res.status == "done"
    calls = llm_calls(data_root, res.run_id)
    assert calls
    first = calls[0]
    assert first["prompt_hash"] == get_role("planner").prompt_hash
    text = _prior_context(first)
    assert f'id="{rec_id}"' in text
    assert 'verdict="no_effect"' in text
    assert f'outcome_query_id="{outcome_qid}"' in text
    [new] = ops.run_recommendations(res.run_id)
    assert (new["kind"], new["target_type"], new["target_id"]) == (
        "fund",
        "work_item",
        t6.unpaid.epic_record_id,
    )
    basis = cast("dict[str, Any]", new["confidence_basis"])
    assert new["confidence"] < basis["base"]
    assert basis["delta"] < 0
    similar = {s["rec_id"]: s for s in basis["similar"]}
    assert similar[rec_id]["verdict"] == "no_effect"
    assert similar[rec_id]["outcome_query_id"] == outcome_qid
    assert json.dumps(basis["similar"]).count("no_effect") == 1
