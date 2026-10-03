"""Ledger, invariant and size-cap steps of ``ContextCompactor`` (impl 07 U07-76 steps 5, 9).

Size-forced private sibling of ``compactor.py`` (T07-14); only that module imports it. Pure
apart from mutating the ``Scratchpad`` it is given: no I/O, clock, logging or model call.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from herness.core.types import Message, NumberRef, TextPart, ToolCall, ToolCallPart, ToolResultPart
from herness.harness.memory._compact_text import arg_refs, canonical_args
from herness.harness.memory.compact_build import (
    QUERY_ID_SCAN_RE,
    Group,
    ParsedTable,
    cited_from_group,
    entry_from_result,
)
from herness.harness.memory.types import QUERY_ID_RE
from herness.harness.memory.working import SCRATCHPAD_MAX_BYTES, LedgerEntry, Scratchpad

__all__ = [
    "LedgerPass",
    "enforce_cap",
    "invariants_hold",
    "kept_refs",
    "query_ids_in",
    "task_message",
    "text_of",
]

SCRATCHPAD_OPEN: Final = "<scratchpad "
UNKNOWN_TOOL: Final = "unknown"


def task_message(first: Message) -> Message:
    """The original task message (M0): a merged head loses its scratchpad and transcript.

    Passing the merged head back to ``build_compacted`` would nest old scratchpads (T07-13
    carry-over); its parts from the first ``<scratchpad`` text on are what compaction added.
    """
    if first.kind != "compaction_summary":
        return first
    parts: list[TextPart] = []
    for part in first.parts:
        if isinstance(part, TextPart) and part.text.startswith(SCRATCHPAD_OPEN):
            break
        if isinstance(part, TextPart):
            parts.append(part)
    return Message(role="user", parts=[*parts] if parts else [TextPart(text="")])


def text_of(messages: Iterable[Message]) -> str:
    """Every text part, tool result content and canonical tool-call argument JSON."""
    out: list[str] = []
    for part in (p for m in messages for p in m.parts):
        if isinstance(part, TextPart):
            out.append(part.text)
        elif isinstance(part, ToolResultPart):
            out.append(part.content)
        elif isinstance(part, ToolCallPart):
            out.append(canonical_args(part.call.arguments))
    return "\n".join(out)


def query_ids_in(messages: Iterable[Message]) -> set[str]:
    """``qids`` of U07-76 step 9: every ``q_[0-9a-f]{16}`` in ``text_of(messages)``."""
    return set(QUERY_ID_SCAN_RE.findall(text_of(messages)))


def kept_refs(groups: Sequence[Group], messages: Sequence[Message]) -> list[NumberRef]:
    """The valid cited refs under the ``numbers`` argument of the kept groups' calls."""
    return [
        ref
        for group in groups
        for index in group.indices
        for part in messages[index].parts
        if isinstance(part, ToolCallPart)
        for ref in arg_refs(part.call)
    ]


@dataclass(slots=True)
class LedgerPass:
    """U07-76 step 5 over one ``on_context_pressure`` call; each group is ledgered once."""

    pad: Scratchpad
    messages: Sequence[Message]
    allowed: Sequence[re.Pattern[str]]
    max_cells: int
    tables: list[ParsedTable] = field(default_factory=list)
    done: set[int] = field(default_factory=set)

    def group(self, group: Group) -> None:
        """Results to entries (orphans under a placeholder ``unknown`` call), then numerals."""
        if group.indices[0] in self.done or group.is_summary:
            return  # a summary preamble is the restored scratchpad itself
        self.done.add(group.indices[0])
        members = [self.messages[i] for i in group.indices]
        calls = {p.call.id: p.call for m in members for p in m.parts if isinstance(p, ToolCallPart)}
        for result in (p for m in members for p in m.parts if isinstance(p, ToolResultPart)):
            call = calls.get(result.tool_call_id)
            call = call or ToolCall(id=result.tool_call_id, name=UNKNOWN_TOOL, arguments={})
            entries, table = entry_from_result(call, result, group.step, self.max_cells)
            for entry in entries:
                self.pad.upsert(entry)
            self.tables += [table] if table is not None else []
        cited, unmatched = cited_from_group(members, self.tables, self.allowed, group.step)
        for ref in cited:
            self.pad.cite(ref)
        for number in unmatched:
            self.pad.add_unmatched(number.value, number.step)

    def rescue(self, ids: Iterable[str]) -> None:
        """A minimal entry (tool ``unknown``, step 0) for each id the ledger lacks."""
        known = self.pad.query_ids()
        for qid in sorted(set(ids) - known):
            if QUERY_ID_RE.fullmatch(qid) is not None:
                self.pad.upsert(LedgerEntry(query_id=qid, tool=UNKNOWN_TOOL, step=0))


def invariants_hold(
    before: Sequence[Message], after: Sequence[Message], pad: Scratchpad, refs: list[NumberRef]
) -> bool:
    """U07-76 step 9: ids, cited refs and recorded numeral mentions all survive."""
    text = text_of(after)
    if not query_ids_in(before) <= set(QUERY_ID_SCAN_RE.findall(text)):
        return False
    cited = [*pad.cited_numbers(), *refs]
    if not all(ref.query_id in text and str(ref.value) in text for ref in cited):
        return False
    return all(number.value in text for number in pad.unmatched)


def enforce_cap(pad: Scratchpad, limit: int = SCRATCHPAD_MAX_BYTES) -> bool:
    """U07-67 limit: over ``limit`` → ``compact()``, then drop the oldest unmatched numerals.

    ``compact()`` already clears every sample. ``query_id``s and cited refs are never dropped.
    Returns whether ``compact()`` ran.
    """
    size = pad.size_bytes()
    if size <= limit:
        return False
    pad.compact()
    size = pad.size_bytes()
    while size > limit and pad.unmatched:
        excess, cut = size - limit, 0
        while cut < len(pad.unmatched) and excess > 0:
            excess -= len(pad.unmatched[cut].model_dump_json()) + 1  # the item and its comma
            cut += 1
        del pad.unmatched[:cut]
        size = pad.size_bytes()
    return True
