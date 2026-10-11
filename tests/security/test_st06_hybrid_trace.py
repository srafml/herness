"""ST06-06 (TH06-06) hybrid egress and ST06-09 (TH06-09) decision traceability (T06-26).

Both rows drive full review runs through the real run driver over the T06-22 `drv` set-up
with every role on the real spec 05 loop and spec 11 `FakeLLMClient` scripts
(`tests.support._f06_swarm`); ST06-06 sends the Writer off-network through the real Anthropic
adapter and the real spec 10 egress guard, spied on to see exactly what it decides on.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.support import _f06_hybrid as fh
from tests.support import _f06_swarm as f6
from tests.support import _f08_llm as fl
from tests.support.egress_harness import egress_lines
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.harness._blackboard_env import PLANTED_NAME

from herness.core import config as c
from herness.core import egress
from herness.core import redact as r
from herness.core.types import ReportDraft
from herness.harness.llm.anthropic_client import AnthropicClient
from herness.harness.llm.registry import LLMRegistry
from herness.harness.swarm import _run_env
from herness.harness.swarm.lifecycle import RunResult
from herness.harness.swarm.run import Swarm
from herness.store.ops import query_findings, select_tasks

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
test_redactor = e.env_mod.test_redactor  # type: ignore[attr-defined]
__all__ = ["drv", "test_redactor"]


def _trace(drv: Drv, run_id: str) -> list[dict[str, Any]]:
    path = Path(drv.hcfg.paths.data) / "traces" / f"{run_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- ST06-06 -----------------------------------------------------------------------------------

API: Final = "https://api.anthropic.com/v1/messages"
ENTITY_ID: Final = re.compile(r"(?<![A-Za-z0-9_])t[12](?![A-Za-z0-9_])")
BUILD_ID_GAP = (
    "product gap T06-29 (gap A): every off-network request carries the spec 05 role context"
    " line 'build: <build_id>' (herness/harness/roles/base.py); a build id (8 digits, 6"
    " digits, 6 letters, e.g. 20260925-101500-ABCDEF) has 14 digits and matches the PHONE"
    " pattern of herness/core/redact_patterns.py, so the egress guard re-scan refuses every"
    " hybrid Writer and final-Skeptic call (pii_detected, PHONE) and hybrid always falls back"
    " locally"
)
COST_GAP = (
    "product gap T06-29 (gap B): routing.role_budget('writer') leaves TaskBudget.max_cost_usd"
    " at 0 and the spec 05 loop stops once cost_usd > max_cost_usd, so an off-network Writer"
    " stops at its first paid call (stop_reason task_budget), the task is dead and the run"
    " ends partial"
)
BANNER_GAP = (
    "product gap T06-29 (gap C): the Writer sets banner hybrid_fallback only for the cost-cap"
    " route fallback (route.fallback_local, writer.py); a spec 08 chain fallback after"
    " EgressBlocked inside run_agent is not reported to the Writer, so the draft has no banner"
    " (impl 06 §6 table 'Hybrid egress refused', design T13)"
)


@dataclass
class Hybrid:
    hcfg: c.HernessConfig
    llms: LLMRegistry
    guard: fh.SpyGuard
    wire: fh.WriterRouter
    opus: AnthropicClient


@pytest.fixture
def hybrid(
    drv: Drv,
    test_redactor: r.Redactor,
    fake_keyring: MemoryKeyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Hybrid:
    """Profile `hybrid` with the chat approval flag (D5 substitute, `fh.hybrid_config`), the
    real guard spied on (its re-scan directory also holds `fh.GUARD_ONLY_NAME`), the real
    Anthropic adapter for `claude-opus` (the hybrid Writer) on the scripted Anthropic wire,
    and ticket text planted in every evidence result sample."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    hcfg = fh.hybrid_config(tmp_path / "hybrid", Path(drv.hcfg.paths.data))
    monkeypatch.setattr(r._State, "redactor", test_redactor)  # the config load dropped it
    guard = fh.SpyGuard(hcfg, fh.guard_redactor(PLANTED_NAME))
    monkeypatch.setattr(egress, "get_guard", lambda: guard)
    fake_keyring.store[("herness", "anthropic.api_key")] = "sk-ant-" + "st0606" + "unused"
    llms = LLMRegistry(hcfg.models, profile="hybrid", egress_enabled=True)
    wire = fh.writer_router()
    wire.router.install(monkeypatch)
    fh.plant_sample_text()
    return Hybrid(hcfg, llms, guard, wire, AnthropicClient(llms.config("claude-opus")))


def _start(drv: Drv, hy: Hybrid, monkeypatch: pytest.MonkeyPatch, name: str) -> RunResult:
    """A standard hybrid funding run (every role local and scripted except the Writer) whose
    Skeptics write `name` into every check note; its result, whatever the status."""
    scripts = f6.TaskScripts(skeptic_note=f"Confirmed with {name}.")
    f6.use_real_agents(drv, monkeypatch, scripts, llms=hy.llms, real={"claude-opus": hy.opus})
    deps = replace(drv.deps, hcfg=hy.hcfg)
    swarm = Swarm(hy.hcfg.pipelines, hy.llms, drv.memory, deps=deps)  # type: ignore[arg-type]
    drv.swarms.append(swarm)
    return asyncio.run(swarm.start(drv.request(depth="standard")))


def _hybrid_run(drv: Drv, hy: Hybrid, monkeypatch: pytest.MonkeyPatch, name: str) -> ReportDraft:
    """`_start`, required to end `done`; its draft."""
    res = _start(drv, hy, monkeypatch, name)
    assert res.status == "done"
    assert res.draft_path is not None
    return ReportDraft.model_validate_json(Path(res.draft_path).read_bytes())


def _writer_calls(hy: Hybrid) -> list[fh.Seen]:
    """The Writer's `POST /v1/messages` requests the guard decided on (allowed or refused)."""
    return [s for s in hy.guard.seen if s.url == API]


def _refusals(hy: Hybrid) -> list[dict[str, Any]]:
    """The guard's `blocked` egress log lines (reason and per-type scan hit counts)."""
    return [x for x in egress_lines(hy.hcfg.paths.logs) if x.get("decision") == "blocked"]


def test_st06_06_hybrid_pack_has_no_sample_or_ticket_text_and_pseudonymized_entities(
    drv: Drv, hybrid: Hybrid, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST06-06 (T13) in a hybrid run only the Writer goes off-network (purpose
    `reasoning_final`); every request body the real guard decided on holds the evidence pack:
    the run's findings with entity tokens (`team_001`) and no entity id or catalog name, no
    `result_sample`, none of the ticket text planted in every evidence sample, and the
    personal name planted in the Skeptic notes only as its redaction token, so the guard found
    no PERSON in it. The draft is restored to real ids.

    Depends on T06-29 gap A: today every Writer call is refused (build id read as PHONE) and
    the run is done through the local fallback. Fixing gap A without gap B makes this test
    fail at `_hybrid_run` (the off-network Writer stops on task_budget, run partial), and once
    no call is refused the PERSON check over the refusal lines is vacuous (the body checks
    above still hold)."""
    draft = _hybrid_run(drv, hybrid, monkeypatch, PLANTED_NAME)
    sent = _writer_calls(hybrid)
    assert sent
    assert {s.purpose for s in sent} == {"reasoning_final"}
    for seen in sent:
        text = seen.text()
        assert "Team team_001 had [[n1]] incidents" in text  # pseudonymized finding claims
        assert "Team team_002 had [[n1]] incidents" in text
        assert "result_sample" not in text
        assert fh.TICKET_TEXT not in text
        assert "INC-4711" not in text
        assert ENTITY_ID.search(text) is None
        assert "Team One" not in text
        assert "Team Two" not in text
        assert PLANTED_NAME not in text
        assert "Confirmed with [PERSON_" in text  # the note went out redacted
    assert all("PERSON" not in line["scan_hits"] for line in _refusals(hybrid))
    assert draft.recommendations[0].target_id == "c1"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=BUILD_ID_GAP)
def test_st06_06_clean_hybrid_writer_call_is_not_refused(
    drv: Drv, hybrid: Hybrid, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST06-06 (T13) without personal data in the pack the guard refuses no Writer call: no
    request is blocked and no refusal carries a PHONE hit (the run's status is not checked,
    so gap B does not mask this gap)."""
    _start(drv, hybrid, monkeypatch, PLANTED_NAME)
    sent = _writer_calls(hybrid)
    assert sent
    assert all("PHONE" not in line["scan_hits"] for line in _refusals(hybrid))
    assert [s.blocked for s in sent] == [None] * len(sent)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=COST_GAP)
def test_st06_06_allowed_off_network_writer_finishes_its_draft(
    drv: Drv, hybrid: Hybrid, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST06-06 (T13) a Writer call the guard lets out is answered by the (scripted)
    Anthropic endpoint and the off-network Writer finishes the draft: the run is done, the
    Writer task is not stopped by its budget, no fallback banner. Gap A is sidestepped on
    the test side only: this guard's re-scan does not block PHONE."""
    monkeypatch.setattr(hybrid.guard, "_blocking", hybrid.guard._blocking - {"PHONE"})
    res = _start(drv, hybrid, monkeypatch, PLANTED_NAME)
    sent = _writer_calls(hybrid)
    assert sent
    assert [s.blocked for s in sent] == [None] * len(sent)
    assert len(hybrid.wire.router.requests) == len(sent)
    (writer_task,) = select_tasks(res.run_id, roles={"writer"})
    assert (res.status, writer_task.status) == ("done", "done")
    draft = ReportDraft.model_validate_json(Path(str(res.draft_path)).read_bytes())
    assert "hybrid_fallback" not in draft.banners


def _blocked_run(drv: Drv, hy: Hybrid, monkeypatch: pytest.MonkeyPatch) -> ReportDraft:
    """The hybrid run with `fh.GUARD_ONLY_NAME` planted in every Skeptic check note: a name
    the swarm's redaction misses and only the guard's re-scan knows, so it reaches the guard."""
    draft = _hybrid_run(drv, hy, monkeypatch, fh.GUARD_ONLY_NAME)
    blocked = _writer_calls(hy)
    assert blocked
    assert {s.blocked for s in blocked} == {"pii_detected"}
    assert all(fh.GUARD_ONLY_NAME in s.text() for s in blocked)  # what the guard saw
    refusals = _refusals(hy)  # the Writer calls and their token counts
    assert len([x for x in refusals if x["path"] == "/v1/messages"]) == len(blocked)
    assert all("PERSON" in line["scan_hits"] for line in refusals)
    assert hy.wire.router.requests == []  # nothing left the process
    return draft


def test_st06_06_planted_pii_is_blocked_and_the_writer_falls_back_locally(
    drv: Drv, hybrid: Hybrid, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST06-06 (T13) personal data the swarm's own redaction misses reaches the Writer's
    evidence pack; the real guard's re-scan refuses every off-network Writer call
    (`EgressBlocked`, reason `pii_detected`, a PERSON hit) before anything is sent; spec 08
    falls back to the local Writer (`fallback` event claude-opus to local-30b, reason
    `egress_blocked`) and the run is done with a full draft."""
    draft = _blocked_run(drv, hybrid, monkeypatch)
    fallbacks = [
        (ev["from_profile"], ev["to_profile"], ev["reason"]) for ev in fl.events("fallback")
    ]
    assert ("claude-opus", "local-30b", "egress_blocked") in fallbacks
    assert draft.mode == "full"
    assert [rec.target_id for rec in draft.recommendations] == ["c1"]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=BANNER_GAP)
def test_st06_06_planted_pii_fallback_draft_has_hybrid_fallback_banner(
    drv: Drv, hybrid: Hybrid, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST06-06 (T13) after the egress refusal and local fallback the draft carries banner
    `hybrid_fallback`."""
    draft = _blocked_run(drv, hybrid, monkeypatch)
    assert "hybrid_fallback" in draft.banners


# --- ST06-09 -----------------------------------------------------------------------------------

LLM_TRACE_GAP = (
    "product gap T06-29 (gap D): herness/harness/swarm/_routing_ctx.py build_tool_context hands"
    " the loop the unbound run tracer (tracer=env.tracer, not env.tracer.bind(task_id=, role=)), so"
    " the spec 05 llm_call and tool_call events of swarm tasks carry task_id null and role null;"
    " no ops row records an analyst task's model either (task.result has none)"
)


def _full_run(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> tuple[str, list[dict[str, Any]]]:
    """A standard run in which one analyst spawns a subtask, with the real review tracer."""
    spawn_from = f6.allow_spawn(drv, monkeypatch)
    f6.use_real_agents(drv, monkeypatch, f6.TaskScripts(spawn_from=spawn_from))
    swarm = drv.swarm(tracer_factory=_run_env._review_tracer)
    res = asyncio.run(swarm.start(drv.request(depth="standard")))
    assert res.status == "done"
    return res.run_id, _trace(drv, res.run_id)


def test_st06_09_findings_verdicts_and_spawns_trace_to_task_and_model(
    drv: Drv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST06-09 a full standard run (one analyst spawns a subtask) with the real review tracer:
    every finding row names its task (an analyst task of the run) and `author_role` analyst;
    every Skeptic verdict (append-only `challenge`) names its Skeptic task, which holds the
    finding, and its model (a client of the task's model-role chain that answered this run's
    `llm_call`s); every spawned task matches an approving `spawn_decision` (parent, child) of
    its parent; every `plan_decision` is the planner task's and names a task of the run unless
    it records a drop."""
    run_id, events = _full_run(drv, monkeypatch)
    tasks = {t.task_id: t for t in select_tasks(run_id)}
    findings = query_findings(run_id, limit=500)
    assert findings
    for f in findings:
        task = tasks[f.task_id]
        assert (task.run_id, task.role, f.author_role) == (run_id, "analyst", "analyst")

    answered = {(ev["client"], ev["model"]) for ev in events if ev["type"] == "llm_call"}
    assert answered
    assert all(client and model for client, model in answered)
    verdicts = [ch for f in findings for ch in f.challenge]
    assert verdicts
    for ch in verdicts:
        skeptic = tasks[ch.skeptic_task_id]
        assert skeptic.role == "skeptic"
        assert ch.finding_id in skeptic.spec.inputs.finding_ids
        assert ch.model in drv.llms.chain_for(skeptic.spec.model_role, "standard")
        assert ch.model in {client for client, _model in answered}

    spawns = [ev for ev in events if ev["type"] == "spawn_decision"]
    children = [t for t in tasks.values() if t.parent_task_id is not None]
    assert len(children) == 1
    approved = {
        (ev["task_id"], ev["parent_task_id"], ev["child_task_id"]) for ev in spawns
        if ev["approved"]
    }  # fmt: skip
    assert approved == {(t.parent_task_id, t.parent_task_id, t.task_id) for t in children}
    assert all(tasks[t.parent_task_id].role == "analyst" for t in children if t.parent_task_id)

    (planner,) = [t for t in tasks.values() if t.role == "planner"]
    plans = [ev for ev in events if ev["type"] == "plan_decision"]
    assert plans
    assert {(ev["task_id"], ev["role"]) for ev in plans} == {(planner.task_id, "planner")}
    keys = {t.spec.dedup_key for t in tasks.values()}
    assert {ev["dedup_key"] for ev in plans if ev["action"] != "dropped"} <= keys


@pytest.mark.xfail(strict=True, raises=(AssertionError, KeyError), reason=LLM_TRACE_GAP)
def test_st06_09_finding_model_traceable_through_llm_call_traces(
    drv: Drv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST06-09 the model behind every finding and every spawn is traceable: the finding's task
    and the spawning parent task have `llm_call` trace events bound to their task ids, each
    naming the client and model."""
    run_id, events = _full_run(drv, monkeypatch)
    models: dict[str, set[tuple[str, str]]] = {}
    for ev in events:
        if ev["type"] == "llm_call":
            models.setdefault(ev["task_id"], set()).add((ev["client"], ev["model"]))
    findings = query_findings(run_id, limit=500)
    assert findings
    for f in findings:
        assert models[f.task_id]
        assert all(client and model for client, model in models[f.task_id])
    parents = {ev["parent_task_id"] for ev in events if ev["type"] == "spawn_decision"}
    assert parents
    for parent in parents:
        assert models[parent]
        assert all(client and model for client, model in models[parent])
