"""Helpers of ``compact_build`` (impl 07 U07-72 … U07-74): numerals, notes repair, JSON.

Size-forced private sibling of ``compact_build.py`` (T07-13); only that module imports it.
Pure: no I/O, clock, randomness or logging.
"""

import decimal
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue, ValidationError

from herness.core import numbers as core_numbers
from herness.core.errors import SchemaViolation
from herness.core.ids import canonical_json
from herness.core.types import NumberRef, ToolCall
from herness.harness.memory.policy import find_uncited_numerals
from herness.harness.memory.working import CompactionNotes, Hypothesis, Scratchpad

QUERY_ID_SCAN_RE: Final = re.compile(r"q_[0-9a-f]{16}")
UNKNOWN_MARK: Final = "[[?]]"
PROGRESS_CHARS: Final = 600
HYPOTHESIS_CHARS: Final = 300
ITEM_CHARS: Final = 200
_SUFFIX_RE: Final = re.compile(r"\s*(%|k|K|M|bn|x)$")
_CTX: Final = decimal.Context(prec=60, rounding=decimal.ROUND_HALF_EVEN)


def parse_numeral(text: str) -> tuple[decimal.Decimal, int] | None:
    """The written value and its decimal places; ``None`` when it is not a plain number.

    ``$``, ``,`` and a trailing ``%``, ``k``, ``K``, ``M``, ``bn`` or ``x`` are stripped.
    """
    plain = _SUFFIX_RE.sub("", text.strip().replace("$", "").replace(",", "")).strip()
    try:
        value = decimal.Decimal(plain)
    except decimal.InvalidOperation:
        return None
    return (value, len(plain.partition(".")[2])) if value.is_finite() else None


def rounds_to(cell: decimal.Decimal, value: decimal.Decimal, places: int) -> bool:
    """``round_half_even(cell, places) == value``; ``False`` past 60 significant digits."""
    try:
        return cell.quantize(decimal.Decimal(1).scaleb(-places), context=_CTX) == value
    except decimal.InvalidOperation:
        return False


def _cut(value: JsonValue, limit: int) -> JsonValue:
    return value[:limit] if isinstance(value, str) else value


def _cut_hypothesis(value: JsonValue) -> JsonValue:
    if not isinstance(value, dict) or "text" not in value:
        return value
    return {**value, "text": _cut(value["text"], HYPOTHESIS_CHARS)}


def cut_raw(raw: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """A copy with over-long strings cut; the model's ``steps`` dropped (deterministic only)."""
    out = {key: value for key, value in raw.items() if key != "steps"}
    if "progress" in out:
        out["progress"] = _cut(out["progress"], PROGRESS_CHARS)
    hypotheses = out.get("hypotheses")
    if isinstance(hypotheses, list):
        out["hypotheses"] = [_cut_hypothesis(item) for item in hypotheses]
    for key in ("dead_ends", "next_steps"):
        items = out.get(key)
        if isinstance(items, list):
            out[key] = [_cut(item, ITEM_CHARS) for item in items]
    return out


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


@dataclass(frozen=True, slots=True)
class NotesRepair:
    """Replaces what notes may not hold with ``[[?]]`` (TH07-16).

    Replaced: markers whose id is not a ledger id, malformed double-bracket tokens, numerals
    outside markers and allowed patterns, and ``query_id``s that are not in the ledger.
    """

    ids: frozenset[str]
    query_ids: frozenset[str]
    allowed: Sequence[re.Pattern[str]]

    def _spans(self, text: str) -> list[tuple[int, int]]:
        scan = core_numbers.parse_markers(text)
        return [
            *((m.start, m.end) for m in scan.markers if m.id not in self.ids),
            *((m.start, m.end) for m in scan.malformed if m.text != "?"),
            *((h.start, h.end) for h in find_uncited_numerals(text, self.allowed)),
            *(m.span() for m in QUERY_ID_SCAN_RE.finditer(text) if m[0] not in self.query_ids),
        ]

    def repair(self, text: str) -> str:
        """``text`` with every bad span replaced, right to left to keep offsets."""
        for start, end in reversed(_merge_spans(self._spans(text))):
            text = text[:start] + UNKNOWN_MARK + text[end:]
        return text

    def fit(self, text: str, limit: int) -> str:
        """Repaired text within ``limit``; a cut that exposes a numeral is repaired again."""
        base = self.repair(text)
        out, cut = base, limit
        while len(out) > limit:
            out = self.repair(base[:cut])
            cut -= 1
        return out


def arg_refs(call: ToolCall) -> list[NumberRef]:
    """Valid NumberRefs under a call's ``numbers`` argument; invalid ones are ignored."""
    raw = call.arguments.get("numbers")
    refs: list[NumberRef] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            refs.append(NumberRef.model_validate(item))
        except ValidationError:
            continue  # an invalid ref is ignored
    return refs


def canonical_args(arguments: Mapping[str, JsonValue]) -> str:
    """Canonical JSON (sorted keys, compact); a non-finite float falls back to ``json``."""
    try:
        return canonical_json(arguments)
    except SchemaViolation:
        return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def repair_notes(
    raw: Mapping[str, JsonValue] | None,
    scratchpad: Scratchpad,
    allowed: Sequence[re.Pattern[str]],
) -> CompactionNotes | None:
    """``compact_build.validate_notes`` (U07-73)."""
    if raw is None:
        return None
    try:
        notes = CompactionNotes.model_validate(cut_raw(raw))
    except ValidationError:
        return None
    known = frozenset(scratchpad.query_ids())
    fix = NotesRepair(frozenset(ref.id for ref in scratchpad.cited_numbers()), known, allowed)
    hypotheses = [
        Hypothesis(
            text=fix.fit(hyp.text, HYPOTHESIS_CHARS),
            result=hyp.result,
            query_ids=[qid for qid in hyp.query_ids if qid in known],
        )
        for hyp in notes.hypotheses
    ]
    return CompactionNotes(
        progress=fix.fit(notes.progress, PROGRESS_CHARS),
        hypotheses=hypotheses,
        dead_ends=[fix.fit(item, ITEM_CHARS) for item in notes.dead_ends],
        next_steps=[fix.fit(item, ITEM_CHARS) for item in notes.next_steps],
    )
