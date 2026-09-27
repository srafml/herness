"""Property tests for impl 07 compaction (PT07-01, PT07-02; U07-70 … U07-76).

PT07-01 has two parts: the pure steps (U07-70 … U07-75) under a test-local driver of the
design 07 §5.4 ledger algorithm (T07-13), and the ``ContextCompactor`` loop (U07-76, T07-14)
that feeds each returned list back as ``state.messages``.
"""

import asyncio
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.unit.harness.memory import _compactor_support as cs

from herness.core.types import (
    Message,
    NumberRef,
    ReasoningPart,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
)
from herness.harness.memory.compact_build import (
    QUERY_ID_SCAN_RE,
    Group,
    ParsedTable,
    build_compacted,
    cited_from_group,
    entry_from_result,
    split_groups,
)
from herness.harness.memory.settings import CompactionConfig
from herness.harness.memory.working import Scratchpad

pytestmark = pytest.mark.unit

_SETTINGS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
UNKNOWN = "unknown"


@dataclass(frozen=True)
class _Step:
    """One generated tool step: its call, result and the assistant's follow-up."""

    qid: int
    kind: str
    cells: tuple[int, ...]
    say: int | None
    cite: bool
    nudge: bool
    reasoning: bool


_steps = st.lists(
    st.builds(
        _Step,
        qid=st.integers(0, 2**64 - 1),
        kind=st.sampled_from(["table", "table", "error", "text", "two_ids"]),
        cells=st.lists(st.integers(-(10**6), 10**6), min_size=1, max_size=4).map(tuple),
        say=st.none() | st.integers(0, 3),
        cite=st.booleans(),
        nudge=st.booleans(),
        reasoning=st.booleans(),
    ),
    min_size=1,
    max_size=6,
)


def _qid(seed: int) -> str:
    return f"q_{seed:016x}"


def _content(step: _Step) -> str:
    rows = "\n".join(f"r{i} | {v}" for i, v in enumerate(step.cells))
    table = f"query_id={_qid(step.qid)} rows={len(step.cells)}\nk | v\nVARCHAR | BIGINT\n{rows}"
    return {
        "table": table,
        "error": f"query {_qid(step.qid)} failed\nsyntax",
        "text": "tables: a, b",
        "two_ids": f"{table}\nsee {_qid(step.qid ^ 1)}",
    }[step.kind]


def _step_messages(step: _Step, call_id: str) -> list[Message]:
    calls: list[Any] = [ToolCallPart(call=ToolCall(id=call_id, name="run_sql", arguments={}))]
    if step.cite and step.kind != "text":
        ref: dict[str, Any] = {
            "id": "n1",
            "value": step.cells[0],
            "unit": "count",
            "query_id": _qid(step.qid),
            "column": "v",
            "row_key": {"k": "r0"},
        }
        post = ToolCall(id=call_id + "p", name="post_finding", arguments={"numbers": [ref]})
        calls.append(ToolCallPart(call=post))
    out = [
        Message(role="assistant", parts=calls),
        Message(
            role="tool",
            parts=[
                ToolResultPart(
                    tool_call_id=call_id, content=_content(step), is_error=step.kind == "error"
                )
            ],
        ),
    ]
    follow: list[Any] = [ReasoningPart(text="hmm", provider="vllm")] if step.reasoning else []
    if step.say is not None:
        value = step.cells[step.say % len(step.cells)]
        follow.append(TextPart(text=f"saw {value} rows"))
    if follow:
        out.append(Message(role="assistant", parts=follow))
    if step.nudge:
        out.append(Message(role="user", parts=[TextPart(text="go on")], kind="nudge"))
    return out


def _history(batch: list[_Step], tag: str) -> list[Message]:
    return [m for i, step in enumerate(batch) for m in _step_messages(step, f"{tag}{i}")]


def _ledger(
    pad: Scratchpad, group: Group, messages: list[Message], tables: list[ParsedTable]
) -> None:
    """The design 07 §5.4 step 1 for one group (the U07-76 driver of T07-14 does the same)."""
    members = [messages[i] for i in group.indices]
    calls = {p.call.id: p.call for m in members for p in m.parts if isinstance(p, ToolCallPart)}
    for result in (p for m in members for p in m.parts if isinstance(p, ToolResultPart)):
        call = calls.get(result.tool_call_id) or ToolCall(id="x", name=UNKNOWN, arguments={})
        entries, table = entry_from_result(call, result, group.step, 60)
        for entry in entries:
            pad.upsert(entry)
        tables += [table] if table is not None else []
    cited, unmatched = cited_from_group(members, tables, [], group.step)
    for ref in cited:
        pad.cite(ref)
    for num in unmatched:
        pad.add_unmatched(num.value, num.step)


def _compact(
    m0: Message, messages: list[Message], pad: Scratchpad, keep_k: int, *, fresh: bool
) -> list[Message]:
    groups = split_groups(messages[1:], 0)
    keep = groups[-keep_k:]
    tables: list[ParsedTable] = []
    for group in groups if fresh else groups[:-keep_k]:  # Claude: kept groups become text
        _ledger(pad, group, messages, tables)
    return build_compacted(m0, pad.render("b"), keep, messages, fresh_conversation=fresh)


def _text(messages: list[Message]) -> str:
    out: list[str] = []
    for part in (p for m in messages for p in m.parts):
        if isinstance(part, TextPart):
            out.append(part.text)
        elif isinstance(part, ToolResultPart):
            out.append(part.content)
    return "\n".join(out)


def _cited(messages: list[Message]) -> list[NumberRef]:
    return [
        NumberRef.model_validate(raw)
        for m in messages
        for p in m.parts
        if isinstance(p, ToolCallPart)
        for raw in p.call.arguments.get("numbers", [])  # type: ignore[union-attr]
    ]


@_SETTINGS
@given(
    batches=st.lists(_steps, min_size=1, max_size=3),
    keep_k=st.integers(1, 3),
    fresh=st.booleans(),
)
def test_pt07_01_query_ids_and_cited_numbers_survive(
    batches: list[list[_Step]], keep_k: int, *, fresh: bool
) -> None:
    """PT07-01 after any number of compactions every query_id and cited number is present."""
    m0 = Message(role="user", parts=[TextPart(text="task")])
    pad = Scratchpad()
    messages = [m0]
    seen_ids: set[str] = set()
    seen_refs: list[NumberRef] = []
    for number, batch in enumerate(batches):
        new = _history(batch, f"b{number}c")
        seen_ids |= set(QUERY_ID_SCAN_RE.findall(_text(new)))
        seen_refs += _cited(new)
        out = _compact(m0, [*messages, *new], pad, keep_k, fresh=fresh)
        assert seen_ids <= set(QUERY_ID_SCAN_RE.findall(_text(out)))
        known = {(r.query_id, str(r.value)) for r in pad.cited_numbers()}
        kept = {(r.query_id, str(r.value)) for r in _cited(out[1:])}
        assert {(r.query_id, str(r.value)) for r in seen_refs} <= known | kept
        messages = [m0, *out[1:]]  # the next round starts from the original task message


def _unsplit(before: list[Message], after: list[Message]) -> None:
    """Every kept call keeps all its results, in the same group of the new list."""
    results: dict[str, int] = {}
    for part in (p for m in before for p in m.parts if isinstance(p, ToolResultPart)):
        results[part.tool_call_id] = results.get(part.tool_call_id, 0) + 1
    groups = split_groups(after[1:], 0)
    where = {i: n for n, g in enumerate(groups) for i in g.indices}
    owner: dict[str, int] = {}
    seen: dict[str, int] = {}
    for index, message in enumerate(after[1:], start=1):
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                owner[part.call.id] = where[index]
            elif isinstance(part, ToolResultPart):
                assert owner[part.tool_call_id] == where[index]
                seen[part.tool_call_id] = seen.get(part.tool_call_id, 0) + 1
    assert all(seen.get(call_id, 0) == results.get(call_id, 0) for call_id in owner)


@_SETTINGS
@given(
    batches=st.lists(_steps, min_size=1, max_size=4),
    keep_k=st.integers(1, 3),
    fresh=st.booleans(),
)
def test_pt07_01_compactor_loop_keeps_ids_numbers_and_groups(
    batches: list[list[_Step]], keep_k: int, *, fresh: bool
) -> None:
    """PT07-01 through ContextCompactor, output fed back as state.messages: every original
    query_id and cited number is present after each compaction; groups never split."""
    keep = {"local": keep_k, "claude": keep_k}
    cfg = CompactionConfig.model_validate({"keep_last_tool_groups": keep})
    comp = cs.compactor(kind="anthropic" if fresh else "local", cfg=cfg)
    messages = [Message(role="user", parts=[TextPart(text="task")])]
    seen_ids: set[str] = set()
    seen_refs: set[tuple[str, str]] = set()
    for number, batch in enumerate(batches):
        new = _history(batch, f"b{number}c")
        seen_ids |= set(QUERY_ID_SCAN_RE.findall(cs.all_text(new)))
        seen_refs |= {(r.query_id, str(r.value)) for r in _cited(new)}
        before = [*messages, *new]
        out = asyncio.run(comp.on_context_pressure(cs.state_of(before)))
        text = cs.all_text(out)
        assert seen_ids <= set(QUERY_ID_SCAN_RE.findall(text))
        assert all(qid in text and value in text for qid, value in seen_refs)
        if not fresh:
            _unsplit(before, out)
        messages = out
    assert comp.scratchpad.compactions == len(batches)


_histories = st.lists(
    st.tuples(
        st.sampled_from(["call", "result", "late", "orphan", "user", "text"]), st.integers(0, 5)
    ),
    max_size=25,
)


def _random_history(plan: list[tuple[str, int]]) -> list[Message]:
    out: list[Message] = [Message(role="user", parts=[TextPart(text="task")])]
    calls: list[str] = []
    for index, (kind, pick) in enumerate(plan):
        if kind == "call":
            calls.append(f"c{index}")
            call = ToolCall(id=calls[-1], name="run_sql", arguments={})
            out.append(Message(role="assistant", parts=[ToolCallPart(call=call)]))
        elif kind in {"result", "late", "orphan"}:
            target = (
                f"none{index}" if kind == "orphan" or not calls else calls[-1 - pick % len(calls)]
            )
            part = ToolResultPart(tool_call_id=target, content=f"r {_qid(index)}")
            out.append(Message(role="tool", parts=[part]))
        elif kind == "user":
            out.append(Message(role="user", parts=[TextPart(text="nudge")], kind="nudge"))
        else:
            parts: list[Any] = [ReasoningPart(text="r", provider="p"), TextPart(text="t")]
            out.append(Message(role="assistant", parts=parts))
    return out


@_SETTINGS
@given(plan=_histories)
def test_pt07_01_groups_never_split(plan: list[tuple[str, int]]) -> None:
    """PT07-01 groups are contiguous, in order, and never split a call from its results."""
    messages = _random_history(plan)
    groups = split_groups(messages[1:], 0)
    assert [i for g in groups for i in g.indices] == list(range(1, len(messages)))
    where = {i: n for n, g in enumerate(groups) for i in g.indices}
    owner: dict[str, int] = {}
    for index, message in enumerate(messages[1:], start=1):
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                owner.setdefault(part.call.id, where[index])
            elif isinstance(part, ToolResultPart) and part.tool_call_id in owner:
                assert owner[part.tool_call_id] == where[index]
    assert all(n == 0 for n, g in enumerate(groups) if g.is_preamble)


def _dump(messages: list[Message]) -> list[dict[str, Any]]:
    return [m.model_dump(mode="json") for m in messages]


def _objects(messages: list[Message]) -> set[int]:
    return {id(o) for m in messages for o in (m, m.parts, *m.parts)}


@_SETTINGS
@given(plan=_histories, keep_k=st.integers(1, 4), fresh=st.booleans())
def test_pt07_02_inputs_untouched_outputs_fresh(
    plan: list[tuple[str, int]], keep_k: int, *, fresh: bool
) -> None:
    """PT07-02 inputs deep-equal before/after; no object reuse; Claude output is one message."""
    messages = _random_history(plan)
    before = _dump(messages)
    groups = split_groups(messages[1:], 0)
    keep = groups[-keep_k:]
    out = build_compacted(messages[0], "S", keep, messages, fresh_conversation=fresh)
    again = build_compacted(messages[0], "S", keep, messages, fresh_conversation=fresh)
    assert _dump(messages) == before
    assert [m.model_dump_json() for m in out] == [m.model_dump_json() for m in again]
    assert not _objects(out) & _objects(messages)
    assert out[0].role == "user"
    assert out[0].kind == "compaction_summary"
    if fresh:
        assert len(out) == 1
        assert all(isinstance(p, TextPart) for p in out[0].parts)
    assert not any(isinstance(p, ReasoningPart) for m in out for p in m.parts)
    roles = [m.role for m in out]
    assert all(not (a == b == "user") for a, b in pairwise(roles))
