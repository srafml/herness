"""Tests for herness.core.types.harness.agent (U05-13, U05-14, U05-15, U05-16)."""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

from herness.core.errors import ConfigError, SchemaViolation
from herness.core.ids import canonical_json, sha256_hex
from herness.core.types import (
    AgentResult,
    LLMResponse,
    LoopCheckpoint,
    LoopLimits,
    LoopSignal,
    LoopState,
    Message,
    ReasoningPart,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolErrorInfo,
    ToolResult,
    ToolResultPart,
    Usage,
)

pytestmark = pytest.mark.unit

Q1, Q2 = "q_0000000000000001", "q_0000000000000002"
F1 = "fnd_01ARZ3NDEKTSV4RRFFQ69G5FAV"
FIRST = Message(role="user", parts=[TextPart(text="task")])
LIMITS = LoopLimits(context_budget_tokens=10_000, soft_tokens=7_000, hard_tokens=8_500)


class SpyEstimator:
    """Counts calls; estimates 10 tokens per message."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, messages: Sequence[Message]) -> int:
        self.calls.append(len(messages))
        return 10 * len(messages)


def _state(limits: LoopLimits = LIMITS, estimator: SpyEstimator | None = None) -> LoopState:
    return LoopState.fresh(FIRST, limits=limits, estimator=estimator or SpyEstimator())


def _resp(**overrides: Any) -> LLMResponse:
    fields: dict[str, Any] = {
        "text": "hi",
        "tool_calls": [],
        "parsed": None,
        "reasoning": [],
        "stop_reason": "end_turn",
        "raw_stop_reason": "stop",
        "refusal_category": None,
        "usage": Usage(input_tokens=100, output_tokens=20, cache_read_tokens=30),
        "cost_usd": Decimal("0.010000"),
        "client": "c",
        "model": "m",
        "provider": "openai_compat",
        "latency_ms": 5,
        "request_id": None,
    }
    fields.update(overrides)
    return LLMResponse(**fields)


def _ok(qids: list[str] | None = None, fids: list[str] | None = None) -> ToolResult:
    return ToolResult(
        tool_call_id="c1", ok=True, content="ok", query_ids=qids or [], finding_ids=fids or []
    )


def _fail() -> ToolResult:
    err = ToolErrorInfo(type="QueryError", message="bad", hint=None)
    return ToolResult(tool_call_id="c2", ok=False, content="ERROR", error=err)


def test_ut05_08_charge_twice_totals_and_last_usage() -> None:
    """UT05-08 charge counts cache tokens as input; totals and last_usage are correct."""
    state = _state()
    second = Usage(input_tokens=50, output_tokens=5, cache_write_tokens=7, reasoning_tokens=2)
    assert state.charge(_resp()) == (130, 20)
    assert state.charge(_resp(usage=second, cost_usd=Decimal("0.002"))) == (57, 5)
    assert (state.tokens_in, state.tokens_out, state.tokens_used()) == (187, 25, 212)
    assert state.cost_usd == Decimal("0.012000")
    assert state.last_usage == second
    assert state.total_usage() == Usage(
        input_tokens=150,
        output_tokens=25,
        cache_read_tokens=30,
        cache_write_tokens=7,
        reasoning_tokens=2,
    )


def test_ut05_08_append_assistant_parts_and_nudges() -> None:
    """UT05-08 append_assistant orders parts; reasoning replayed only for anthropic."""
    state = _state()
    call = ToolCall(id="c1", name="run_sql", arguments={"sql": "select 1"})
    reasoning = ReasoningPart(text="think", provider="anthropic")
    state.append_assistant(_resp(reasoning=[reasoning], tool_calls=[call], provider="anthropic"))
    assert state.messages[-1].parts == [reasoning, TextPart(text="hi"), ToolCallPart(call=call)]
    state.append_assistant(_resp(reasoning=[reasoning], text=""))
    assert state.messages[-1].parts == [TextPart(text="")]
    state.add_nudge("go on")
    state.add_nudge("wrap up", counts=False)
    assert [m.kind for m in state.messages[-2:]] == ["nudge", "nudge"]
    assert state.messages[-1].role == "user"
    assert state.nudges == 1
    state.note_gate_wait(40)
    state.note_gate_wait(2)
    assert state.gate_wait_ms() == 42
    assert state.limits() is LIMITS


def test_ut05_09_repeat_signal_cleared_after_evaluation() -> None:
    """UT05-09 check_repeat hit gives a repeat signal once; the flag is then cleared."""
    state = _state()
    sig = LoopState.call_signature("run_sql", {"sql": "select 1"})
    expected = sha256_hex(canonical_json({"name": "run_sql", "arguments": {"sql": "select 1"}}))
    assert sig == expected[:16]
    assert state.check_repeat(sig) is None
    state.remember_call(sig)
    state.step = 3
    state.remember_call(sig)
    assert state.check_repeat(sig) == 0
    state.append_tool_results([_ok([Q1])])
    state.step += 1
    signal = state.loop_signal()
    assert signal == LoopSignal(
        cause="repeat",
        message="You repeated a tool call you already made. "
        "Use the earlier result or change the arguments.",
    )
    assert state.loop_signals == 1
    state.append_tool_results([_ok([Q2])])
    assert state.loop_signal() is None
    assert state.loop_signals == 1
    with pytest.raises(ValidationError):
        LoopSignal(cause="other", message="x")  # type: ignore[arg-type]


def test_ut05_10_no_progress_signal_on_step_four_then_reset() -> None:
    """UT05-10 four steps without new ids give no_progress; the counter resets."""
    state = _state()
    state.append_tool_results([_ok([Q1])])
    assert state.loop_signal() is None
    signals = [state.loop_signal() for _ in range(4)]
    assert signals[:3] == [None, None, None]
    assert signals[3] == LoopSignal(
        cause="no_progress",
        message="No new evidence in the last 4 steps. "
        "Change your approach or give your final answer.",
    )
    assert [state.loop_signal() for _ in range(3)] == [None, None, None]
    state.append_tool_results([_ok([Q1], [F1])])
    assert state.loop_signal() is None
    assert state.finding_ids == [F1]
    assert state.query_ids == [Q1]
    assert state.loop_signals == 1


def test_ut05_11_error_streak_signal_and_success_resets() -> None:
    """UT05-11 three failed results give error_streak; a success resets the streak."""
    state = _state()
    state.append_tool_results([_fail(), _fail(), _ok(), _fail(), _fail()])
    assert state.messages[-1].parts[0] == ToolResultPart(
        tool_call_id="c2", content="ERROR", is_error=True
    )
    assert state.loop_signal() is None
    state.append_tool_results([_fail()])
    signal = state.loop_signal()
    assert signal is not None
    assert signal.cause == "error_streak"
    assert signal.message == (
        "Your last 3 tool calls failed. Read the hints and simplify, or give your final answer."
    )
    state.append_tool_results([_fail(), _fail()])
    assert state.loop_signal() is None
    state.append_tool_results([])
    assert len(state.messages) == 4
    assert state.note_sql_failure("k") == 1
    assert state.note_sql_failure("k") == 2
    assert (state.sql_failures("k"), state.sql_failures("other")) == (2, 0)


def test_ut05_12_checkpoint_round_trip_and_keys() -> None:
    """UT05-12 to_checkpoint keys equal the LoopCheckpoint fields but scratchpad; restore."""
    state = _state()
    state.charge(_resp())
    state.append_tool_results([_ok([Q1, Q2], [F1])])
    state.remember_call("b" * 16)
    state.remember_call("a" * 16)
    state.step, state.nudges, state.loop_signals = 5, 3, 2
    cp = state.to_checkpoint()
    assert set(cp) == set(LoopCheckpoint.model_fields) - {"scratchpad"}
    assert cp == {
        "v": 1,
        "query_ids": [Q1, Q2],
        "finding_ids": [F1],
        "budget": {"tokens_in": 130, "tokens_out": 20, "cost_usd": "0.010000", "steps": 5},
        "state": {"step": 5, "nudges": 3, "loop_signals": 2},
        "seen_signatures": ["a" * 16, "b" * 16],
    }
    restored = _state()
    restored.restore(LoopCheckpoint.model_validate(cp))
    assert restored.to_checkpoint() == cp
    assert restored.check_repeat("a" * 16) == 5
    assert restored.cost_usd == Decimal("0.010000")
    old: dict[str, Any] = {**cp, "state": {"step": 1, "nudges": 0}}
    legacy = _state()
    legacy.restore(LoopCheckpoint.model_validate(old))
    assert (legacy.step, legacy.loop_signals) == (1, 0)
    bad = LoopCheckpoint.model_construct(**{**cp, "v": 2})
    with pytest.raises(SchemaViolation, match="loop checkpoint version 2 unsupported"):
        legacy.restore(bad)


def _loop() -> dict[str, JsonValue]:
    return {
        "v": 1,
        "query_ids": [Q1],
        "finding_ids": [],
        "budget": {"tokens_in": 1, "tokens_out": 2, "cost_usd": "0.5", "steps": 1},
        "state": {"step": 1, "nudges": 0, "loop_signals": 0},
        "seen_signatures": [],
    }


def test_ut05_13_from_envelope_cases() -> None:
    """UT05-13 envelope with loop, without loop, invalid loop, scratchpad string and dict."""
    base: dict[str, JsonValue] = {"schema_version": 1, "state": {"x": 1}, "loop": _loop()}
    cp = LoopCheckpoint.from_envelope(base)
    assert cp is not None
    assert (cp.query_ids, cp.scratchpad) == ([Q1], None)
    assert LoopCheckpoint.from_envelope({"schema_version": 1, "state": {}}) is None
    with_text = LoopCheckpoint.from_envelope({**base, "scratchpad": "notes"})
    assert with_text is not None
    assert with_text.scratchpad == "notes"
    with_dict = LoopCheckpoint.from_envelope({**base, "scratchpad": {"b": 1, "a": "é"}})
    assert with_dict is not None
    assert with_dict.scratchpad == '{"a":"é","b":1}'


@pytest.mark.parametrize(
    "loop",
    [
        "not a dict",
        {**_loop(), "v": 2},
        {**_loop(), "extra": 1},
        {**_loop(), "state": {"step": 1}},
        {**_loop(), "state": {"step": 1, "nudges": 0, "other": 1}},
        {**_loop(), "state": {"step": -1, "nudges": 0}},
        {**_loop(), "state": {"step": True, "nudges": 0}},
        {**_loop(), "budget": {"tokens_in": 1, "tokens_out": 2}},
        {**_loop(), "budget": {"tokens_in": 1, "tokens_out": 2, "cost_usd": "NaN"}},
        {**_loop(), "budget": {"tokens_in": 1, "tokens_out": 2, "cost_usd": "-1"}},
        {**_loop(), "budget": {"tokens_in": 1, "tokens_out": 2, "cost_usd": "x"}},
        {**_loop(), "budget": {"tokens_in": 1.5, "tokens_out": 2, "cost_usd": "1"}},
        {**_loop(), "budget": {"tokens_in": 1, "tokens_out": 2, "cost_usd": "1", "z": 0}},
        {**_loop(), "query_ids": ["nope"]},
        {**_loop(), "seen_signatures": ["XYZ"]},
    ],
)
def test_ut05_13_invalid_loop_raises_schema_violation(loop: JsonValue) -> None:
    """UT05-13 an invalid loop value raises SchemaViolation with a fixed message."""
    with pytest.raises(SchemaViolation, match="task checkpoint loop invalid"):
        LoopCheckpoint.from_envelope({"loop": loop})


def test_ut05_13_invalid_scratchpad_raises() -> None:
    """UT05-13 a scratchpad that is neither string nor object is invalid."""
    with pytest.raises(SchemaViolation, match="task checkpoint loop invalid"):
        LoopCheckpoint.from_envelope({"loop": _loop(), "scratchpad": [1, 2]})


def test_ut05_14_est_input_tokens_uses_spy_only() -> None:
    """UT05-14 estimate is usage prefix plus the spy's estimate of new messages; reset."""
    spy = SpyEstimator()
    limits = LoopLimits(
        context_budget_tokens=10_000, soft_tokens=7_000, hard_tokens=8_500, fixed_tokens=500
    )
    state = _state(limits, spy)
    assert state.est_input_tokens() == 510
    state.charge(_resp())
    assert state.est_input_tokens() == 130
    state.append_assistant(_resp())
    state.add_nudge("more")
    assert state.est_input_tokens() == 150
    state.replace_messages([FIRST, FIRST])
    assert state.last_usage is None
    assert state.est_input_tokens() == 520
    assert spy.calls == [1, 0, 2, 2]


def test_ut05_100_loop_limits_formula_and_invariants() -> None:
    """UT05-100 for_client on 32k/4k gives budget 27,744 and soft 19,420 (U05-13)."""
    limits = LoopLimits.for_client(
        32_768, None, 4_000, no_progress_steps=4, error_streak=3, fixed_tokens=900
    )
    assert limits == LoopLimits(
        context_budget_tokens=27_744, soft_tokens=19_420, hard_tokens=23_582, fixed_tokens=900
    )
    capped = LoopLimits.for_client(
        200_000, 100_000, 8_000, no_progress_steps=2, error_streak=5, fixed_tokens=0
    )
    assert capped.context_budget_tokens == 100_000 - 8_000 - 3_000
    assert (capped.no_progress_steps, capped.error_streak) == (2, 5)
    with pytest.raises(ConfigError, match="context budget non-positive for client"):
        LoopLimits.for_client(
            4_096, None, 4_000, no_progress_steps=4, error_streak=3, fixed_tokens=0
        )
    with pytest.raises(ConfigError, match="context budget too small for client"):
        LoopLimits.for_client(1_027, None, 2, no_progress_steps=4, error_streak=3, fixed_tokens=0)
    for bad in [
        {"soft_tokens": 0, "hard_tokens": 10},
        {"soft_tokens": 10, "hard_tokens": 10},
        {"soft_tokens": 10, "hard_tokens": 20_000},
        {"soft_tokens": 10, "hard_tokens": 20, "error_streak": 0},
    ]:
        with pytest.raises(ValidationError):
            LoopLimits(context_budget_tokens=10_000, **bad)
    with pytest.raises(ValidationError):
        LIMITS.soft_tokens = 1  # type: ignore[misc]


def _result(**overrides: Any) -> AgentResult:
    fields: dict[str, Any] = {
        "status": "completed",
        "stop_reason": "final",
        "output": {"answer": 1},
        "steps": 3,
        "usage": Usage(),
        "cost_usd": Decimal("0.1"),
        "query_ids": [Q1],
        "finding_ids": [],
    }
    fields.update(overrides)
    return AgentResult(**fields)


def test_ut05_103_agent_result_completed_iff_final() -> None:
    """UT05-103 AgentResult: completed exactly when stop_reason is final (U05-16)."""
    assert _result().error is None
    assert _result(status="partial", stop_reason="max_steps", output=None).steps == 3
    for status, reason in [("completed", "max_steps"), ("partial", "final"), ("failed", "final")]:
        with pytest.raises(ValidationError, match="completed"):
            _result(status=status, stop_reason=reason)


@pytest.mark.parametrize(
    "overrides",
    [
        {"stop_reason": "other", "status": "partial"},
        {"steps": -1},
        {"steps": True},
        {"cost_usd": Decimal("-1")},
        {"cost_usd": Decimal("NaN")},
        {"extra": 1},
    ],
)
def test_ut05_109_agent_result_rejects_invalid_fields(overrides: dict[str, Any]) -> None:
    """UT05-109 AgentResult rejects unknown stop reasons, bad counts and costs (U05-16)."""
    with pytest.raises(ValidationError):
        _result(**overrides)


def test_ut05_109_agent_result_is_frozen() -> None:
    """UT05-109 AgentResult is frozen and accepts every R-22 stop reason."""
    result = _result(status="partial", stop_reason="wall_clock", error="late")
    with pytest.raises(ValidationError):
        result.steps = 4  # type: ignore[misc]
    for reason in ["task_budget", "error_streak", "repeat_call", "cancelled", "refusal"]:
        assert _result(status="failed", stop_reason=reason).stop_reason == reason
