"""Trace enqueue and writer benchmark (BT05-11).

Spec target (impl 05 §10.1): "BT05-11 | Trace enqueue and writer | 100,000 events without
payload | enqueue mean < 0.2 ms; writer ≥ 1,000 events/s". Method: a 1,000-event warm-up
tracer, then 100,000 events on a fresh tracer `REPEATS` times; each statistic gates on the
median of its per-repeat values. Run: pytest -m "integration and slow" tests/bench.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from tests.support.bench_stats import REPEATS, report

from herness.harness.llm.settings import TraceSettings
from herness.harness.tracing import Tracer

pytestmark = [pytest.mark.integration, pytest.mark.slow]

RUN_ID = "run_01J8BT05110000000000000000"
N_EVENTS = 100_000
WARM_UP = 1_000


def _one(traces_dir: Path, n_events: int) -> tuple[float, float]:
    """`n_events` through a fresh tracer: (enqueue mean in ms, writer events/s)."""
    traces_dir.mkdir()
    tracer = Tracer(
        RUN_ID,
        build_id=None,
        run_kind="chat",
        traces_dir=traces_dir,
        settings=TraceSettings(),
        queue_max=n_events,  # measure the writer, not the drop path
    )
    start = time.perf_counter()
    for i in range(n_events):
        tracer.emit("tool_call", step=i, tool="run_sql", ok=True, duration_ms=3)
    enqueued = time.perf_counter()
    tracer.close(timeout_s=120)
    written = time.perf_counter()
    with (traces_dir / f"{RUN_ID}.jsonl").open(encoding="utf-8") as fh:
        assert sum(1 for _ in fh) == n_events
    return (enqueued - start) / n_events * 1000, n_events / (written - start)


def test_bt05_11_enqueue_mean_and_writer_throughput(tmp_path: Path) -> None:
    """BT05-11 100,000 events without payload: enqueue mean < 0.2 ms; writer >= 1,000 events/s."""
    _one(tmp_path / "warm", WARM_UP)
    runs = [_one(tmp_path / f"r{r}", N_EVENTS) for r in range(REPEATS)]
    enqueue_mean_ms = report("BT05-11", "enqueue_mean", [r[0] for r in runs], "ms", "< 0.2 ms")
    events_per_s = report(
        "BT05-11", "writer_throughput", [r[1] for r in runs], "events/s", ">= 1000 events/s"
    )
    assert enqueue_mean_ms < 0.2, f"enqueue mean {enqueue_mean_ms:.4f} ms"
    assert events_per_s >= 1_000, f"writer {events_per_s:.0f} events/s"
