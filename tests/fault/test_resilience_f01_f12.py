"""Impl 08 fault suite F1-F12 (FT08-01 ... FT08-12; T08-24).

Every test injects its fault through a JSON plan loaded by `fault_point` under
`HERNESS_ENV=test` (`fault_env`, R-40); no production shim is added. Retry and probe waits
run on no-op sleeps (`reset_process_state`) or a fake clock; time bounds are asserted on the
fake clock or on lease arithmetic, never by sleeping.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.integration.harness._swarm_run_env import Drv, drv
from tests.support import _f08_llm as fl
from tests.support import _f08_sources as fs
from tests.support import loop_standin as ls
from tests.support.fake_clock import FakeClock
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_llm import FakeLLMClient, FakeLLMServer
from tests.support.fault_env import FaultEnv
from tests.support.harness_fakes import RecordingTracer
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness._blackboard_env import test_redactor

from herness.core import config as c
from herness.core.errors import SourceUnavailable
from herness.core.resilience import ModelChain, ProcessState, breaker
from herness.store.ops import get_watermark, select_tasks

pytestmark = pytest.mark.fault

__all__ = ["drv", "test_redactor"]  # fixtures imported for this module


# --- FT08-01 (F1) -------------------------------------------------------------------------


def test_ft08_01_llm_dies_mid_review_breaker_opens_fallback_run_done(
    drv: Drv, fault_env: FaultEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT08-01 F1: the reasoning server (`FakeLLMServer`, real `OpenAICompatClient`) dies
    mid-review (`llm.call kill_service:vllm-reasoning model=local-30b nth=2`). The calls are
    retried, the `model:local-30b` breaker opens, the remaining analyst calls fall back to
    `local-small-cpu`, the run ends `done` and no `done` task has `attempts > 1`.

    Driver level with the T06-22 stub pipeline (IT06-01/02 shape): a real review cannot pass
    the record step until T06-22b (carry-over T06-26)."""
    server = FakeLLMServer(fl.book(fl.text_turns(20)), service_name=None)
    server.start()
    monkeypatch.setenv("HERNESS_STUB_SERVICES", json.dumps({"vllm-reasoning": server.base_url}))
    rule = {"point": "llm.call", "action": "kill_service:vllm-reasoning", "model": fl.PRIMARY}
    fault_env([rule | {"nth": 2}])
    primary = fl.Recording(fl.openai_client(server.base_url), fl.PRIMARY)
    fallback = fl.Recording(FakeLLMClient(fl.book(fl.text_turns(20))), fl.FALLBACK)
    clients = {fl.PRIMARY: primary, fl.FALLBACK: fallback}
    chain = ModelChain("analyst", registry=fl.Registry(), depth="fast", gpu=fl.Gpu())
    answered: list[str] = []
    analyst, gate = drv.agents._analyst, asyncio.Lock()  # one model call at a time: "mid-review"

    async def analyst_with_model(ctx: Any, hooks: Any) -> Any:
        async with gate:
            req = fl.request(ctx.run_id, ctx.task_id)
            resp, _ = await chain.acomplete(req, client_for=clients.get)
        answered.append(resp.client)
        return await analyst(ctx, hooks)

    monkeypatch.setattr(drv.agents, "_analyst", analyst_with_model)
    drv.set_pipeline(t1=2, t2=3)
    try:
        res = asyncio.run(drv.swarm().start(drv.request(depth="fast")))
    finally:
        server.stop()

    assert res.status == "done"
    assert server.completed == 1  # one answer, then the kill: every later connect is refused
    assert answered == [fl.PRIMARY, *["fake"] * 4]  # the fallback client answers as `fake`
    # 1 answered + 4 attempts (llm_local) + 1 more failure reaching the threshold of 5; then
    # the open breaker keeps the dead endpoint out of the candidates
    assert len(primary.requests) == 6
    assert len(fallback.requests) == 4
    retries = [e for e in fl.events("retry") if e["breaker_key"] == "model:" + fl.PRIMARY]
    assert [e["attempt"] for e in retries] == [1, 2, 3, 1]  # task 2: 3 waits; task 3: 1
    assert {e["error_type"] for e in retries} == {"ModelUnavailable"}
    opened = [e for e in fl.events("breaker_open") if e["target"] == "model:" + fl.PRIMARY]
    assert [(e["failures"], e["trips"]) for e in opened] == [(5, 1)]
    assert breaker("model:" + fl.PRIMARY).state() == "open"
    fallbacks = fl.events("fallback")
    assert [(e["from_profile"], e["to_profile"]) for e in fallbacks] == [
        (fl.PRIMARY, fl.FALLBACK)
    ] * 2
    assert [e["reason"] for e in fallbacks] == ["unavailable", "circuit_open"]
    tasks = select_tasks(res.run_id)
    assert tasks
    assert all(t.status == "done" for t in tasks)
    assert [t.task_id for t in tasks if t.status == "done" and t.attempts > 1] == []


# --- FT08-02 (F2) -------------------------------------------------------------------------


@pytest.fixture
def chain_env(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Full test config, migrated ops store bound as the resilience backend, test redactor."""
    del ops_store, reset_process_state
    fl.configure(tmp_path, monkeypatch)
    yield
    c.reset_config()


@pytest.mark.parametrize(("count", "fallbacks"), [(2, 0), (3, 1)])
def test_ft08_02_malformed_json_repaired_twice_then_fallback(
    chain_env: None, fault_env: FaultEnv, count: int, fallbacks: int
) -> None:
    """FT08-02 F2: `llm.output malformed_json count=2` gives 2 repairs and success on the
    first candidate; `count=3` exhausts the 2 repairs (`max_repairs`) and gives 1 fallback,
    whose valid reply is returned. At most 3 model calls per candidate (TH08-14)."""
    del chain_env
    fault_env([{"point": "llm.output", "action": "malformed_json", "count": count}])
    answer = {"turns": [{"final": {"output": {"n": 7}}}] * 4}
    primary = fl.Recording(FakeLLMClient(fl.book(answer)), fl.PRIMARY)
    fallback = fl.Recording(FakeLLMClient(fl.book(answer)), fl.FALLBACK)
    clients = {fl.PRIMARY: primary, fl.FALLBACK: fallback}
    chain = ModelChain("analyst", registry=fl.Registry(), depth="standard", gpu=fl.Gpu())
    req = fl.request("run_F2", "task_F2", schema=fl.ANSWER_SCHEMA)

    resp, parsed = asyncio.run(chain.acomplete(req, client_for=clients.get))

    assert parsed is None
    assert json.loads(resp.text) == {"n": 7}
    assert len(primary.requests) == 3  # the call and its two repairs, never more
    assert len(fallback.requests) == fallbacks
    repairs = fl.events("repair")
    assert [e["repair_no"] for e in repairs] == [1, 2]
    assert {e["model_profile"] for e in repairs} == {fl.PRIMARY}
    assert all(e["error_paths"] == ["/"] for e in repairs)
    assert {(e["run_id"], e["task_id"]) for e in repairs} == {("run_F2", "task_F2")}
    for repair in primary.requests[1:]:  # repair requests: temperature 0 and the error line
        assert repair.temperature == 0.0
        assert "/: fault: malformed_json" in fl.last_text(repair)
    rows = fl.events("fallback")
    assert [(e["from_profile"], e["to_profile"], e["reason"]) for e in rows] == [
        (fl.PRIMARY, fl.FALLBACK, "validation")
    ] * fallbacks


# --- FT08-03 (F3) -------------------------------------------------------------------------

F3_SEED = 7  # random.Random(7): of six draws, the 2nd and the 4th are below 0.2


def test_ft08_03_sql_timeouts_become_query_error_results_with_hints(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fault_env: FaultEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FT08-03 F3: `sql.query timeout p=0.2` (seeded) during a real loop of six `run_sql`
    calls: the two timed-out calls come back to the model as `QueryError` tool results with
    the timeout hint, the other four succeed, and the run completes."""
    del ops_store, reset_process_state
    fault_env([{"point": "sql.query", "action": "timeout", "p": 0.2, "seed": F3_SEED}])
    sqls = [f"SELECT {n} AS n" for n in range(1, 7)]
    turns: list[dict[str, Any]] = [
        {"tool_calls": [{"name": "run_sql", "arguments": {"sql": q, "purpose": "p"}}]} for q in sqls
    ]
    turns += [{"final": {"text": "done"}}, {"final": {"output": fl.ANALYST_OUTPUT}}]
    client = fl.Recording(FakeLLMClient(fl.book({"turns": turns})), "fake")
    tracer = RecordingTracer("run_1", "task_1")
    with fl.loop_env(tmp_path, monkeypatch) as wh:
        ctx = ls.loop_ctx(tool_names=("run_sql",), warehouse=wh, tracer=tracer)
        result = fl.run_loop(client, ctx, fl.hooks(tracer))

    assert result.status == "completed"
    calls = ls.events(tracer, "tool_call")
    assert [e["ok"] for e in calls] == [True, False, True, False, True, True]
    assert [e["error_type"] for e in calls if not e["ok"]] == ["QueryError", "QueryError"]
    shown = fl.tool_texts(client.requests[-1])  # what the model saw, in call order
    assert len(shown) == 6
    for i in (1, 3):
        assert shown[i].startswith("ERROR QueryError: timeout after 30.0s\nHINT: ")
    assert not any(shown[i].startswith("ERROR") for i in (0, 2, 4, 5))
    assert len(result.query_ids) == 4  # only the successful queries became evidence


# --- FT08-04 (F4) -------------------------------------------------------------------------


def test_ft08_04_http_429_retry_after_three_retries_watermark_once(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fault_env: FaultEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FT08-04 F4: `http.page http_429 retry_after=7 count=3` on a Jira incremental sync:
    3 `retry` events with `retry_after_s = 7` (each wait at least 7 s), the refused page is
    the only one fetched again, and the watermark is advanced exactly once."""
    del reset_process_state
    fs.bind_resilience()
    fault_env([{"point": "http.page", "action": "http_429", "retry_after": 7, "count": 3}])
    sets = fs.count_watermark_writes(monkeypatch)
    runner, replay = fs.jira_runner(ops_store.data_root)

    result = runner.run_incremental("issue")

    assert (result.mode, result.rows) == ("incremental", 4)
    retries = fs.events("retry")
    assert [e["attempt"] for e in retries] == [1, 2, 3]
    assert [e["retry_after_s"] for e in retries] == [7, 7, 7]
    assert all(e["wait_s"] >= 7 for e in retries)
    assert {e["target"] for e in retries} == {"jira"}
    assert fs.rule_calls() == 3 + 9  # three refused attempts, then the 9 recorded requests
    assert not replay.interactions  # every recorded request was served exactly once
    assert len(sets) == 1
    wm = get_watermark("jira", "issue")
    assert wm is not None
    assert wm.value == sets[0] > fs.JIRA_WM


# --- FT08-05 (F5) -------------------------------------------------------------------------


F5_RULE = {"point": "http.page", "action": "http_503", "count": 10, "source": "servicenow"}
F5_GAP = (
    "the runner's stream pre-check guard(key) (U01-39 step 3) claims the half-open probe, so "
    "the page fetch's own guard (U08-28) refuses the claimed probe and no probe request is "
    "ever sent: a source breaker never closes through sync - carry-over: new fix card "
    "(controller to assign; owners impl 01 U01-39 runner, impl 08 U08-24 breaker)"
)


@pytest.fixture
def store_env(
    ops_store: OpsStoreHandle, reset_process_state: ProcessState, fake_keyring: MemoryKeyring
) -> OpsStoreHandle:
    """A migrated ops store, a fresh process state (no-op sleeps) and an in-memory keyring."""
    del reset_process_state, fake_keyring
    return ops_store


@pytest.fixture
def f5(
    store_env: OpsStoreHandle,
    fake_clock: FakeClock,
    fault_env: FaultEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Any, FakeClock]:
    """The F5 plan and the `servicenow` sync job; runs 1 and 2 have opened the breaker."""
    del store_env
    fault_env([F5_RULE])
    run_job, host = fs.servicenow_job(tmp_path, monkeypatch, fake_clock.now())
    b = breaker("servicenow")
    with pytest.raises(SourceUnavailable):
        run_job()  # source_http_page: 6 attempts, all 503
    assert (fs.rule_calls(), b.state()) == (6, "closed")
    second = run_job()  # attempts 7 and 8: the threshold; the 3rd attempt is refused
    assert (second.status, second.result["skipped_open_circuit"]) == ("done", ["servicenow"])
    assert second.result["partial"] is True
    assert (fs.rule_calls(), b.state()) == (8, "open")
    return run_job, host, fake_clock


def test_ft08_05_503s_open_the_breaker_and_no_call_while_open(
    f5: tuple[Any, Any, FakeClock],
) -> None:
    """FT08-05 F5 (open part): `http.page http_503 count=10 source=servicenow`: 8
    consecutive 503s open the `servicenow` breaker (one `breaker_open`, trips 1) and the sync
    job ends `done` with `skipped_open_circuit`; until the 300 s cooldown is over every run
    is skipped without reaching the fault point or the source."""
    run_job, host, clock = f5
    for wait_s in (0, 150, 149):  # up to 299 s after the opening
        clock.advance(wait_s)
        outcome = run_job()
        assert (outcome.status, outcome.result["skipped_open_circuit"]) == ("done", ["servicenow"])
        assert (fs.rule_calls(), breaker("servicenow").state()) == (8, "open")
    assert host.requests == []
    assert [(e["target"], e["trips"]) for e in fs.events("breaker_open")] == [("servicenow", 1)]
    assert fs.events("breaker_half_open") == []


@pytest.mark.xfail(strict=True, reason=F5_GAP)
def test_ft08_05_half_open_after_cooldown_and_closed_after_a_good_probe(
    f5: tuple[Any, Any, FakeClock],
) -> None:
    """FT08-05 F5 (probe part): after each cooldown (300 s, then 600 s: exponential) one
    probe request goes out (`half_open`); the 9th and 10th 503s re-open the breaker; the
    probe after 1 200 s succeeds, the breaker closes and the sync is no longer skipped."""
    run_job, host, clock = f5
    b = breaker("servicenow")
    for wait_s, calls_after in [(300, 9), (599, 9), (1, 10)]:
        clock.advance(wait_s)
        outcome = run_job()
        assert outcome.result["skipped_open_circuit"] == ["servicenow"]
        assert (fs.rule_calls(), b.state()) == (calls_after, "open")  # one probe per cooldown
    assert host.requests == []  # the 503s were injected before any request left
    clock.advance(1200)  # trips 3: cooldown 300 * 2**2
    healthy = run_job()

    assert (healthy.status, healthy.result["skipped_open_circuit"]) == ("done", [])
    assert healthy.result["partial"] is False
    assert host.requests  # the good probe and the rest of the sync reached the source
    assert b.state() == "closed"
    assert [e["trips"] for e in fs.events("breaker_open")] == [1, 2, 3]
    assert len(fs.events("breaker_half_open")) == 3
    assert [e["target"] for e in fs.events("breaker_close")] == ["servicenow"]
