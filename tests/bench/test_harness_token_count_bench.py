"""Token count benchmark against a real vLLM server (BT05-10).

Spec target (impl 05 §10.1): "BT05-10 | Token count (`vllm_endpoint`) | 200 calls against the
real vLLM (marker `gpu`) | p95 < 30 ms". Method: 5 warm-up calls, then the 200 calls measured
`REPEATS` times; the gate is the median of the per-repeat p95s. Every call must be answered by
the server's `/tokenize` (`exact=True`), never by the estimate fallback.

Skipped unless ``HERNESS_LLM_URL`` names the real server (a loopback ``.../v1`` URL, the IT05-08
convention); ``HERNESS_LLM_MODEL`` names the served model (default ``qwen3-30b``). Default runs
make no network call. Run: pytest -m "integration and slow and gpu" tests/bench.
"""

from __future__ import annotations

import os
import statistics
import time
from decimal import Decimal

import pytest
from tests.support.bench_stats import REPEATS, report

from herness.core.types import (
    Message,
    SystemBlock,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
)
from herness.harness.llm.settings import ClientConfig, PricePerMTok
from herness.harness.llm.tokens import count_tokens

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.gpu]

URL_ENV = "HERNESS_LLM_URL"
CALLS = 200
WARM_UP = 5
P95_BUDGET_MS = 30.0
_ZERO = Decimal("0")


def _cfg(base_url: str) -> ClientConfig:
    return ClientConfig(
        name="bench-vllm",
        kind="openai_compat",
        base_url=base_url,
        model=os.environ.get("HERNESS_LLM_MODEL", "qwen3-30b"),
        context_window=32768,
        max_output_tokens=1024,
        tokenizer="vllm_endpoint",
        max_concurrency=1,
        server="vllm",
        price_per_mtok=PricePerMTok(input=_ZERO, output=_ZERO, cache_read=_ZERO, cache_write=_ZERO),
    )


def _conversation(i: int) -> list[Message]:
    """A typical analyst exchange; `i` varies the text so no server-side cache answers."""
    call = ToolCall(
        id=f"call_{i}",
        name="run_sql",
        arguments={"sql": f"SELECT count(*) AS n FROM core.incident LIMIT {i + 1}"},  # noqa: S608 - integer loop index only
    )
    return [
        Message(role="user", parts=[TextPart(text=f"Question {i}: which services degrade?")]),
        Message(role="assistant", parts=[TextPart(text="Checking."), ToolCallPart(call=call)]),
        Message(
            role="tool",
            parts=[ToolResultPart(tool_call_id=f"call_{i}", content="service_id | n\nsvc_a | 7")],
        ),
    ]


_TOOLS = [ToolSpec(name="run_sql", description="Run SQL.", input_schema={"type": "object"})]
_SYSTEM = [SystemBlock(text="You are the analyst. Cite every number.")]


@pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} not set (real vLLM only)")
def test_bt05_10_token_count_vllm_endpoint_p95() -> None:
    """BT05-10 200 `count_tokens` calls against the real vLLM `/tokenize`: p95 < 30 ms."""
    cfg = _cfg(os.environ[URL_ENV])
    for i in range(WARM_UP):
        _, exact = count_tokens(cfg, _conversation(-1 - i), _TOOLS, _SYSTEM)
        assert exact, "the server did not answer /tokenize (estimate fallback)"
    p95s: list[float] = []
    for r in range(REPEATS):
        samples: list[float] = []
        for i in range(CALLS):
            messages = _conversation(r * CALLS + i)
            start = time.perf_counter()
            count, exact = count_tokens(cfg, messages, _TOOLS, _SYSTEM)
            samples.append((time.perf_counter() - start) * 1000)
            assert exact
            assert count > 0
        p95s.append(statistics.quantiles(samples, n=100)[94])
    p95 = report("BT05-10", "p95_count_tokens", p95s, "ms", f"< {P95_BUDGET_MS:g} ms")
    assert p95 < P95_BUDGET_MS, f"count_tokens p95 {p95:.2f} ms"
