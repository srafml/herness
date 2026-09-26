"""Trace enqueue and writer benchmark (BT05-11).

Run: pytest -m "integration and slow" tests/bench.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from herness.harness.llm.settings import TraceSettings
from herness.harness.tracing import Tracer

pytestmark = [pytest.mark.integration, pytest.mark.slow]

RUN_ID = "run_01J8BT05110000000000000000"
N_EVENTS = 100_000


def test_bt05_11_enqueue_mean_and_writer_throughput(tmp_path: Path) -> None:
    """BT05-11 100,000 events without payload: enqueue mean < 0.2 ms; writer >= 1,000 events/s."""
    tracer = Tracer(
        RUN_ID,
        build_id=None,
        run_kind="chat",
        traces_dir=tmp_path,
        settings=TraceSettings(),
        queue_max=N_EVENTS,  # measure the writer, not the drop path
    )
    start = time.perf_counter()
    for i in range(N_EVENTS):
        tracer.emit("tool_call", step=i, tool="run_sql", ok=True, duration_ms=3)
    enqueued = time.perf_counter()
    tracer.close(timeout_s=120)
    written = time.perf_counter()
    enqueue_mean_ms = (enqueued - start) / N_EVENTS * 1000
    events_per_s = N_EVENTS / (written - start)
    with (tmp_path / f"{RUN_ID}.jsonl").open(encoding="utf-8") as fh:
        assert sum(1 for _ in fh) == N_EVENTS
    assert enqueue_mean_ms < 0.2, f"enqueue mean {enqueue_mean_ms:.4f} ms"
    assert events_per_s >= 1_000, f"writer {events_per_s:.0f} events/s"
