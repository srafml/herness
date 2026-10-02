"""Worker supervisor with real child processes (impl 08 IT08-04 to IT08-07, IT08-09 to
IT08-11; T08-21): an in-process `Supervisor` driven tick by tick on a migrated ops store,
every job running in a `multiprocessing` spawn child through the test bootstrap."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from datetime import timedelta
from typing import Any

import pytest
import structlog
from tests.support.worker_bootstrap import BOOTSTRAP
from tests.support.worker_env import WorkerEnv

from herness.core import time as clock
from herness.core.errors import StoreBusy
from herness.core.jobs._supervisor_child import ChildRun
from herness.core.jobs.ports import bind_jobs_backend, require_jobs_backend
from herness.core.jobs.queue import cancel, claim
from herness.core.jobs.supervisor import Supervisor, run_worker

pytestmark = pytest.mark.integration


def _done(env: WorkerEnv, *ids: str) -> bool:
    return all(env.job(i).status == "done" for i in ids)


def _runs(sup: Supervisor) -> dict[str, ChildRun]:
    return {run.row.job_id: run for run in sup.slots.values() if run is not None}


def _working(sup: Supervisor, *ids: str) -> bool:
    """Every job runs in a child that has sent at least one heartbeat."""
    runs = _runs(sup)
    return all(i in runs and runs[i].note is not None for i in ids)


def test_it08_04_three_none_jobs_done_in_children(worker_env: WorkerEnv) -> None:
    """IT08-04 `--concurrency 1`, three `none` jobs of a fake `done` handler: all `done`, each
    in a child process (not this one), with `job_done` events and job metrics written."""
    ids = [worker_env.enqueue() for _ in range(3)]
    sup = worker_env.supervisor(concurrency=1)
    assert sup.start() is None
    worker_env.drive(sup, lambda: _done(worker_env, *ids))
    assert sup.stop() == 0
    pids = {worker_env.job(i).result["pid"] for i in ids}  # type: ignore[index]
    assert os.getpid() not in pids
    assert len(pids) == 3  # one child per job
    done = {e["job_id"] for e in worker_env.events("job_done")}
    assert set(ids) <= done
    names = worker_env.metric_names()
    assert {"herness_jobs_finished_total", "herness_jobs_run_seconds"} <= names
    assert "herness_jobs_queue_wait_seconds" in names


def test_it08_05_cancel_yields_or_terminates_after_grace(worker_env: WorkerEnv) -> None:
    """IT08-05 cancel while running: the handler looping until `should_yield` yields and the
    job is finalised `canceled`; the handler ignoring stop is terminated after
    `cancel_grace_s` (2 s) and its job is finalised `canceled` too."""
    obey, ignore = worker_env.enqueue("loop"), worker_env.enqueue("ignore_stop")
    sup = worker_env.supervisor(concurrency=2)
    assert sup.start() is None
    worker_env.drive(sup, lambda: _working(sup, obey, ignore))
    runs = _runs(sup)
    assert cancel(obey) == "cancel_requested"
    assert cancel(ignore) == "cancel_requested"
    started = time.monotonic()
    with structlog.testing.capture_logs() as logs:
        worker_env.drive(sup, lambda: not _runs(sup))
    assert time.monotonic() - started >= 2.0  # the ignoring child got its grace
    for job_id in (obey, ignore):
        row = worker_env.job(job_id)
        assert row.status == "canceled"
        assert row.finished_at is not None
        assert row.lease_owner is None
    assert runs[obey].proc.exitcode == 0  # it yielded and exited by itself
    assert runs[ignore].proc.exitcode != 0  # terminated
    canceled = [e for e in logs if e["event"] == "jobs.job.canceled"]
    assert {e["job_id"] for e in canceled} == {obey, ignore}
    assert sup.stop() == 0


def test_it08_06_stalled_child_killed_and_requeued(worker_env: WorkerEnv) -> None:
    """IT08-06 a handler sleeping without heartbeat and `stall_timeout_s` 3 s: the child is
    killed and the job requeued with `ModelUnavailable("stalled")` (attempt 2 then runs)."""
    worker_env.configure(stall_timeout_s=3)
    job_id = worker_env.enqueue("stall")
    sup = worker_env.supervisor(concurrency=1)
    assert sup.start() is None
    worker_env.drive(sup, lambda: job_id in _runs(sup))
    first = _runs(sup)[job_id]
    with structlog.testing.capture_logs() as logs:
        worker_env.drive(sup, lambda: _done(worker_env, job_id))
    assert first.proc.exitcode != 0  # killed, not finished
    row = worker_env.job(job_id)
    assert row.attempts == 2
    assert row.last_error is not None
    assert row.last_error["class"] == "ModelUnavailable"
    assert row.last_error["message"] == "stalled"
    retries = [e for e in worker_env.events("retry") if e["job_id"] == job_id]
    assert [e["error_type"] for e in retries] == ["ModelUnavailable"]
    assert any(e["event"] == "jobs.job.stalled" and e["job_id"] == job_id for e in logs)
    assert sup.stop() == 0


def test_it08_07_child_crash_requeued_then_attempt_2_done(worker_env: WorkerEnv) -> None:
    """IT08-07 a handler calling `os._exit(9)`: `child_crash`, the job is requeued, and
    attempt 2 succeeds."""
    job_id = worker_env.enqueue("crash")
    sup = worker_env.supervisor(concurrency=1)
    assert sup.start() is None
    worker_env.drive(sup, lambda: job_id in _runs(sup))
    first = _runs(sup)[job_id]
    worker_env.drive(sup, lambda: _done(worker_env, job_id))
    assert first.proc.exitcode == 9
    row = worker_env.job(job_id)
    assert row.attempts == 2
    assert row.result is not None
    assert row.result["attempt"] == 2
    assert row.last_error is not None
    assert row.last_error["class"] == "ModelUnavailable"
    assert row.last_error["message"] == "child_crash"
    assert sup.stop() == 0


def _dead_pid() -> int:
    """The pid of a process that has already exited."""
    probe = subprocess.Popen([sys.executable, "-c", "pass"])
    probe.wait(timeout=60)
    return probe.pid


def test_it08_09_crash_recovery_requeues_dead_owner_rows(worker_env: WorkerEnv) -> None:
    """IT08-09 `running` rows owned by `<host>:<dead pid>:cpu0`: a starting worker requeues
    them within 10 s, attempts unchanged by the recovery, one `lease_expired` event each."""
    host = socket.gethostname().lower()
    owner = f"{host}:{_dead_pid()}:cpu0"
    ids = [worker_env.enqueue("loop"), worker_env.enqueue("loop")]
    for _ in ids:
        assert claim(owner=owner, allowed_classes=["none"]) is not None
    assert all(worker_env.job(i).status == "running" for i in ids)
    sup = worker_env.supervisor(concurrency=0)
    started = time.monotonic()
    assert sup.start() is None
    assert time.monotonic() - started < 10.0
    for job_id in ids:
        row = worker_env.job(job_id)
        assert row.status == "queued"
        assert row.lease_owner is None
        assert row.attempts == 1  # the claim's count; the recovery charges nothing
    expired = {e["job_id"]: e["outcome"] for e in worker_env.events("lease_expired")}
    assert expired == dict.fromkeys(ids, "queued")
    assert sup.stop() == 0


class _Busy:
    """The real jobs backend; every call raises `StoreBusy` while `down` is set."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.down = inner, False

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            if self.down:
                msg = "database is locked"
                raise StoreBusy(msg)
            return attr(*args, **kwargs)

        return call


def test_it08_10_store_busy_every_tick_shuts_down_with_exit_1(worker_env: WorkerEnv) -> None:
    """IT08-10 a backend raising `StoreBusy` on every tick: the child is untouched for 9
    failed ticks; the 10th starts the graceful shutdown and the worker exits 1 (R-46)."""
    job_id = worker_env.enqueue("loop")
    sup = worker_env.supervisor(concurrency=1)
    assert sup.start() is None
    worker_env.drive(sup, lambda: _working(sup, job_id))
    run = _runs(sup)[job_id]
    busy = _Busy(require_jobs_backend())
    bind_jobs_backend(busy)
    busy.down = True
    base = clock.now()
    with structlog.testing.capture_logs() as logs:
        for tick in range(1, 13):
            sup.tick(base + timedelta(seconds=5 * tick))
            if tick <= 9:
                assert sup.failed_ticks == tick
                assert not sup.draining
                assert run.proc.is_alive()
                assert run.stops == set()
    assert sup.draining
    assert sup.exit_code == 1
    critical = [e for e in logs if e["event"] == "jobs.supervisor.store_unavailable"]
    assert len(critical) == 1
    assert critical[0]["failed_ticks"] == 10
    assert sup.loop_done(clock.now()) is False  # stop("shutdown") sent, grace starts
    assert run.stops == {"shutdown"}
    run.proc.join(60)
    assert run.proc.exitcode == 0  # the child yielded on the shutdown stop
    assert sup.stop() == 1


def test_it08_11_once_runs_one_job_or_nothing(worker_env: WorkerEnv) -> None:
    """IT08-11 `--once` with 2 queued jobs: one job done, exit 0; with none queued:
    `jobs.worker.nothing_to_do`, exit 0."""
    first, second = worker_env.enqueue(), worker_env.enqueue()
    code = run_worker(gpu_classes=["none"], concurrency=2, once=True, bootstrap=BOOTSTRAP)
    assert code == 0
    statuses = sorted(worker_env.job(i).status for i in (first, second))
    assert statuses == ["done", "queued"]
    remaining = first if worker_env.job(first).status == "queued" else second
    assert cancel(remaining) == "canceled"
    with structlog.testing.capture_logs() as logs:
        code = run_worker(gpu_classes=["none"], once=True, bootstrap=BOOTSTRAP)
    assert code == 0
    assert any(e["event"] == "jobs.worker.nothing_to_do" for e in logs)
