"""Tests for herness.eval.scripted_render: tool table parsing and turn rendering (T11-22).

UT11-64 parses the spec 05 §5.4.5 sample, UT11-65 renders `numbers_from` and templates
against real tool results, and PT11-05 round-trips generated tables through the real
spec 05 formatter (`herness.harness.tools.format_result`) and `parse_tool_table`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from herness.core.types import Message, TextPart, ToolResultPart
from herness.eval.scripted import ScriptMismatch, ScriptTurn
from herness.eval.scripted_render import ParsedToolTable, parse_tool_table, render_turn
from herness.harness.tools import RecordedResult, format_result

pytestmark = pytest.mark.unit

_SAMPLE = (
    "query_id=q_3f9a0c1d2e4b5a67 rows=1243 shown=200 truncated=yes ordered=no\n"
    "team_id | incidents | mttr_hours\n"
    "VARCHAR | BIGINT    | DOUBLE\n"
    "servicenow:sys_user_group:ab12 | 5321 | 7.41667\n"
    "rows shown are arbitrary; add ORDER BY"
)
_QID_A = "q_00000000000000aa"
_QID_B = "q_00000000000000bb"
_ONE_ROW = (
    f"query_id={_QID_A} rows=1 shown=1 truncated=no ordered=yes\n"
    "team_name | incidents | mttr_hours\n"
    "VARCHAR | BIGINT | DOUBLE\n"
    "Payments | 42 | 7.5"
)
_THREE_ROWS = (
    f"query_id={_QID_B} rows=3 shown=3 truncated=no ordered=yes\n"
    "team_name | cost_usd | mttr_hours | incident_rate\n"
    "VARCHAR | DECIMAL(18,2) | DOUBLE | DOUBLE\n"
    "Payments | 1234.5 | 7.5 | 0.25\n"
    "Search | 99.00 | 3 | 0.5\n"
    "Mobile | NULL | NULL | 0.75"
)


def _tool(content: str, call_id: str = "c1") -> Message:
    return Message(role="tool", parts=[ToolResultPart(tool_call_id=call_id, content=content)])


def _user(text: str = "go") -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


def _turn(raw: dict[str, Any]) -> ScriptTurn:
    return ScriptTurn.model_validate(raw)


# ---------------------------------------------------------------- UT11-64 parse_tool_table


def test_ut11_64_parses_spec05_sample() -> None:
    """UT11-64 the spec 05 §5.4.5 sample gives its query id, 3 columns and typed values."""
    table = parse_tool_table(_SAMPLE)
    assert table == ParsedToolTable(
        query_id="q_3f9a0c1d2e4b5a67",
        columns=["team_id", "incidents", "mttr_hours"],
        types=["VARCHAR", "BIGINT", "DOUBLE"],
        rows=[["servicenow:sys_user_group:ab12", 5321, 7.41667]],
    )
    assert isinstance(table.rows[0][1], int)
    assert isinstance(table.rows[0][2], float)


def test_ut11_64_decimal_null_escapes_and_cut_cells() -> None:
    """UT11-64 DECIMAL → Decimal, NULL → None, `\\|` unescaped, unparsable number kept as text."""
    content = (
        "query_id=q_0000000000000001 rows=2 shown=2 truncated=no ordered=no\n"
        "name | amount | n | ratio | small | huge | real\n"
        "VARCHAR | DECIMAL(18,2) | INTEGER | FLOAT | SMALLINT | HUGEINT | REAL\n"
        "a\\|b\\nc | 12.30 | 7 | 0.5 | 1 | 99 | 1.5\n"
        "NULL | NULL | 12… | x | -1 | 0 | 2"
    )
    table = parse_tool_table(content)
    assert table is not None
    assert table.rows[0] == ["a|b\nc", Decimal("12.30"), 7, 0.5, 1, 99, 1.5]
    assert table.rows[1] == [None, None, "12…", "x", -1, 0, 2.0]


@pytest.mark.parametrize(
    "content",
    [
        "plain text result",
        "query_id=q_1 but no proper header",
        "query_id=q_0000000000000001 rows=1 shown=1 truncated=no ordered=no\na | b",
        "query_id=q_0000000000000001 rows=1 shown=1 truncated=no ordered=no\na | b\nX\n1 | 2",
        "query_id=q_0000000000000001 rows=1 shown=1 truncated=no ordered=no\na | b\nX | Y\n1",
    ],
)
def test_ut11_64_non_tables_return_none(content: str) -> None:
    """UT11-64 text that is not a well-formed table parses to None."""
    assert parse_tool_table(content) is None


# ---------------------------------------------------------------- UT11-65 render_turn


_FINDING = {
    "tool_calls": [
        {
            "name": "post_finding",
            "arguments": {
                "title": "{{row.team_name}} is slow",
                "claim": "Cost [[n1]] and MTTR [[n2]] hours; again [[n1]], rate [[n3]].",
                "numbers_from": "last_tool_result",
            },
        }
    ]
}


def test_ut11_65_numbers_from_one_row_table() -> None:
    """UT11-65 one-row table: n1, n2 from numeric columns, row_key None, units by suffix."""
    turn = _turn(
        {
            "tool_calls": [
                {
                    "name": "post_finding",
                    "arguments": {
                        "title": "{{row.team_name}} in {{last.query_id}}",
                        "claim": "[[n1]] incidents, MTTR [[n2]] h",
                        "numbers_from": "last_tool_result",
                    },
                }
            ]
        }
    )
    out = render_turn(turn, [_user(), _tool(_ONE_ROW)], call_index=3)
    assert out.stop_reason == "tool_use"
    assert out.text == ""
    assert out.parsed is None
    (call,) = out.tool_calls
    assert call.id == "call_3_0"
    assert call.name == "post_finding"
    args = call.arguments
    assert "numbers_from" not in args
    assert args["title"] == f"Payments in {_QID_A}"
    assert args["query_ids"] == [_QID_A]
    assert args["numbers"] == [
        {
            "id": "n1",
            "value": 42,
            "unit": "count",
            "query_id": _QID_A,
            "column": "incidents",
            "row_key": None,
            "format": None,
        },
        {
            "id": "n2",
            "value": 7.5,
            "unit": "hours",
            "query_id": _QID_A,
            "column": "mttr_hours",
            "row_key": None,
            "format": None,
        },
    ]


def test_ut11_65_numbers_from_three_row_table_uses_last_result() -> None:
    """UT11-65 the last parsable result (3 rows) wins: row_key {first column: value}, usd as 2dp."""
    messages = [_tool(_ONE_ROW), _user(), _tool(_THREE_ROWS, "c2"), _tool("error: timeout", "c3")]
    out = render_turn(_turn(_FINDING), messages)
    args = out.tool_calls[0].arguments
    assert args["title"] == "Payments is slow"
    numbers = args["numbers"]
    assert isinstance(numbers, list)
    assert [(n["id"], n["value"], n["unit"], n["column"]) for n in numbers] == [  # type: ignore[index,call-overload]
        ("n1", "1234.50", "usd", "cost_usd"),
        ("n2", 7.5, "hours", "mttr_hours"),
        ("n3", 0.25, "ratio", "incident_rate"),
    ]
    assert all(n["row_key"] == {"team_name": "Payments"} for n in numbers)  # type: ignore[index,call-overload]
    assert all(n["query_id"] == _QID_B for n in numbers)  # type: ignore[index,call-overload]


def test_ut11_65_explicit_numbers_map_and_existing_query_ids() -> None:
    """UT11-65 explicit `numbers.nK` column/unit win; given `query_ids` stay."""
    turn = _turn(
        {
            "final": {
                "output": {
                    "text": "Rate [[n1]] and [[n2]]",
                    "numbers_from": "last_tool_result",
                    "numbers": {"n1": {"column": "incident_rate", "unit": "pct"}},
                    "query_ids": ["q_00000000000000cc"],
                }
            }
        }
    )
    out = render_turn(turn, [_tool(_THREE_ROWS)])
    assert out.stop_reason == "end_turn"
    assert out.tool_calls == []
    assert out.parsed is not None
    assert out.parsed["query_ids"] == ["q_00000000000000cc"]
    n1, n2 = out.parsed["numbers"]
    assert (n1["column"], n1["unit"], n1["value"]) == ("incident_rate", "pct", 0.25)
    assert (n2["column"], n2["unit"]) == ("mttr_hours", "hours")
    assert out.text == json.dumps(out.parsed, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize(
    ("column", "sql_type", "cell", "unit", "value"),
    [
        ("score", "DECIMAL(9,3)", "1.500", "other", 1.5),
        ("wait_minutes", "BIGINT", "12", "minutes", 12),
        ("age_days", "INTEGER", "3", "days", 3),
        ("share_pct", "DOUBLE", "12.5", "pct", 12.5),
        ("close_ratio", "DOUBLE", "0.5", "ratio", 0.5),
        ("ticket_count", "BIGINT", "9", "count", 9),
        ("n_teams", "BIGINT", "4", "count", 4),
        ("spend_usd", "BIGINT", "7", "usd", "7.00"),
    ],
)
def test_ut11_65_units_by_suffix(
    column: str, sql_type: str, cell: str, unit: str, value: object
) -> None:
    """UT11-65 units come from the column suffix; DECIMAL non-usd values become floats."""
    content = (
        f"query_id={_QID_A} rows=1 shown=1 truncated=no ordered=yes\n"
        f"team | {column}\nVARCHAR | {sql_type}\nx | {cell}"
    )
    raw = {"final": {"output": {"text": "[[n1]]", "numbers_from": "last_tool_result"}}}
    out = render_turn(_turn(raw), [_tool(content)])
    assert out.parsed is not None
    (n1,) = out.parsed["numbers"]
    assert (n1["unit"], n1["value"], n1["column"]) == (unit, value, column)


def test_ut11_65_final_text_and_final_tool() -> None:
    """UT11-65 `final.text` renders text only; `final.<tool>` is one tool call."""
    text = render_turn(_turn({"final": {"text": "Done: {{row.team_name}}"}}), [_tool(_ONE_ROW)])
    assert (text.text, text.tool_calls, text.parsed, text.stop_reason) == (
        "Done: Payments",
        [],
        None,
        "end_turn",
    )
    raw = {"final": {"submit": {"q": "{{last.query_id}}"}}}
    tool = render_turn(_turn(raw), [_tool(_ONE_ROW)], call_index=1)
    assert (tool.text, tool.parsed, tool.stop_reason) == ("", None, "tool_use")
    (call,) = tool.tool_calls
    assert (call.id, call.name, call.arguments) == ("call_1_0", "submit", {"q": _QID_A})


def test_ut11_65_no_templates_need_no_table() -> None:
    """UT11-65 a turn without templates renders without any tool result; ids per index."""
    turn = _turn(
        {
            "tool_calls": [
                {"name": "run_sql", "arguments": {"sql": "select 1", "n": [1, {"k": None}]}},
                {"name": "list_tables"},
            ]
        }
    )
    out = render_turn(turn, [_user()], call_index=2)
    assert [c.id for c in out.tool_calls] == ["call_2_0", "call_2_1"]
    assert out.tool_calls[0].arguments == {"sql": "select 1", "n": [1, {"k": None}]}
    assert out.tool_calls[1].arguments == {}


def test_ut11_65_null_cell_renders_as_null_text() -> None:
    """UT11-65 a NULL row value fills a template as `NULL`."""
    three_null_first = _THREE_ROWS.replace("Payments | 1234.5", "NULL | 1234.5")
    out = render_turn(_turn({"final": {"text": "{{row.team_name}}"}}), [_tool(three_null_first)])
    assert out.text == "NULL"


@pytest.mark.parametrize(
    ("raw", "messages"),
    [
        ({"final": {"text": "{{row.team_name}}"}}, []),
        ({"final": {"text": "{{last.query_id}}"}}, [_tool("not a table")]),
        ({"final": {"text": "{{row.missing}}"}}, [_tool(_ONE_ROW)]),
        ({"final": {"text": "{{last.build_id}}"}}, [_tool(_ONE_ROW)]),
        (
            {"final": {"text": "{{row.a}}"}},
            [_tool(f"query_id={_QID_A} rows=0 shown=0 truncated=no ordered=yes\na\nBIGINT")],
        ),
        (
            {"final": {"output": {"text": "[[n4]]", "numbers_from": "last_tool_result"}}},
            [_tool(_ONE_ROW)],
        ),
        (
            {
                "final": {
                    "output": {
                        "text": "[[n1]]",
                        "numbers_from": "last_tool_result",
                        "numbers": {"n1": {"column": "team_name"}},
                    }
                }
            },
            [_tool(_ONE_ROW)],
        ),
        (
            {
                "final": {
                    "output": {
                        "text": "[[n1]]",
                        "numbers_from": "last_tool_result",
                        "numbers": {"n1": {"column": "team_name", "unit": "usd"}},
                    }
                }
            },
            [_tool(_ONE_ROW)],
        ),
        (
            {"final": {"output": {"text": "[[n1]]", "numbers_from": "last_tool_result"}}},
            [],
        ),
        (
            {
                "final": {
                    "output": {
                        "text": "[[n1]]",
                        "numbers_from": "last_tool_result",
                        "numbers": {"n1": {"unit": "furlongs"}},
                    }
                }
            },
            [_tool(_ONE_ROW)],
        ),
        (
            {"final": {"output": {"text": "[[n1]]", "numbers_from": "last_tool_result"}}},
            [_tool(_THREE_ROWS.replace("Payments | 1234.5", "Payments | NULL"))],
        ),
    ],
)
def test_ut11_65_template_gaps_raise_script_mismatch(
    raw: dict[str, Any], messages: list[Message]
) -> None:
    """UT11-65 a missing table, row, column, numeric column or number is a ScriptMismatch."""
    with pytest.raises(ScriptMismatch) as info:
        render_turn(_turn(raw), messages, call_index=5)
    assert info.value.call_index == 5


def test_ut11_65_rendering_is_deterministic() -> None:
    """UT11-65 the same turn and messages render to identical output."""
    messages = [_tool(_THREE_ROWS)]
    assert render_turn(_turn(_FINDING), messages) == render_turn(_turn(_FINDING), messages)


# ---------------------------------------------------------------- PT11-05 round trip

_INT_RANGES = {"SMALLINT": 15, "INTEGER": 31, "BIGINT": 63, "HUGEINT": 127}
_FLOAT_TYPES = ("DOUBLE", "FLOAT", "REAL")
_TYPES = [*_INT_RANGES, *_FLOAT_TYPES, "DECIMAL(18,2)", "DECIMAL(38,6)", "VARCHAR"]
# VARCHAR exclusions are genuine format ambiguities: cells are stripped, and `NULL` is null.
_TEXT = st.text(alphabet="abcXYZ019 |-_:\n", max_size=20).filter(
    lambda s: s == s.strip() and s != "NULL"
)
# Floats: any value incl. +-inf, NaN, subnormals and exponent forms. The format shows 6
# significant digits (spec 05 §5.4.5), so the stored value is pre-rounded to what the
# format can carry; nothing else is excluded (NaN is compared as NaN below).
_FLOATS = st.floats(allow_nan=True, allow_infinity=True).map(lambda v: float(format(v, ".6g")))
_VALUES: dict[str, st.SearchStrategy[object]] = {
    **{t: st.integers(-(2**bits), 2**bits - 1) for t, bits in _INT_RANGES.items()},
    **dict.fromkeys(_FLOAT_TYPES, _FLOATS),
    "DECIMAL(18,2)": st.decimals(
        min_value=-(10**16), max_value=10**16, places=2, allow_nan=False, allow_infinity=False
    ),
    "DECIMAL(38,6)": st.decimals(
        min_value=-(10**32), max_value=10**32, places=6, allow_nan=False, allow_infinity=False
    ),
    "VARCHAR": _TEXT,
}


def _comparable(row: Sequence[object]) -> list[object]:
    return ["nan" if isinstance(v, float) and math.isnan(v) else v for v in row]


@st.composite
def _tables(draw: st.DrawFn) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
    n_cols = draw(st.integers(1, 5))
    names = draw(
        st.lists(
            st.from_regex(r"[a-z][a-z0-9_]{0,11}", fullmatch=True),
            min_size=n_cols,
            max_size=n_cols,
            unique=True,
        )
    )
    types = draw(st.lists(st.sampled_from(_TYPES), min_size=n_cols, max_size=n_cols))
    n_rows = draw(st.integers(1, 20))
    rows = [tuple(draw(st.none() | _VALUES[t]) for t in types) for _ in range(n_rows)]
    return names, types, rows


@settings(max_examples=200, deadline=None)
@given(table=_tables(), qid=st.from_regex(r"q_[0-9a-f]{16}", fullmatch=True))
def test_pt11_05_format_then_parse_round_trips(
    table: tuple[list[str], list[str], list[tuple[object, ...]]], qid: str
) -> None:
    """PT11-05 any 1-5 column, 1-20 row mixed-type table round-trips format then parse."""
    names, types, rows = table
    result = RecordedResult(
        query_id=qid,
        columns=names,
        types=types,
        rows=rows,
        row_count=len(rows),
        truncated=False,
        ordered=True,
    )
    content, shown = format_result(result)
    assert shown == len(rows)
    parsed = parse_tool_table(content)
    assert parsed is not None
    assert parsed.query_id == qid
    assert parsed.columns == names
    assert parsed.types == types
    assert [_comparable(r) for r in parsed.rows] == [_comparable(row) for row in rows]
