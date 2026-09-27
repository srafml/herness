"""Fault tests for herness.harness.memory.compactor (FT07-05; U07-76, U07-77).

A hung summarizer server (a ``FakeLLMClient`` slower than ``profile.timeout_s``, or the
scripted ``hang`` fault) gives deterministic notes and the compaction still succeeds. The
restart part kills the next compaction inside its summarizer call (a ``BaseException`` that
unwinds the coroutine, as a killed process would stop it), then a NEW ``ContextCompactor``
with the same ops restores the scratchpad saved at the fault point and continues.
"""

from __future__ import annotations

import re

import pytest
from tests.support.fake_llm import FakeLLMClient
from tests.unit.harness.memory import _compactor_support as cs

from herness.core.types import LLMRequest, LLMResponse, Message
from herness.eval.scripted import ScriptFault
from herness.harness.memory.working import Scratchpad

pytestmark = pytest.mark.fault

_QID = re.compile(r"q_[0-9a-f]{16}")


class _Killed(BaseException):
    """Stands in for the process dying mid-call."""


class _KillingLLM(FakeLLMClient):
    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        raise _Killed


def _hung(kind: str) -> FakeLLMClient:
    if kind == "timeout":
        return cs.SlowLLM(cs.book({"output": cs.VALID_NOTES}))
    fault = ScriptFault(at=0, kind="hang", count=10)
    return FakeLLMClient(cs.book({"output": cs.VALID_NOTES}, faults=[fault]))


def _ids(messages: list[Message]) -> set[str]:
    return set(_QID.findall(cs.all_text(messages)))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["timeout", "hang"])
async def test_ft07_05_hung_summarizer_then_restart_from_checkpoint(kind: str) -> None:
    """FT07-05 summarizer server timeout: deterministic notes, compaction succeeds; a kill
    in the next compaction restarts from the saved scratchpad with nothing lost."""
    ops = cs.InMemoryOps()
    first = cs.compactor(client=_hung(kind), ops=ops, timeout_s=0.05)
    task, *rest = cs.history(6)
    cite = cs.call_group("cite", 77, cells=(41, 9), say="total 41 incidents")
    msgs = [task, *cite, *rest]  # the citing group is dropped by the first compaction
    out = await first.on_context_pressure(cs.state_of(msgs))
    report = first.last_report
    assert report is not None
    assert report.notes_source == "deterministic"
    assert ops.saves == 1  # the scratchpad is checkpointed at the fault point
    saved = Scratchpad.model_validate(ops.saved[cs.TASK_ID])
    assert saved == first.scratchpad
    cited = {(ref.query_id, str(ref.value)) for ref in saved.cited_numbers()}
    assert (cs.qid(77), "41") in cited

    more = [*out]
    for i in range(4):
        more += cs.call_group(f"n{i}", 200 + i)
    killed = cs.compactor(client=_KillingLLM(cs.book({"output": cs.VALID_NOTES})), ops=ops)
    with pytest.raises(_Killed):
        await killed.on_context_pressure(cs.state_of(more))
    assert ops.saves == 1  # the killed compaction saved nothing

    restarted = cs.compactor(client=cs.notes_llm(cs.VALID_NOTES), ops=ops)
    final = await restarted.on_context_pressure(cs.state_of(more))
    assert restarted.scratchpad.compactions == 2
    assert _ids(msgs) | _ids(more) <= _ids(final)
    text = cs.all_text(final)
    for query_id, value in cited:
        assert query_id in text
        assert value in text
    assert restarted.last_report is not None
    assert restarted.last_report.notes_source == "llm"
    assert ops.saves == 2
