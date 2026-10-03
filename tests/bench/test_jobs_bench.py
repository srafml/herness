"""Jobs benchmarks (impl 08 BT08-04 requeue after owner death, BT08-06/07/08 GPU swaps; T08-25).

BT08-04 builds on FT08-07's claim-then-die shape (no FT08-07 module exists yet): a child
process claims a job and is killed. BT08-06/07/08 drive `GpuController` against the real
containers on the target PC and skip elsewhere (set `HERNESS_GPU_BENCH=1` there).

Run: pytest -m fault tests/bench/test_jobs_bench.py; pytest -m gpu tests/bench/test_jobs_bench.py.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import psutil
import pytest
from tests.support.bench_stats import REPEATS, report
from tests.support.config_tree import REPO
from tests.support.ops_store import OpsStoreHandle
from tests.unit.core.jobs._queue_env import jobs_db  # noqa: F401 - fixture

from herness.core import config as c
from herness.core import time as clock
from herness.core.jobs import queue
from herness.core.jobs._supervisor_boot import dead_owners
from herness.core.jobs.gpu import GpuController
from herness.core.jobs.gpu_services import ComposeRunner, LoopbackHttp
from herness.store.ops import bind_core_backends
from herness.store.ops.core import run_write
from herness.store.ops.jobs import SqliteJobsBackend

pytestmark = [pytest.mark.fault, pytest.mark.slow]

GPU_ENV = "HERNESS_GPU_BENCH"
SAME_HOST_BUDGET_S = 10.0
OTHER_OWNER_MARGIN_S = 30.0
LEASE_S = 3  # the lease the other-owner case runs with; the budget is LEASE_S + 30 s

_CHILD = """
import os, sys, time
from pathlib import Path
from herness.core import config as c
from herness.core.jobs import queue
from herness.store.ops import bind_core_backends, reset_connections

reset_connections(path=Path(sys.argv[2]))
c.init_config("local", config_dir=Path(sys.argv[1]), env={})
bind_core_backends()
row = queue.claim(owner=f"{sys.argv[3]}:{os.getpid()}:cli", allowed_classes=["none"])
print("claimed", row.job_id if row else "-", flush=True)
time.sleep(600)
"""


def _claim_in_child(
    tmp_path: Path, ops_store: OpsStoreHandle, host: str
) -> tuple[subprocess.Popen[str], str]:
    """Start a child that claims one job as `<host>:<its pid>:cli`; return it and the job id."""
    argv = [sys.executable, "-c", _CHILD, str(tmp_path / "config"), str(ops_store.db_path), host]
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)  # noqa: S603
    assert child.stdout is not None
    for line in child.stdout:  # the child's stdout also carries log lines
        parts = line.split()
        if parts[:1] == ["claimed"]:
            assert parts[1] != "-", "the child claimed nothing"
            return child, parts[1]
    msg = "child exited before claiming"
    raise AssertionError(msg)


def _kill(child: subprocess.Popen[str]) -> None:
    """Kill the child and its descendants (the venv python.exe launcher spawns the real one)."""
    proc = psutil.Process(child.pid)
    family = [*proc.children(recursive=True), proc]
    for member in family:
        member.kill()
    psutil.wait_procs(family, timeout=30)
    child.wait(timeout=30)


def _same_host_repeat(tmp_path: Path, ops_store: OpsStoreHandle) -> float:
    """Kill the claim's owner, then run the worker restart recovery (boot step 4)."""
    queue.enqueue("maintenance", {"n": time.monotonic_ns()}, "none", max_attempts=5)
    host = socket.gethostname().lower()
    child, job_id = _claim_in_child(tmp_path, ops_store, host)
    _kill(child)
    start = time.perf_counter()
    owners = dead_owners(host)
    SqliteJobsBackend().requeue_owned(owners, clock.now())
    elapsed = time.perf_counter() - start
    assert queue.get(job_id).status == "queued"
    return elapsed


def _other_owner_repeat(tmp_path: Path, ops_store: OpsStoreHandle) -> float:
    """Kill an owner on another host; the reaper requeues once the (short) lease lapses."""
    queue.enqueue("maintenance", {"n": time.monotonic_ns()}, "none", max_attempts=5)
    child, job_id = _claim_in_child(tmp_path, ops_store, "otherhost")
    expires = clock.format_utc(clock.now() + timedelta(seconds=LEASE_S))

    def shorten(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE job SET lease_expires_at = ? WHERE job_id = ?", (expires, job_id))

    run_write(shorten, op="bench_shorten_lease")
    start = time.perf_counter()
    _kill(child)
    backend = SqliteJobsBackend()
    while queue.get(job_id).status != "queued":
        backend.reap_expired(clock.now())
        assert time.perf_counter() - start < LEASE_S + OTHER_OWNER_MARGIN_S, "never requeued"
        time.sleep(0.1)
    return time.perf_counter() - start


@pytest.mark.usefixtures("jobs_db")
def test_bt08_04_requeue_after_owner_death(ops_store: OpsStoreHandle, tmp_path: Path) -> None:
    """BT08-04 a claim's owner is killed: same host requeued <= 10 s after restart
    recovery; another owner requeued <= lease_s + 30 s (median of the repeats)."""
    same = [_same_host_repeat(tmp_path, ops_store) for _ in range(REPEATS)]
    other = [_other_owner_repeat(tmp_path, ops_store) for _ in range(REPEATS)]
    median_same = report("BT08-04", "same host", same, "s", f"<= {SAME_HOST_BUDGET_S:g} s")
    budget = LEASE_S + OTHER_OWNER_MARGIN_S
    median_other = report("BT08-04", "other owner", other, "s", f"<= {budget:g} s")
    assert median_same <= SAME_HOST_BUDGET_S
    assert median_other <= budget


@pytest.fixture
def gpu(ops_store: OpsStoreHandle) -> Iterator[GpuController]:
    """A controller on the real config and compose project (target PC only)."""
    del ops_store
    c.reset_config()
    c.init_config("local", config_dir=REPO / "config", env=dict(os.environ))
    bind_core_backends()
    controller = GpuController(worker_id=None, runner=ComposeRunner(), http=LoopbackHttp())
    controller.detect_loaded_class()
    yield controller
    c.reset_config()


_needs_gpu = pytest.mark.skipif(
    os.environ.get(GPU_ENV) != "1",
    reason=f"needs the real containers on the target PC (set {GPU_ENV}=1)",
)
_needs_openjev = pytest.mark.skipif(
    os.environ.get(GPU_ENV) != "1",
    reason=(
        "BT08-07 blocked by open question (b)9 (D7): whether OpenJev weights run on the target "
        f"GPU is unconfirmed; also needs the real containers (set {GPU_ENV}=1)"
    ),
)


def _timed(label: str, bench_id: str, seconds: float, budget_s: float) -> None:
    sys.stderr.write(f"{bench_id} {label} {seconds:.1f} s (target < {budget_s:g} s)\n")
    assert seconds < budget_s


@pytest.mark.gpu
@_needs_gpu
def test_bt08_06_swap_reasoning_to_decider(gpu: GpuController) -> None:
    """BT08-06 swap reasoning -> decider on real containers (stop, VRAM check, start): < 2 min."""
    gpu.swap("reasoning", reason="bench_setup")
    seconds = gpu.swap("decider", reason="bench")
    _timed("swap reasoning->decider", "BT08-06", seconds, 120)


@pytest.mark.gpu
@_needs_openjev
def test_bt08_07_openjev_start_with_warmup(gpu: GpuController) -> None:
    """BT08-07 `services.start("openjev")` including warm-up on real containers: < 5 min."""
    gpu.swap("decider", reason="bench_setup")
    gpu.service_stop("openjev")
    start = time.perf_counter()
    gpu.service_start("openjev")
    _timed("openjev start+warm-up", "BT08-07", time.perf_counter() - start, 300)


@pytest.mark.gpu
@_needs_gpu
def test_bt08_08_swap_decider_to_reasoning(gpu: GpuController) -> None:
    """BT08-08 swap decider -> reasoning with the 30B model on real containers: < 6 min."""
    gpu.swap("decider", reason="bench_setup")
    seconds = gpu.swap("reasoning", reason="bench")
    _timed("swap decider->reasoning", "BT08-08", seconds, 360)
