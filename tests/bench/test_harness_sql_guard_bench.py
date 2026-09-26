"""SQL guard benchmark (BT05-03). Run: pytest -m "integration and slow" tests/bench.

Stand-in for the 200 queries of `tests/fixtures/sql_ok/` on the `full` build schema (impl 11,
not built yet): the test-local accepted corpus on the stand-in schema.
"""

from __future__ import annotations

import statistics
import time

import pytest
from tests.unit.harness._sql_guard_standin import BLOCKED_COLUMNS, SCHEMA, accepted_corpus

from herness.harness.sql_guard import SqlGuard

pytestmark = [pytest.mark.integration, pytest.mark.slow]

P95_BUDGET_MS = 25.0


def test_bt05_03_sql_guard_p95_under_25ms() -> None:
    """BT05-03 p95 of SqlGuard.check over the accepted corpus is below 25 ms."""
    guard = SqlGuard(SCHEMA, BLOCKED_COLUMNS)
    corpus = accepted_corpus()
    assert len(corpus) >= 200
    guard.check(corpus[0])  # warm the per-thread parser connection
    samples: list[float] = []
    for _ in range(3):
        for sql in corpus:
            t0 = time.perf_counter()
            guard.check(sql)
            samples.append((time.perf_counter() - t0) * 1000)
    p95 = statistics.quantiles(samples, n=100)[94]
    assert p95 < P95_BUDGET_MS, f"p95 {p95:.2f} ms"
