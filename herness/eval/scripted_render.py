"""Script turn rendering against real tool results (U11-39, design 11 §5.2).

`parse_tool_table` reads the model-facing table of spec 05 §5.4.5; `render_turn` fills
`{{row.<col>}}` / `{{last.query_id}}` templates from the last parseable tool result and,
for `numbers_from: last_tool_result`, builds the `NumberRef`s the Verifier re-checks.
Pure functions: the same turn and messages always render to the same output.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, cast

from pydantic import ValidationError

from herness.core.ids import canonical_json
from herness.core.types import Message, NumberRef, ToolCall, ToolResultPart
from herness.eval.scripted import ScriptMismatch, ScriptTurn
from herness.harness.tools import ARBITRARY_ROWS_LINE

__all__ = ["ParsedToolTable", "RenderedTurn", "parse_tool_table", "render_turn"]

type Cell = int | float | Decimal | str | None

_HEADER_RE: Final = re.compile(
    r"query_id=(?P<qid>\S+) rows=(?P<rows>\d+) shown=(?P<shown>\d+)"
    r" truncated=(?:yes|no) ordered=(?:yes|no)"
)
_SPLIT_RE: Final = re.compile(r"(?<!\\)\|")
_TEMPLATE_RE: Final = re.compile(r"\{\{\s*((?:row|last)\.[^{}\s]*)\s*\}\}")
_MARKER_RE: Final = re.compile(r"\[\[n([0-9]+)\]\]")
_INT_TYPES: Final = frozenset({"BIGINT", "INTEGER", "SMALLINT", "HUGEINT"})
_FLOAT_TYPES: Final = frozenset({"DOUBLE", "FLOAT", "REAL"})
_CENTS: Final = Decimal("0.01")
_NUMBERS_FROM: Final = "numbers_from"
_SUFFIX_UNITS: Final = (
    ("_usd", "usd"),
    ("_hours", "hours"),
    ("_minutes", "minutes"),
    ("_days", "days"),
    ("_pct", "pct"),
    ("_ratio", "ratio"),
    ("_rate", "ratio"),
)


@dataclass(frozen=True, slots=True)
class ParsedToolTable:
    """One parsed tool result table: query id, column names, SQL types and typed rows."""

    query_id: str
    columns: list[str]
    types: list[str]
    rows: list[list[Cell]]


@dataclass(frozen=True, slots=True)
class RenderedTurn:
    """A script turn rendered into what the model client returns."""

    tool_calls: list[ToolCall]
    text: str
    parsed: dict[str, Any] | None
    stop_reason: Literal["tool_use", "end_turn"]


def _split(line: str) -> list[str]:
    """Split on unescaped `|`, strip, and undo the spec 05 cell escapes (`\\|`, `\\n`)."""
    return [p.strip().replace("\\|", "|").replace("\\n", "\n") for p in _SPLIT_RE.split(line)]


def _typed(text: str, sql_type: str) -> Cell:
    if text == "NULL":
        return None
    base = sql_type.upper()
    try:
        if base in _INT_TYPES:
            return int(text)
        if base in _FLOAT_TYPES:
            return float(text)
        if base.startswith("DECIMAL"):
            return Decimal(text)
    except (ValueError, InvalidOperation):
        return text  # a cut cell (`…`) keeps its text
    return text


def parse_tool_table(content: str) -> ParsedToolTable | None:
    """Parse a spec 05 §5.4.5 table; None when it is not one (or its shape is broken)."""
    if not content.startswith("query_id="):
        return None
    lines = content.split("\n")
    header = _HEADER_RE.match(lines[0])
    if header is None or len(lines) < 3:  # noqa: PLR2004 - header, names and types lines
        return None
    columns, types = _split(lines[1]), _split(lines[2])
    body = [line for line in lines[3:] if line != ARBITRARY_ROWS_LINE][: int(header["shown"])]
    cells = [_split(line) for line in body]
    if len(types) != len(columns) or any(len(row) != len(columns) for row in cells):
        return None
    rows = [[_typed(v, t) for v, t in zip(row, types, strict=True)] for row in cells]
    return ParsedToolTable(header["qid"], columns, types, rows)


def _mismatch(message: str, call_index: int) -> ScriptMismatch:
    # The client re-raises with the real call identity (role, model role, dedup key).
    return ScriptMismatch(message, role="*", model_role="*", dedup_key="*", call_index=call_index)


def _last_table(messages: Sequence[Message]) -> ParsedToolTable | None:
    for message in reversed(messages):
        for part in reversed(message.parts):
            if isinstance(part, ToolResultPart):
                table = parse_tool_table(part.content)
                if table is not None:
                    return table
    return None


class _Renderer:
    """Template filling and `NumberRef` building for one turn."""

    def __init__(self, last: ParsedToolTable | None, call_index: int) -> None:
        self._last = last
        self._call_index = call_index

    def table(self) -> ParsedToolTable:
        if self._last is None:
            msg = "template needs a tool result table but none parses"
            raise _mismatch(msg, self._call_index)
        return self._last

    def row1(self) -> list[Cell]:
        table = self.table()
        if not table.rows:
            msg = "template needs row 1 but the tool result has no rows"
            raise _mismatch(msg, self._call_index)
        return table.rows[0]

    def cell(self, column: str) -> Cell:
        table = self.table()
        if column not in table.columns:
            msg = f"template column {column} not in the tool result"
            raise _mismatch(msg, self._call_index)
        return self.row1()[table.columns.index(column)]

    def _sub(self, match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "last.query_id":
            return self.table().query_id
        if name.startswith("row."):
            value = self.cell(name.removeprefix("row."))
            return "NULL" if value is None else str(value)
        msg = f"unknown template {name}"
        raise _mismatch(msg, self._call_index)

    def fill(self, value: object) -> object:
        """Replace templates in every string of a nested JSON-like value."""
        if isinstance(value, str):
            return _TEMPLATE_RE.sub(self._sub, value)
        if isinstance(value, Mapping):
            return {k: self.fill(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.fill(v) for v in value]
        return value

    def _numeric_column(self, k: int) -> str:
        table = self.table()
        numeric = [c for c, t in zip(table.columns, table.types, strict=True) if _is_numeric(t)]
        if not 1 <= k <= len(numeric):
            msg = f"marker n{k} has no numeric column {k} in the tool result"
            raise _mismatch(msg, self._call_index)
        return numeric[k - 1]

    def number(self, k: int, explicit: Mapping[str, Any]) -> dict[str, Any]:
        table = self.table()
        column = str(explicit.get("column") or self._numeric_column(k))
        unit = str(explicit.get("unit") or _unit_for(column))
        value = self._value(self.cell(column), unit, k)
        first = table.columns[0]
        row_key = None if len(table.rows) == 1 else {first: _scalar(self.row1()[0])}
        raw = {"id": f"n{k}", "value": value, "unit": unit, "query_id": table.query_id}
        try:
            ref = NumberRef.model_validate(raw | {"column": column, "row_key": row_key})
        except ValidationError:
            msg = f"marker n{k} does not form a valid NumberRef"
            raise _mismatch(msg, self._call_index) from None
        return ref.model_dump(mode="json")

    def _value(self, cell: Cell, unit: str, k: int) -> int | float | str:
        if unit == "usd":
            amount = _decimal(cell)
            if amount is not None:
                return str(amount.quantize(_CENTS))
        elif isinstance(cell, Decimal) and cell.is_finite():
            return float(cell)
        elif isinstance(cell, int | float) and math.isfinite(cell):
            return cell
        msg = f"marker n{k} cites a value that is not a number"
        raise _mismatch(msg, self._call_index)

    def numbers(self, mapping: Mapping[str, Any], text_field: str) -> dict[str, Any]:
        """Apply `numbers_from: last_tool_result` to one mapping (U11-39 step 3)."""
        out = {k: v for k, v in mapping.items() if k != _NUMBERS_FROM}
        given = out.get("numbers")
        explicit: Mapping[str, Any] = given if isinstance(given, Mapping) else {}
        text = out.get(text_field)
        ids = dict.fromkeys(
            int(m) for m in _MARKER_RE.findall(text if isinstance(text, str) else "")
        )
        out["numbers"] = [self.number(k, _entry(explicit, k)) for k in ids]
        out.setdefault("query_ids", [self.table().query_id])
        return out


def _entry(explicit: Mapping[str, Any], k: int) -> Mapping[str, Any]:
    entry = explicit.get(f"n{k}")
    return entry if isinstance(entry, Mapping) else {}


def _is_numeric(sql_type: str) -> bool:
    base = sql_type.upper()
    return base in _INT_TYPES or base in _FLOAT_TYPES or base.startswith("DECIMAL")


def _unit_for(column: str) -> str:
    """Unit by column-name suffix (U11-39 step 3)."""
    for suffix, unit in _SUFFIX_UNITS:
        if column.endswith(suffix):
            return unit
    if "count" in column or "incidents" in column or column.startswith("n_"):
        return "count"
    return "other"


def _decimal(cell: Cell) -> Decimal | None:
    """A finite `Decimal` of a cell (USD cells may arrive typed or as text), else None."""
    if cell is None:
        return None
    try:
        amount = Decimal(str(cell))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def _scalar(value: Cell) -> int | float | str | None:
    return str(value) if isinstance(value, Decimal) else value


def _render_mapping(r: _Renderer, mapping: Mapping[str, Any], text_field: str) -> dict[str, Any]:
    filled = cast("dict[str, Any]", r.fill(mapping))
    if filled.get(_NUMBERS_FROM) == "last_tool_result":
        return r.numbers(filled, text_field)
    return filled


def _call(r: _Renderer, call_index: int, i: int, name: str, args: Mapping[str, Any]) -> ToolCall:
    field = "claim" if name == "post_finding" else "text"
    return ToolCall(
        id=f"call_{call_index}_{i}", name=name, arguments=_render_mapping(r, args, field)
    )


def render_turn(
    turn: ScriptTurn, messages: Sequence[Message], *, call_index: int = 0
) -> RenderedTurn:
    """Render `turn` against the last parseable tool result in `messages` (U11-39)."""
    r = _Renderer(_last_table(messages), call_index)
    if turn.tool_calls is not None:
        calls = [
            _call(r, call_index, i, spec.name, spec.arguments)
            for i, spec in enumerate(turn.tool_calls)
        ]
        return RenderedTurn(calls, "", None, "tool_use")
    final = turn.final or {}
    ((key, value),) = final.items()  # the loader guarantees exactly one key
    if key == "text":
        return RenderedTurn([], str(r.fill(value)), None, "end_turn")
    if key == "output":
        parsed = _render_mapping(r, value, "text")
        return RenderedTurn([], canonical_json(parsed), parsed, "end_turn")
    return RenderedTurn([_call(r, call_index, 0, key, value)], "", None, "tool_use")
