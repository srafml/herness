"""ST07-14 (TH07-14; LLM02): memory content on its way off the host is re-scanned by the
egress guard; a planted unredacted string from memory is blocked (T07-24).

Hybrid profile (D5 substitute: the hybrid test tree with `chat_approved: true`,
`tests.support._f07_hybrid`). The planted string (a person's name the memory redactor does not
know, so the write path stores it unredacted: forced into memory past write-time redaction) is a
human glossary item of the real spec 07 `MemoryStore`; the spec 10 guard's re-scan directory
knows the name. Hybrid keeps memory itself away from off-network calls by design (the off-network
Writer gets no `recall_memory` tool, U06-100, and its evidence pack no `prior_context`,
U06-123), so the route this row exercises is the one left: a local analyst (real spec 05 loop,
`FakeLLMClient` script) recalls the record with the real `recall_memory` tool and quotes it in
its finding; the hybrid Writer (`claude-opus`, real loop, real Anthropic adapter) is handed the
pack, whose token count (`messages.count_tokens`, R-17) and request go through the real guard
(`RecordingGuard`). The count request carries no build id, so T06-29 gap A (build id read as
PHONE) does not mask its decision; the control run without the planted string shows the count
request is then allowed. Other roles are the T06-22 driver's scripted agents (`drv`).
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any, Final

import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.integration.memory._it07_agents import script
from tests.integration.memory._it07_env import memory_store, record_portfolio
from tests.support._f07_hybrid import (
    LazyWriterRouter,
    RecordingGuard,
    guard_redactor,
    hybrid_profile_config,
    writer_script,
)
from tests.support.egress_harness import egress_lines
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_llm import FakeLLMClient
from tests.unit.harness._blackboard_env import test_redactor
from tests.unit.harness.memory._write_env import proposal

from herness.core import egress
from herness.core import redact as r
from herness.eval.scripted import ScriptBook
from herness.harness import loop
from herness.harness import warehouse_tools as wt
from herness.harness.llm.registry import LLMRegistry
from herness.harness.memory import register_memory_components
from herness.harness.swarm import scheduler, writer
from herness.harness.swarm.run import Swarm
from herness.harness.tools import tool_registry
from herness.store.ops import core

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
__all__ = ["drv", "test_redactor"]

API: Final = "https://api.anthropic.com/v1/messages"
COUNT: Final = API + "/count_tokens"
PLANTED: Final = "Marta Quintero"  # known to the guard's re-scan directory only
QUERY: Final = "Who handles billing escalations?"
RECALL_KEYS: Final = {"layers": None, "k": None, "kinds": None, "entity": None}


@dataclasses.dataclass
class Hybrid:
    guard: RecordingGuard
    logs: Path


def _analyst_turns(quote: str) -> list[dict[str, Any]]:
    """Recall the record, then post a finding quoting it (the model's own choice, scripted)."""
    nref = {"id": "n1", "value": 7, "unit": "count", "query_id": e.incident_qid("t1"),
            "column": "incidents", "row_key": None, "format": None}  # fmt: skip
    post = {"claim": f"Team t1 had [[n1]] incidents. {quote}", "entity_type": "team",
            "entity_id": "t1", "query_ids": [], "confidence": 0.8, "numbers": [nref]}  # fmt: skip
    out = {"summary": "looked", "unknowns": [], "suggested_followups": []}
    return [
        {"tool_calls": [{"name": "recall_memory", "arguments": {"query": QUERY} | RECALL_KEYS}]},
        {"tool_calls": [{"name": "post_finding", "arguments": post}]},
        {"final": {"text": "done"}}, {"final": {"output": out}},
    ]  # fmt: skip


def _writer_book() -> ScriptBook:
    [row] = core.read_all("SELECT run_id FROM run", ())
    out = e.writer_output(str(row["run_id"]))
    return ScriptBook([writer_script([{"final": {"text": "draft"}}, {"final": {"output": out}}])])


def _local_analyst(drv: Drv, llms: LLMRegistry, content: str, monkeypatch: pytest.MonkeyPatch
                   ) -> None:  # fmt: skip
    """One analyst task with `recall_memory`, on the real loop over the scripted client; the
    hybrid registry still builds the real Anthropic client for `claude-opus`."""
    drv.set_pipeline(t1=1, t2=0)
    det_spec = e.det_spec

    def with_recall(*args: Any, **kwargs: Any) -> Any:
        spec = det_spec(*args, **kwargs)
        return spec.model_copy(update={"tools": [*spec.tools, "recall_memory"]})

    monkeypatch.setattr(e, "det_spec", with_recall)
    fake = FakeLLMClient(ScriptBook([script("analyst_general", _analyst_turns(content))]))
    real = llms.client

    def route(name: str) -> Any:
        if name == "claude-opus":
            return real(name)
        fake.name = name
        return fake

    monkeypatch.setattr(llms, "client", route)
    monkeypatch.setattr(scheduler, "run_agent", loop.run_agent)  # the analyst: real loop
    monkeypatch.setattr(writer, "run_agent", loop.run_agent)  # the Writer: real loop


def _hybrid_run(
    drv: Drv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keyring_: MemoryKeyring,
    redactor: r.Redactor, content: str,
) -> Hybrid:  # fmt: skip
    """`content` in memory (stored as written), then one hybrid funding run."""
    data_root = Path(drv.hcfg.paths.data)
    record_portfolio()
    mem = memory_store(tmp_path, data_root)
    item = mem.store.propose(proposal(content))
    [row] = core.read_all("SELECT status, content FROM memory_item WHERE memory_id = ?",
                          (item.memory_id,))  # fmt: skip
    assert (row["status"], row["content"]) == ("active", content)  # stored unredacted
    mem.embed.overrides[QUERY] = mem.embed(f"glossary: {content}")  # ANN similarity 1
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    hcfg = hybrid_profile_config(tmp_path / "hybrid", data_root)
    monkeypatch.setattr(r._State, "redactor", redactor)  # the config load dropped it
    guard = RecordingGuard(hcfg, guard_redactor(PLANTED))
    monkeypatch.setattr(egress, "get_guard", lambda: guard)
    keyring_.store[("herness", "anthropic.api_key")] = "sk-ant-" + "st0714" + "unused"
    keyring_.store[("herness", "vllm.api_key")] = "vllm-" + "st0714"
    register_memory_components(tool_registry(), mem.store)
    wt.register_warehouse_tools()
    LazyWriterRouter(_writer_book).install(monkeypatch)  # nothing reaches a real network
    llms = LLMRegistry(hcfg.models, profile="hybrid", egress_enabled=True)
    _local_analyst(drv, llms, content, monkeypatch)
    swarm = Swarm(hcfg.pipelines, llms, mem.store, deps=dataclasses.replace(drv.deps, hcfg=hcfg))
    drv.swarms.append(swarm)
    asyncio.run(swarm.start(drv.request(depth="standard")))
    return Hybrid(guard, Path(hcfg.paths.logs))


def _refusals(hy: Hybrid) -> list[dict[str, Any]]:
    return [x for x in egress_lines(hy.logs) if x.get("decision") == "blocked"]


def test_st07_14_planted_memory_string_is_blocked_by_the_egress_guard(
    drv: Drv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring: MemoryKeyring,
    test_redactor: r.Redactor,
) -> None:
    """ST07-14 the hybrid Writer's off-network requests carrying the planted string from memory
    are refused by the guard re-scan (`pii_detected`); the token count request, which has no
    build id, is refused for PERSON alone; nothing that left the host holds the string."""
    content = f"Billing escalations go to {PLANTED} before the on-call lead."
    hy = _hybrid_run(drv, tmp_path, monkeypatch, fake_keyring, test_redactor, content)

    off = [d for d in hy.guard.decisions if d.url in (API, COUNT)]
    carrying = [d for d in off if PLANTED in d.text()]
    assert {d.url for d in carrying} == {API, COUNT}  # the pack's count and the request
    assert {d.purpose for d in carrying} == {"reasoning_final"}
    assert {d.blocked for d in carrying} == {"pii_detected"}  # egress guard blocks
    assert all(PLANTED not in d.text() for d in hy.guard.decisions if d.blocked is None)
    counts = [x for x in _refusals(hy) if x.get("path") == "/v1/messages/count_tokens"]
    assert counts
    assert all(set(x["scan_hits"]) == {"PERSON"} for x in counts)  # the planted string only
    assert all(PLANTED not in str(x) for x in _refusals(hy))  # the log never echoes it


def test_st07_14_control_without_planted_string_count_request_is_allowed(
    drv: Drv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring: MemoryKeyring,
    test_redactor: r.Redactor,
) -> None:
    """ST07-14 control: the same run with clean memory text sends the pack's token count
    request (allowed) and no refusal reports a PERSON hit."""
    content = "Billing escalations go to the payments desk before the on-call lead."
    hy = _hybrid_run(drv, tmp_path, monkeypatch, fake_keyring, test_redactor, content)

    counts = [d for d in hy.guard.decisions if d.url == COUNT]
    assert counts
    assert {d.blocked for d in counts} == {None}
    assert any("payments desk" in d.text() for d in counts)  # the memory text went this way
    assert all("PERSON" not in x["scan_hits"] for x in _refusals(hy))
