"""Compaction deterministic part benchmark (BT07-04).

Run: pytest -m "integration and slow" tests/bench.

T07-13 acceptance: the pure steps of one compaction over 100 messages stay under 50 ms. The
steps are driven the way design 07 §5.4 describes (ledger, notes fallback, rebuild); the
``ContextCompactor`` itself (U07-76, T07-14) adds only bookkeeping around them.
"""

import time
from typing import Any

import pytest

from herness.core.types import Message, TextPart, ToolCall, ToolCallPart, ToolResultPart
from herness.harness.memory.compact_build import (
    ParsedTable,
    build_compacted,
    cited_from_group,
    deterministic_notes,
    entry_from_result,
    split_groups,
)
from herness.harness.memory.working import Scratchpad

pytestmark = [pytest.mark.integration, pytest.mark.slow]

LIMIT_S = 0.050
MESSAGES = 100
ROUNDS = 7


def _history() -> list[Message]:
    out: list[Message] = [Message(role="user", parts=[TextPart(text="investigate MTTR")])]
    step = 0
    while len(out) < MESSAGES:
        cells = "\n".join(f"team_{r} | {r * 7} | {r * 1.37:.2f} | {r + step}" for r in range(50))
        content = (
            f"query_id=q_{step:016x} rows=50 shown=50 truncated=no ordered=yes\n"
            f"team | incidents | mttr_h | age\nVARCHAR | BIGINT | DOUBLE | BIGINT\n{cells}"
        )
        args: dict[str, Any] = {"sql": f"SELECT team FROM t LIMIT {step}"}  # noqa: S608 - fixed text
        out += [
            Message(
                role="assistant",
                parts=[ToolCallPart(call=ToolCall(id=f"c{step}", name="run_sql", arguments=args))],
            ),
            Message(role="tool", parts=[ToolResultPart(tool_call_id=f"c{step}", content=content)]),
            Message(
                role="assistant",
                parts=[TextPart(text=f"team_3 has 21 incidents, MTTR 4.11 h, and {step}99 open")],
            ),
        ]
        step += 1
    return out[:MESSAGES]


def _compact(messages: list[Message]) -> list[Message]:
    pad = Scratchpad()
    groups = split_groups(messages[1:], 0)
    drop, keep = groups[:-3], groups[-3:]
    tables: list[ParsedTable] = []
    for group in drop:
        members = [messages[i] for i in group.indices]
        results = {
            p.tool_call_id: p for m in members for p in m.parts if isinstance(p, ToolResultPart)
        }
        for call in (p.call for m in members for p in m.parts if isinstance(p, ToolCallPart)):
            entries, table = entry_from_result(call, results[call.id], group.step, 60)
            for entry in entries:
                pad.upsert(entry)
            tables += [table] if table is not None else []
        cited, unmatched = cited_from_group(members, tables, [], group.step)
        for ref in cited:
            pad.cite(ref)
        for num in unmatched:
            pad.add_unmatched(num.value, num.step)
    pad.notes = deterministic_notes(drop, messages, None)
    return build_compacted(messages[0], pad.render("b"), keep, messages, fresh_conversation=False)


def test_bt07_04_compaction_deterministic_part_under_50ms() -> None:
    """BT07-04 compaction deterministic part, 100 messages: < 50 ms (best of 7 rounds)."""
    messages = _history()
    timings: list[float] = []
    for _ in range(ROUNDS):
        start = time.perf_counter()
        out = _compact(messages)
        timings.append(time.perf_counter() - start)
    assert len(out) > 1
    assert min(timings) < LIMIT_S, f"best {min(timings) * 1000:.1f} ms"
