"""Working memory: the scratchpad and its ledger (impl 07 U07-66, U07-67; design 07 §5.1, §5.5).

The ledger is tool output, not model-written text, so it carries values directly; every
dynamic string is escaped on render (TH07-07). ``query_id``s and cited numbers are never lost
by ``compact`` (TH07-15). Nothing here logs scratchpad content.
"""

import json
import math
import re
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from herness.core.logging import get_logger
from herness.core.types import NumberRef
from herness.harness.memory.render import escape_attr, escape_content
from herness.harness.memory.types import QUERY_ID_RE

__all__ = [
    "SCRATCHPAD_MAX_BYTES",
    "CompactionNotes",
    "Hypothesis",
    "LedgerEntry",
    "Scratchpad",
    "UnmatchedNumeral",
]

SCRATCHPAD_MAX_BYTES: Final = 1_048_576  # canonical JSON cap; the compactor enforces it (U07-76)
UNKNOWN_TOOL: Final = "unknown"
COMPACT_ERROR_CHARS: Final = 80
LEDGER_HEADER: Final = "LEDGER (verbatim from tool results; cite these query_ids and numbers)"
# Every character str.splitlines breaks on: field text can never start a line of its own.
_LINE_BREAKS: Final = re.compile(r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]+")

_log = get_logger("harness.memory")

type _Str200 = Annotated[str, Field(max_length=200)]
type _RefKey = tuple[str, str, str, str]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LedgerEntry(_Model):
    """One tool call in the ledger (U07-66); ``query_id`` is ``""`` for a failed call."""

    query_id: str
    tool: str = Field(max_length=64)
    step: int
    sql_head: str | None = Field(default=None, max_length=200)
    row_count: int | None = None
    columns: list[str] = Field(default=[], max_length=50)
    cited: list[NumberRef] = []
    sample: list[dict[str, JsonValue]] = Field(default=[], max_length=5)
    error: str | None = Field(default=None, max_length=200)

    @field_validator("sample")
    @classmethod
    def _finite_sample(cls, value: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
        if not all(_finite(row) for row in value):
            msg = "sample values must be finite numbers"
            raise ValueError(msg)
        return value

    @field_validator("query_id")
    @classmethod
    def _query_id(cls, value: str) -> str:
        if value and QUERY_ID_RE.fullmatch(value) is None:
            msg = "query_id must match q_<16 hex> or be empty"
            raise ValueError(msg)
        return value


class Hypothesis(_Model):
    """One hypothesis of the compaction notes (U07-66)."""

    text: str = Field(max_length=300)
    result: Literal["supported", "refuted", "unclear"]
    query_ids: list[str] = Field(default=[], max_length=10)


class CompactionNotes(_Model):
    """Model-written (or deterministic) notes of a compaction (U07-66; design 07 §5.4)."""

    progress: str = Field(max_length=600)
    hypotheses: list[Hypothesis] = Field(default=[], max_length=10)
    dead_ends: list[_Str200] = Field(default=[], max_length=10)
    next_steps: list[_Str200] = Field(default=[], max_length=10)
    steps: list[Annotated[str, Field(max_length=160)]] = Field(default=[], max_length=200)


class UnmatchedNumeral(_Model):
    """A numeral the agent wrote that matched no tool result (U07-66)."""

    value: str = Field(max_length=40)
    step: int


def _finite(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    return True


def _flat(text: str) -> str:
    """One rendered line: line-breaking characters become one space (TH07-07)."""
    return _LINE_BREAKS.sub(" ", text)


def _json(value: object) -> str:
    """Compact, key-sorted JSON; never raises on a non-finite float (render raises nothing)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _ref_key(ref: NumberRef) -> _RefKey:
    row_key = _json(ref.row_key) if ref.row_key else ""  # None and {} render alike
    return ref.query_id, ref.column, row_key, str(ref.value)


def _merge(old: LedgerEntry, new: LedgerEntry) -> LedgerEntry:
    """Merge a repeated call into its entry (U07-67 ``upsert``)."""
    placeholder = old.tool == UNKNOWN_TOOL  # a minimal entry made by ``cite``
    return LedgerEntry(
        query_id=old.query_id,
        tool=new.tool if placeholder else old.tool,
        step=new.step if placeholder else min(old.step, new.step),
        sql_head=old.sql_head if old.sql_head is not None else new.sql_head,
        row_count=old.row_count if old.row_count is not None else new.row_count,
        columns=old.columns or new.columns,
        cited=old.cited,  # new refs are added through ``cite`` (unique ids)
        sample=old.sample or new.sample,
        error=new.error if new.error is not None else old.error,
    )


def _entry_line(entry: LedgerEntry) -> str:
    parts = [f"- {entry.query_id} {entry.tool} step {entry.step}"]
    if entry.row_count is not None:
        parts.append(f" rows={entry.row_count}")
    if entry.columns:
        parts.append(f" cols=[{','.join(entry.columns)}]")
    if entry.sample:
        parts.append(f" sample={_json(entry.sample)}")
    for ref in entry.cited:
        key = ", ".join(f"{k}={v}" for k, v in (ref.row_key or {}).items())
        parts.append(f" cited: {ref.id} {ref.column}={ref.value}" + (f" ({key})" if key else ""))
    if entry.error is not None:
        parts.append(f" ERROR: {entry.error}")
    return "".join(parts)


def _labelled(label: str, items: list[str]) -> str:
    return f"{label}: {'; '.join(items)}" if items else f"{label}:"


def _hypothesis(hyp: Hypothesis) -> str:
    ids = f" ({', '.join(hyp.query_ids)})" if hyp.query_ids else ""
    return f"[{hyp.result}] {hyp.text}{ids}"


def _notes_lines(notes: CompactionNotes) -> list[str]:
    return [
        f"progress: {notes.progress}",
        _labelled("hypotheses", [_hypothesis(hyp) for hyp in notes.hypotheses]),
        _labelled("dead_ends", notes.dead_ends),
        _labelled("next_steps", notes.next_steps),
        *(f"- {line}" for line in notes.steps),
    ]


class Scratchpad(_Model):
    """Per-task working memory, saved under checkpoint key ``scratchpad`` (U07-67; R-21).

    Mutable and not thread-safe: owned by one task's ``ContextCompactor``.
    """

    ledger: list[LedgerEntry] = []
    unmatched: list[UnmatchedNumeral] = []
    notes: CompactionNotes | None = None
    compactions: int = 0
    covers_steps: tuple[int, int] = (0, 0)

    def _find(self, entry: LedgerEntry) -> int | None:
        for index, known in enumerate(self.ledger):
            if entry.query_id:
                if known.query_id == entry.query_id:
                    return index
            elif not known.query_id and (known.tool, known.step) == (entry.tool, entry.step):
                return index
        return None

    def upsert(self, entry: LedgerEntry) -> None:
        """Add a call, or merge it into the entry of the same ``query_id`` (or tool and step).

        Its cited refs go through ``cite``, so ids stay ``n1..nK`` and unique in the ledger.
        """
        bare = entry.model_copy(update={"cited": []}, deep=True)
        index = self._find(bare)
        if index is None:
            self.ledger.append(bare)
        else:
            self.ledger[index] = _merge(self.ledger[index], bare)
        for ref in entry.cited:
            self.cite(ref)

    def cite(self, ref: NumberRef) -> str:
        """Record a cited number; return its ledger id (``n1..nK``, unique in the ledger)."""
        known = self.cited_numbers()
        key = _ref_key(ref)
        for cited in known:
            if _ref_key(cited) == key:
                return cited.id
        taken = {cited.id for cited in known}
        number = len(known) + 1
        while f"n{number}" in taken:
            number += 1
        ref_id = f"n{number}"
        stub = LedgerEntry(query_id=ref.query_id, tool=UNKNOWN_TOOL, step=0)
        index = self._find(stub)
        if index is None:
            self.ledger.append(stub)
            index = len(self.ledger) - 1
        entry = self.ledger[index]
        cited_ref = ref.model_copy(update={"id": ref_id})
        self.ledger[index] = entry.model_copy(update={"cited": [*entry.cited, cited_ref]})
        return ref_id

    def add_unmatched(self, value: str, step: int) -> None:
        """Record a numeral that matched no tool result."""
        self.unmatched.append(UnmatchedNumeral(value=value, step=step))

    def compact(self) -> None:
        """Shrink every entry; ``query_id``, tool, step, row count and cited refs stay."""
        self.ledger = [
            entry.model_copy(
                update={
                    "sql_head": None,
                    "columns": [],
                    "sample": [],
                    "error": None if entry.error is None else entry.error[:COMPACT_ERROR_CHARS],
                }
            )
            for entry in self.ledger
        ]

    def render(self, build_id: str) -> str:
        """The design 07 §5.5 scratchpad message; all dynamic text escaped (U07-44)."""
        a, b = self.covers_steps
        opening = (
            f'<scratchpad compactions="{self.compactions}" covers_steps="{a}-{b}"'
            f' build_id="{escape_attr(_flat(build_id))}">'
        )
        body = [
            LEDGER_HEADER,
            *(_entry_line(entry) for entry in self.ledger),
            *(f"- unmatched: {num.value} (step {num.step})" for num in self.unmatched),
            "NOTES",
            *(_notes_lines(self.notes) if self.notes is not None else []),
        ]
        # One body item is one line; the fixed labels hold no line break or escapable
        # character, so flattening and escaping each whole item treats every dynamic string.
        lines = (escape_content(_flat(line)) for line in body)
        return "\n".join([opening, *lines, "</scratchpad>"])

    def to_checkpoint(self) -> dict[str, JsonValue]:
        """The JSON value saved under checkpoint key ``scratchpad`` (T08-16, R-21)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_checkpoint(cls, text: str | None) -> "Scratchpad":
        """Restore from checkpoint JSON text; missing or invalid gives an empty scratchpad."""
        if text is None:
            return cls()
        try:
            return cls.model_validate_json(text)
        except ValidationError as exc:  # invalid JSON is a ValidationError too
            _log.warning("memory.scratchpad.invalid", error_type=type(exc).__name__)
            return cls()

    def size_bytes(self) -> int:
        """UTF-8 size of the compact JSON checkpoint value (cap ``SCRATCHPAD_MAX_BYTES``)."""
        return len(_json(self.to_checkpoint()).encode("utf-8"))

    def query_ids(self) -> set[str]:
        """Every non-empty ``query_id`` in the ledger."""
        return {entry.query_id for entry in self.ledger if entry.query_id}

    def cited_numbers(self) -> list[NumberRef]:
        """Every cited ref, in ledger order."""
        return [ref for entry in self.ledger for ref in entry.cited]
