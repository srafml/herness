"""Job queue benchmarks (impl 08 BT08-02 enqueue, BT08-03 claim; T08-12).

Run: pytest -m "integration and slow" tests/bench/test_jobs_queue_bench.py.
"""

from __future__ import annotations

import random
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from tests.unit.core.jobs._queue_env import jobs_db  # noqa: F401 - fixture

from herness.core import time as clock
from herness.core.jobs import queue
from herness.core.types import GpuClass, JobKind
from herness.store.ops.core import run_write

pytestmark = [pytest.mark.integration, pytest.mark.slow]

EXISTING = 10_000
KINDS: tuple[JobKind, ...] = get_args(JobKind.__value__)
CLASSES: tuple[GpuClass, ...] = get_args(GpuClass.__value__)


def _seed(count: int) -> None:
    """``count`` queued jobs over every class and kind, due one minute ago."""
    rng = random.Random(0)
    stamp = clock.format_utc(datetime.now(UTC) - timedelta(minutes=1))
    rows = [
        (
            f"job_seed{i:05d}",
            KINDS[i % len(KINDS)],
            CLASSES[i % len(CLASSES)],
            rng.randint(0, 100),
            "{}",
            f"seed:{i}",
            3,
            stamp,
            stamp,
        )
        for i in range(count)
    ]

    def insert(conn: sqlite3.Connection) -> None:
        conn.executemany(
            "INSERT INTO job (job_id, kind, gpu_class, priority, payload, idem_key, max_attempts,"
            " scheduled_for, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    run_write(insert, op="bench_seed")


def _p95(durations: list[float]) -> float:
    durations.sort()
    return durations[int(0.95 * (len(durations) - 1))]


@pytest.mark.usefixtures("jobs_db")
def test_bt08_02_enqueue_p95_under_10_ms() -> None:
    """BT08-02 2 000 `enqueue` calls on a store with 10 000 jobs: p95 < 10 ms."""
    _seed(EXISTING)
    durations: list[float] = []
    for i in range(2_000):
        start = time.perf_counter()
        queue.enqueue(KINDS[i % len(KINDS)], {"source": "bench", "n": i}, "none")
        durations.append(time.perf_counter() - start)
    p95 = _p95(durations)
    sys.stderr.write(f"BT08-02 enqueue p95={p95 * 1000:.3f} ms\n")
    assert p95 < 0.010


@pytest.mark.usefixtures("jobs_db")
def test_bt08_03_claim_p95_under_20_ms() -> None:
    """BT08-03 1 000 claims with 10 000 queued jobs over 4 classes and 10 kinds: p95 < 20 ms."""
    _seed(EXISTING)
    durations: list[float] = []
    claimed = 0
    for _ in range(1_000):
        start = time.perf_counter()
        row = queue.claim(owner="bench:1:cli", allowed_classes=CLASSES)
        durations.append(time.perf_counter() - start)
        claimed += row is not None
    p95 = _p95(durations)
    sys.stderr.write(f"BT08-03 claim p95={p95 * 1000:.3f} ms ({claimed} claimed)\n")
    assert claimed == 1_000
    assert p95 < 0.020
