"""Compaction with LLM notes against a real local 30B model (BT07-05, marker `gpu`).

Spec target (impl 07 §10, §11): "BT07-05 compaction with LLM notes (marker `gpu`) | local 30B
| < 20 s". Method: one `ContextCompactor.on_context_pressure` over a 100-message history on a
real model, repeated `REPEATS` times; the gate is the median of the repeat times. Every run
must take the LLM notes path (`last_report.notes_source == "llm"`), never the deterministic
fallback.

Skipped unless ``HERNESS_LLM_URL`` names the real server (a loopback ``.../v1`` URL, the
BT05-10 convention); ``HERNESS_LLM_MODEL`` names the served model (default ``qwen3-30b``).
Default runs make no network call. Run: pytest -m "integration and slow and gpu" tests/bench.
"""

from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal

import pytest
from tests.support.bench_stats import REPEATS, report
from tests.unit.harness.memory import _compactor_support as cs

from herness.core.resilience import ProcessState
from herness.harness.llm.openai_compat import OpenAICompatClient
from herness.harness.llm.settings import ClientConfig, PricePerMTok
from herness.harness.memory.compactor import ContextCompactor
from herness.harness.memory.settings import CompactionConfig
from herness.harness.memory.tokens import TokenCounter

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.gpu]

URL_ENV = "HERNESS_LLM_URL"
LIMIT_S = 20.0
GROUPS = 33  # 1 task message + 99 group messages = 100
_ZERO = Decimal("0")


def _profile(base_url: str) -> ClientConfig:
    return ClientConfig(
        name="bench-vllm",
        kind="openai_compat",
        base_url=base_url,
        model=os.environ.get("HERNESS_LLM_MODEL", "qwen3-30b"),
        context_window=32768,
        max_output_tokens=1024,
        tokenizer="estimate",
        max_concurrency=1,
        server="vllm",
        timeout_s=60.0,
        price_per_mtok=PricePerMTok(input=_ZERO, output=_ZERO, cache_read=_ZERO, cache_write=_ZERO),
    )


def _compactor(profile: ClientConfig) -> ContextCompactor:
    return ContextCompactor(
        profile,
        ctx=cs.make_ctx(),
        cfg=CompactionConfig(),
        counter=TokenCounter(profile),
        client=OpenAICompatClient(profile),
        allowed=[],
        ops=cs.InMemoryOps(),
    )


@pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} not set (real local 30B only)")
def test_bt07_05_compaction_with_llm_notes_under_20s(reset_process_state: ProcessState) -> None:
    """BT07-05 one compaction with LLM notes over 100 messages on the local 30B: < 20 s."""
    del reset_process_state
    cs.bind_backend()
    profile = _profile(os.environ[URL_ENV])
    messages = cs.history(GROUPS)
    assert len(messages) == 100
    seconds: list[float] = []
    for _ in range(REPEATS):
        compactor = _compactor(profile)
        start = time.perf_counter()
        new = asyncio.run(compactor.on_context_pressure(cs.state_of(messages)))
        seconds.append(time.perf_counter() - start)
        assert len(new) < len(messages)
        assert compactor.last_report is not None
        assert compactor.last_report.notes_source == "llm", "the deterministic fallback ran"
    median = report("BT07-05", "median_compaction", seconds, "s", f"< {LIMIT_S:g} s")
    assert median < LIMIT_S, f"compaction with LLM notes {median:.1f} s"
