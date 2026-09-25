"""Tests for herness.core.types.harness.llm (U05-01, U05-02, U05-03)."""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from herness.core.types import (
    LLMRequest,
    LLMResponse,
    Message,
    ReasoningPart,
    RequestMeta,
    SystemBlock,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    Usage,
)

pytestmark = pytest.mark.unit

META = RequestMeta(
    run_id="run_1",
    task_id="task_1",
    role="analyst",
    model_role="reasoning",
    step=0,
    request_key="task_1:0:step",
)
USER = Message(role="user", parts=[TextPart(text="hi")])


def _call(name: str = "run_sql") -> ToolCall:
    return ToolCall(id="c1", name=name, arguments={"sql": "select 1", "n": [1, 2]})


def _request(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "client": "local",
        "messages": [USER],
        "max_output_tokens": 100,
        "timeout_s": 30.0,
        "metadata": META,
    }
    fields.update(overrides)
    return LLMRequest(**fields)


def _spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description="d", input_schema={"type": "object"})


def _response(cost: Decimal) -> LLMResponse:
    return LLMResponse(
        text="ok",
        tool_calls=[],
        parsed=None,
        reasoning=[],
        stop_reason="end_turn",
        raw_stop_reason="stop",
        refusal_category=None,
        usage=Usage(),
        cost_usd=cost,
        client="local",
        model="m",
        provider="openai_compat",
        latency_ms=5,
        request_id=None,
    )


def test_ut05_01_valid_messages_round_trip() -> None:
    """UT05-01 every part and each role build, and Message round-trips through JSON."""
    reasoning = ReasoningPart(text="think", provider="anthropic", opaque={"signature": "s"})
    messages = [
        USER,
        Message(
            role="assistant",
            parts=[reasoning, TextPart(text="calling"), ToolCallPart(call=_call())],
        ),
        Message(role="tool", parts=[ToolResultPart(tool_call_id="c1", content="1 row")]),
        Message(role="user", parts=[TextPart(text="wrap up")], kind="nudge"),
    ]
    for msg in messages:
        again = Message.model_validate_json(msg.model_dump_json())
        assert again == msg
    assert messages[2].parts[0] == ToolResultPart(tool_call_id="c1", content="1 row")
    assert ToolResultPart(tool_call_id="c1", content="x").is_error is False
    assert ReasoningPart(provider="p").text == ""
    assert ToolCall(id="c", name="t", arguments={}).raw_arguments is None


@pytest.mark.parametrize(
    ("role", "parts"),
    [
        ("tool", [TextPart(text="x")]),
        ("tool", [ToolResultPart(tool_call_id="c1", content="x"), TextPart(text="y")]),
        ("tool", [ToolCallPart(call=ToolCall(id="c1", name="t", arguments={}))]),
        ("user", [ToolResultPart(tool_call_id="c1", content="x")]),
        ("user", [ToolCallPart(call=ToolCall(id="c1", name="t", arguments={}))]),
        ("user", [ReasoningPart(provider="p")]),
        ("assistant", [ToolResultPart(tool_call_id="c1", content="x")]),
    ],
)
def test_ut05_01_invalid_role_part_mixes_raise(role: str, parts: list[Any]) -> None:
    """UT05-01 a tool message holds only tool results, a user message only text,
    an assistant message no tool result."""
    with pytest.raises(ValidationError):
        Message(role=role, parts=parts)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fields",
    [
        {"id": "", "name": "t", "arguments": {}},
        {"id": "c" * 129, "name": "t", "arguments": {}},
        {"id": "c", "name": "Bad", "arguments": {}},
        {"id": "c", "name": "a" * 65, "arguments": {}},
        {"id": "c", "name": "t", "arguments": [1, 2]},
        {"id": "c", "name": "t", "arguments": {}, "extra": 1},
    ],
)
def test_ut05_01_tool_call_limits(fields: dict[str, Any]) -> None:
    """UT05-01 ToolCall id length, name pattern, object arguments and no extra keys."""
    with pytest.raises(ValidationError):
        ToolCall.model_validate(fields)


def test_ut05_01_part_and_message_limits() -> None:
    """UT05-01 text, tool result and part-count limits are enforced."""
    assert len(TextPart(text="a" * 200_000).text) == 200_000
    with pytest.raises(ValidationError):
        TextPart(text="a" * 200_001)
    with pytest.raises(ValidationError):
        ToolResultPart(tool_call_id="c", content="a" * 12_001)
    with pytest.raises(ValidationError):
        Message(role="user", parts=[])
    with pytest.raises(ValidationError):
        Message(role="user", parts=[TextPart(text="x")] * 257)
    assert len(Message(role="user", parts=[TextPart(text="x")] * 256).parts) == 256
    with pytest.raises(ValidationError):
        Message.model_validate({"role": "user", "parts": [{"type": "image", "text": "x"}]})


def test_ut05_01_frozen_parts_reject_mutation() -> None:
    """UT05-01 every message model is frozen."""
    call = _call()
    items: list[tuple[Any, str, object]] = [
        (TextPart(text="x"), "text", "y"),
        (call, "name", "other"),
        (ToolCallPart(call=call), "call", call),
        (ToolResultPart(tool_call_id="c", content="x"), "content", "y"),
        (ReasoningPart(provider="p"), "text", "y"),
        (USER, "kind", "nudge"),
    ]
    for obj, attr, value in items:
        with pytest.raises(ValidationError):
            setattr(obj, attr, value)


def test_ut05_02_schema_name_pairing() -> None:
    """UT05-02 response_schema and response_schema_name are set together or not at all."""
    with pytest.raises(ValidationError):
        _request(response_schema={"type": "object"})
    with pytest.raises(ValidationError):
        _request(response_schema_name="Finding")
    with pytest.raises(ValidationError):
        _request(response_schema={"type": "object"}, response_schema_name="1bad")
    req = _request(response_schema={"type": "object"}, response_schema_name="Finding")
    assert req.response_schema_name == "Finding"
    assert _request().response_schema is None


def test_ut05_02_tools_sorted_and_unique() -> None:
    """UT05-02 tools come back sorted by name; duplicate names raise."""
    req = _request(tools=[_spec("run_sql"), _spec("describe"), _spec("list_tables")])
    assert [t.name for t in req.tools] == ["describe", "list_tables", "run_sql"]
    with pytest.raises(ValidationError):
        _request(tools=[_spec("run_sql"), _spec("describe"), _spec("run_sql")])


def test_ut05_02_defaults_and_limits() -> None:
    """UT05-02 defaults match design §4.2 and field limits are enforced."""
    req = _request(system=[SystemBlock(text="sys", cache=True)])
    assert (req.tool_choice, req.parallel_tool_calls, req.thinking) == ("auto", True, "auto")
    assert (req.temperature, req.effort, req.seed, req.stop) == (None, None, None, [])
    assert req.tools == []
    assert SystemBlock(text="s").cache is False
    assert _spec("t").strict is True
    bad: list[dict[str, Any]] = [
        {"messages": []},
        {"max_output_tokens": 0},
        {"temperature": 2.1},
        {"temperature": -0.1},
        {"stop": ["a", "b", "c", "d", "e"]},
        {"timeout_s": 0},
        {"effort": "extreme"},
        {"api_key": "sk-secret"},
    ]
    for overrides in bad:
        with pytest.raises(ValidationError):
            _request(**overrides)
    with pytest.raises(ValidationError):
        ToolSpec(name="t", description="d" * 1_025, input_schema={})
    with pytest.raises(ValidationError):
        RequestMeta.model_validate({**META.model_dump(), "step": -1})
    with pytest.raises(ValidationError):
        RequestMeta.model_validate({**META.model_dump(), "step": True})
    with pytest.raises(ValidationError):
        RequestMeta.model_validate({**META.model_dump(), "request_key": "k" * 201})
    with pytest.raises(ValidationError):
        req.client = "other"  # type: ignore[misc]


def test_ut05_02_request_round_trip() -> None:
    """UT05-02 a full request survives a JSON round trip unchanged."""
    req = _request(
        tools=[_spec("b"), _spec("a")],
        response_schema={"type": "object"},
        response_schema_name="Out",
        stop=["END"],
        effort="high",
        thinking_budget_tokens=2_000,
        seed=7,
        temperature=0.0,
    )
    assert LLMRequest.model_validate_json(req.model_dump_json()) == req


def test_ut05_03_usage_plus_and_prompt_total() -> None:
    """UT05-03 Usage.plus sums field-wise; prompt_total is input plus cache read and write."""
    a = Usage(input_tokens=10, output_tokens=5, cache_read_tokens=3, cache_write_tokens=2)
    b = Usage(input_tokens=1, output_tokens=2, reasoning_tokens=4, cache_read_tokens=7)
    total = a.plus(b)
    assert total == Usage(
        input_tokens=11,
        output_tokens=7,
        cache_read_tokens=10,
        cache_write_tokens=2,
        reasoning_tokens=4,
    )
    assert a.prompt_total() == 15
    assert Usage().prompt_total() == 0
    with pytest.raises(ValidationError):
        Usage(output_tokens=-1)
    with pytest.raises(ValidationError):
        Usage(input_tokens=True)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (Decimal("0.0000005"), Decimal("0.000000")),
        (Decimal("0.0000015"), Decimal("0.000002")),
        (Decimal("0.0000025"), Decimal("0.000002")),
        (Decimal("1.23456749"), Decimal("1.234567")),
        (Decimal(3), Decimal("3.000000")),
    ],
)
def test_ut05_03_cost_quantized_half_even(given: Decimal, expected: Decimal) -> None:
    """UT05-03 cost_usd is quantized to 6 decimal places with ROUND_HALF_EVEN."""
    cost = _response(given).cost_usd
    assert cost == expected
    assert cost.as_tuple().exponent == -6


def test_ut05_03_response_rules() -> None:
    """UT05-03 negative cost raises, the response is frozen and round-trips JSON."""
    with pytest.raises(ValidationError):
        _response(Decimal("-0.000001"))
    for huge in (Decimal("1e22"), Decimal("1e30")):
        with pytest.raises(ValidationError):
            _response(huge)
    resp = _response(Decimal("0.1234567"))
    assert resp.batch is False
    assert LLMResponse.model_validate_json(resp.model_dump_json()) == resp
    with pytest.raises(ValidationError):
        resp.text = "x"  # type: ignore[misc]
