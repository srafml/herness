"""IT07-03 (F07-12, F07-03; design 07 §10 case 3): the correction lifecycle (T07-24).

Chat rows are written through the spec 09 chat area functions; the turn is saved through the
real spec 07 `MemoryStore.session_save_turn`, whose correction classifier is a `FakeLLMClient`
script (R-65). Session B's recall is the real `recall_memory` tool on a chat run of the same
user. The funding review runs through the T06-22 driver (`drv`) with the real `MemoryStore`,
pinned to the scored `metrics_tiny` build (`plant_scored_build`: `score.*` exists; `tiny_build`
has no score layer); its Planner runs on the real spec 05 loop and searches memory for the
correction with `recall_memory`. Approval goes through `MemoryStore.approve` (F07-03, R-33).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import duckdb
import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.integration.memory._it07_agents import full_tracer, llm_calls, script, use_real_loop
from tests.integration.memory._it07_env import (
    Mem,
    memory_store,
    pin_build,
    record_portfolio,
    swarm_for,
)
from tests.support.app_env import plant_scored_build
from tests.support.dispatch_standin import make_tool_ctx
from tests.unit.harness._blackboard_env import test_redactor
from tests.unit.harness.memory._chat_env import ChatLLM, Clock, Models, correction, session, turn
from tests.unit.harness.memory._tools_env import seed_run
from tests.unit.harness.memory._write_env import AUTHOR

from herness.harness import warehouse_tools as wt
from herness.harness.memory import register_memory_components
from herness.harness.memory.tools import RecallMemoryTool
from herness.harness.tools import tool_registry
from herness.store.ops import core
from herness.store.ops.shared import ReviewItem, get_review_item, list_review_items

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
__all__ = ["drv", "test_redactor"]

APPROVER = "b" * 32
SAID = "Team t1 was reorganized in May and moved to the platform org."
STATEMENT = "Team t1 was reorganized in May and now belongs to the platform org."
RECALL_KEYS = {"layers": None, "k": None, "kinds": None, "entity": None}


def _scores(warehouse: Path) -> str:
    """A digest of every `score.*` table's rows (sorted), read-only."""
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        names = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'score'"
            " ORDER BY 1").fetchall()]  # fmt: skip
        assert names
        rows = {n: sorted(map(repr, con.execute(f"SELECT * FROM score.{n}").fetchall()))  # noqa: S608 - catalog table names
                for n in names}  # fmt: skip
    finally:
        con.close()
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()


def _item(memory_id: str) -> dict[str, Any]:
    [row] = core.read_all("SELECT status, kind, content, data FROM memory_item"
                          " WHERE memory_id = ?", (memory_id,))  # fmt: skip
    return dict(row) | {"data": core.load_json(row["data"], field="data")}


def _plan_turns(query: str) -> list[dict[str, Any]]:
    task = {"dedup_key": e._det_keys()[0], "specialty": "general", "objective": "Frame it.",
            "entity_type": "team", "entity_ids": ["t1"], "notes": "framed"}  # fmt: skip
    call = {"name": "recall_memory", "arguments": {"query": query} | RECALL_KEYS}
    return [{"tool_calls": [call]}, {"final": {"text": "Plan drafted."}},
            {"final": {"output": {"tasks": [task], "rationale": "r", "unknowns": []}}}]  # fmt: skip


def _session_a(mem: Mem) -> tuple[str, dict[str, Any], ReviewItem]:
    """Session A: the user's correction is captured as a pending item with a review item."""
    session_a = session(AUTHOR)
    _, chat_run = turn(session_a, SAID, Clock())
    memory_id = mem.store.session_save_turn(session_a, chat_run)
    assert memory_id is not None
    item = _item(memory_id)
    assert (item["kind"], item["status"]) == ("user_correction", "pending_approval")
    review = get_review_item(item["data"]["review_item_id"])
    assert (review.kind, review.status) == ("memory_write", "pending")
    return memory_id, item, review


def _session_b(mem: Mem, memory_id: str, item: dict[str, Any]) -> str:
    """Session B of the same user: `recall_memory` returns the item marked UNCONFIRMED.
    Returns the query, embedded as the item's own text (U07-58) so the ANN similarity is 1."""
    meta: dict[str, object] = {"session_id": session(AUTHOR), "user_ref": AUTHOR,
                               "message_id": "msg_b"}  # fmt: skip
    chat_ctx = make_tool_ctx().model_copy(update={"role": "chat", "run_id": seed_run("chat", meta)})
    query = "What changed for team t1 in May?"
    mem.embed.overrides[query] = mem.embed(f"user_correction: {item['content']}")
    found = RecallMemoryTool(mem.store)(chat_ctx, query=query, **RECALL_KEYS)
    assert found.ok
    items = cast("dict[str, Any]", found.data)["items"]
    [hit] = [i for i in items if i["memory_id"] == memory_id]
    assert hit["unconfirmed"] is True
    assert f"[UNCONFIRMED] {item['content']}" in found.content
    return query


def _review_run(drv: Drv, mem: Mem, monkeypatch: pytest.MonkeyPatch, query: str) -> str:
    """A funding review whose Planner searches memory with `query`; every llm_call payload
    and every task input of the run. Returns the run id."""
    seen: list[tuple[str, Any]] = []
    use_real_loop(drv, monkeypatch, [script("planner", _plan_turns(query))], {"planner"},
                  seen=seen)  # fmt: skip
    swarm = swarm_for(drv, mem.store, tracer_factory=full_tracer)
    res = asyncio.run(swarm.start(drv.request(depth="standard")))
    assert res.status == "done"
    calls = llm_calls(Path(drv.hcfg.paths.data), res.run_id)
    assert len(calls) >= 2  # the Planner searched memory, then answered
    texts = [json.dumps(c["payload"]) for c in calls]
    texts += [json.dumps(x, default=str) for _, x in seen]
    for text in texts:  # absent from every review prompt and task input
        assert "reorganized" not in text
        assert all(memory_id not in text for memory_id in _correction_ids())
    return res.run_id


def _correction_ids() -> list[str]:
    rows = core.read_all("SELECT memory_id FROM memory_item WHERE kind = 'user_correction'", ())
    return [str(r["memory_id"]) for r in rows]


def test_it07_03_correction_pending_unconfirmed_absent_from_review_then_derived_item(
    drv: Drv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT07-03 session A correction -> pending `user_correction` + review item; session B
    (same user) recall -> UNCONFIRMED; a funding review before approval -> in no prompt and
    `score.*` unchanged; approval -> active and a derived `mapping_suggestion` review item,
    `score.*` still unchanged (no rebuild)."""
    data_root = Path(drv.hcfg.paths.data)
    build_id = plant_scored_build(data_root / "warehouse")
    warehouse = data_root / "warehouse" / f"wh-{build_id}.duckdb"
    pin_build(drv, build_id, monkeypatch)
    record_portfolio()
    classify = correction(0.9, statement=STATEMENT, entities=[{"type": "team", "id": "t1"}],
                          effective_date="2026-05-01")  # fmt: skip
    mem = memory_store(tmp_path, data_root, allowed=drv.hcfg.app.reports.allowed_numeral_patterns,
                       llms=Models(ChatLLM(classify)))  # fmt: skip
    register_memory_components(tool_registry(), mem.store)
    wt.register_warehouse_tools()
    scores = _scores(warehouse)

    memory_id, item, review = _session_a(mem)
    query = _session_b(mem, memory_id, item)
    run_id = _review_run(drv, mem, monkeypatch, query)
    assert _correction_ids() == [memory_id]
    assert _scores(warehouse) == scores

    approved = mem.store.approve(memory_id, APPROVER, "confirmed by the org owner")
    assert approved.status == "active"
    assert get_review_item(review.item_id).status == "approved"
    derived = list_review_items(kind="mapping_suggestion")
    assert [d.payload["source_memory_id"] for d in derived] == [memory_id]
    assert derived[0].status == "pending"
    assert _item(memory_id)["data"]["derived_review_item_id"] == derived[0].item_id
    assert list_review_items(kind="weight_change") == []
    review_ctx = make_tool_ctx().model_copy(update={"role": "planner", "run_id": run_id})
    after = RecallMemoryTool(mem.store)(review_ctx, query=query, **RECALL_KEYS)
    assert memory_id in after.content  # the same search finds it once approved
    assert _scores(warehouse) == scores  # scores change only after that item and a rebuild
