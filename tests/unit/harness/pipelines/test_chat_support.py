"""UT06-86 … UT06-90: chat constants and pure helpers (U06-126, U06-130 … U06-133)."""

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import JsonValue

from herness.core.types.harness import NumberRef, VerificationResult
from herness.core.types.harness.evidence import ItemResult, NumberCheck, UncitedSpan
from herness.core.types.harness.tooling import ToolContext, ToolResult
from herness.core.types.swarm import (
    CHAT_EVENT_ADAPTER,
    ChatAnswer,
    ChatEvent,
    EvidenceEvent,
    ToolEvent,
)
from herness.harness.pipelines.chat_support import (
    CHAT_TOOLS,
    EGRESS_NOTICE,
    MODE_MESSAGES,
    ObservedTool,
    chunk_text,
    has_review_intent,
    trim_failing_claims,
)

pytestmark = pytest.mark.unit

Q1 = "q_" + "1" * 16
Q2 = "q_" + "2" * 16
Q3 = "q_" + "3" * 16
CTX = cast("ToolContext", object())


# --- UT06-86 ---------------------------------------------------------------------------------


def test_ut06_86_what_should_we_fund_is_funding_review() -> None:
    """UT06-86 "what should we fund" is review intent of kind funding_review."""
    assert has_review_intent("So, what should we fund next quarter?", {}) == (
        True,
        "funding_review",
    )


def test_ut06_86_which_teams_should_improve_is_org_review() -> None:
    """UT06-86 "which teams should improve" is review intent of kind org_review."""
    assert has_review_intent("Which teams should improve their MTTR?", {}) == (True, "org_review")
    assert has_review_intent("which team needs to improve most", {}) == (True, "org_review")


def test_ut06_86_plain_question_is_not_review_intent() -> None:
    """UT06-86 a plain question is not review intent."""
    assert has_review_intent("How many incidents did payments have last week?", {}) == (
        False,
        None,
    )
    assert has_review_intent("Rank the payments team", {"team": ["t1"]}) == (False, None)


def test_ut06_86_ranking_needs_three_entities_or_a_plural_noun() -> None:
    """UT06-86 ranking words count only with three or more entities or a plural noun."""
    three = {"team": ["t1", "t2"], "service": ["s1"]}
    assert has_review_intent("rank these for me", three) == (True, "org_review")
    assert has_review_intent("Prioritise the services by cost", {}) == (True, "org_review")
    assert has_review_intent("top 5 epics by ROI", {}) == (True, "funding_review")
    assert has_review_intent("ranking of candidates", {}) == (True, "funding_review")


def test_ut06_86_candidate_entities_make_funding_review() -> None:
    """UT06-86 candidate entities found make the kind funding_review."""
    entities = {"candidate": ["c1", "c2", "c3"]}
    assert has_review_intent("rank these", entities) == (True, "funding_review")


# --- UT06-87 ---------------------------------------------------------------------------------


def _num(nid: str, query_id: str, value: int) -> NumberRef:
    return NumberRef(id=nid, value=value, unit="count", query_id=query_id, column="c", row_key=None)


def _check(num: NumberRef, result: str) -> NumberCheck:
    return NumberCheck.model_validate(
        {
            "number_id": num.id,
            "query_id": num.query_id,
            "column": num.column,
            "row_key": None,
            "claimed": num.value,
            "actual": num.value,
            "result": result,
        }
    )


def _verification(
    checks: list[NumberCheck],
    *,
    unknown: list[str] | None = None,
    uncited: list[UncitedSpan] | None = None,
) -> VerificationResult:
    unknown = unknown or []
    uncited = uncited or []
    passed = not unknown and not uncited and all(c.result == "match" for c in checks)
    item = ItemResult(
        where="chat",
        passed=passed,
        checks=checks,
        uncited=uncited,
        unknown_markers=unknown,
        bad_refs=[],
        unverified_findings=[],
    )
    return VerificationResult(
        build_id="b1",
        passed=passed,
        items=[item],
        n_numbers=len(checks),
        n_failed=sum(1 for c in checks if c.result != "match"),
        verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        duration_ms=0,
    )


def test_ut06_87_failing_marker_sentence_removed_and_number_dropped() -> None:
    """UT06-87 the sentence with the failing marker is removed and its number dropped."""
    n1, n2 = _num("n1", Q1, 4), _num("n2", Q2, 7)
    answer = ChatAnswer(
        text="Payments had [[n1]] incidents. Search had [[n2]] incidents! Both are teams.",
        numbers=[n1, n2],
        query_ids=[Q1, Q2, Q3],
    )
    trimmed, removed = trim_failing_claims(
        answer, _verification([_check(n1, "match"), _check(n2, "mismatch")])
    )
    assert trimmed.text == "Payments had [[n1]] incidents. Both are teams."
    assert trimmed.numbers == [n1]
    assert trimmed.query_ids == [Q1, Q3]
    assert removed == ["Search had [[n2]] incidents!"]


def test_ut06_87_unknown_marker_and_uncited_span_drop_sentences() -> None:
    """UT06-87 unknown markers and uncited spans also remove their sentences."""
    n1 = _num("n1", Q1, 4)
    text = "Payments had [[n1]] incidents. Ghost [[n9]] value. Search had 12 incidents."
    start = text.index("12")
    answer = ChatAnswer(text=text, numbers=[n1], query_ids=[Q1])
    span = UncitedSpan(text="12", start=start, end=start + 2)
    trimmed, removed = trim_failing_claims(
        answer, _verification([_check(n1, "match")], unknown=["n9"], uncited=[span])
    )
    assert trimmed.text == "Payments had [[n1]] incidents."
    assert trimmed.numbers == [n1]
    assert removed == ["Ghost [[n9]] value.", "Search had 12 incidents."]


def test_ut06_87_everything_removed_gives_fallback_text() -> None:
    """UT06-87 an empty result text becomes the fixed no-answer sentence."""
    n1 = _num("n1", Q1, 4)
    answer = ChatAnswer(text="Payments had [[n1]] incidents.", numbers=[n1], query_ids=[Q1, Q3])
    trimmed, removed = trim_failing_claims(answer, _verification([_check(n1, "query_failed")]))
    assert trimmed.text == "No verified answer could be produced."
    assert trimmed.numbers == []
    assert trimmed.query_ids == []
    assert removed == ["Payments had [[n1]] incidents."]


def test_ut06_87_passing_answer_unchanged() -> None:
    """UT06-87 an answer without failures is returned unchanged."""
    n1 = _num("n1", Q1, 4)
    answer = ChatAnswer(text="Payments had [[n1]] incidents.", numbers=[n1], query_ids=[Q1])
    trimmed, removed = trim_failing_claims(answer, _verification([_check(n1, "match")]))
    assert trimmed == answer
    assert removed == []


def test_ut06_87_original_separators_kept_between_sentences() -> None:
    """UT06-87 paragraph breaks and list items between kept sentences survive a removal."""
    n1, n2 = _num("n1", Q1, 4), _num("n2", Q2, 7)
    text = (
        "Summary first.\n\nPayments had [[n1]] incidents. Search had [[n2]] incidents.\n"
        "- Checkout is stable.\n- Search is not. Fixed [[n2]] times!\nDone."
    )
    answer = ChatAnswer(text=text, numbers=[n1, n2], query_ids=[Q1, Q2])
    trimmed, removed = trim_failing_claims(
        answer, _verification([_check(n1, "match"), _check(n2, "mismatch")])
    )
    assert trimmed.text == (
        "Summary first.\n\nPayments had [[n1]] incidents.\n- Checkout is stable.\n"
        "- Search is not.\nDone."
    )
    assert removed == ["Search had [[n2]] incidents.", "Fixed [[n2]] times!"]
    assert trimmed.query_ids == [Q1]


def test_ut06_87_trailing_whitespace_leaves_no_empty_sentence() -> None:
    """UT06-87 trailing whitespace after the last sentence yields no empty sentence."""
    n1, n2 = _num("n1", Q1, 4), _num("n2", Q2, 7)
    answer = ChatAnswer(
        text="Payments had [[n1]] incidents. Search had [[n2]]. ", numbers=[n1, n2], query_ids=[]
    )
    trimmed, removed = trim_failing_claims(
        answer, _verification([_check(n1, "match"), _check(n2, "row_not_found")])
    )
    assert trimmed.text == "Payments had [[n1]] incidents."
    assert trimmed.query_ids == []
    assert removed == ["Search had [[n2]]."]


# --- UT06-88 ---------------------------------------------------------------------------------


def test_ut06_88_long_text_chunks_concatenate_to_input() -> None:
    """UT06-88 chunks of long text concatenate to the input and are at most 64 chars."""
    text = ("The payments team had [[n1]] incidents last week, mostly on checkout. " * 20).strip()
    chunks = chunk_text(text)
    assert "".join(chunks) == text
    assert all(0 < len(c) <= 64 for c in chunks)
    assert all(c.endswith(" ") for c in chunks[:-1])


def test_ut06_88_no_whitespace_breaks_hard_and_empty_gives_nothing() -> None:
    """UT06-88 a window without whitespace breaks at the size; empty text gives no chunks."""
    assert chunk_text("x" * 150, 64) == ["x" * 64, "x" * 64, "x" * 22]
    assert chunk_text("") == []
    assert chunk_text("short text", 64) == ["short text"]
    assert chunk_text("ab cd", 3) == ["ab ", "cd"]


def test_ut06_88_invalid_size_rejected() -> None:
    """UT06-88 a non-positive size is rejected."""
    with pytest.raises(ValueError, match="size"):
        chunk_text("abc", 0)


# --- UT06-89 ---------------------------------------------------------------------------------


def test_ut06_89_cloud_lacks_text_tools_and_defer_is_empty() -> None:
    """UT06-89 `cloud` lacks the text tools and `defer` has no tools."""
    full = CHAT_TOOLS["live"]
    assert CHAT_TOOLS["small_model"] == full
    assert full == (
        "list_tables", "describe_table", "run_sql", "get_metric", "get_scores", "get_cluster",
        "get_record", "semantic_search", "recall_memory", "propose_memory", "list_findings",
        "escalate",
    )  # fmt: skip
    text_tools = {"get_record", "get_cluster", "semantic_search"}
    assert not text_tools & set(CHAT_TOOLS["cloud"])
    assert CHAT_TOOLS["cloud"] == tuple(t for t in full if t not in text_tools)
    assert CHAT_TOOLS["defer"] == ()


def test_ut06_89_mode_messages_verbatim() -> None:
    """UT06-89 the mode banner texts and the egress notice are the spec texts."""
    assert MODE_MESSAGES["live"] == ""
    assert MODE_MESSAGES["small_model"] == (
        "Outside chat hours: answering with a reduced model; answers may be less thorough."
    )
    assert MODE_MESSAGES["defer"] == (
        "The GPU is running scheduled work. Your question is queued and the answer will appear"
        " here when a slot is free."
    )
    assert MODE_MESSAGES["cloud"] == (
        "Answered by an off-network model under the approved data policy."
    )
    assert EGRESS_NOTICE == (
        "Answered by the local reduced model; the off-network request was blocked."
    )
    assert set(MODE_MESSAGES) == set(CHAT_TOOLS) == {"live", "small_model", "defer", "cloud"}


# --- UT06-90 ---------------------------------------------------------------------------------


class _SyncTool:
    name = "run_sql"
    description = "Run SQL."
    input_schema: dict[str, JsonValue] = {"type": "object"}  # noqa: RUF012 - test double

    def __init__(self, result: ToolResult | None = None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        self.calls.append(kwargs)
        if self.result is None:
            msg = "boom"
            raise RuntimeError(msg)
        return self.result


class _AsyncTool:
    name = "get_metric"
    description = "Get a metric."
    input_schema: dict[str, JsonValue] = {"type": "object"}  # noqa: RUF012 - test double

    def __init__(self, result: ToolResult) -> None:
        self.result = result

    async def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        return self.result


@pytest.mark.asyncio
async def test_ut06_90_two_query_ids_emit_tool_then_evidence_in_order() -> None:
    """UT06-90 a tool returning two query ids emits one tool event then two evidence events."""
    events: list[ChatEvent] = []
    inner = _SyncTool(ToolResult(ok=True, content="rows", query_ids=[Q1, Q2]))
    tool = ObservedTool(inner, events.append)
    assert (tool.name, tool.description, tool.input_schema) == (
        inner.name,
        inner.description,
        inner.input_schema,
    )
    result = await tool(CTX, sql="select 1")
    assert result is inner.result
    assert inner.calls == [{"sql": "select 1"}]
    assert events == [
        ToolEvent(name="run_sql", query_id=Q1, ok=True),
        EvidenceEvent(query_id=Q1),
        EvidenceEvent(query_id=Q2),
    ]
    dumped = [CHAT_EVENT_ADAPTER.dump_python(e) for e in events]
    assert [d["type"] for d in dumped] == ["tool", "evidence", "evidence"]


@pytest.mark.asyncio
async def test_ut06_90_async_tool_and_repeat_query_ids_not_reemitted() -> None:
    """UT06-90 async tools are awaited; query ids already emitted this turn are not repeated."""
    events: list[ChatEvent] = []
    tool = ObservedTool(
        _AsyncTool(ToolResult(ok=True, content="a", query_ids=[Q1, Q2])), events.append
    )
    await tool(CTX)
    await tool(CTX)
    assert len(events) == 4
    assert events[3:] == [ToolEvent(name="get_metric", query_id=Q1, ok=True)]


@pytest.mark.asyncio
async def test_ut06_90_wrappers_sharing_seen_emit_each_query_id_once_per_turn() -> None:
    """UT06-90 two wrappers sharing one `seen` set emit a query id as evidence once."""
    events: list[ChatEvent] = []
    seen: set[str] = set()
    sql = ObservedTool(
        _SyncTool(ToolResult(ok=True, content="a", query_ids=[Q1, Q2])), events.append, seen=seen
    )
    metric = ObservedTool(
        _AsyncTool(ToolResult(ok=True, content="b", query_ids=[Q2, Q3])), events.append, seen=seen
    )
    await sql(CTX)
    await metric(CTX)
    assert events == [
        ToolEvent(name="run_sql", query_id=Q1, ok=True),
        EvidenceEvent(query_id=Q1),
        EvidenceEvent(query_id=Q2),
        ToolEvent(name="get_metric", query_id=Q2, ok=True),
        EvidenceEvent(query_id=Q3),
    ]
    assert seen == {Q1, Q2, Q3}


class _AwaitableReturningTool:
    """A tool whose plain `__call__` returns a coroutine instead of being `async def`."""

    name = "get_scores"
    description = "Get scores."
    input_schema: dict[str, JsonValue] = {"type": "object"}  # noqa: RUF012 - test double

    def __init__(self, result: object) -> None:
        self.result = result

    def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> Any:
        async def run() -> object:
            return self.result

        return run()


@pytest.mark.asyncio
async def test_ut06_90_awaitable_returned_by_plain_call_is_awaited() -> None:
    """UT06-90 an awaitable returned by a non-async `__call__` is awaited."""
    events: list[ChatEvent] = []
    result = ToolResult(ok=True, content="s", query_ids=[Q1])
    tool = ObservedTool(cast("Any", _AwaitableReturningTool(result)), events.append)
    assert await tool(CTX) is result
    assert events == [
        ToolEvent(name="get_scores", query_id=Q1, ok=True),
        EvidenceEvent(query_id=Q1),
    ]


@pytest.mark.asyncio
async def test_ut06_90_non_tool_result_raises_and_emits_failed_event() -> None:
    """UT06-90 a tool returning something other than a ToolResult fails like a raised error."""
    events: list[ChatEvent] = []
    tool = ObservedTool(cast("Any", _AwaitableReturningTool("oops")), events.append)
    with pytest.raises(TypeError, match="ToolResult"):
        await tool(CTX)
    assert events == [ToolEvent(name="get_scores", query_id=None, ok=False)]


@pytest.mark.asyncio
async def test_ut06_90_failed_result_and_no_query_ids() -> None:
    """UT06-90 a failed result without query ids emits one tool event with no query id."""
    events: list[ChatEvent] = []
    failed = ToolResult.model_validate(
        {"ok": False, "content": "ERR", "error": {"type": "X", "message": "m", "hint": None}}
    )
    await ObservedTool(_SyncTool(failed), events.append)(CTX)
    assert events == [ToolEvent(name="run_sql", query_id=None, ok=False)]


@pytest.mark.asyncio
async def test_ut06_90_raised_error_emits_failed_tool_event_and_reraises() -> None:
    """UT06-90 a raised error emits ToolEvent(ok=False) and re-raises."""
    events: list[ChatEvent] = []
    with pytest.raises(RuntimeError, match="boom"):
        await ObservedTool(_SyncTool(None), events.append)(CTX)
    assert events == [ToolEvent(name="run_sql", query_id=None, ok=False)]
