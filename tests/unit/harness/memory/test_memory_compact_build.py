"""Tests for herness.harness.memory.compact_build (impl 07 U07-70 … U07-75; design 07 §5.4)."""

import re
from typing import Any

import pytest

from herness.core.numbers import compile_allowed_patterns
from herness.core.types import (
    Message,
    NumberRef,
    ReasoningPart,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
)
from herness.harness.memory import compact_build as cb
from herness.harness.memory.compact_build import (
    Group,
    ParsedTable,
    build_compacted,
    cited_from_group,
    deterministic_notes,
    entry_from_result,
    split_groups,
    validate_notes,
)
from herness.harness.memory.working import CompactionNotes, LedgerEntry, Scratchpad

pytestmark = pytest.mark.unit

Q1 = "q_3f9a0c1d2e4b5a67"
Q2 = "q_91ab0c1d2e4b5a11"
Q3 = "q_77cd0c1d2e4b5a22"
ALLOWED = compile_allowed_patterns([r"\b(19|20)\d{2}\b", r"\bQ[1-4]\b"])
TABLE = (
    f"query_id={Q1} rows=3 shown=3 truncated=no ordered=yes\n"
    "team_id | incidents | mttr_h\n"
    "VARCHAR | BIGINT | DOUBLE\n"
    "grp_db_ops | 12 | 41.2\n"
    "grp_web | 7 | 3.14159\n"
    "grp_pay | 1200 | 0.125"
)


def _call(call_id: str, name: str = "run_sql", **args: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args or {"sql": "SELECT 1"})


def _asst(*parts: Any) -> Message:
    return Message(role="assistant", parts=list(parts))


def _calls(*calls: ToolCall, text: str | None = None) -> Message:
    parts: list[Any] = [TextPart(text=text)] if text else []
    return _asst(*parts, *(ToolCallPart(call=c) for c in calls))


def _tool(*results: tuple[Any, ...]) -> Message:
    parts: list[Any] = [
        ToolResultPart(tool_call_id=r[0], content=r[1], is_error=len(r) > 2 and r[2])
        for r in results
    ]
    return Message(role="tool", parts=parts)


def _user(text: str, kind: str = "normal") -> Message:
    return Message(role="user", parts=[TextPart(text=text)], kind=kind)  # type: ignore[arg-type]


def _flat(groups: list[Group]) -> list[int]:
    return [i for g in groups for i in g.indices]


# ---------------------------------------------------------------- U07-70 split_groups


def test_ut07_53_preamble_groups_and_steps() -> None:
    """UT07-53 preamble, tool groups with nudges and trailing text, steps from first_step."""
    msgs = [
        _user("<scratchpad>...</scratchpad>", kind="compaction_summary"),
        _calls(_call("a")),
        _tool(("a", TABLE)),
        _user("please continue", kind="nudge"),
        _asst(TextPart(text="thinking out loud")),
        _calls(_call("b"), _call("c")),
        _tool(("b", "x"), ("c", "y")),
    ]
    groups = split_groups(msgs, 5)
    assert [g.indices for g in groups] == [(1,), (2, 3, 4, 5), (6, 7)]
    assert [g.step for g in groups] == [5, 6, 7]
    assert [g.is_preamble for g in groups] == [True, False, False]
    assert [g.is_summary for g in groups] == [True, False, False]


def test_ut07_53_no_preamble_and_empty() -> None:
    """UT07-53 an empty preamble is omitted; no messages gives no groups."""
    msgs = [_calls(_call("a")), _tool(("a", "r"))]
    groups = split_groups(msgs, 0)
    assert groups == [Group(indices=(1, 2), step=1, is_preamble=False, is_summary=False)]
    assert split_groups([], 3) == []
    only = split_groups([_user("hi")], 2)
    assert only == [Group(indices=(1,), step=2, is_preamble=True, is_summary=False)]


def test_ut07_53_late_result_merges_groups() -> None:
    """UT07-53 a result after a later call keeps its call's group whole (groups merge)."""
    msgs = [
        _calls(_call("a")),
        _calls(_call("b")),
        _tool(("b", "rb"), ("a", "ra")),
        _calls(_call("c")),
        _tool(("c", "rc")),
    ]
    groups = split_groups(msgs, 0)
    assert [g.indices for g in groups] == [(1, 2, 3), (4, 5)]
    assert [g.step for g in groups] == [1, 3]
    assert _flat(groups) == [1, 2, 3, 4, 5]


def test_ut07_53_early_result_and_orphans() -> None:
    """UT07-53 a result before its call merges forward; orphans stay; preamble never merges."""
    msgs = [
        _tool(("z", "preamble result of a later call")),
        _calls(_call("a")),
        _tool(("b", "early"), ("nobody", "orphan")),
        _calls(_call("b")),
        _calls(_call("z")),
    ]
    groups = split_groups(msgs, 0)
    assert [g.indices for g in groups] == [(1,), (2, 3, 4), (5,)]
    assert [g.is_preamble for g in groups] == [True, False, False]


def test_ut07_53_duplicate_call_ids_use_first() -> None:
    """UT07-53 a reused tool_call_id binds its results to the first call."""
    msgs = [_calls(_call("a")), _calls(_call("a")), _tool(("a", "r"))]
    assert [g.indices for g in split_groups(msgs, 0)] == [(1, 2, 3)]


# ---------------------------------------------------------------- U07-71 entry_from_result


def test_ut07_54_spec05_table() -> None:
    """UT07-54 a spec 05 table gives one primary entry with table details and a sample."""
    call = _call("a", sql="SELECT  team_id,\n\t incidents\nFROM t")
    result = ToolResultPart(tool_call_id="a", content=TABLE + f"\nsee also {Q2} and {Q1}")
    entries, table = entry_from_result(call, result, 4, 60)
    assert [e.query_id for e in entries] == [Q1, Q2]
    first = entries[0]
    assert first == LedgerEntry(
        query_id=Q1,
        tool="run_sql",
        step=4,
        sql_head="SELECT team_id, incidents FROM t",
        row_count=3,
        columns=["team_id", "incidents", "mttr_h"],
        sample=[
            {"team_id": "grp_db_ops", "incidents": "12", "mttr_h": "41.2"},
            {"team_id": "grp_web", "incidents": "7", "mttr_h": "3.14159"},
            {"team_id": "grp_pay", "incidents": "1200", "mttr_h": "0.125"},
        ],
    )
    assert entries[1] == LedgerEntry(query_id=Q2, tool="run_sql", step=4)
    assert table == ParsedTable(
        query_id=Q1,
        columns=["team_id", "incidents", "mttr_h"],
        rows=[
            ["grp_db_ops", "12", "41.2"],
            ["grp_web", "7", "3.14159"],
            ["grp_pay", "1200", "0.125"],
        ],
        row_count=3,
    )


def test_ut07_54_sample_rule_and_sql_head() -> None:
    """UT07-54 no sample above max_cells; sql_head only for run_sql, cut to 200 chars."""
    long_sql = "SELECT " + "x, " * 150
    entries, _ = entry_from_result(
        _call("a", sql=long_sql), ToolResultPart(tool_call_id="a", content=TABLE), 1, 8
    )
    assert entries[0].sample == []
    assert entries[0].sql_head is not None
    assert len(entries[0].sql_head) == 200
    other, _ = entry_from_result(
        _call("b", name="get_metric", metric="mttr"),
        ToolResultPart(tool_call_id="b", content=TABLE),
        1,
        60,
    )
    assert other[0].sql_head is None
    assert other[0].tool == "get_metric"
    bad_sql, _ = entry_from_result(
        _call("c", sql=5), ToolResultPart(tool_call_id="c", content=TABLE), 1, 60
    )
    assert bad_sql[0].sql_head is None


def test_ut07_54_single_column_and_row_end() -> None:
    """UT07-54 one-column results use `shown`; rows end at an empty or ragged line."""
    content = (
        f"query_id={Q2} rows=1 shown=1 truncated=no ordered=no\nvalue\nDOUBLE\n0.183\n"
        "rows shown are arbitrary; add ORDER BY"
    )
    entries, table = entry_from_result(
        _call("a", name="get_metric"), ToolResultPart(tool_call_id="a", content=content), 7, 60
    )
    assert entries[0].sample == [{"value": "0.183"}]
    assert table is not None
    assert table.rows == [["0.183"]]
    ragged = f"query_id={Q1} rows=9\na | b\nX | Y\n1 | 2\n3 | 4 | 5\n6 | 7"
    _, table = entry_from_result(
        _call("b"), ToolResultPart(tool_call_id="b", content=ragged), 1, 60
    )
    assert table is not None
    assert table.rows == [["1", "2"]]
    blank = f"query_id={Q1} rows=9\na | b\nX | Y\n1 | 2\n\n6 | 7"
    _, table = entry_from_result(_call("b"), ToolResultPart(tool_call_id="b", content=blank), 1, 60)
    assert table is not None
    assert table.rows == [["1", "2"]]


def test_ut07_54_row_cap_and_wide_tables() -> None:
    """UT07-54 at most 200 rows; ledger columns capped at 50."""
    rows = "\n".join(f"{i} | {i}" for i in range(250))
    content = f"query_id={Q1} rows=250\nk | v\nINT | INT\n{rows}"
    _, table = entry_from_result(
        _call("a"), ToolResultPart(tool_call_id="a", content=content), 1, 60
    )
    assert table is not None
    assert len(table.rows) == 200
    assert table.row_count == 250
    cols = " | ".join(f"c{i}" for i in range(60))
    wide = f"query_id={Q1} rows=0\n{cols}\n{cols}"
    entries, table = entry_from_result(
        _call("a"), ToolResultPart(tool_call_id="a", content=wide), 1, 60
    )
    assert table is not None
    assert len(table.columns) == 60
    assert len(entries[0].columns) == 50


def test_ut07_54_error_and_unparsable() -> None:
    """UT07-54 an error result without ids gives one `""` entry; plain text gives none."""
    err = ToolResultPart(tool_call_id="a", content='column "prio"\nnot found ' * 30, is_error=True)
    entries, table = entry_from_result(_call("a"), err, 9, 60)
    assert table is None
    assert len(entries) == 1
    assert entries[0].query_id == ""
    assert entries[0].error is not None
    assert len(entries[0].error) == 200
    assert "\n" not in entries[0].error
    ok = ToolResultPart(tool_call_id="a", content="tables: a, b")
    assert entry_from_result(_call("a"), ok, 1, 60) == ([], None)
    mention = ToolResultPart(tool_call_id="a", content=f"cached as {Q3}")
    entries, table = entry_from_result(_call("a"), mention, 2, 60)
    assert table is None
    assert entries == [LedgerEntry(query_id=Q3, tool="run_sql", step=2, sql_head="SELECT 1")]


# ---------------------------------------------------------------- U07-72 cited_from_group


def _table() -> ParsedTable:
    _, table = entry_from_result(_call("a"), ToolResultPart(tool_call_id="a", content=TABLE), 1, 60)
    assert table is not None
    return table


def test_ut07_55_post_finding_refs_and_matches() -> None:
    """UT07-55 valid post_finding refs are cited; written numerals match table cells."""
    good = {
        "id": "n1", "value": 41.2, "unit": "other", "query_id": Q1, "column": "mttr_h",
        "row_key": {"team_id": "grp_db_ops"},
    }  # fmt: skip
    finding = _call("f", name="post_finding", numbers=[good, {"id": "bad"}, 3])
    msgs = [
        _calls(finding, _call("g", name="other_tool", numbers="not a list")),
        _tool(("f", "ok")),
        _asst(TextPart(text="MTTR 41.20 h, pi 3.14, 1,200 tickets, 12 teams in 2024 Q1; 99 x")),
        _user("user text 77 is ignored"),
    ]
    cited, unmatched = cited_from_group(msgs, [_table()], ALLOWED, 3)
    assert cited[0] == NumberRef.model_validate(good)
    rest = [(r.value, r.column, r.row_key) for r in cited[1:]]
    assert rest == [
        (41.2, "mttr_h", {"team_id": "grp_db_ops"}),
        (3.14159, "mttr_h", {"team_id": "grp_web"}),
        (1200, "incidents", {"team_id": "grp_pay"}),
        (12, "incidents", {"team_id": "grp_db_ops"}),
    ]
    assert all(r.id == "n0" and r.unit == "other" and r.query_id == Q1 for r in cited[1:])
    assert [(u.value, u.step) for u in unmatched] == [("99 x", 3)]


def test_ut07_55_beyond_decimal_precision() -> None:
    """UT07-55 a huge numeral near a huge cell past 60 digits stays unmatched."""
    table = ParsedTable(query_id=Q1, columns=["k", "v"], rows=[["a", "1e70"]], row_count=1)
    written = "1" + "0" * 70
    cited, unmatched = cited_from_group([_asst(TextPart(text=written))], [table], [], 4)
    assert cited == []
    assert [u.value for u in unmatched] == [written[:40]]


def test_ut07_56_missing_progress() -> None:
    """UT07-56 notes without progress fail validation and give None."""
    assert validate_notes({"dead_ends": ["x"]}, _pad(), ALLOWED) is None


def test_ut07_55_search_order_and_row_key() -> None:
    """UT07-55 most recent table first; key column and one-row tables carry no row_key."""
    old = ParsedTable(query_id=Q1, columns=["k", "v"], rows=[["a", "5"]], row_count=2)
    new = ParsedTable(query_id=Q2, columns=["k", "v"], rows=[["5", "5"]], row_count=1)
    cited, _ = cited_from_group([_asst(TextPart(text="got 5"))], [old, new], ALLOWED, 1)
    assert [(r.query_id, r.column, r.row_key) for r in cited] == [(Q2, "k", None)]
    cited, _ = cited_from_group([_asst(TextPart(text="got 5"))], [old], ALLOWED, 1)
    assert [(r.query_id, r.column, r.row_key) for r in cited] == [(Q1, "v", {"k": "a"})]


def test_ut07_55_rounding_and_unparsable() -> None:
    """UT07-55 half-even rounding, suffixes, signs; odd cells and numerals never match."""
    table = ParsedTable(
        query_id=Q1,
        columns=["k", "v"],
        rows=[["a", "nan"], ["b", "x1"], ["c", "1e400"], ["d", "0.125"], ["e", "-2.5"], ["", ""]],
        row_count=6,
    )
    text = "0.12 and -2 and $-2.5k and 0.13 and 9" + "9" * 90
    cited, unmatched = cited_from_group([_asst(TextPart(text=text))], [table], [], 2)
    assert [r.value for r in cited] == [0.125, -2.5, -2.5]
    assert [u.value for u in unmatched] == ["0.13", ("9" * 91)[:40]]
    wide = ParsedTable(query_id=Q1, columns=["k", "x" * 200], rows=[["a", "4"]], row_count=1)
    cited, unmatched = cited_from_group([_asst(TextPart(text="4 and ½"))], [wide], [], 2)
    assert cited == []
    assert [u.value for u in unmatched] == ["4", "½"]


# ---------------------------------------------------------------- U07-73 validate_notes


def _pad() -> Scratchpad:
    pad = Scratchpad()
    pad.upsert(LedgerEntry(query_id=Q1, tool="run_sql", step=1))
    pad.cite(NumberRef(id="n9", value=41.2, unit="other", query_id=Q1, column="m", row_key=None))
    return pad


def test_ut07_56_repairs_numbers_markers_ids() -> None:
    """UT07-56 stray numbers and bad markers become [[?]]; unknown ids are removed."""
    raw: dict[str, Any] = {
        "progress": f"MTTR [[n1]] vs [[n7]] and 38 tickets [[x]] [[?]] in 2024 via {Q2} {Q1}",
        "hypotheses": [
            {"text": "about 12%", "result": "supported", "query_ids": [Q1, Q2, "q_bogus"]},
        ],
        "dead_ends": ["tried 3 things"],
        "next_steps": ["[[5]] rerun"],
        "steps": ["model steps are discarded"],
    }
    notes = validate_notes(raw, _pad(), ALLOWED)
    assert notes == CompactionNotes(
        progress="MTTR [[n1]] vs [[?]] and [[?]] tickets [[?]] [[?]] in 2024 via [[?]] " + Q1,
        hypotheses=[{"text": "about [[?]]", "result": "supported", "query_ids": [Q1]}],  # type: ignore[list-item]
        dead_ends=["tried [[?]] things"],
        next_steps=["[[?]] rerun"],
    )


def test_ut07_56_cuts_and_rejects() -> None:
    """UT07-56 over-long strings are cut; None or invalid shapes give None."""
    pad = _pad()
    long: dict[str, Any] = {
        "progress": "a" * 900,
        "dead_ends": ["b" * 300],
        "hypotheses": [{"text": "c" * 400, "result": "unclear"}],
    }
    notes = validate_notes(long, pad, ALLOWED)
    assert notes is not None
    assert (len(notes.progress), len(notes.dead_ends[0]), len(notes.hypotheses[0].text)) == (
        600,
        200,
        300,
    )
    assert validate_notes(None, pad, ALLOWED) is None
    assert validate_notes({"progress": 5}, pad, ALLOWED) is None
    assert validate_notes({"progress": "x", "extra": 1}, pad, ALLOWED) is None
    assert validate_notes({"progress": "x", "hypotheses": "no"}, pad, ALLOWED) is None
    assert validate_notes({"progress": "x", "hypotheses": [3]}, pad, ALLOWED) is None


def test_ut07_56_repair_growth_stays_in_limit() -> None:
    """UT07-56 [[?]] growth past the field limit is cut again without leaving numerals."""
    notes = validate_notes({"progress": "1 " * 300}, _pad(), ALLOWED)
    assert notes is not None
    assert len(notes.progress) <= 600
    assert re.search(r"\d", notes.progress) is None
    dates = validate_notes({"progress": "a" * 595 + " 2024"}, _pad(), [re.compile(r"2024")])
    assert dates is not None
    assert dates.progress.endswith(" 2024")
    cut_date = validate_notes(
        {"progress": "7 " + "a" * 590 + " 20240"}, _pad(), [re.compile(r"\b20240\b")]
    )
    assert cut_date is not None
    assert cut_date.progress == "[[?]] " + "a" * 590 + " "
    assert re.search(r"\d", cut_date.progress) is None


# ---------------------------------------------------------------- U07-74 deterministic_notes


def test_ut07_57_step_lines() -> None:
    """UT07-57 one line per call of each dropped tool group, after prior steps."""
    msgs = [
        _user("task"),
        _user("pre"),
        _calls(_call("a", sql="SELECT " + "c, " * 40), _call("b", name="get_metric", m=1.5)),
        _tool(("a", TABLE), ("b", "boom", True)),
        _calls(_call("c", name="list_tables", x=float("inf"))),
        _tool(("c", "tables: a")),
        _calls(_call("d", name="describe_table", name_="t")),
    ]
    groups = split_groups(msgs[1:], 2)
    notes = deterministic_notes(groups, msgs, None)
    assert notes.progress == "model notes unavailable; see steps"
    head = '{"sql":"SELECT c, c, c, c, c, c, c, c, c, c, c, c, c, c, c, c, c, '
    assert notes.steps == [
        f"step 3: run_sql({head[:60]}) -> rows=3",
        'step 3: get_metric({"m":1.5}) -> ERROR',
        'step 4: list_tables({"x":Infinity}) -> rows=?',
        'step 5: describe_table({"name_":"t"}) -> rows=?',
    ]
    prior = CompactionNotes(progress="p", steps=[f"old {i}" for i in range(199)])
    again = deterministic_notes(groups, msgs, prior)
    assert again.progress == "p"
    assert len(again.steps) == 200
    assert again.steps[0] == "old 3"
    assert prior.steps[0] == "old 0"
    long_name = _calls(_call("e", name="t" * 64, a="b" * 200))
    huge = _tool(("e", f"query_id={Q1} rows={'9' * 40}"))
    groups = split_groups([long_name, huge], 0)
    line = deterministic_notes(groups, [msgs[0], long_name, huge], None).steps[0]
    assert len(line) == 160


# ---------------------------------------------------------------- U07-75 build_compacted


def _history() -> list[Message]:
    return [
        _user("original task"),
        _user("<scratchpad>old</scratchpad>", kind="compaction_summary"),
        _calls(_call("a")),
        _tool(("a", TABLE)),
        Message(
            role="assistant",
            parts=[ReasoningPart(text="why", provider="vllm"), TextPart(text="found 12")],
        ),
        Message(role="assistant", parts=[ReasoningPart(text="only thoughts", provider="vllm")]),
        _user("nudge", kind="nudge"),
        _calls(_call("b", name="get_metric", z=1, a=[2]), text="checking"),
        _tool(("b", f"query_id={Q2} rows=1\nvalue\nDOUBLE\n0.5"), ("stray", f"orphan {Q3}")),
    ]


def test_ut07_58_local_profile() -> None:
    """UT07-58 merged head, kept groups verbatim, no reasoning, empty messages omitted."""
    msgs = _history()
    groups = split_groups(msgs[1:], 0)
    out = build_compacted(msgs[0], "SCRATCH", groups[1:], msgs, fresh_conversation=False)
    assert out[0] == Message(
        role="user",
        kind="compaction_summary",
        parts=[TextPart(text="original task"), TextPart(text="SCRATCH")],
    )
    assert out[1:] == [
        msgs[2],
        msgs[3],
        Message(role="assistant", parts=[TextPart(text="found 12")]),
        msgs[6],
        msgs[7],
        msgs[8],
    ]
    assert not any(isinstance(p, ReasoningPart) for m in out for p in m.parts)


def test_ut07_58_local_merges_leading_user() -> None:
    """UT07-58 a kept user message right after the head joins it, so roles alternate."""
    msgs = _history()
    groups = split_groups(msgs[1:], 0)
    out = build_compacted(msgs[0], "S", groups, msgs, fresh_conversation=False)
    assert [m.role for m in out[:2]] == ["user", "assistant"]
    assert [p.text for p in out[0].parts if isinstance(p, TextPart)] == [
        "original task",
        "S",
        "<scratchpad>old</scratchpad>",
    ]
    assert out[0].kind == "compaction_summary"


def test_ut07_59_claude_profile() -> None:
    """UT07-59 one user message; transcript of kept groups; no ToolCall or Reasoning part."""
    msgs = _history()
    groups = split_groups(msgs[1:], 0)
    out = build_compacted(msgs[0], "SCRATCH", groups, msgs, fresh_conversation=True)
    assert len(out) == 1
    head = out[0]
    assert (head.role, head.kind) == ("user", "compaction_summary")
    assert all(isinstance(p, TextPart) for p in head.parts)
    texts = [p.text for p in head.parts if isinstance(p, TextPart)]
    assert texts[:2] == ["original task", "SCRATCH"]
    assert texts[2] == "\n".join(
        [
            "Recent tool calls (verbatim):",
            "user: <scratchpad>old</scratchpad>",
            'tool: run_sql args: {"sql":"SELECT 1"}',
            "result:",
            TABLE,
            "assistant: found 12",
            "user: nudge",
            "assistant: checking",
            'tool: get_metric args: {"a":[2],"z":1}',
            "result:",
            f"query_id={Q2} rows=1\nvalue\nDOUBLE\n0.5",
            "result:",
            f"orphan {Q3}",
        ]
    )


def test_ut07_59_claude_long_transcript_and_missing_result() -> None:
    """UT07-59 transcripts over one TextPart's limit span parts; a call with no result."""
    big = "r" * 12_000
    msgs: list[Message] = [_user("t")]
    for i in range(20):
        msgs += [_calls(_call(f"c{i}")), _tool((f"c{i}", big))]
    msgs.append(_calls(_call("last")))
    groups = split_groups(msgs[1:], 0)
    out = build_compacted(msgs[0], "S", groups, msgs, fresh_conversation=True)
    texts = [p.text for p in out[0].parts if isinstance(p, TextPart)]
    assert len(texts) == 4
    assert all(len(t) <= cb.TEXT_PART_MAX_CHARS for t in texts)
    assert "".join(texts[2:]).endswith('tool: run_sql args: {"sql":"SELECT 1"}')
