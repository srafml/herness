"""Foundation benchmarks (impl 00 §10, §11.6). Run: pytest -m "integration and slow" tests/bench."""

import time
from typing import Any

import pytest

from herness.core import ids

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _p95(benchmark: Any) -> float:
    data = sorted(benchmark.stats.stats.data)
    return float(data[int(0.95 * (len(data) - 1))])


def test_bt00_01_ulid_throughput() -> None:
    """BT00-01 at least 200,000 new_ulid calls per second."""
    start = time.perf_counter()
    for _ in range(1_000_000):
        ids.new_ulid()
    rate = 1_000_000 / (time.perf_counter() - start)
    assert rate >= 200_000


def test_bt00_02_query_id_p95(benchmark: Any) -> None:
    """BT00-02 query_id on 2 KB SQL with 10 params: p95 under 50 microseconds."""
    sql = "SELECT " + ", ".join(f"col_{i}" for i in range(250)) + " FROM t"  # noqa: S608 - benchmark text, never executed
    params = {f"p{i}": i for i in range(10)}
    benchmark.pedantic(
        ids.query_id, args=(sql, params, "20260924-211403-ABCDEF"), rounds=10_000, iterations=1
    )
    assert _p95(benchmark) < 50e-6
