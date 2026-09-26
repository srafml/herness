"""Trace flood test (ST05-19; TH05-19): a tight emit loop never blocks and stays bounded."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from herness.harness.llm.settings import TraceSettings
from herness.harness.tracing import Tracer

pytestmark = [pytest.mark.integration, pytest.mark.slow]

RUN_ID = "run_01J8ST05190000000000000000"
N_EMITS = 1_000_000
QUEUE_MAX = 10_000


def test_st05_19_million_emits_do_not_block_and_drops_are_reported(tmp_path: Path) -> None:
    """ST05-19 1,000,000 emits in a tight loop: enqueue p99 < 1 ms, queue (memory) bounded,
    drops reported in dropped_events budget lines that account for every missing event."""
    tracer = Tracer(
        RUN_ID, build_id=None, run_kind="chat", traces_dir=tmp_path, settings=TraceSettings()
    )
    queue = tracer._core.queue
    samples: list[int] = []
    max_depth = 0
    for i in range(N_EMITS):
        t0 = time.perf_counter_ns()
        tracer.emit("tool_call", step=i, payload={"args": {"i": i}})
        samples.append(time.perf_counter_ns() - t0)
        if i % 1_000 == 0:
            max_depth = max(max_depth, queue.qsize())
    tracer.close(timeout_s=120)
    samples.sort()
    p99_ms = samples[int(len(samples) * 0.99)] / 1e6
    assert p99_ms < 1.0, f"enqueue p99 {p99_ms:.3f} ms"
    assert max_depth <= QUEUE_MAX
    written = dropped = 0
    with (tmp_path / f"{RUN_ID}.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            event = json.loads(line)
            if event["type"] == "tool_call":
                written += 1
            else:
                assert event["kind"] == "dropped_events"
                dropped += int(event["message"].split()[1])
    assert dropped > 0, "the flood should overflow the 10,000-event queue"
    assert written + dropped == N_EMITS
