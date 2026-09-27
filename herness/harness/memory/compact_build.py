"""Pure context-compaction steps (impl 07 U07-70 … U07-75; design 07 §5.4, spec 00 §12.4).

Every function is pure: no I/O, clock, randomness, logging or model call; inputs are never
mutated and outputs share no object with them. The ``query_id``s and cited numbers of dropped
groups go to the ledger (TH07-15); model-written notes are repaired so they carry no invented
number, marker or ``query_id`` (TH07-16). ``ContextCompactor`` (U07-76) wires the steps up.
"""

import decimal
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue, ValidationError

from herness.core.types import (
    Message,
    NumberRef,
    ReasoningPart,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
)
from herness.harness.memory._compact_text import (
    QUERY_ID_SCAN_RE,
    arg_refs,
    canonical_args,
    parse_numeral,
    repair_notes,
    rounds_to,
)
from herness.harness.memory.policy import find_uncited_numerals
from herness.harness.memory.working import (
    CompactionNotes,
    LedgerEntry,
    Scratchpad,
    UnmatchedNumeral,
)

__all__ = [
    "QUERY_ID_SCAN_RE",
    "Group",
    "ParsedTable",
    "build_compacted",
    "cited_from_group",
    "deterministic_notes",
    "entry_from_result",
    "split_groups",
    "validate_notes",
]

CELL_SEP: Final = " | "
MAX_ROWS: Final = 200
SAMPLE_ROWS: Final = 5
HEAD_CHARS: Final = 200  # sql_head and error
LEDGER_MAX_COLUMNS: Final = 50
ARGS_HEAD_CHARS: Final = 60
STEP_LINE_CHARS: Final = 160
MAX_STEPS: Final = 200
UNMATCHED_CHARS: Final = 40
TEXT_PART_MAX_CHARS: Final = 200_000
NO_MODEL_PROGRESS: Final = "model notes unavailable; see steps"
TRANSCRIPT_HEADER: Final = "Recent tool calls (verbatim):"
_HEADER_RE: Final = re.compile(r"^query_id=(q_[0-9a-f]{16}) rows=(\d{1,40})(?: shown=(\d{1,40}))?")
_WS_RE: Final = re.compile(r"\s+")


type _AnyPart = TextPart | ToolCallPart | ToolResultPart | ReasoningPart


@dataclass(frozen=True, slots=True)
class Group:
    """Messages that are never split; ``indices`` point into ``state.messages`` (U07-70)."""

    indices: tuple[int, ...]
    step: int
    is_preamble: bool
    is_summary: bool


@dataclass(frozen=True, slots=True)
class ParsedTable:
    """A spec 05 §5.4.5 result table; cells stay strings (U07-71)."""

    query_id: str
    columns: list[str]
    rows: list[list[str]]
    row_count: int


def _has_call(message: Message) -> bool:
    return message.role == "assistant" and any(isinstance(p, ToolCallPart) for p in message.parts)


def _spans(messages: Sequence[Message]) -> list[list[int]]:
    """Positional groups: a new one at each assistant message with a tool call."""
    spans: list[list[int]] = []
    for index, message in enumerate(messages):
        if not spans or _has_call(message):
            spans.append([])
        spans[-1].append(index)
    return spans


def _links(messages: Sequence[Message], spans: list[list[int]], has_pre: bool) -> set[int]:
    """Span numbers ``k`` that join span ``k + 1`` so a call and its results stay together."""
    owner: dict[str, int] = {}
    for k, span in enumerate(spans):
        for index in span:
            for part in messages[index].parts:
                if isinstance(part, ToolCallPart):
                    owner.setdefault(part.call.id, k)
    links: set[int] = set()
    for k, span in enumerate(spans):
        for index in span:
            for part in messages[index].parts:
                other = owner.get(part.tool_call_id, k) if isinstance(part, ToolResultPart) else k
                low, high = min(k, other), max(k, other)
                if not (has_pre and low == 0):  # the preamble is never merged
                    links.update(range(low, high))
    return links


def split_groups(messages: Sequence[Message], first_step: int) -> list[Group]:
    """Partition ``state.messages[1:]`` into contiguous groups that are never split."""
    spans = _spans(messages)
    has_pre = bool(messages) and not _has_call(messages[0])
    links = _links(messages, spans, has_pre)
    groups: list[Group] = []
    start = 0
    for k in range(len(spans)):
        if k in links:
            continue
        indices = tuple(i + 1 for span in spans[start : k + 1] for i in span)
        preamble = has_pre and start == 0
        summary = preamble and any(messages[i - 1].kind == "compaction_summary" for i in indices)
        ordinal = start if has_pre else start + 1
        groups.append(Group(indices, first_step + ordinal, preamble, summary))
        start = k + 1
    return groups


def _parse_table(content: str) -> ParsedTable | None:
    lines = content.split("\n")
    head = _HEADER_RE.match(lines[0])
    if head is None or len(lines) < 2:  # noqa: PLR2004 - header and column names
        return None
    columns = lines[1].split(CELL_SEP)
    shown = head.group(3)
    limit = min(MAX_ROWS, int(shown)) if shown is not None else MAX_ROWS
    rows: list[list[str]] = []
    for line in lines[3:]:
        cells = line.split(CELL_SEP)
        if len(rows) >= limit or not line or len(cells) != len(columns):
            break
        rows.append(cells)
    return ParsedTable(head.group(1), columns, rows, int(head.group(2)))


def _sql_head(call: ToolCall) -> str | None:
    sql = call.arguments.get("sql") if call.name == "run_sql" else None
    return _WS_RE.sub(" ", sql).strip()[:HEAD_CHARS] if isinstance(sql, str) else None


def _sample(table: ParsedTable | None, max_cells: int) -> list[dict[str, JsonValue]]:
    if table is None or table.row_count * len(table.columns) > max_cells:
        return []
    return [dict(zip(table.columns, row, strict=True)) for row in table.rows[:SAMPLE_ROWS]]


def entry_from_result(
    call: ToolCall, result: ToolResultPart, step: int, max_cells: int
) -> tuple[list[LedgerEntry], ParsedTable | None]:
    """Ledger entries (one per distinct ``query_id``) and the parsed table of one call."""
    qids = list(dict.fromkeys(QUERY_ID_SCAN_RE.findall(result.content)))
    if not qids and not result.is_error:
        return [], None
    table = _parse_table(result.content)
    error = result.content.replace("\n", " ")[:HEAD_CHARS] if result.is_error else None
    primary = LedgerEntry(
        query_id=qids[0] if qids else "",
        tool=call.name,
        step=step,
        sql_head=_sql_head(call),
        row_count=table.row_count if table is not None else None,
        columns=table.columns[:LEDGER_MAX_COLUMNS] if table is not None else [],
        sample=_sample(table, max_cells),
        error=error,
    )
    rest = [LedgerEntry(query_id=qid, tool=call.name, step=step) for qid in qids[1:]]
    return [primary, *rest], table


type _Cell = tuple[ParsedTable, list[str], int, float]


def _cells(tables: Sequence[ParsedTable]) -> Iterator[_Cell]:
    """Numeric cells: most recent table first, rows in order, columns in order."""
    for table in reversed(tables):
        for row in table.rows:
            for column, text in enumerate(row):
                try:
                    number = float(text)
                except ValueError:
                    continue
                if math.isfinite(number):
                    yield table, row, column, number


def _match(written: str, cells: Sequence[_Cell]) -> NumberRef | None:
    parsed = parse_numeral(written)
    if parsed is None:
        return None
    value, places = parsed
    target = float(value)
    slack = 10.0**-places + abs(target) * 1e-9  # a cheap float filter before the exact test
    for table, row, column, number in cells:
        near = abs(number - target) <= slack
        if not (near and rounds_to(decimal.Decimal(repr(number)), value, places)):
            continue
        keyed = table.row_count > 1 and column != 0
        try:
            return NumberRef(
                id="n0",
                value=int(number) if number.is_integer() else number,
                unit="other",
                query_id=table.query_id,
                column=table.columns[column],
                row_key={table.columns[0]: row[0]} if keyed else None,
            )
        except ValidationError:
            continue  # e.g. a column name NumberRef cannot hold
    return None


def cited_from_group(
    group_messages: Sequence[Message],
    tables: Sequence[ParsedTable],
    allowed: Sequence[re.Pattern[str]],
    step: int,
) -> tuple[list[NumberRef], list[UnmatchedNumeral]]:
    """Numbers the agent used in a group: cited refs, and numerals no result cell backs."""
    cited = [
        ref
        for message in group_messages
        for part in message.parts
        if isinstance(part, ToolCallPart)
        for ref in arg_refs(part.call)
    ]
    unmatched: list[UnmatchedNumeral] = []
    cells: list[_Cell] | None = None  # parsed on the first numeral only
    texts = [
        part.text
        for message in group_messages
        if message.role == "assistant"
        for part in message.parts
        if isinstance(part, TextPart)
    ]
    for text in texts:
        for hit in find_uncited_numerals(text, allowed):
            cells = list(_cells(tables)) if cells is None else cells
            ref = _match(hit.text, cells)
            if ref is None:
                unmatched.append(UnmatchedNumeral(value=hit.text[:UNMATCHED_CHARS], step=step))
            else:
                cited.append(ref)
    return cited, unmatched


def validate_notes(
    raw: Mapping[str, JsonValue] | None,
    scratchpad: Scratchpad,
    allowed: Sequence[re.Pattern[str]],
) -> CompactionNotes | None:
    """Validated, repaired model notes: stray numbers, markers and ids become ``[[?]]``."""
    return repair_notes(raw, scratchpad, allowed)


def _calls_results(
    messages: Sequence[Message],
) -> tuple[list[ToolCall], dict[str, ToolResultPart]]:
    """The calls of a group in order, and the first result per ``tool_call_id``."""
    calls = [p.call for m in messages for p in m.parts if isinstance(p, ToolCallPart)]
    results: dict[str, ToolResultPart] = {}
    for part in (p for m in messages for p in m.parts if isinstance(p, ToolResultPart)):
        results.setdefault(part.tool_call_id, part)
    return calls, results


def _step_line(step: int, call: ToolCall, result: ToolResultPart | None) -> str:
    head = _HEADER_RE.match(result.content) if result is not None else None
    outcome = f"rows={head.group(2) if head is not None else '?'}"
    if result is not None and result.is_error:
        outcome = "ERROR"
    args = canonical_args(call.arguments)[:ARGS_HEAD_CHARS]
    return f"step {step}: {call.name}({args}) -> {outcome}"[:STEP_LINE_CHARS]


def deterministic_notes(
    groups: Sequence[Group], messages: Sequence[Message], prior: CompactionNotes | None
) -> CompactionNotes:
    """Notes without a model: one step line per call of each dropped tool group."""
    base = prior.model_copy(deep=True) if prior is not None else None
    notes = base if base is not None else CompactionNotes(progress=NO_MODEL_PROGRESS)
    lines: list[str] = []
    for group in (g for g in groups if not g.is_preamble):
        calls, results = _calls_results([messages[i] for i in group.indices])
        lines += [_step_line(group.step, call, results.get(call.id)) for call in calls]
    return notes.model_copy(update={"steps": [*notes.steps, *lines][-MAX_STEPS:]})


def _text_parts(text: str) -> list[TextPart]:
    """``text`` as TextParts within the per-part limit (one part unless it is longer)."""
    size = TEXT_PART_MAX_CHARS
    return [TextPart(text=text[i : i + size]) for i in range(0, max(len(text), 1), size)]


def _append(out: list[Message], message: Message) -> None:
    """A deep copy without ReasoningParts; a user message after a user message joins it."""
    kept = (p for p in message.parts if not isinstance(p, ReasoningPart))
    parts: list[_AnyPart] = [p.model_copy(deep=True) for p in kept]
    if not parts:
        return
    last = out[-1]
    if message.role == last.role == "user":
        out[-1] = Message(role="user", kind=last.kind, parts=[*last.parts, *parts])
    else:
        out.append(Message(role=message.role, kind=message.kind, parts=parts))


def _message_lines(
    message: Message, calls: set[str], results: dict[str, ToolResultPart]
) -> list[str]:
    lines: list[str] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            lines.append(f"{message.role}: {part.text}")
        elif isinstance(part, ToolCallPart):
            lines.append(f"tool: {part.call.name} args: {canonical_args(part.call.arguments)}")
            result = results.get(part.call.id)
            lines += ["result:", result.content] if result is not None else []
        elif isinstance(part, ToolResultPart):
            paired = part.tool_call_id in calls and results[part.tool_call_id] is part
            lines += [] if paired else ["result:", part.content]
    return lines


def _transcript(keep: Sequence[Group], messages: Sequence[Message]) -> str:
    lines = [TRANSCRIPT_HEADER]
    for group in keep:
        members = [messages[i] for i in group.indices]
        calls, results = _calls_results(members)
        call_ids = {call.id for call in calls}
        for message in members:
            lines += _message_lines(message, call_ids, results)
    return "\n".join(lines)


def build_compacted(
    m0: Message,
    scratchpad_text: str,
    keep: Sequence[Group],
    messages: Sequence[Message],
    *,
    fresh_conversation: bool,
) -> list[Message]:
    """The new message list: merged M0 + scratchpad head, then the kept groups."""
    head = [*(p.model_copy(deep=True) for p in m0.parts), *_text_parts(scratchpad_text)]
    if fresh_conversation:  # Claude: one user message, no ToolCall or Reasoning part
        head += _text_parts(_transcript(keep, messages))
        return [Message(role="user", kind="compaction_summary", parts=head)]
    out = [Message(role="user", kind="compaction_summary", parts=head)]
    for group in keep:
        for index in group.indices:
            _append(out, messages[index])
    return out
