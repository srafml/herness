"""Security test ST05-18 (TH05-18): every finding can be traced to its model, prompt and
queries (T05-27).

A scripted run (`analyst_5_steps.yaml` through `FakeLLMClient`) of the real loop with the real
warehouse tools on the stand-in build, a real `Tracer` writing its JSONL file and the real ops
store `evidence` area (`StoreOps` over the migrated `ops_store`): every `llm_call` line carries
`prompt_hash`, `model` and `client`; every `tool_call` line carries `args_hash`; every query
the run made has its `evidence` row and an `evidence_use` row for the run and task.
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
from tests.support.fake_llm import FakeLLMClient
from tests.support.ops_store import OpsStoreHandle
from tests.support.tools_standin import StoreOps

from herness.core import config as c
from herness.core import redact as r
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.settings import RedactionConfig
from herness.core.types import AgentResult, LoopState, ToolContext, ToolResult
from herness.eval.scripted import load_scripts
from herness.harness import hooks as h
from herness.harness import loop
from herness.harness import warehouse_tools as wt
from herness.harness.roles import base
from herness.harness.roles.base import get_role
from herness.harness.tracing import Tracer
from herness.harness.warehouse import DuckWarehouse
from herness.store.ops.core import read_all
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tests" / "fixtures" / "llm_scripts" / "analyst_5_steps.yaml"
RUN_ID = "run_01J8ST05180000000000000000"
TASK_ID = "task_1"
TOOLS = ("list_tables", "describe_table", "run_sql", "post_finding")


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
    """IT05-01's setting: migrated ops store, test config, tmp prompts, a test redactor, the
    warehouse tools on the stand-in build and a fake `post_finding` (spec 06 task tool)."""
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
    get_role("analyst_general").__dict__.pop("_prompts", None)


def _run(env: Env) -> tuple[AgentResult, list[dict[str, Any]]]:
    """The scripted run with a real `Tracer` (file under tmp); the result and the trace lines."""
    settings = c.get_config().models.harness.trace.model_copy(
        update={"payload_sample_rate": {"eval": 1.0, "chat": 1.0, "review": 1.0}}
    )
    root = Tracer(
        RUN_ID, build_id=env.wh.build_id, run_kind="eval", traces_dir=env.tmp / "traces",
        settings=settings,
    )  # fmt: skip
    view = root.bind(task_id=TASK_ID, role="analyst_general")
    ctx = ls.loop_ctx(
        tool_names=TOOLS, task_tools={"post_finding": env.post_finding}, warehouse=env.wh,
        ops=StoreOps(), tracer=view, task_id=TASK_ID,
    )  # fmt: skip
    ctx = ctx.model_copy(update={"run_id": RUN_ID})
    hooks = h.HarnessHooks(
        registry=None, gates={}, chain=None, compactor=None, task_id=None, phase=None,  # type: ignore[arg-type]
        stop=None, on_text_delta=None, tracer=view,
    )  # fmt: skip
    role = get_role("analyst_general")
    client = FakeLLMClient(SCRIPT)
    try:
        result = asyncio.run(
            loop.run_agent(role, {"question": "incidents?"}, ctx, client, ls.profile(), hooks)
        )
    finally:
        root.close()
    lines = (env.tmp / "traces" / f"{RUN_ID}.jsonl").read_text(encoding="utf-8").splitlines()
    return result, [json.loads(line) for line in lines]


def _scripted_calls() -> list[tuple[str, dict[str, Any]]]:
    (script,) = load_scripts(SCRIPT).scripts
    return [(call.name, call.arguments) for turn in script.turns for call in turn.tool_calls or ()]


def test_st05_18_llm_calls_and_tool_calls_are_attributable(env: Env) -> None:
    """ST05-18 every `llm_call` has a non-empty `prompt_hash` (the role's), `model` and
    `client`; every `tool_call` has the `args_hash` of its call's name and arguments."""
    result, events = _run(env)
    assert result.status == "completed"
    role = get_role("analyst_general")
    llm_calls = [e for e in events if e["type"] == "llm_call"]
    assert len(llm_calls) == 7
    for event in llm_calls:
        assert event["prompt_hash"] == role.prompt_hash
        assert event["prompt_hash"]
        assert event["model"]
        assert event["client"] == "fake"
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    expected = [LoopState.call_signature(name, args) for name, args in _scripted_calls()]
    assert [e["args_hash"] for e in tool_calls] == expected
    assert all(isinstance(e["args_hash"], str) and e["args_hash"] for e in tool_calls)


def test_st05_18_every_query_has_evidence_and_evidence_use(env: Env) -> None:
    """ST05-18 every query of the run (the result's `query_ids` and those on the `tool_call`
    lines) has one `evidence` row and an `evidence_use` row for this run and task."""
    result, events = _run(env)
    traced = {qid for e in events if e["type"] == "tool_call" for qid in e["query_ids"]}
    assert traced == set(result.query_ids)
    assert len(traced) == 4
    evidence = {row["query_id"] for row in read_all("SELECT query_id FROM evidence", ())}
    uses = {
        (row["query_id"], row["run_id"], row["task_id"])
        for row in read_all("SELECT query_id, run_id, task_id FROM evidence_use", ())
    }
    assert traced <= evidence
    assert {(qid, RUN_ID, TASK_ID) for qid in traced} <= uses
    assert {use[0] for use in uses} == traced  # no use row without a query of the run
