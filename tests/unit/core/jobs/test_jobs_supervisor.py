"""Unit tests of U08-87 (Supervisor) and U08-89 (run_worker) on a real ops store with the
spawn context faked by threads (`sup_env`): UT08-62 (core half) and the T08-21 supervisor
paths (start, loop, signals, shutdown, tick failures)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta
from typing import Any

import pytest
import structlog
from tests.support.worker_bootstrap import BOOTSTRAP
from tests.unit.core.jobs._supervisor_env import FakeChild, SupEnv

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.config_view import ConfigIssue
from herness.core.errors import ConfigError, StoreBusy
from herness.core.jobs import supervisor as sv
from herness.core.jobs._supervisor_boot import _dead, dead_owners, install_signals, restore_signals
from herness.core.jobs.gpu_lock import GpuLock
from herness.core.jobs.pipe import OutcomeMsg
from herness.core.jobs.ports import bind_jobs_backend, require_jobs_backend
from herness.core.jobs.supervisor import Supervisor, WorkerOptions, run_worker
from herness.core.resilience.metrics import flush_metrics
from herness.store.ops.core import read_all, run_write

pytestmark = pytest.mark.unit


def _worker(sup: Supervisor) -> Any:
    return next(w for w in require_jobs_backend().list_workers() if w.worker_id == sup.worker_id)


def _loop_child(child: FakeChild) -> int:
    """Heartbeat until a stop arrives, then yield."""
    child.heartbeat()
    while True:
        msg = child.recv(timeout=0.05)
        if msg is not None and msg.type == "stop":
            child.send(OutcomeMsg(status="yield", result={}))
            return 0
        if child.proc.terminated:
            return -15


def _stubborn_child(child: FakeChild) -> int:
    child.heartbeat()
    child.wait_released(60)
    return 0


def test_cv_t08_21_worker_options_validated() -> None:
    """CV-T08-21 `WorkerOptions` rejects unknown classes and concurrency outside 0-16;
    `run_worker` turns that into exit code 1."""
    with pytest.raises(ConfigError):
        WorkerOptions(("huge",), 1, False, BOOTSTRAP)  # type: ignore[arg-type]
    for bad in (-1, 17, True):
        with pytest.raises(ConfigError):
            WorkerOptions(("none",), bad, False, BOOTSTRAP)
    assert run_worker(gpu_classes=["none"], concurrency=99, bootstrap=BOOTSTRAP) == 1


def test_cv_t08_21_timings_from_config(sup_env: SupEnv) -> None:
    """CV-T08-21 the supervisor timings are read from `R.jobs` and `S` (fast test config)."""
    t = sup_env.supervisor().timings
    assert (t.heartbeat, t.lease, t.tick_s) == (timedelta(seconds=1), timedelta(seconds=10), 0.2)
    assert t.preempt_grace == timedelta(minutes=15)


def test_cv_t08_21_start_config_errors_exit_1(
    sup_env: SupEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T08-21 step 1: a bootstrap that is not `module:function`, an import failure and
    `validate_resilience_config` issues all end the start with exit code 1 (R-46)."""
    for spec in ("no_colon", "tests.support.nope:bootstrap", "tests.support.worker_bootstrap:NOPE"):
        with structlog.testing.capture_logs() as logs:
            assert sup_env.supervisor(bootstrap_spec=spec).run() == 1
        assert [e["event"] for e in logs if e["log_level"] == "error"] == [
            "jobs.worker.config_invalid"
        ]
    issue = ConfigIssue("error", "schedule.windows", "gap", "resilience.yaml")
    monkeypatch.setattr(sv, "validate_resilience_config", lambda cfg: [issue])
    with structlog.testing.capture_logs() as logs:
        assert sup_env.supervisor().run() == 1
    bad = [e for e in logs if e["event"] == "jobs.worker.config_invalid"]
    assert bad[0]["paths"] == ["schedule.windows"]


def test_cv_t08_21_gpu_lock_held_exit_1(sup_env: SupEnv) -> None:
    """CV-T08-21 step 3 (TH08-09): another owner holds `data/locks/gpu.lock` → ERROR
    `jobs.worker.gpu_lock_held`, the worker row is `stopped`, exit code 1."""
    with GpuLock(get_config().paths.data / "locks" / "gpu.lock"):
        sup = sup_env.supervisor(gpu_classes=("reasoning",))
        with structlog.testing.capture_logs() as logs:
            assert sup.run() == 1
    assert any(e["event"] == "jobs.worker.gpu_lock_held" for e in logs)
    assert _worker(sup).status == "stopped"


def test_ut08_62_reaper_events_and_counter(sup_env: SupEnv) -> None:
    """UT08-62 (core half) expired running jobs at attempts 1/3 and 3/3: the tick's reaper
    requeues the first and fails the second, with one `lease_expired` event and one
    `herness_jobs_lease_expired_total{kind}` count per row."""
    first = sup_env.enqueue("done", max_attempts=3)
    second = sup_env.enqueue("done", kind="review", max_attempts=3)

    def at_cap(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE job SET attempts = 1 WHERE job_id = ?", (first,))
        conn.execute("UPDATE job SET attempts = 3 WHERE job_id = ?", (second,))
        conn.execute(
            "UPDATE job SET status = 'running', lease_owner = 'otherhost:1:cpu0',"
            " lease_expires_at = ? WHERE job_id IN (?, ?)",
            (clock.format_utc(clock.now()), first, second),
        )

    run_write(at_cap, op="test_seed")
    sup = sup_env.supervisor(concurrency=0)
    assert sup.start() is None
    sup.tick(clock.now() + timedelta(minutes=1))
    assert sup_env.job(first).status == "queued"
    assert sup_env.job(first).last_error["class"] == "LeaseExpired"  # type: ignore[index]
    assert sup_env.job(second).status == "failed"
    rows = read_all("SELECT job_id, detail FROM resilience_event WHERE kind = 'lease_expired'")
    outcomes = {r["job_id"]: json.loads(r["detail"])["outcome"] for r in rows}
    assert outcomes == {first: "queued", second: "failed"}
    flush_metrics()
    counts = read_all(
        "SELECT labels, value FROM metric_sample WHERE name = 'herness_jobs_lease_expired_total'"
    )
    assert sorted(json.loads(r["labels"])["kind"] for r in counts) == ["review", "sync"]
    assert sup.stop() == 0


def test_cv_t08_21_dead_owner_parsing(sup_env: SupEnv) -> None:
    """CV-T08-21 step 4: only `<host>:<pid>:<slot>` owners with a pid other than ours that no
    longer exists count as dead; `dead_owners` reads the running rows of one host."""
    import os  # noqa: PLC0415

    assert not _dead("bad")
    assert not _dead("h:notapid:cpu0")
    assert not _dead(f"h:{os.getpid()}:cpu0")
    assert _dead("h:999999999:cpu0")
    assert dead_owners("no-such-host") == []


def test_cv_t08_21_signals_drain_then_terminate(sup_env: SupEnv) -> None:
    """CV-T08-21 step 5 and 8: the first signal drains, the second terminates now; a job
    still running then stays `running` (crash recovery requeues it) and the row is `stopped`."""
    sup_env.ctx.script = _stubborn_child
    job_id = sup_env.enqueue("loop")
    sup = sup_env.supervisor()
    assert sup.start() is None
    sup_env.drive(sup, lambda: sup.slots["cpu0"] is not None and sup.slots["cpu0"].note is not None)
    sup._on_signal(2, None)
    assert sup.draining
    assert not sup.terminate_now
    sup._on_signal(2, None)
    assert sup.terminate_now
    sup._loop()  # returns at once
    assert sup.stop() == 0
    assert sup_env.job(job_id).status == "running"
    assert _worker(sup).status == "stopped"
    assert sup_env.ctx.procs[0].terminated


def test_cv_t08_21_install_and_restore_signals() -> None:
    """CV-T08-21 step 5: handlers are installed on the main thread only and restored."""
    import signal  # noqa: PLC0415

    before = signal.getsignal(signal.SIGINT)
    previous = install_signals(lambda s, f: None)
    assert signal.SIGINT in previous
    restore_signals(previous)
    assert signal.getsignal(signal.SIGINT) is before
    out: list[dict[int, object]] = []
    thread = threading.Thread(target=lambda: out.append(install_signals(lambda s, f: None)))
    thread.start()
    thread.join()
    assert out == [{}]


def test_cv_t08_21_run_once_and_nothing_to_do(sup_env: SupEnv) -> None:
    """CV-T08-21 steps 6-8 through `run()`: `once` runs one job and exits 0; with nothing
    queued it logs `jobs.worker.nothing_to_do` and exits 0."""
    job_id = sup_env.enqueue()
    assert sup_env.supervisor(once=True).run() == 0
    assert sup_env.job(job_id).status == "done"
    with structlog.testing.capture_logs() as logs:
        assert sup_env.supervisor(once=True, concurrency=None).run() == 0
    assert any(e["event"] == "jobs.worker.nothing_to_do" for e in logs)


def test_cv_t08_21_run_unexpected_error_exit_1(
    sup_env: SupEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T08-21 R-46: an unexpected failure in the loop ends the worker with 1 and terminates
    its children; nothing but 0 or 1 is ever returned."""
    sup_env.ctx.script = _stubborn_child
    sup_env.enqueue()
    sup = sup_env.supervisor()
    real_tick = sup.tick

    def tick_then_fail(now: Any) -> None:
        real_tick(now)
        if sup.slots["cpu0"] is not None:
            msg = "boom"
            raise RuntimeError(msg)

    monkeypatch.setattr(sup, "tick", tick_then_fail)
    with structlog.testing.capture_logs() as logs:
        assert sup.run() == 1
    assert any(e["event"] == "jobs.worker.start_failed" for e in logs)
    assert sup_env.ctx.procs[0].terminated


def test_cv_t08_21_shutdown_grace_then_yield(sup_env: SupEnv) -> None:
    """CV-T08-21 step 8: draining sends `stop("shutdown")` once; a child still running when
    `shutdown_grace_s` has passed is terminated and its job requeued at once without charge."""
    sup_env.ctx.script = _stubborn_child
    job_id = sup_env.enqueue()
    sup = sup_env.supervisor()
    assert sup.start() is None
    sup_env.drive(sup, lambda: sup.slots["cpu0"] is not None and sup.slots["cpu0"].note is not None)
    run = sup.slots["cpu0"]
    assert run is not None
    sup.draining = True
    now = clock.now()
    assert sup.loop_done(now) is False
    assert run.stops == {"shutdown"}
    assert sup.loop_done(now) is False  # sent once only
    assert sup.loop_done(now + timedelta(seconds=21)) is True
    assert sup.stop() == 0
    row = sup_env.job(job_id)
    assert (row.status, row.attempts) == ("queued", 0)


def test_cv_t08_21_drain_collects_outcome_before_grace(sup_env: SupEnv) -> None:
    """CV-T08-21 step 8: a child that yields on the shutdown stop is finished as a yield
    (queued at once, no attempt charge) before the grace ends."""
    sup_env.ctx.script = _loop_child
    job_id = sup_env.enqueue()
    sup = sup_env.supervisor()
    assert sup.start() is None
    sup_env.drive(sup, lambda: sup.slots["cpu0"] is not None and sup.slots["cpu0"].note is not None)
    sup.draining = True
    assert sup.loop_done(clock.now()) is False
    sup_env.drive(sup, lambda: sup.loop_done(clock.now()))
    assert sup.stop() == 0
    assert (sup_env.job(job_id).status, sup_env.job(job_id).attempts) == ("queued", 0)


class _Failing:
    def __init__(self, inner: Any) -> None:
        self.inner, self.down = inner, True

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            if self.down:
                msg = "locked"
                raise StoreBusy(msg)
            return attr(*args, **kwargs)

        return call


def test_cv_t08_21_failed_ticks_reset_and_store_unavailable(sup_env: SupEnv) -> None:
    """CV-T08-21 step 7 (F08-04): a clean tick resets `failed_ticks`; the 10th failure in a
    row logs CRITICAL once, drains, and the worker's exit code becomes 1; shutdown writes
    that fail are logged, not raised."""
    sup = sup_env.supervisor(concurrency=0)
    assert sup.start() is None
    failing = _Failing(require_jobs_backend())
    bind_jobs_backend(failing)
    base = clock.now()
    sup.tick(base)
    assert sup.failed_ticks == 1
    failing.down = False
    sup.tick(base + timedelta(seconds=5))
    assert sup.failed_ticks == 0
    failing.down = True
    with structlog.testing.capture_logs() as logs:
        for i in range(11):
            sup.tick(base + timedelta(seconds=10 + 5 * i))
    assert sup.exit_code == 1
    assert sup.draining
    assert sum(e["event"] == "jobs.supervisor.store_unavailable" for e in logs) == 1
    with structlog.testing.capture_logs() as logs:
        assert sup.stop() == 1
    assert any(e["event"] == "jobs.supervisor.tick_failed" for e in logs)


def test_cv_t08_21_spawn_failure_requeues(sup_env: SupEnv) -> None:
    """CV-T08-21 step 7g: a child that cannot be started is a crash: the claimed job is
    requeued with `ModelUnavailable("child_crash")`."""
    sup_env.ctx.start_error = OSError("no processes")
    job_id = sup_env.enqueue()
    sup = sup_env.supervisor()
    assert sup.start() is None
    sup.tick(clock.now())
    row = sup_env.job(job_id)
    assert row.status == "queued"
    assert row.last_error["message"] == "child_crash"  # type: ignore[index]
    assert sup.slots["cpu0"] is None
    assert sup.stop() == 0


def test_cv_t08_21_worker_row_heartbeat_and_current_jobs(sup_env: SupEnv) -> None:
    """CV-T08-21 step 7h: the worker row carries the heartbeat, status and current jobs."""
    sup_env.ctx.script = _stubborn_child
    job_id = sup_env.enqueue()
    sup = sup_env.supervisor(concurrency=2)
    assert sup.start() is None
    sup_env.drive(sup, lambda: sup.slots["cpu0"] is not None and sup.slots["cpu0"].note is not None)
    sup.tick(clock.now() + timedelta(seconds=2))
    row = _worker(sup)
    assert row.status == "running"
    assert row.cpu_slots == 2
    assert row.gpu_slot == 0
    assert [(j["job_id"], j["slot"], j["note"]) for j in row.current_jobs] == [
        (job_id, "cpu0", "working")
    ]
    sup.terminate_now = True
    assert sup.stop() == 0


def test_cv_t08_21_signal_during_start_drains(
    sup_env: SupEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T08-21 R-46: the handlers are installed before start-up, so an interrupt during
    `start()` drains (nothing is claimed, exit 0) and the old handler is restored."""
    import signal  # noqa: PLC0415

    before = signal.getsignal(signal.SIGINT)
    job_id = sup_env.enqueue()
    sup = sup_env.supervisor()
    real_start = sup.start

    def interrupted_start() -> int | None:
        signal.raise_signal(signal.SIGINT)
        return real_start()

    monkeypatch.setattr(sup, "start", interrupted_start)
    assert sup.run() == 0
    assert sup.draining
    assert sup_env.job(job_id).status == "queued"
    assert signal.getsignal(signal.SIGINT) is before
