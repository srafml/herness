"""Integration tests for the agent loop (T05-23: IT05-01..IT05-06, ST08-14 integration half).

The real warehouse tools run on the stand-in build of `tests.support.warehouse_tools_build`
(spec 11's `tiny_build` does not exist yet); `post_finding` is a fake spec 06 swarm tool (task
tool, owner 06). The model is the real `OpenAICompatClient` over `respx_router` (no network)
or the in-process `FakeLLMClient`; the hooks are the real `HarnessHooks` with the spec 08
policy, `complete_validated` and `ModelChain`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue
from tests.support import loop_standin as ls
from tests.support import warehouse_tools_build as wb
from tests.support.dispatch_standin import SyncTool, strict_schema, use_test_config
from tests.support.fake_llm import FakeLLMClient, respx_router
from tests.support.harness_fakes import FakeOps, RecordingTracer
from tests.support.ops_store import OpsStoreHandle
from tests.support.tools_standin import StoreOps

from herness.core import config as c
from herness.core import redact as r
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ModelChain, ProcessState, bind_ops_backend
from herness.core.settings import RedactionConfig
from herness.core.types import AgentResult, ToolContext, ToolResult
from herness.eval.scripted import LLMScript, ScriptBook
from herness.harness import hooks as h
from herness.harness import loop
from herness.harness import warehouse_tools as wt
from herness.harness.llm.openai_compat import OpenAICompatClient
from herness.harness.llm.settings import ClientConfig
from herness.harness.roles import base
from herness.harness.roles.base import get_role
from herness.harness.tracing import Tracer
from herness.harness.warehouse import DuckWarehouse
from herness.store.ops.core import read_all
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tests" / "fixtures" / "llm_scripts" / "analyst_5_steps.yaml"
OPENAI = "http://127.0.0.1:8000/v1/chat/completions"
RUN_ID = "run_01J8ZZZZZZZZZZZZZZZZZZZZZZ"
TOOLS = ("list_tables", "describe_table", "run_sql", "post_finding")
_ZERO = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}


@dataclass
class Env:
    wh: DuckWarehouse
    post_finding: SyncTool
    tmp: Path


@pytest.fixture
def env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
) -> Iterator[Env]:
    """Migrated ops store bound as the resilience backend, test config, tmp prompts, a test
    redactor, the warehouse tools registered on the opened stand-in build."""
    del ops_store, reset_process_state
    use_test_config(tmp_path / "cfg")
    bind_ops_backend(SqliteResilienceBackend())
    prompts = ls.write_prompts(tmp_path / "prompts", monkeypatch)
    (prompts / "analyst_general.md").write_text("# Analyst\nInvestigate.\n", encoding="utf-8")
    monkeypatch.setattr(base, "_catalog_describe", list)
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    wb.patch_redaction(monkeypatch)
    wt.register_warehouse_tools()
    counter = iter(range(1, 30))

    def post(_ctx: ToolContext, **kw: JsonValue) -> ToolResult:
        del kw
        fid = ls.FIDS[next(counter)]
        return ToolResult(ok=True, content=f"posted {fid}", finding_ids=[fid])

    finding = SyncTool("post_finding", post, schema=strict_schema({"title": {"type": "string"}}))
    with wb.opened(tmp_path / "wh") as wh:
        yield Env(wh, finding, tmp_path)
    c.reset_config()


def _openai_cfg() -> ClientConfig:
    return ClientConfig.model_validate(
        {
            "name": "local-30b", "kind": "openai_compat", "base_url": "http://127.0.0.1:8000/v1",
            "model": "qwen3-30b", "context_window": 32768, "max_output_tokens": 4096,
            "tokenizer": "estimate", "max_concurrency": 4, "price_per_mtok": _ZERO,
            "server": "vllm",
        }
    )  # fmt: skip


def _ctx(env: Env, tracer: object, *, ops: object = None, task_id: str = "task_1") -> ToolContext:
    return ls.loop_ctx(
        tool_names=TOOLS,
        task_tools={"post_finding": env.post_finding},
        warehouse=env.wh,
        ops=ops or FakeOps(),
        tracer=tracer,
        task_id=task_id,
    )


def _hooks(tracer: Any, **kw: Any) -> h.HarnessHooks:
    values: dict[str, Any] = {
        "registry": None, "gates": {}, "chain": None, "compactor": None, "task_id": None,
        "phase": None, "stop": None, "on_text_delta": None, "tracer": tracer,
    }  # fmt: skip
    return h.HarnessHooks(**(values | kw))


def _book(*scripts: dict[str, Any]) -> ScriptBook:
    return ScriptBook(
        [LLMScript.model_validate({**s, "source": f"it#{i}"}) for i, s in enumerate(scripts)]
    )


def _sql_turn(*sqls: str) -> dict[str, Any]:
    calls = [{"name": "run_sql", "arguments": {"sql": s, "purpose": "p"}} for s in sqls]
    return {"tool_calls": calls}


_TEXT = {"final": {"text": "done"}}
_OUTPUT = {"final": {"output": {"summary": "ok", "unknowns": [], "suggested_followups": []}}}


def _run(env: Env, client: Any, ctx: ToolContext, hooks: Any, prof: Any = None) -> AgentResult:
    role = get_role("analyst_general")
    profile = prof or ls.profile()
    return asyncio.run(
        loop.run_agent(role, {"question": "incidents?"}, ctx, client, profile, hooks)
    )


def _bodies(router: Any) -> list[dict[str, Any]]:
    return [json.loads(req.content) for req in router.requests]


# --- IT05-01 ----------------------------------------------------------------------------------


def test_it05_01_analyst_5_steps_over_respx_router(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT05-01 `respx_router` with `analyst_5_steps.yaml`, stand-in build, fake swarm tool:
    5 steps, 2 findings posted, received requests match the script, trace file complete."""
    router = respx_router(SCRIPT).install(monkeypatch)
    cfg = _openai_cfg()
    settings = c.get_config().models.harness.trace.model_copy(
        update={"payload_sample_rate": {"eval": 1.0, "chat": 1.0, "review": 1.0}}
    )
    root = Tracer(
        RUN_ID, build_id=env.wh.build_id, run_kind="eval", traces_dir=env.tmp / "traces",
        settings=settings,
    )  # fmt: skip
    view = root.bind(task_id="task_1", role="analyst_general")
    ops = FakeOps()
    try:
        result = _run(env, OpenAICompatClient(cfg), _ctx(env, view, ops=ops), _hooks(view), cfg)
    finally:
        root.close()
    assert result.status == "completed"
    assert result.steps == 5
    assert result.finding_ids == ls.FIDS[1:3]
    assert len(result.query_ids) == 4  # list_tables, describe_table (columns, sample), run_sql
    assert set(result.query_ids) == set(ops.evidence)
    assert result.output == {
        "summary": "Incidents are spread evenly across the four services.",
        "unknowns": [],
        "suggested_followups": ["Check priority mix per service"],
    }
    bodies = _bodies(router)
    assert [str(req.url) for req in router.requests] == [OPENAI] * 7
    names = [[t["function"]["name"] for t in b.get("tools", [])] for b in bodies]
    assert names[:6] == [sorted(TOOLS)] * 6
    assert "response_format" in bodies[6]
    assert "response_format" not in bodies[5]
    assert bodies[6]["messages"][-1]["content"] == loop.FINAL_JSON_INSTRUCTION
    assert [m["role"] for m in bodies[5]["messages"]].count("tool") == 5
    lines = (env.tmp / "traces" / f"{RUN_ID}.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    kinds = [e["type"] for e in events]
    assert kinds.count("llm_call") == 7
    assert kinds.count("tool_call") == 5
    assert "guard_stop" not in kinds
    assert {e["task_id"] for e in events} == {"task_1"}
    assert [e["tool"] for e in events if e["type"] == "tool_call"] == [
        "list_tables", "describe_table", "run_sql", "post_finding", "post_finding"
    ]  # fmt: skip
    assert all("payload" in e for e in events if e["type"] == "llm_call")


# --- IT05-02 ----------------------------------------------------------------------------------


def test_it05_02_three_parallel_calls_one_tool_message_in_order(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT05-02 a script with 3 parallel calls: one tool message, results in call order."""
    sqls = [f"SELECT {n} AS n" for n in (3, 1, 2)]
    router = respx_router(_book({"turns": [_sql_turn(*sqls), _TEXT, _OUTPUT]}))
    router.install(monkeypatch)
    tracer = RecordingTracer("run_1", "task_1")
    cfg = _openai_cfg()
    result = _run(env, OpenAICompatClient(cfg), _ctx(env, tracer), _hooks(tracer), cfg)
    assert result.status == "completed"
    second = _bodies(router)[1]["messages"]
    calls = next(m for m in second if m["role"] == "assistant")["tool_calls"]
    results = [m for m in second if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in results] == [call["id"] for call in calls]
    assert [json.loads(call["function"]["arguments"])["sql"] for call in calls] == sqls
    for text, message in zip(("3", "1", "2"), results, strict=True):
        assert f"\n{text}" in message["content"]


# --- IT05-03 ----------------------------------------------------------------------------------


def test_it05_03_bad_sql_then_corrected_both_traced(env: Env) -> None:
    """IT05-03 bad SQL then corrected: an error result with a hint, then success; both traced."""
    bad = "SELECT * FROM secret.credentials"
    good = "SELECT count(*) AS n FROM core.incident"
    client = FakeLLMClient(_book({"turns": [_sql_turn(bad), _sql_turn(good), _TEXT, _OUTPUT]}))
    tracer = RecordingTracer("run_1", "task_1")
    ctx = _ctx(env, tracer)
    result = _run(env, client, ctx, _hooks(tracer))
    assert result.status == "completed"
    assert len(result.query_ids) == 1
    calls = ls.events(tracer, "tool_call")
    assert [(e["tool"], e["ok"]) for e in calls] == [("run_sql", False), ("run_sql", True)]
    assert calls[0]["error_type"] is not None


# --- IT05-04 and ST08-14 (integration half) ---------------------------------------------------


def test_it05_04_st08_14_repeat_identical_call_nudge_then_partial_guard_stop(env: Env) -> None:
    """IT05-04 / ST08-14 a script repeating an identical call, with the spec 08 policy in
    `HarnessHooks`: first signal nudges, second stops `partial` `repeat_call` with one
    `guard_stop` (trace and `resilience_event` row); one model call per step, the repeats
    never execute."""
    sql = "SELECT 1 AS n"
    turns = [_sql_turn(sql), _sql_turn(sql), _sql_turn(sql), _sql_turn(sql)]
    client = FakeLLMClient(_book({"turns": turns}))
    tracer = RecordingTracer("run_1", "task_1")
    ctx = _ctx(env, tracer)
    result = _run(env, client, ctx, _hooks(tracer))
    assert (result.status, result.stop_reason) == ("partial", "repeat_call")
    assert client.book.calls("analyst_general", "*") == 3
    assert ls.events(tracer, "guard_stop") == [{"cause": "repeat", "step": 3}]
    assert [e["ok"] for e in ls.events(tracer, "tool_call")] == [True, False, False]
    rows = read_all("SELECT detail FROM resilience_event WHERE kind = 'guard_stop'", ())
    assert [json.loads(row["detail"]) for row in rows] == [{"cause": "repeat", "step": 3}]


# --- IT05-05 ----------------------------------------------------------------------------------


@dataclass
class _ChainInfo:
    off_network: bool = False
    gpu_class: str | None = None
    timeout_s: float = 60.0
    model: str = "small-cpu-model"
    base_url: str | None = None
    api_key: str | None = None


class _ChainRegistry:
    def __init__(self, client: Any) -> None:
        self._client = client

    def chain_for(self, model_role: str, depth: str) -> list[str]:
        return ["fake"] if (model_role, depth) == ("analyst", "standard") else []

    def config(self, name: str) -> _ChainInfo:
        return _ChainInfo()

    def client(self, name: str) -> Any:
        return self._client


class _Gpu:
    def loaded_class(self) -> str:
        return "reasoning"

    def service_healthy(self, name: str) -> bool:
        return True


def test_it05_05_malformed_final_json_repaired_through_chain(env: Env) -> None:
    """IT05-05 a script with malformed final JSON, spec 08 chain: `repair` event, then
    completed with the repaired output."""
    script = {"turns": [_TEXT, _OUTPUT], "faults": [{"at": 1, "kind": "malformed_json"}]}
    client = FakeLLMClient(_book(script))
    registry = _ChainRegistry(client)
    chain = ModelChain("analyst", registry=registry, depth="standard", gpu=_Gpu())
    tracer = RecordingTracer("run_1", "task_1")
    hooks = _hooks(tracer, chain=chain, registry=registry)
    result = _run(env, client, _ctx(env, tracer), hooks)
    assert result.status == "completed"
    assert result.output == {"summary": "ok", "unknowns": [], "suggested_followups": []}
    assert len(ls.events(tracer, "repair")) == 1
    assert client.book.calls("analyst_general", "*") == 3


# --- IT05-06 ----------------------------------------------------------------------------------


def test_it05_06_two_tasks_same_query_one_evidence_two_uses(env: Env) -> None:
    """IT05-06 two tasks running the same query: one `evidence` row, two `evidence_use` rows."""
    sql = "SELECT count(*) AS n FROM core.incident"
    for task in ("task_1", "task_2"):
        client = FakeLLMClient(_book({"turns": [_sql_turn(sql), _TEXT, _OUTPUT]}))
        tracer = RecordingTracer("run_1", task)
        ctx = _ctx(env, tracer, ops=StoreOps(), task_id=task)
        result = _run(env, client, ctx, _hooks(tracer))
        assert result.status == "completed"
    evidence = read_all("SELECT query_id FROM evidence", ())
    uses = read_all("SELECT query_id, task_id FROM evidence_use ORDER BY task_id", ())
    assert len(evidence) == 1
    assert [(u["query_id"], u["task_id"]) for u in uses] == [
        (evidence[0]["query_id"], "task_1"),
        (evidence[0]["query_id"], "task_2"),
    ]
