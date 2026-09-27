"""Security tests for the agent loop (T05-23: ST05-08, TH05-08 runaway loop; TH05-07).

A scripted model that repeats a call forever, never finishes, burns tokens, or names tools
and arguments outside the registry: the loop stops by a loop signal or a limit with steps and
tokens within the budget, and model output never becomes a tool or argument the registry
schema does not allow (such calls get error results and never execute).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import JsonValue
from tests.support import loop_standin as ls
from tests.support.dispatch_standin import SyncTool, call, strict_schema, use_test_config
from tests.support.harness_fakes import FakeLedger, RecordingTracer
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core.errors import OutputValidationError
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.types import ToolContext, ToolResult, ToolResultPart
from herness.harness import hooks as h
from herness.harness import loop
from herness.harness.tools import tool_registry
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.unit

MAX_STEPS = 8


@pytest.fixture
def env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
) -> Iterator[dict[str, SyncTool]]:
    """Config, prompts, the spec 08 policy backend; `run_sql` (strict `sql` string) and
    `get_record` registered, only `run_sql` given to the task."""
    del ops_store, reset_process_state
    use_test_config(tmp_path / "cfg")
    bind_ops_backend(SqliteResilienceBackend())
    ls.write_prompts(tmp_path / "prompts", monkeypatch)
    counter = iter(range(1, 10_000))

    def run_sql(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        qid = f"q_{next(counter):016x}"
        return ToolResult(ok=True, content=f"query_id={qid}", query_ids=[qid])

    tools = {
        "run_sql": SyncTool("run_sql", run_sql, schema=strict_schema({"sql": {"type": "string"}})),
        "get_record": SyncTool("get_record", schema=strict_schema({"id": {"type": "string"}})),
    }
    for tool in tools.values():
        tool_registry().register(tool, owner="05")
    yield tools
    c.reset_config()


def _hooks(tracer: RecordingTracer) -> h.HarnessHooks:
    return h.HarnessHooks(
        registry=None, gates={}, chain=None, compactor=None, task_id=None, phase=None,
        stop=None, on_text_delta=None, tracer=tracer,
    )  # fmt: skip


def _ctx(tracer: RecordingTracer, **budget: object) -> ToolContext:
    values = {"max_steps": MAX_STEPS, "max_tokens": 100_000} | budget
    return ls.loop_ctx(budgets=ls.budgets(**values), tracer=tracer, ledger=FakeLedger())


def _run(client: ls.ListClient, ctx: ToolContext, tracer: RecordingTracer) -> object:
    role = ls.demo_role(tools=("run_sql", "get_record", "list_tables"))
    return asyncio.run(loop.run_agent(role, {"q": "x"}, ctx, client, ls.profile(), _hooks(tracer)))


def _charged(ctx: ToolContext) -> int:
    assert isinstance(ctx.ledger, FakeLedger)
    return sum(tin + tout for tin, tout, _cost in ctx.ledger.charges)


def test_st05_08_repeating_forever_stops_by_signal(env: dict[str, SyncTool]) -> None:
    """ST05-08 a model repeating the same call forever: the spec 08 policy stops the task on
    the second loop signal (`repeat_call`, `guard_stop`); steps and tokens stay in budget."""
    tracer = RecordingTracer("run_1", "task_1")
    ctx = _ctx(tracer)
    client = ls.ListClient(lambda n, _req: ls.resp(calls=[call("run_sql", f"c{n}", sql="x")]))
    result = _run(client, ctx, tracer)
    assert (result.status, result.stop_reason) == ("partial", "repeat_call")
    assert len(client.requests) == 3
    assert result.steps <= MAX_STEPS
    assert len(env["run_sql"].calls) == 1
    assert [e["step"] for e in ls.events(tracer, "guard_stop")] == [3]
    assert _charged(ctx) <= ctx.budgets.max_tokens


def test_st05_08_never_finishing_cut_off_stops_at_max_steps(env: dict[str, SyncTool]) -> None:
    """ST05-08 a model that never finishes (always cut off): `partial` `max_steps`, exactly
    `max_steps` model calls."""
    tracer = RecordingTracer("run_1", "task_1")
    steps = 5  # below the second no-progress signal (4 steps each)
    ctx = _ctx(tracer, max_steps=steps)
    client = ls.ListClient(lambda _n, _req: ls.resp("more", stop="max_tokens"))
    result = _run(client, ctx, tracer)
    assert (result.status, result.stop_reason, result.steps) == ("partial", "max_steps", steps)
    assert len(client.requests) == steps
    assert ls.events(tracer, "guard_stop") == [{"step": steps, "cause": "max_steps"}]


def test_st05_08_never_finishing_cut_off_stops_by_no_progress(env: dict[str, SyncTool]) -> None:
    """ST05-08 the same model with a larger step budget: the second no-progress signal stops
    it (`partial` `no_progress`) before the step limit."""
    tracer = RecordingTracer("run_1", "task_1")
    ctx = _ctx(tracer, max_steps=20)
    client = ls.ListClient(lambda _n, _req: ls.resp("more", stop="max_tokens"))
    result = _run(client, ctx, tracer)
    assert (result.status, result.stop_reason, result.steps) == ("partial", "no_progress", 8)
    assert len(client.requests) == 8


def test_st05_08_never_finishing_tool_calls_bounded(env: dict[str, SyncTool]) -> None:
    """ST05-08 a model that keeps calling tools (new arguments, new evidence) and never gives
    a final answer: tools run at most `max_steps - 1` times, the wrap-up step's calls never
    run, and the final call fails after the spec 08 repairs (at most 3 calls)."""
    tracer = RecordingTracer("run_1", "task_1")
    ctx = _ctx(tracer)
    client = ls.ListClient(lambda n, _req: ls.resp(calls=[call("run_sql", f"c{n}", sql=f"s{n}")]))
    with pytest.raises(OutputValidationError):
        _run(client, ctx, tracer)
    assert len(env["run_sql"].calls) == MAX_STEPS - 1
    assert len(client.requests) <= MAX_STEPS + 3
    wrap = client.requests[MAX_STEPS - 1]
    assert wrap.tools == []


def test_st05_08_token_runaway_stops_within_one_call(env: dict[str, SyncTool]) -> None:
    """ST05-08 each reply burns 40 % of the task tokens: the task stops `task_tokens` and the
    overshoot is at most one call's usage."""
    tracer = RecordingTracer("run_1", "task_1")
    ctx = _ctx(tracer, max_tokens=1_000)
    client = ls.ListClient(
        lambda n, _req: ls.resp(calls=[call("run_sql", f"c{n}", sql=f"s{n}")], tin=390, tout=10)
    )
    result = _run(client, ctx, tracer)
    assert (result.status, result.stop_reason) == ("partial", "task_tokens")
    assert _charged(ctx) <= ctx.budgets.max_tokens + 400
    assert result.steps < MAX_STEPS


def test_st05_08_cost_runaway_stops_before_tools(env: dict[str, SyncTool]) -> None:
    """ST05-08 an expensive reply pushes the task over its cost cap: `partial` `task_budget`
    and its tool calls never run."""
    tracer = RecordingTracer("run_1", "task_1")
    ctx = _ctx(tracer, max_cost_usd=Decimal("0.01"))
    client = ls.ListClient([ls.resp(calls=[call("run_sql", "c1", sql="s")], cost="0.02")])
    result = _run(client, ctx, tracer)
    assert (result.status, result.stop_reason) == ("partial", "task_budget")
    assert env["run_sql"].calls == []


def test_st05_08_model_output_never_outside_registry_schema(env: dict[str, SyncTool]) -> None:
    """ST05-08 / TH05-07 unknown tool names, a registered tool the task was not given, extra
    or mistyped arguments: each gets an error result and nothing executes; the next valid
    call runs normally."""
    tracer = RecordingTracer("run_1", "task_1")
    ctx = _ctx(tracer)
    bad = [
        call("shell", "c1", cmd="rm -rf /"),
        call("get_record", "c2", id="sn:incident:1"),
        call("run_sql", "c3", sql="select 1", cmd="rm"),
        call("run_sql", "c4", sql=7),
        call("run_sql", "c5"),
        call("list_tables", "c6", schema="core"),
    ]
    client = ls.ListClient(
        [ls.resp(calls=bad), ls.resp(calls=[call("run_sql", "c7", sql="ok")]), ls.resp("x"),
         ls.final()]
    )  # fmt: skip
    result = _run(client, ctx, tracer)
    assert result.status == "completed"
    assert env["run_sql"].calls == [{"sql": "ok"}]
    assert env["get_record"].calls == []
    tool_msg = next(m for m in client.requests[1].messages if m.role == "tool")
    parts = [p for p in tool_msg.parts if isinstance(p, ToolResultPart)]
    assert [p.tool_call_id for p in parts] == [f"c{i}" for i in range(1, 7)]  # call order
    assert all(p.is_error for p in parts)
    assert [s.name for s in client.requests[0].tools] == ["run_sql"]
