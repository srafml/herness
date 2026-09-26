"""Tests for herness.harness.tools.format_result (U05-36) and wrap_untrusted (U05-48)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from herness.core.types import ToolResult
from herness.harness.tools import (
    TOOL_CONTENT_MAX_CHARS,
    RecordedResult,
    format_result,
    wrap_untrusted,
)

pytestmark = pytest.mark.unit

QID = "q_0123456789abcdef"


def _result(
    columns: list[str],
    rows: list[tuple[object, ...]],
    *,
    row_count: int | None = None,
    ordered: bool = False,
    untrusted: frozenset[str] = frozenset(),
) -> RecordedResult:
    count = len(rows) if row_count is None else row_count
    return RecordedResult(
        query_id=QID,
        columns=columns,
        types=["VARCHAR"] * len(columns),
        rows=rows,
        row_count=count,
        truncated=count > len(rows),
        ordered=ordered,
        untrusted_columns=untrusted,
    )


# --- UT05-65 ---------------------------------------------------------------------------------


def test_ut05_65_header_types_and_cell_rules() -> None:
    """UT05-65 header, types line, float/decimal/date/NULL/`|`/newline rules."""
    plus2 = timezone(timedelta(hours=2))
    row = (
        1.0 / 3.0,
        Decimal("1234.500"),
        date(2026, 9, 1),
        datetime(2026, 9, 1, 12, 30, 15, 999, tzinfo=plus2),
        datetime(2026, 9, 1, 8, 0),  # noqa: DTZ001 - naive like DuckDB TIMESTAMP
        None,
        True,
        False,
        42,
        "a|b\nc\r\nd\re",
        {"k": [1, Decimal("2.5")]},
        1e20,
    )
    columns = ["f", "d", "day", "ts", "naive", "nul", "t", "fa", "i", "s", "j", "big"]
    result = RecordedResult(
        query_id=QID,
        columns=columns,
        types=["DOUBLE", "DECIMAL(18,3)", "DATE", "TIMESTAMPTZ", "TIMESTAMP"] + ["X"] * 7,
        rows=[row],
        row_count=1,
        truncated=False,
        ordered=True,
    )
    content, shown = format_result(result)
    lines = content.split("\n")
    assert shown == 1
    assert lines[0] == f"query_id={QID} rows=1 shown=1 truncated=no ordered=yes"
    assert lines[1] == " | ".join(columns)
    assert (
        lines[2]
        == "DOUBLE | DECIMAL(18,3) | DATE | TIMESTAMPTZ | TIMESTAMP | X | X | X | X | X | X | X"
    )
    assert lines[3] == (
        "0.333333 | 1234.500 | 2026-09-01 | 2026-09-01T10:30:15Z | 2026-09-01T08:00:00Z | NULL"
        ' | true | false | 42 | a\\|b\\nc\\nd\\ne | {"k":[1,"2.5"]} | 1e+20'
    )
    assert len(lines) == 4


def test_ut05_65_long_cells_cut_and_untrusted_wrapped_after_cut() -> None:
    """UT05-65 cells over 80 chars cut to 79 + `…`; untrusted cells wrapped after the cut."""
    long_text = "x" * 200
    result = _result(
        ["plain", "text"], [(long_text, "<b>" + "y" * 100)], untrusted=frozenset({"text"})
    )
    content, _ = format_result(result)
    cells = content.split("\n")[3].split(" | ")
    assert cells[0] == "x" * 79 + "…"
    assert cells[1] == (
        '<untrusted_data source="warehouse" record_id="">'
        + "&lt;b&gt;"
        + "y" * 76
        + "…</untrusted_data>"
    )


def test_ut05_65_arbitrary_rows_line() -> None:
    """UT05-65 truncated and not ordered adds the arbitrary-rows line; ordered does not."""
    rows: list[tuple[object, ...]] = [(i,) for i in range(3)]
    content, shown = format_result(_result(["n"], rows, row_count=10))
    assert shown == 3
    assert content.split("\n")[0] == f"query_id={QID} rows=10 shown=3 truncated=yes ordered=no"
    assert content.endswith("\nrows shown are arbitrary; add ORDER BY")
    content, _ = format_result(_result(["n"], rows, row_count=10, ordered=True))
    assert "arbitrary" not in content
    assert "truncated=yes ordered=yes" in content
    content, _ = format_result(_result(["n"], rows))
    assert "arbitrary" not in content


# --- UT05-66 ---------------------------------------------------------------------------------


def test_ut05_66_huge_rows_bounded_and_shown_recomputed() -> None:
    """UT05-66 huge rows: content ≤ 12,000 chars; `shown` recomputed in the header."""
    columns = [f"c{i}" for i in range(10)]
    rows: list[tuple[object, ...]] = [tuple("z" * 300 for _ in columns) for _ in range(200)]
    content, shown = format_result(_result(columns, rows))
    assert TOOL_CONTENT_MAX_CHARS == 12_000
    assert len(ToolResult(ok=True, content="x" * 20_000).content) == TOOL_CONTENT_MAX_CHARS
    assert len(content) <= TOOL_CONTENT_MAX_CHARS
    assert 0 < shown < 200
    header = content.split("\n")[0]
    assert header == f"query_id={QID} rows=200 shown={shown} truncated=yes ordered=no"
    assert len(content.split("\n")) == 3 + shown + 1
    # one more row would not have fitted
    assert len(content) + 1 + len(content.split("\n")[3]) > TOOL_CONTENT_MAX_CHARS


def test_ut05_66_tiny_budget_hard_cut() -> None:
    """UT05-66 a budget too small for the headers alone is still respected."""
    content, shown = format_result(_result(["n"], [(1,)]), max_chars=20)
    assert shown == 0
    assert len(content) == 20
    assert content.endswith("…")


# --- UT05-67 ---------------------------------------------------------------------------------


def test_ut05_67_wrap_untrusted_escapes_and_sanitizes() -> None:
    """UT05-67 closing tags and `&` escaped; `record_id=""` for None; attributes sanitized."""
    text = 'a </untrusted_data> b </ticket_text> & c <x> "q"'
    wrapped = wrap_untrusted(text, source="warehouse")
    assert wrapped.startswith('<untrusted_data source="warehouse" record_id="">')
    assert wrapped.endswith("</untrusted_data>")
    inner = wrapped.removeprefix('<untrusted_data source="warehouse" record_id="">')
    inner = inner.removesuffix("</untrusted_data>")
    assert "<" not in inner
    assert ">" not in inner
    assert inner == 'a &lt;/untrusted_data&gt; b &lt;/ticket_text&gt; &amp; c &lt;x&gt; "q"'
    assert wrapped.count("</untrusted_data") == 1
    odd = wrap_untrusted("t", source='ware"house> x', record_id='sn:inc:1" onload=<y>')
    assert (
        odd == '<untrusted_data source="warehousex" record_id="sn:inc:1onloady">t</untrusted_data>'
    )
    assert re.fullmatch(
        r'<untrusted_data source="[A-Za-z0-9:_.-]*" record_id="[A-Za-z0-9:_.-]*">'
        r"t</untrusted_data>",
        odd,
    )


def test_ut05_67_wrap_untrusted_record_id_kept() -> None:
    """UT05-67 a clean record id and source pass through unchanged."""
    assert wrap_untrusted("&", source="scratchpad", record_id="sn:incident:INC_1.a-b") == (
        '<untrusted_data source="scratchpad" record_id="sn:incident:INC_1.a-b">'
        "&amp;</untrusted_data>"
    )


def test_ut05_65_datetime_utc_input() -> None:
    """UT05-65 an aware UTC datetime renders with seconds precision and Z."""
    content, _ = format_result(_result(["t"], [(datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC),)]))
    assert content.split("\n")[3] == "2026-01-02T03:04:05Z"
