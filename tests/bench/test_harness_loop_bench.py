"""Agent loop overhead benchmark (BT05-01).

Spec target (impl 05 §10.1): "BT05-01 | Loop overhead per step | `FakeLLMClient` answering
instantly, fake tools returning instantly, 30-step script, 200 runs | mean per-step overhead
< 20 ms". Method: 5 warm-up runs, then the 200 runs measured `REPEATS` times; the gate is the
median of the per-repeat means. Every run stops at the scripted step count (TH05-08).
Run: pytest -m "integration and slow" tests/bench.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue
from tests.support import loop_standin as ls
from tests.support.bench_stats import REPEATS, report
from tests.support.dispatch_standin import SyncTool, strict_schema, use_test_config
from tests.support.fake_llm import FakeLLMClient
from tests.support.harness_fakes import RecordingTracer

from herness.core import config as c
from herness.core.resilience import ProcessState
from herness.core.types import LLMRequest, LLMResponse, ToolContext, ToolResult
from herness.eval.scripted import LLMScript, ScriptBook
from herness.harness import hooks as h
from herness.harness import loop
from herness.harness.tools import tool_registry
from herness.harness.tracing import Tracer

pytestmark = [pytest.mark.integration, pytest.mark.slow]

STEPS = 30
RUNS = 200
WARM_UP = 5


@pytest.fixture
def cfg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState
) -> Iterator[None]:
    del reset_process_state
    use_test_config(tmp_path / "cfg")
    ls.write_prompts(tmp_path / "prompts", monkeypatch)
    yield
    c.reset_config()


class _TimedClient(FakeLLMClient):
    """`FakeLLMClient` that accumulates the time spent answering (excluded from overhead)."""

    spent = 0.0

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        start = time.perf_counter()
        try:
            return await super().acomplete(req)
        finally:
            _TimedClient.spent += time.perf_counter() - start


def _script() -> list[LLMScript]:
    turns: list[dict[str, object]] = [
        {"tool_calls": [{"name": "run_sql", "arguments": {"sql": f"select {i}"}}]}
        for i in range(STEPS)
    ]
    turns += [{"final": {"text": "done"}}, {"final": {"output": {"answer": "ok"}}}]
    return [LLMScript.model_validate({"turns": turns, "source": "bt05_01#0"})]


def test_bt05_01_loop_overhead_per_step(cfg: None) -> None:
    """BT05-01 `FakeLLMClient` answering instantly, instant tools, 30-step script, 200 runs:
    mean per-step loop overhead (model and tool time excluded) < 20 ms."""
    del cfg
    tool_time = [0.0]
    counter = iter(range(1, 10**9))

    def run_sql(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        start = time.perf_counter()
        qid = f"q_{next(counter):016x}"
        result = ToolResult(ok=True, content=f"query_id={qid} rows=1", query_ids=[qid])
        tool_time[0] += time.perf_counter() - start
        return result

    tool = SyncTool("run_sql", run_sql, schema=strict_schema({"sql": {"type": "string"}}))
    tool_registry().register(tool, owner="05")
    scripts = _script()
    role = ls.demo_role(tools=("run_sql",))
    profile = ls.profile()

    async def one() -> int:
        client = _TimedClient(ScriptBook(scripts))
        tracer = RecordingTracer("run_1", "task_1")
        ctx = ls.loop_ctx(budgets=ls.budgets(max_steps=STEPS + 5), tracer=tracer)
        hooks = h.HarnessHooks(
            registry=None, gates={}, chain=None, compactor=None, task_id=None, phase=None,
            stop=None, on_text_delta=None, tracer=Tracer.null(),
        )  # fmt: skip
        result = await loop.run_agent(role, {"q": "x"}, ctx, client, profile, hooks)
        assert result.status == "completed"
        assert result.stop_reason == "final"
        assert result.steps == STEPS  # bounded: stops at the scripted step count (TH05-08)
        return result.steps

    async def rounds() -> tuple[float, int]:
        _TimedClient.spent, tool_time[0] = 0.0, 0.0
        steps = 0
        start = time.perf_counter()
        for _ in range(RUNS):
            steps += await one()
        total = time.perf_counter() - start
        return total - _TimedClient.spent - tool_time[0], steps

    async def repeats() -> list[float]:
        for _ in range(WARM_UP):
            await one()
        means: list[float] = []
        for _ in range(REPEATS):
            overhead, steps = await rounds()
            assert steps == RUNS * STEPS
            means.append(overhead / steps * 1000)
        return means

    mean_ms = report("BT05-01", "mean_step_overhead", asyncio.run(repeats()), "ms", "< 20 ms")
    assert mean_ms < 20, f"loop overhead mean {mean_ms:.3f} ms per step"
