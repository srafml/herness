"""`pressure()` benchmark over 100 messages (BT07-06).

Spec target (impl 07 §10, §11): "BT07-06 `pressure()` cached counts | 100 messages | < 5 ms".
Run: pytest -m "integration and slow" tests/bench -k memory.

Two readings of "cached counts" are measured, both with the fixture history of 100 messages
of `ContextCompactor` tests (a task message plus tool groups with 2-row result tables):
`ContextCompactor.pressure` on the `estimate` tokenizer (the loop's last usage plus the
estimate of newer messages, no model call), and `TokenCounter.count_messages` on the
`vllm_endpoint` tokenizer with every per-message exact count already in the cache (the
counter's backend is a stand-in that must not be called again). Method: 20 warm-up calls,
then 200 calls measured `REPEATS` times; the gate is the median of the per-repeat p95s.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable

import pytest
from tests.support.bench_stats import REPEATS, report
from tests.unit.harness.memory import _compactor_support as cs

from herness.core.types import LoopState, Message
from herness.harness.memory.tokens import TokenCounter

pytestmark = [pytest.mark.integration, pytest.mark.slow]

MESSAGES = 100
CALLS = 200
WARM_UP = 20
P95_BUDGET_MS = 5.0


def _history() -> list[Message]:
    """Exactly 100 messages: the task message and 33 groups of three."""
    messages = cs.history(MESSAGES // 3 + 1)
    assert len(messages) >= MESSAGES
    return messages[:MESSAGES]


def _p95_of_repeats(call: Callable[[], object]) -> list[float]:
    for _ in range(WARM_UP):
        call()
    p95s: list[float] = []
    for _ in range(REPEATS):
        samples: list[float] = []
        for _ in range(CALLS):
            start = time.perf_counter()
            call()
            samples.append((time.perf_counter() - start) * 1000)
        p95s.append(statistics.quantiles(samples, n=100)[94])
    return p95s


def test_bt07_06_pressure_estimate_100_messages_under_5ms() -> None:
    """BT07-06 `ContextCompactor.pressure()` over 100 messages (estimate tokenizer): < 5 ms."""
    state: LoopState = cs.state_of(_history())
    compactor = cs.compactor()
    assert len(state.messages) == MESSAGES
    stats = compactor.pressure(state)
    assert stats.tokens > 0
    p95s = _p95_of_repeats(lambda: compactor.pressure(state))
    p95 = report("BT07-06", "p95_pressure", p95s, "ms", f"< {P95_BUDGET_MS:g} ms")
    assert p95 < P95_BUDGET_MS, f"pressure p95 {p95:.3f} ms"


def test_bt07_06_cached_message_counts_100_messages_under_5ms() -> None:
    """BT07-06 `TokenCounter.count_messages` with every per-message count cached: < 5 ms."""
    asked: list[int] = []

    def backend(_cfg: object, messages: list[Message], _t: object, _s: object) -> tuple[int, bool]:
        asked.append(len(messages))
        return 40, True

    prof = cs.profile().model_copy(update={"tokenizer": "vllm_endpoint"})
    counter = TokenCounter(prof, exact=backend)  # type: ignore[arg-type]
    messages = _history()
    total, exact = counter.count_messages(messages, 100_000)  # fills the cache
    assert (total, exact) == (40 * MESSAGES, True)
    filled = len(asked)
    p95s = _p95_of_repeats(lambda: counter.count_messages(messages, 100_000))
    assert len(asked) == filled, "a cached count must not ask the backend again"
    p95 = report("BT07-06", "p95_cached_counts", p95s, "ms", f"< {P95_BUDGET_MS:g} ms")
    assert p95 < P95_BUDGET_MS, f"cached counts p95 {p95:.3f} ms"
