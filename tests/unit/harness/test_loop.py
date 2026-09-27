"""Unit tests for the agent loop `herness.harness.loop` (T05-23: U05-57..U05-60, U05-74).

UT05-103..UT05-113, UT05-128, UT05-130. The model is `ListClient` (or `FakeLLMClient` for
UT05-103) and the hooks are the recording `FakeHooks`, or the real `HarnessHooks` where the
test says so; tools are counting stand-ins registered in the process registry.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import JsonValue
from structlog.testing import capture_logs
from tests.support import loop_standin as ls
from tests.support.dispatch_standin import SyncTool, call, strict_schema, use_test_config
from tests.support.fake_clock import FakeClock
from tests.support.fake_llm import FakeLLMClient
from tests.support.harness_fakes import FakeLedger, RecordingTracer

from herness.core import config as c
from herness.core import time as clock
from herness.core.errors import (
    BudgetExceeded,
    ConfigError,
    ModelRefused,
    OutputValidationError,
    ToolInputError,
)
from herness.core.jobs import tasks as jobs_tasks
from herness.core.resilience import ProcessState
from herness.core.types import (
    LoopCheckpoint,
    LoopLimits,
    LoopState,
    Message,
    TextPart,
    ToolContext,
    ToolResult,
    ToolResultPart,
)
from herness.eval.scripted import ScriptBook, load_scripts
from herness.harness import hooks as h
from herness.harness import loop
from herness.harness.tools import tool_registry
from herness.harness.tracing import Tracer

pytestmark = pytest.mark.unit

QIDS, FIDS = ls.QIDS, ls.FIDS


# --- fixtures and helpers -------------------------------------------------------------------


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState
) -> Iterator[dict[str, SyncTool]]:
    """Test config, tmp prompts, and `run_sql` (owner 05) registered; `post_finding` is a
    task tool. `run_sql` returns one query id per distinct SQL text; `fail` raises."""
    del reset_process_state
    use_test_config(tmp_path / "cfg")
    ls.write_prompts(tmp_path / "prompts", monkeypatch)
    seen: dict[str, str] = {}

    def run_sql(_ctx: ToolContext, **kw: JsonValue) -> ToolResult:
        sql = str(kw["sql"])
        if sql.startswith("fail"):
            msg = "bad sql"
            raise ToolInputError(msg, hint="fix it")
        qid = seen.setdefault(sql.strip(), QIDS[len(seen)])
        return ToolResult(ok=True, content=f"sql={sql.strip()} query_id={qid}", query_ids=[qid])

    def post_finding(_ctx: ToolContext, **kw: JsonValue) -> ToolResult:
        fid = FIDS[int(str(kw["title"])[-1])]
        return ToolResult(ok=True, content=f"posted {fid}", finding_ids=[fid])

    tools = {
        "run_sql": SyncTool("run_sql", run_sql, schema=strict_schema({"sql": {"type": "string"}})),
        "post_finding": SyncTool(
            "post_finding", post_finding, schema=strict_schema({"title": {"type": "string"}})
        ),
    }
    tool_registry().register(tools["run_sql"], owner="05")
    yield tools
    c.reset_config()


def _ctx(env: dict[str, SyncTool], **kw: object) -> ToolContext:
    names = kw.pop("tool_names", ("run_sql", "post_finding"))
    return ls.loop_ctx(tool_names=names, task_tools={"post_finding": env["post_finding"]}, **kw)


def _run(
    client: object,
    ctx: ToolContext,
    hooks: object | None = None,
    *,
    role: object = None,
    profile: object = None,
    resume_from: LoopCheckpoint | None = None,
) -> object:
    return ls.run(
        loop.run_agent(
            role or ls.demo_role(),
            {"question": "why"},
            ctx,
            client,
            profile or ls.profile(),
            hooks or ls.FakeHooks(),
            resume_from,
        )
    )


def _sql(sql: str, cid: str = "c1") -> list:
    return [call("run_sql", cid, sql=sql)]


def _tracer(ctx: ToolContext) -> RecordingTracer:
    assert isinstance(ctx.tracer, RecordingTracer)
    return ctx.tracer


# --- UT05-103 happy path --------------------------------------------------------------------


def test_ut05_103_two_tool_steps_then_final_with_fake_llm_client(
    env: dict[str, SyncTool], tmp_path: Path
) -> None:
    """UT05-103 FakeLLMClient script: 2 tool steps then final -> completed, output validated,
    query and finding ids collected, one `llm_call` per model call."""
    script = tmp_path / "s.yaml"
    script.write_text(
        "- match: {role: analyst_general}\n"
        "  turns:\n"
        "    - tool_calls: [{name: run_sql, arguments: {sql: select 1}}]\n"
        "    - tool_calls: [{name: post_finding, arguments: {title: finding 3}}]\n"
        "    - final: {text: all done}\n"
        "    - final: {output: {answer: forty-two}}\n",
        encoding="utf-8",
    )
    client = FakeLLMClient(load_scripts(script))
    ctx = _ctx(env)
    result = _run(client, ctx)
    assert result.status == "completed"
    assert result.stop_reason == "final"
    assert result.output == {"answer": "forty-two"}
    assert result.steps == 2
    assert result.query_ids == [QIDS[0]]
    assert result.finding_ids == [FIDS[3]]
    assert result.usage.input_tokens > 0
    assert [e["step"] for e in ls.events(_tracer(ctx), "llm_call")] == [0, 1, 2, 2]
    assert isinstance(client.book, ScriptBook)


def test_ut05_103_role_without_output_model_returns_last_text(env: dict[str, SyncTool]) -> None:
    """UT05-103 (U05-60 step 1) a role without output model completes at once with
    `{"text": <last assistant text>}` and makes no final call."""
    client = ls.ListClient([ls.resp("plain answer")])
    result = _run(client, _ctx(env), role=ls.demo_role(output_model=None))
    assert result.status == "completed"
    assert result.output == {"text": "plain answer"}
    assert len(client.requests) == 1


def test_ut05_103_final_prompt_hash_and_gate_wait_traced(env: dict[str, SyncTool]) -> None:
    """UT05-103 `llm_call` carries the role's prompt hash and the state's gate wait."""
    role = ls.demo_role()
    ctx = _ctx(env)
    _run(ls.ListClient([ls.resp("x"), ls.final()]), ctx, role=role)
    for event in ls.events(_tracer(ctx), "llm_call"):
        assert event["prompt_hash"] == role.prompt_hash
        assert event["gate_wait_ms"] == 0
        assert "payload" in event


# --- UT05-104 parallel calls ----------------------------------------------------------------


def test_ut05_104_three_parallel_calls_one_tool_message_in_call_order(
    env: dict[str, SyncTool],
) -> None:
    """UT05-104 3 parallel calls -> one tool message, results in call order."""
    calls = [*_sql("select 3", "c1"), *_sql("select 1", "c2"), *_sql("select 2", "c3")]
    client = ls.ListClient([ls.resp(calls=calls), ls.resp("ok"), ls.final()])
    _run(client, _ctx(env))
    tool_messages = [m for m in client.requests[1].messages if m.role == "tool"]
    assert len(tool_messages) == 1
    parts = tool_messages[0].parts
    assert [p.tool_call_id for p in parts if isinstance(p, ToolResultPart)] == ["c1", "c2", "c3"]
    contents = [p.content for p in parts if isinstance(p, ToolResultPart)]
    assert [text.split(" ")[0] for text in contents] == ["sql=select", "sql=select", "sql=select"]
    assert [text.split(" ")[1] for text in contents] == ["3", "1", "2"]


# --- UT05-105 wrap-up -------------------------------------------------------------------------


def _wrap_up_run(env: dict[str, SyncTool], prof: object) -> tuple[ls.ListClient, ToolContext]:
    ctx = _ctx(env, budgets=ls.budgets(max_tokens=10_000))
    client = ls.ListClient(
        [
            ls.resp(calls=_sql("select 1"), tin=8_000, tout=1_000),  # 9,000 = 90 %
            ls.resp(calls=_sql("select 2", "c2")),  # wrap-up step: calls are not run
            ls.final(),
        ]
    )
    _run(client, ctx, profile=prof)
    return client, ctx


def test_ut05_105_wrap_up_openai_removes_tools_once(env: dict[str, SyncTool]) -> None:
    """UT05-105 tokens reach 90 % -> wrap-up nudge once, tools removed (OpenAI), `budget`
    `wrap_up` and `warn` events once each; the wrap-up step's tool calls never run."""
    client, ctx = _wrap_up_run(env, ls.profile())
    first, wrap, fin = client.requests
    assert [s.name for s in first.tools] == ["post_finding", "run_sql"]
    assert wrap.tools == []
    assert wrap.tool_choice == "auto"
    assert wrap.messages[-1].kind == "nudge"
    assert wrap.messages[-1].parts == [TextPart(text=loop.WRAP_UP_NUDGE)]
    assert fin.tools == []
    assert fin.response_schema is not None
    nudges = [m for m in fin.messages if m.parts == [TextPart(text=loop.WRAP_UP_NUDGE)]]
    assert len(nudges) == 1
    budget = ls.events(_tracer(ctx), "budget")
    assert [e["kind"] for e in budget] == ["wrap_up", "warn"]
    used = budget[0]["used"]
    assert (used["tokens"], Decimal(used["cost_usd"]), used["steps"]) == (9_000, 0, 1)
    assert budget[0]["limit"] == {"tokens": 10_000, "cost_usd": "5", "steps": 20}
    assert budget[0]["message"] == loop.WRAP_UP_NUDGE
    assert budget[1]["message"] is None
    assert env["run_sql"].calls == [{"sql": "select 1"}]


def test_ut05_105_wrap_up_anthropic_keeps_tools_with_choice_none(
    env: dict[str, SyncTool],
) -> None:
    """UT05-105 Anthropic profile: wrap-up keeps the tool definitions with tool_choice none."""
    prof = ls.profile(key="claude-haiku")
    ctx_budgets = ls.budgets(max_tokens=10_000)
    ctx = _ctx(env, budgets=ctx_budgets, egress_purpose="reasoning")
    client = ls.ListClient(
        [ls.resp(calls=_sql("select 1"), tin=9_000), ls.resp("final text"), ls.final()]
    )
    _run(client, ctx, profile=prof)
    wrap = client.requests[1]
    assert [s.name for s in wrap.tools] == ["post_finding", "run_sql"]
    assert wrap.tool_choice == "none"


def test_ut05_105_wrap_up_on_last_step_and_warn_only_below_ratio(
    env: dict[str, SyncTool],
) -> None:
    """UT05-105 `step == max_steps - 1` also wraps up; 75 % alone emits only `warn`."""
    ctx = _ctx(env, budgets=ls.budgets(max_steps=2, max_tokens=10_000))
    client = ls.ListClient(
        [ls.resp(calls=_sql("select 1"), tin=7_600), ls.resp("done"), ls.final()]
    )
    result = _run(client, ctx)
    assert result.status == "completed"
    kinds = [e["kind"] for e in ls.events(_tracer(ctx), "budget")]
    assert kinds == ["wrap_up", "warn"]
    assert client.requests[0].tools != []
    assert client.requests[1].tools == []


def test_ut05_105_warn_without_wrap_up(env: dict[str, SyncTool]) -> None:
    """UT05-105 76 % of the tokens: one `warn` event, no wrap-up nudge, tools kept."""
    ctx = _ctx(env, budgets=ls.budgets(max_tokens=10_000))
    client = ls.ListClient([ls.resp(calls=_sql("select 1"), tin=7_600), ls.resp("x"), ls.final()])
    _run(client, ctx)
    assert [e["kind"] for e in ls.events(_tracer(ctx), "budget")] == ["warn"]
    assert client.requests[1].tools != []
    assert all(m.parts != [TextPart(text=loop.WRAP_UP_NUDGE)] for m in client.requests[2].messages)


# --- UT05-106 repeat --------------------------------------------------------------------------


def test_ut05_106_repeat_twice_nudge_then_partial_guard_stop(env: dict[str, SyncTool]) -> None:
    """UT05-106 repeat twice with a policy fake (spec 08 behaviour): first signal nudges,
    second ends `partial` `repeat_call` with exactly one `guard_stop` (from the policy)."""
    ctx = _ctx(env)
    hooks = ls.FakeHooks(policy=ls.stop_on(2, _tracer(ctx)))
    client = ls.ListClient([ls.resp(calls=_sql("select 1", f"c{i}")) for i in range(3)])
    result = _run(client, ctx, hooks)
    assert result.status == "partial"
    assert result.stop_reason == "repeat_call"
    assert result.output is None
    assert hooks.signals == [("repeat", 1, 0), ("repeat", 2, 1)]
    stops = ls.events(_tracer(ctx), "guard_stop")
    assert stops == [{"cause": "repeat", "step": 3}]
    third = client.requests[2]
    assert third.messages[-1].parts[0].text.startswith("You repeated a tool call")
    assert env["run_sql"].calls == [{"sql": "select 1"}]
    assert hooks.after[-1][1] is True  # `_finish` checkpoints with the state stopping


# --- UT05-107 max_tokens ----------------------------------------------------------------------


def test_ut05_107_max_tokens_continuation_not_counted(env: dict[str, SyncTool]) -> None:
    """UT05-107 response `max_tokens` -> continuation nudge, not counted in `nudges`."""
    hooks = ls.FakeHooks()
    client = ls.ListClient([ls.resp("half", stop="max_tokens"), ls.resp("rest"), ls.final()])
    result = _run(client, _ctx(env), hooks)
    assert result.status == "completed"
    second = client.requests[1]
    assert second.messages[-1].kind == "nudge"
    assert second.messages[-1].parts == [TextPart(text=loop.CONTINUE_NUDGE)]
    assert hooks.after == [(1, False, 0)]


# --- UT05-108 refusal -------------------------------------------------------------------------


def test_ut05_108_refusal_without_chain_raises(env: dict[str, SyncTool]) -> None:
    """UT05-108 refusal response, hooks without a chain -> `ModelRefused` (defensive loop
    check), after the call was charged and traced."""
    ctx = _ctx(env)
    with capture_logs() as logs, pytest.raises(ModelRefused) as info:
        _run(ls.ListClient([ls.resp(stop="refusal")]), ctx)
    assert info.value.category == "policy"
    assert len(ls.events(_tracer(ctx), "llm_call")) == 1
    assert any(e["event"] == "harness.llm.refused" for e in logs)


def test_ut05_108_refusal_through_harness_hooks_gated_client(env: dict[str, SyncTool]) -> None:
    """UT05-108 with `HarnessHooks` (no chain) the `GatedClient` raises `ModelRefused`."""
    hooks = h.HarnessHooks(
        registry=None, gates={}, chain=None, compactor=None, task_id=None, phase=None,
        stop=None, on_text_delta=None, tracer=Tracer.null(),
    )  # fmt: skip
    with pytest.raises(ModelRefused):
        _run(ls.ListClient([ls.resp(stop="refusal")]), _ctx(env), hooks)


# --- UT05-109 limits --------------------------------------------------------------------------


def _limit_case(env: dict[str, SyncTool], ctx: ToolContext, replies: list) -> tuple:
    hooks = ls.FakeHooks()
    result = _run(ls.ListClient(replies), ctx, hooks)
    return result, ls.events(_tracer(ctx), "guard_stop"), hooks


def test_ut05_109_max_steps(env: dict[str, SyncTool]) -> None:
    """UT05-109 steps: a model that never finishes stops `partial` `max_steps`."""
    ctx = _ctx(env, budgets=ls.budgets(max_steps=2))
    replies = [ls.resp(calls=_sql("s1")), ls.resp("cut", stop="max_tokens")]
    result, stops, hooks = _limit_case(env, ctx, replies)
    assert (result.status, result.stop_reason, result.steps) == ("partial", "max_steps", 2)
    assert stops == [{"step": 2, "cause": "max_steps"}]
    assert hooks.after[-1] == (2, True, 0)


def test_ut05_109_task_tokens(env: dict[str, SyncTool]) -> None:
    """UT05-109 tokens: task tokens reached -> `partial` `task_tokens`."""
    ctx = _ctx(env, budgets=ls.budgets(max_tokens=1_000))
    result, stops, _ = _limit_case(env, ctx, [ls.resp(calls=_sql("s1"), tin=1_500)])
    assert (result.status, result.stop_reason) == ("partial", "task_tokens")
    assert stops == [{"step": 1, "cause": "task_tokens"}]
    assert result.usage.input_tokens == 1_500


def test_ut05_109_wall_clock_with_fake_clock(env: dict[str, SyncTool]) -> None:
    """UT05-109 `wall_clock_s` passed (FakeClock) -> `partial` `wall_clock`."""
    with FakeClock(clock.now()) as fc:

        def slow(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
            fc.advance(601)
            return ToolResult(ok=True, content="slow")

        tool_registry().register(SyncTool("list_tables", slow), owner="05")
        ctx = _ctx(env, tool_names=("list_tables",), budgets=ls.budgets(wall_clock_s=600))
        replies = [ls.resp(calls=[call("list_tables")])]
        result, stops, _ = _limit_case(env, ctx, replies)
    assert (result.status, result.stop_reason) == ("partial", "wall_clock")
    assert stops == [{"step": 1, "cause": "wall_clock"}]


def test_ut05_109_deadline_in_the_past(env: dict[str, SyncTool]) -> None:
    """UT05-109 `deadline` in the past -> `partial` `wall_clock` before any model call."""
    past = clock.now() - timedelta(seconds=1)
    ctx = _ctx(env, budgets=ls.budgets(deadline=past))
    client = ls.ListClient([])
    result = _run(client, ctx)
    assert (result.status, result.stop_reason, result.steps) == ("partial", "wall_clock", 0)
    assert client.requests == []
    assert ls.events(_tracer(ctx), "guard_stop") == [{"step": 0, "cause": "wall_clock"}]


def test_ut05_109_future_deadline_does_not_stop(env: dict[str, SyncTool]) -> None:
    """UT05-109 a future `deadline` lets the task complete."""
    ctx = _ctx(env, budgets=ls.budgets(deadline=clock.now() + timedelta(hours=1)))
    result = _run(ls.ListClient([ls.resp("x"), ls.final()]), ctx)
    assert result.status == "completed"


def test_ut05_109_task_cost(env: dict[str, SyncTool]) -> None:
    """UT05-109 cost over the task cap -> `partial` `task_budget`, cause `task_cost`, and the
    over-cap reply's tool calls never run."""
    ctx = _ctx(env, budgets=ls.budgets(max_cost_usd=Decimal("0.5")))
    result, stops, _ = _limit_case(env, ctx, [ls.resp(calls=_sql("s1"), cost="0.6")])
    assert (result.status, result.stop_reason) == ("partial", "task_budget")
    assert result.cost_usd == Decimal("0.6")
    assert stops == [{"step": 0, "cause": "task_cost"}]
    assert env["run_sql"].calls == []


def test_ut05_109_error_streak_with_stopping_policy(env: dict[str, SyncTool]) -> None:
    """UT05-109 error-streak signal with a stopping policy -> `partial` `error_streak`, one
    `guard_stop` (the policy's), none from `_finish`."""
    ctx = _ctx(env)
    hooks = ls.FakeHooks(policy=ls.stop_on(1, _tracer(ctx)))
    calls = [call("run_sql", f"c{i}", sql=f"fail {i}") for i in range(3)]
    result = _run(ls.ListClient([ls.resp(calls=calls)]), ctx, hooks)
    assert (result.status, result.stop_reason) == ("partial", "error_streak")
    assert ls.events(_tracer(ctx), "guard_stop") == [{"cause": "error_streak", "step": 1}]


def test_ut05_109_finish_forces_harness_checkpoint(
    env: dict[str, SyncTool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-109 `_finish` marks the state stopping before `after_step`, so `HarnessHooks`
    saves the `loop` checkpoint although the save interval is not due (carry-over)."""
    saves: list[tuple[str, str, dict[str, JsonValue]]] = []
    monkeypatch.setattr(jobs_tasks, "save_checkpoint", lambda t, k, v: saves.append((t, k, v)))
    hooks = h.HarnessHooks(
        registry=None, gates={}, chain=None, compactor=None, task_id="task_1", phase="p",
        stop=None, on_text_delta=None, tracer=Tracer.null(),
    )  # fmt: skip
    ctx = _ctx(env, budgets=ls.budgets(max_steps=2))
    replies = [ls.resp(calls=_sql("s1")), ls.resp("cut", stop="max_tokens")]
    result = _run(ls.ListClient(replies), ctx, hooks)
    assert result.stop_reason == "max_steps"
    # step 1 (first save), step 2 skipped by the 5 s interval, then the forced stop save
    assert [v["state"]["step"] for _t, _k, v in saves] == [1, 2]
    assert {(t, k) for t, k, _v in saves} == {("task_1", "loop")}


def test_ut05_109_cancel_in_after_step_propagates(env: dict[str, SyncTool]) -> None:
    """UT05-109 `after_step` raising `CancelledError` propagates out of `run_agent`, also
    from the stop save of `_finish`."""
    hooks = ls.FakeHooks(cancel_at_step=1)
    with pytest.raises(asyncio.CancelledError):
        _run(ls.ListClient([ls.resp(calls=_sql("s1"))]), _ctx(env), hooks)
    stop_hooks = ls.FakeHooks(cancel_at_step=0)
    past = clock.now() - timedelta(seconds=1)
    with pytest.raises(asyncio.CancelledError):
        _run(ls.ListClient([]), _ctx(env, budgets=ls.budgets(deadline=past)), stop_hooks)
    assert stop_hooks.after == [(0, True, 0)]


# --- UT05-110 compaction ----------------------------------------------------------------------


def test_ut05_110_compactor_fresh_list_anthropic(env: dict[str, SyncTool]) -> None:
    """UT05-110 a fake compactor returning a fresh list (Anthropic profile): messages
    replaced, no earlier message object modified, `compaction` event with
    `fresh_conversation=true` and the identity-based removed count."""
    kept: list[tuple[Message, dict[str, object]]] = []

    def pressure(state: LoopState) -> list[Message]:
        kept.extend((m, m.model_dump()) for m in state.messages)
        summary = Message(role="user", parts=[TextPart(text="summary")], kind="compaction_summary")
        return [state.messages[0], summary]

    hooks = ls.FakeHooks(compact_at=[1], pressure=pressure)
    ctx = _ctx(env, egress_purpose="reasoning")
    client = ls.ListClient([ls.resp(calls=_sql("s1")), ls.resp("x"), ls.final()])
    _run(client, ctx, hooks, profile=ls.profile(key="claude-haiku"))
    after = client.requests[1].messages
    assert [m.kind for m in after] == ["normal", "compaction_summary"]
    assert after[0] is client.requests[0].messages[0]
    assert len(kept) == 3
    for message, dumped in kept:  # no earlier message object was modified
        assert message.model_dump() == dumped
    (event,) = ls.events(_tracer(ctx), "compaction")
    assert event["fresh_conversation"] is True
    assert event["n_messages_removed"] == 2  # the assistant turn and the tool message
    assert event["step"] == 1
    assert isinstance(event["before_tokens"], int)
    assert isinstance(event["after_tokens"], int)


def test_ut05_110_openai_profile_not_fresh_conversation(env: dict[str, SyncTool]) -> None:
    """UT05-110 an OpenAI-compatible profile reports `fresh_conversation=false`."""
    hooks = ls.FakeHooks(compact_at=[1], pressure=lambda s: list(s.messages))
    ctx = _ctx(env)
    _run(ls.ListClient([ls.resp(calls=_sql("s1")), ls.resp("x"), ls.final()]), ctx, hooks)
    (event,) = ls.events(_tracer(ctx), "compaction")
    assert event["fresh_conversation"] is False
    assert event["n_messages_removed"] == 0


# --- UT05-111 resume --------------------------------------------------------------------------


def test_ut05_111_stop_after_step_2_then_resume(env: dict[str, SyncTool]) -> None:
    """UT05-111 stop after step 2, then resume: the second run starts a fresh conversation
    with the resume part, restored counters, ids and seen signatures."""
    hooks = ls.FakeHooks(cancel_at_step=2)
    first = ls.ListClient(
        [ls.resp(calls=_sql("s1")), ls.resp(calls=[call("post_finding", "c2", title="f 4")])]
    )
    with pytest.raises(asyncio.CancelledError):
        _run(first, _ctx(env), hooks)
    cp = LoopCheckpoint.from_envelope({"loop": hooks.checkpoints[-1]})
    assert cp is not None
    second = ls.ListClient(
        [ls.resp(calls=_sql("s1", "c3")), ls.resp(calls=_sql("s2", "c4")), ls.resp("x"), ls.final()]
    )
    hooks2 = ls.FakeHooks()
    result = _run(second, _ctx(env), hooks2, resume_from=cp)
    assert result.status == "completed"
    assert result.steps == 4
    assert result.query_ids == [QIDS[0], QIDS[1]]
    assert result.finding_ids == [FIDS[4]]
    opening = second.requests[0].messages
    assert len(opening) == 1
    assert "## Resumed task" in opening[0].parts[1].text
    assert second.requests[0].metadata.step == 2
    assert hooks2.signals[0][0] == "repeat"  # seen signatures restored


# --- UT05-112 ledger --------------------------------------------------------------------------


def test_ut05_112_ledger_budget_exceeded_propagates(env: dict[str, SyncTool]) -> None:
    """UT05-112 a ledger raising `BudgetExceeded` propagates (the ledger is the only raiser,
    R-25); no `llm_call` is traced for that call and the event is logged."""
    ctx = _ctx(env, ledger=FakeLedger(max_cost_usd=Decimal("0.1")))
    with capture_logs() as logs, pytest.raises(BudgetExceeded):
        _run(ls.ListClient([ls.resp(calls=_sql("s1"), cost="0.2")]), ctx)
    assert ls.events(_tracer(ctx), "llm_call") == []
    (log,) = [e for e in logs if e["event"] == "harness.loop.budget_exceeded"]
    assert (log["log_level"], log["task_id"], log["step"]) == ("info", "task_1", 0)


# --- UT05-113 final call and `_build_request` -------------------------------------------------


def test_ut05_113_final_request_schema_tool_choice_none_instruction(
    env: dict[str, SyncTool],
) -> None:
    """UT05-113 final call: `response_schema` set, tool_choice none (Anthropic), the JSON
    instruction appended last, request key `<task>:<step>:final`."""
    client = ls.ListClient([ls.resp(calls=_sql("s1")), ls.resp("x"), ls.final()])
    ctx = _ctx(env, egress_purpose="reasoning")
    _run(client, ctx, profile=ls.profile(key="claude-haiku"))
    fin = client.requests[-1]
    assert fin.response_schema == ls.Out.model_json_schema()
    assert fin.response_schema_name == "Out"
    assert fin.tool_choice == "none"
    assert fin.messages[-1].parts == [TextPart(text=loop.FINAL_JSON_INSTRUCTION)]
    assert fin.metadata.request_key == "task_1:1:final"
    assert client.requests[0].metadata.request_key == "task_1:0:step"
    assert client.requests[0].response_schema is None


def test_ut05_113_final_parsed_missing_raises(env: dict[str, SyncTool]) -> None:
    """UT05-113 the final call without a parsed output -> `OutputValidationError`."""
    with pytest.raises(OutputValidationError, match="final output missing"):
        _run(ls.ListClient([ls.resp("x"), ls.resp("not json")]), _ctx(env))


def test_ut05_113_build_request_params_and_downgrade_log(env: dict[str, SyncTool]) -> None:
    """UT05-113 `_build_request`: role params from config, max tokens and timeout from the
    profile; a thinking downgrade on an adaptive-always client logs a WARNING."""
    role = ls.demo_role(model_role="judge", thinking="off")
    prof = ls.profile(key="claude-opus")
    state = LoopState.fresh(
        Message(role="user", parts=[TextPart(text="t")]),
        limits=_limits(),
        estimator=lambda _m: 0,
    )
    with capture_logs() as logs:
        req = loop._build_request(role, prof, [], [], state, _ctx(env), wrap_up=False, final=False)
    assert req.thinking == "on"
    assert req.max_output_tokens == prof.max_output_tokens
    assert req.timeout_s == prof.timeout_s
    assert req.parallel_tool_calls is True
    assert req.messages == state.messages
    assert req.messages is not state.messages
    (log,) = [e for e in logs if e["event"] == "harness.llm.thinking_downgraded"]
    assert (log["log_level"], log["client"], log["role"]) == ("warning", "fake", "analyst_general")
    local = loop._build_request(
        ls.demo_role(), ls.profile(), [], [], state, _ctx(env), wrap_up=False, final=False
    )
    assert local.temperature == 0.2


def test_ut05_113_build_request_invalid_is_config_error(env: dict[str, SyncTool]) -> None:
    """UT05-113 an invalid request (no messages) raises `ConfigError` without echo."""
    state = LoopState.fresh(
        Message(role="user", parts=[TextPart(text="secret prompt")]),
        limits=_limits(),
        estimator=lambda _m: 0,
    )
    state.messages = []
    with pytest.raises(ConfigError, match="invalid model request") as info:
        loop._build_request(
            ls.demo_role(), ls.profile(), [], [], state, _ctx(env), wrap_up=False, final=True
        )
    assert "secret" not in str(info.value)


def _limits() -> LoopLimits:
    return LoopLimits(context_budget_tokens=10_000, soft_tokens=5_000, hard_tokens=8_000)


def test_ut05_113_setup_preconditions(env: dict[str, SyncTool]) -> None:
    """UT05-113 (U05-58 preconditions) profile/client name mismatch and an off-network
    profile with the wrong egress purpose raise `ConfigError` before any call."""
    client = ls.ListClient([])
    with pytest.raises(ConfigError, match="does not match"):
        _run(client, _ctx(env), profile=ls.profile(name="other"))
    with pytest.raises(ConfigError, match="egress purpose mismatch"):
        _run(client, _ctx(env), profile=ls.profile(key="claude-haiku"))
    assert client.requests == []


# --- UT05-128 compaction fallback -------------------------------------------------------------


class _FailingCompactor:
    def __init__(self) -> None:
        self.calls = 0

    def pressure(self, state: LoopState) -> object:
        return dataclasses.make_dataclass("P", ["tokens", "soft"])(
            state.est_input_tokens(), state.limits().soft_tokens
        )

    async def on_context_pressure(self, state: LoopState) -> list[Message]:
        self.calls += 1
        msg = "compactor output invalid"
        raise OutputValidationError(msg)


def _small_profile() -> object:
    # budget = 4,000 - 1,000 - 1,024 = 1,976: soft 1,383, hard 1,679 tokens
    return ls.profile(context_window=4_000, max_output_tokens=1_000)


def test_ut05_128_compactor_failure_truncates_and_continues(env: dict[str, SyncTool]) -> None:
    """UT05-128 a compactor raising `OutputValidationError`: truncation used,
    `harness.loop.truncation_fallback` logged, `compaction` event emitted, task continues."""
    compactor = _FailingCompactor()
    ctx = _ctx(env)
    hooks = h.HarnessHooks(
        registry=None, gates={}, chain=None, compactor=compactor, task_id=None, phase=None,
        stop=None, on_text_delta=None, tracer=RecordingTracer(),
    )  # fmt: skip
    client = ls.ListClient([ls.resp(calls=_sql("s1"), tin=1_500), ls.resp("x"), ls.final()])
    with capture_logs() as logs:
        result = _run(client, ctx, hooks, profile=_small_profile())
    assert result.status == "completed"
    assert compactor.calls == 1
    assert any(e["event"] == "harness.loop.truncation_fallback" for e in logs)
    (event,) = ls.events(_tracer(ctx), "compaction")
    assert event["fresh_conversation"] is False
    assert [m.kind for m in client.requests[1].messages][:2] == ["normal", "compaction_summary"]


def test_ut05_128_truncation_cannot_fit_partial_task_tokens(env: dict[str, SyncTool]) -> None:
    """UT05-128 without a compactor, truncation cannot get under the hard limit (the task
    message alone is too large) -> `partial` `task_tokens`, cause `context` (R-25)."""
    ctx = _ctx(env)
    hooks = h.HarnessHooks(
        registry=None, gates={}, chain=None, compactor=None, task_id=None, phase=None,
        stop=None, on_text_delta=None, tracer=RecordingTracer(),
    )  # fmt: skip
    client = ls.ListClient([])
    result = ls.run(
        loop.run_agent(
            ls.demo_role(), {"question": "x" * 8_000}, ctx, client, _small_profile(), hooks
        )
    )
    assert (result.status, result.stop_reason) == ("partial", "task_tokens")
    assert ls.events(_tracer(ctx), "guard_stop") == [{"step": 0, "cause": "context"}]
    assert len(ls.events(_tracer(ctx), "compaction")) == 1
    assert client.requests == []


# --- UT05-130 loop-signal counters and tracer binding -----------------------------------------


def test_ut05_130_loop_signals_then_no_progress_and_bound_tracer(
    env: dict[str, SyncTool],
) -> None:
    """UT05-130 scripted repeat, then no-progress: the recording policy sees
    `loop_signals` 1 then 2, `nudges` counts only signal nudges (not the continuation);
    the bound tracer reports the bound run and task, the root tracer `task_id is None`."""
    root = Tracer.null()
    bound = root.bind(task_id="task_1", role="analyst_general")
    ctx = _ctx(env, tracer=bound)
    replies = [
        ls.resp(calls=_sql("s1", "c0")),  # step 0: progress
        ls.resp(calls=_sql("s1", "c1")),  # step 1: repeat -> signal 1
        ls.resp("cut", stop="max_tokens"),  # step 2: continuation (not counted)
        ls.resp(calls=_sql("s1 ", "c3")),  # step 3: known id, no progress
        ls.resp(calls=_sql("s1  ", "c4")),  # step 4: 4th step without progress -> signal 2
        ls.resp("x"),
        ls.final(),
    ]
    env_seen = env["run_sql"]
    hooks = ls.FakeHooks()
    result = _run(ls.ListClient(replies), ctx, hooks)
    assert result.status == "completed"
    assert hooks.signals == [("repeat", 1, 0), ("no_progress", 2, 1)]
    assert hooks.after[-1][2] == 2
    assert len(env_seen.calls) == 3
    assert bound.run_id == root.run_id
    assert bound.task_id == "task_1"
    assert root.task_id is None


# --- review polish: once-guards, check order, equality boundaries ------------------------------


def test_ut05_105_two_steps_past_both_ratios_nudge_and_events_once(
    env: dict[str, SyncTool],
) -> None:
    """UT05-105 two steps past `wrap_up_ratio` (and `BUDGET_WARN_RATIO`): the wrap-up nudge is
    appended once and each `budget` event (`wrap_up`, `warn`) is emitted exactly once."""
    ctx = _ctx(env, budgets=ls.budgets(max_tokens=10_000))
    client = ls.ListClient(
        [
            ls.resp(calls=_sql("select 1"), tin=9_000),  # step 1 starts past 90 %
            ls.resp("cut", stop="max_tokens"),  # step 1: wrap-up, continuation
            ls.resp("done"),  # step 2: still past both ratios
            ls.final(),
        ]
    )
    result = _run(client, ctx)
    assert result.status == "completed"
    fin = client.requests[-1].messages
    assert sum(m.parts == [TextPart(text=loop.WRAP_UP_NUDGE)] for m in fin) == 1
    assert [e["kind"] for e in ls.events(_tracer(ctx), "budget")] == ["wrap_up", "warn"]


def test_ut05_105_two_steps_past_warn_ratio_warn_once(env: dict[str, SyncTool]) -> None:
    """UT05-105 two steps past 75 % but below 90 %: exactly one `warn` event."""
    ctx = _ctx(env, budgets=ls.budgets(max_tokens=10_000))
    client = ls.ListClient(
        [
            ls.resp(calls=_sql("select 1"), tin=7_600),
            ls.resp(calls=_sql("select 2", "c2")),
            ls.resp("done"),
            ls.final(),
        ]
    )
    _run(client, ctx)
    assert [e["kind"] for e in ls.events(_tracer(ctx), "budget")] == ["warn"]


def test_ut05_109_order_compaction_before_hard_limits(env: dict[str, SyncTool]) -> None:
    """UT05-109 check order: compaction that stays over the hard limit wins over `max_steps`
    reached at the same step (`task_tokens`, cause `context`)."""
    big = Message(role="user", parts=[TextPart(text="x" * 8_000)])
    hooks = ls.FakeHooks(compact_at=[1], pressure=lambda s: [s.messages[0], big])
    ctx = _ctx(env, budgets=ls.budgets(max_steps=1))
    client = ls.ListClient([ls.resp("cut", stop="max_tokens")])
    result = _run(client, ctx, hooks, profile=_small_profile())
    assert (result.status, result.stop_reason) == ("partial", "task_tokens")
    assert ls.events(_tracer(ctx), "guard_stop") == [{"step": 1, "cause": "context"}]


def test_ut05_109_order_steps_before_tokens_before_wall_clock(env: dict[str, SyncTool]) -> None:
    """UT05-109 check order: steps and tokens exhausted together -> `max_steps`; tokens and
    wall clock exhausted together -> `task_tokens`."""
    ctx = _ctx(env, budgets=ls.budgets(max_steps=1, max_tokens=1_000))
    result, stops, _ = _limit_case(env, ctx, [ls.resp("cut", stop="max_tokens", tin=1_500)])
    assert result.stop_reason == "max_steps"
    assert stops == [{"step": 1, "cause": "max_steps"}]
    with FakeClock(clock.now()) as fc:

        def slow(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
            fc.advance(601)
            return ToolResult(ok=True, content="slow")

        tool_registry().register(SyncTool("list_tables", slow), owner="05")
        budgets = ls.budgets(max_tokens=1_000, wall_clock_s=600)
        ctx = _ctx(env, tool_names=("list_tables",), budgets=budgets)
        replies = [ls.resp(calls=[call("list_tables")], tin=1_500)]
        result, stops, _ = _limit_case(env, ctx, replies)
    assert result.stop_reason == "task_tokens"
    assert stops == [{"step": 1, "cause": "task_tokens"}]


def test_ut05_109_equality_boundaries_stop(env: dict[str, SyncTool]) -> None:
    """UT05-109 boundaries: `step == max_steps`, `tokens_used == max_tokens` and
    `elapsed == wall_clock_s` each stop the task."""
    ctx = _ctx(env, budgets=ls.budgets(max_steps=1))  # step 0 is the wrap-up step
    result, _, _ = _limit_case(env, ctx, [ls.resp("cut", stop="max_tokens")])
    assert (result.stop_reason, result.steps) == ("max_steps", 1)
    ctx = _ctx(env, budgets=ls.budgets(max_tokens=1_000))
    result, _, _ = _limit_case(env, ctx, [ls.resp(calls=_sql("s2"), tin=995, tout=5)])
    assert result.stop_reason == "task_tokens"
    assert result.usage.input_tokens + result.usage.output_tokens == 1_000
    with FakeClock(clock.now()) as fc:

        def slow(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
            fc.advance(600)
            return ToolResult(ok=True, content="slow")

        tool_registry().register(SyncTool("list_tables", slow), owner="05")
        ctx = _ctx(env, tool_names=("list_tables",), budgets=ls.budgets(wall_clock_s=600))
        result, _, _ = _limit_case(env, ctx, [ls.resp(calls=[call("list_tables")])])
    assert result.stop_reason == "wall_clock"


def test_ut05_109_cost_equal_to_cap_does_not_stop(env: dict[str, SyncTool]) -> None:
    """UT05-109 boundary: task cost equal to `max_cost_usd` does not stop (strict `>`)."""
    ctx = _ctx(env, budgets=ls.budgets(max_cost_usd=Decimal("0.5")))
    client = ls.ListClient([ls.resp(calls=_sql("s1"), cost="0.5"), ls.resp("x"), ls.final()])
    result = _run(client, ctx)
    assert result.status == "completed"
    assert result.cost_usd == Decimal("0.5")
    assert env["run_sql"].calls == [{"sql": "s1"}]
