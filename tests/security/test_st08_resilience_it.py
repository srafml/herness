"""Integration-level security tests of impl 08 resilience (T08-24: ST08-05, ST08-14).

ST08-05 (TH08-05) is the model-endpoint counterpart of the source retry storm of
`tests/integration/jobs/test_resilience_retry_storm.py` (T08-07): 20 concurrent callers of a
`ModelChain` whose only endpoint always answers 503, for 10 simulated minutes on the fake clock,
over the real breaker and the migrated ops store. ST08-14 (TH08-14) runs the real spec 05 loop
with `HarnessHooks` and the spec 08 `ModelChain`: a model repeating one tool call forever is
stopped on the second loop signal, and a model whose structured output never validates gets at
most 3 calls for it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from tests.support import _f08_llm as fl
from tests.support import loop_standin as ls
from tests.support.fake_clock import FakeClock
from tests.support.fake_llm import FakeLLMClient
from tests.support.harness_fakes import RecordingTracer
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core.errors import CircuitOpen, ModelUnavailable, OutputValidationError
from herness.core.resilience import ModelChain, ProcessState, breaker
from herness.core.types import LLMRequest, LLMResponse

pytestmark = pytest.mark.integration

CALLERS: Final = 20
TICK_S: Final = 5
DURATION_S: Final = 600
THRESHOLD: Final = 5  # breakers.model.failure_threshold (design 08 §7)
PROBES_AT: Final = [60, 180, 420]  # cooldown 60 s doubling (60, 120, 240); 900 s is past 10 min


@pytest.fixture
def env(
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


class Down:
    """A model endpoint that always answers 503; records the fake second of every call."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock, self.start, self.calls = clock, clock.now(), list[float]()
        self.name = fl.PRIMARY

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        del req
        self.calls.append((self.clock.now() - self.start).total_seconds())
        msg = "HTTP 503 from the model server"
        raise ModelUnavailable(msg)


def test_st08_05_model_retry_storm_is_bounded_by_the_breaker(
    env: None, fake_clock: FakeClock
) -> None:
    """ST08-05 endpoint always 503; 20 concurrent callers for 10 min (fake clock): calls stop
    after the `model:` breaker threshold, no call reaches the endpoint while it is open, and
    exactly one probe call goes out per cooldown (60 s, 120 s, 240 s)."""
    del env
    down = Down(fake_clock)
    registry = fl.Registry(chains={("analyst", "standard"): [fl.PRIMARY]})
    chain = ModelChain("analyst", registry=registry, depth="standard", gpu=fl.Gpu())

    async def caller(n: int) -> str:
        try:
            await chain.acomplete(fl.request("run_storm", f"task_{n}"), client_for=lambda _k: down)
        except CircuitOpen:
            return "open"
        except ModelUnavailable:
            return "failed"
        return "ok"  # pragma: no cover - the endpoint never answers

    async def tick() -> list[str]:
        return await asyncio.gather(*(caller(n) for n in range(CALLERS)))

    outcomes: list[str] = []
    for _ in range(DURATION_S // TICK_S):
        outcomes += asyncio.run(tick())
        fake_clock.advance(TICK_S)

    first = [t for t in down.calls if t == 0]
    later = [t for t in down.calls if t > 0]
    assert THRESHOLD <= len(first) <= THRESHOLD + CALLERS  # in-flight calls finish once
    assert later == PROBES_AT  # nothing while open; one probe per cooldown
    assert outcomes.count("ok") == 0
    assert breaker("model:" + fl.PRIMARY).state() == "open"
    assert [e["trips"] for e in fl.events("breaker_open")] == [1, 2, 3, 4]


def _sql_turn(sql: str) -> dict[str, Any]:
    return {"tool_calls": [{"name": "run_sql", "arguments": {"sql": sql, "purpose": "p"}}]}


@pytest.fixture
def loop_env(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Any]:
    """The IT05 loop set-up (stand-in build, warehouse tools, test config and redactor)."""
    del ops_store, reset_process_state
    with fl.loop_env(tmp_path, monkeypatch) as wh:
        yield wh


def _run(wh: Any, client: FakeLLMClient) -> tuple[Any, RecordingTracer]:
    """One analyst task through the loop, `HarnessHooks` with a one-candidate `ModelChain`."""
    registry = fl.LoopRegistry(client)
    chain = ModelChain("analyst", registry=registry, depth="standard", gpu=fl.Gpu())
    tracer = RecordingTracer("run_1", "task_1")
    ctx = ls.loop_ctx(tool_names=("run_sql",), warehouse=wh, tracer=tracer)
    hooks = fl.hooks(tracer, chain=chain, registry=registry)
    return fl.run_loop(client, ctx, hooks), tracer


def test_st08_14_repeated_tool_call_stops_on_second_signal(loop_env: Any) -> None:
    """ST08-14 a fake LLM repeating the same tool call forever (spec 05 loop, `HarnessHooks`
    with the spec 08 policy): the first loop signal nudges, the second stops the task
    (`partial`, `repeat_call`) with exactly one `guard_stop`; the model is called once per
    step, three times in all, and the repeats are never executed."""
    script = {"turns": [_sql_turn("SELECT 1 AS n")] * 20}
    client = FakeLLMClient(fl.book(script))
    result, tracer = _run(loop_env, client)

    assert (result.status, result.stop_reason) == ("partial", "repeat_call")
    assert ls.events(tracer, "guard_stop") == [{"cause": "repeat", "step": 3}]
    assert [row["cause"] for row in fl.events("guard_stop")] == ["repeat"]
    assert client.book.calls("analyst_general", "*") == 3
    assert [e["ok"] for e in ls.events(tracer, "tool_call")] == [True, False, False]


def test_st08_14_invalid_structured_output_gets_at_most_three_calls(loop_env: Any) -> None:
    """ST08-14 a fake LLM whose structured output never validates: the final output call is
    made once and repaired at most twice (`max_repairs` 2), so at most 3 model calls are
    spent on it, with 2 `repair` events; the chain has no other candidate, so the task ends
    with `OutputValidationError` instead of another call."""
    turns = [{"final": {"text": "done"}}] + [{"final": {"output": fl.ANALYST_OUTPUT}}] * 10
    faults = [{"at": 1, "kind": "malformed_json", "count": 20}]
    client = FakeLLMClient(fl.book({"turns": turns, "faults": faults}))
    with pytest.raises(OutputValidationError, match="after 2 repairs"):
        _run(loop_env, client)

    assert client.book.calls("analyst_general", "*") == 1 + 3  # the text turn, then 3 at most
    repairs = fl.events("repair")
    assert [e["repair_no"] for e in repairs] == [1, 2]
    assert {(e["run_id"], e["task_id"]) for e in repairs} == {("run_1", "task_1")}
