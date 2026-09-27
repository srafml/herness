"""Worker supervisor idle CPU (impl 08 BT08-11, design 08 §8; T08-21).

Run: pytest -m "integration and slow" tests/bench/test_jobs_worker_bench.py -s
The worker runs as a subprocess with the shipped job timings (`tick_s` 2, `heartbeat_s` 30,
`reaper_interval_s` 30), two CPU slots and no GPU slot, on an empty queue for 10 minutes; its
CPU use is `psutil.Process.cpu_percent` over the whole interval (percent of one core).
"""

from __future__ import annotations

import os
import sys
import time

import psutil
import pytest
from tests.support import worker_env as _worker_env
from tests.support.worker_env import WAIT_S, WorkerEnv, interrupt, wait_for

from herness.core.jobs.ports import require_jobs_backend

pytestmark = [pytest.mark.integration, pytest.mark.slow]
worker_env = _worker_env.worker_env  # fixture

IDLE_S = float(os.environ.get("HERNESS_BT08_11_IDLE_S", "600"))  # shorter only for a dry run
LIMIT_PERCENT = 1.0
SHIPPED_JOBS = {
    "lease_s": 300,
    "heartbeat_s": 30,
    "reaper_interval_s": 30,
    "tick_s": 2,
    "cancel_grace_s": 60,
    "shutdown_grace_s": 120,
    "stall_timeout_s": 1800,
    "stall_timeout_large_s": 7200,
}


@pytest.mark.timeout(int(IDLE_S) + 600)
def test_bt08_11_idle_worker_under_1_percent_of_a_core(worker_env: WorkerEnv) -> None:
    """BT08-11 a worker with no jobs for 10 min uses < 1 % of one core
    (`psutil.Process.cpu_percent` of the worker process over the interval)."""
    worker_env.configure(**SHIPPED_JOBS)
    proc = worker_env.spawn_worker("--gpu-classes", "none", "--concurrency", "2")
    rows: list[int] = []

    def running() -> bool:
        rows[:] = [w.pid for w in require_jobs_backend().list_workers() if w.status == "running"]
        return len(rows) == 1

    wait_for(running, what="running worker")
    worker = psutil.Process(rows[0])
    worker.cpu_percent(interval=None)  # primes the counter; the next call covers the interval
    before, started = worker.cpu_times(), time.monotonic()
    time.sleep(IDLE_S)
    percent = worker.cpu_percent(interval=None)
    after, elapsed = worker.cpu_times(), time.monotonic() - started
    busy_s = (after.user - before.user) + (after.system - before.system)
    sys.stdout.write(
        f"BT08-11 idle worker pid {worker.pid}: cpu_percent {percent:.3f} % of one core,"
        f" cpu time {busy_s:.3f} s over {elapsed:.0f} s ({worker.name()},"
        f" {after.user + after.system:.3f} s since start)\n"
    )
    interrupt(proc)
    assert proc.wait(timeout=WAIT_S) == 0
    assert percent < LIMIT_PERCENT
