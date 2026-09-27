"""The `herness worker` process: supervisor and `run_worker` (impl 08 U08-87, U08-89).

Design 08 §5.9, §5.10. The main thread runs the tick loop; GPU work runs on one executor
thread (`_supervisor_gpu`); every job runs in a spawn child (`_supervisor_child`,
`child_main`). Exit codes follow R-46: 0 normal end, 1 operation failed (a `ConfigError`
at start, the GPU lock held elsewhere, or the store-unavailable shutdown); never 2 to 4.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import FrameType
from typing import Final, get_args

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError, HernessError
from herness.core.jobs import _supervisor_boot as boot
from herness.core.jobs._supervisor_child import ChildRun, child_crash, spawn
from herness.core.jobs._supervisor_gpu import GpuSlot, reject_request
from herness.core.jobs.child import call_bootstrap
from herness.core.jobs.gpu_lock import GpuLock
from herness.core.jobs.outcomes import finish_job
from herness.core.jobs.ports import JobRow, require_jobs_backend
from herness.core.jobs.queue import _claim
from herness.core.jobs.scheduler import run_scheduler
from herness.core.jobs.validate import validate_resilience_config
from herness.core.jobs.windows import preempt_deadline
from herness.core.logging import get_logger
from herness.core.resilience.breaker import run_due_probes
from herness.core.resilience.faults import _ensure_loaded
from herness.core.resilience.metrics import flush_metrics, record_histogram
from herness.core.types import GpuClass, JobOutcome

__all__ = ["Supervisor", "WorkerOptions", "run_worker"]

FAILED_TICKS_MAX: Final = 10
MAX_CONCURRENCY: Final = 16
TICK_SECONDS: Final = "herness_jobs_supervisor_tick_seconds"
_CLASSES: Final[frozenset[str]] = frozenset(get_args(GpuClass.__value__))

_log = get_logger("jobs")


@dataclass(frozen=True, slots=True)
class WorkerOptions:
    """`herness worker` options; `concurrency=None` means `R.jobs.cpu_slots` (U08-89)."""

    gpu_classes: tuple[GpuClass, ...]
    concurrency: int | None
    once: bool
    bootstrap: str

    def __post_init__(self) -> None:
        if not set(self.gpu_classes) <= _CLASSES:
            msg = "gpu_classes has an unknown GPU class"
            raise ConfigError(msg)
        n = self.concurrency
        if n is not None and (type(n) is not int or not 0 <= n <= MAX_CONCURRENCY):
            msg = f"concurrency must be an integer from 0 to {MAX_CONCURRENCY}"
            raise ConfigError(msg)


class Supervisor:
    """The worker process (U08-87): `run()` returns the exit code (0 or 1, R-46)."""

    def __init__(self, options: WorkerOptions) -> None:
        self.options = options
        self.host = socket.gethostname().lower()
        self.worker_id = f"{self.host}:{os.getpid()}"
        self.gpu: GpuSlot | None = None
        self.slots: dict[str, ChildRun | None] = {}
        self.draining = self.terminate_now = False
        self.exit_code = self.failed_ticks = 0
        self._t: boot.Timings | None = None
        self._lock: GpuLock | None = None
        self._signals = 0
        self._claimed = False
        self._next_reap: datetime | None = None
        self._next_row: datetime | None = None
        self._drain_until: datetime | None = None
        self._wake = threading.Event()

    @property
    def timings(self) -> boot.Timings:
        if self._t is None:
            self._t = boot.timings()
        return self._t

    def run(self) -> int:
        """Steps 1-8 of U08-87; any unexpected failure ends the worker with exit code 1."""
        previous: dict[int, object] = {}
        try:
            code = self.start()
            if code is not None:
                return code
            previous = boot.install_signals(self._on_signal)
            self._loop()
            return self.stop()
        except Exception as exc:  # noqa: BLE001 - worker top level: exit 1, never a traceback
            error_type = type(exc).__name__
            _log.error("jobs.worker.start_failed", worker_id=self.worker_id, error_type=error_type)
            for run in self._runs():
                run.terminate()
            return 1
        finally:
            boot.restore_signals(previous)
            self.release()

    def start(self) -> int | None:
        """Steps 1-4 and the `running` row; an exit code when the worker must not run."""
        try:
            call_bootstrap(self.options.bootstrap)
            _ensure_loaded()  # a bad fault plan fails here, at start (R-40)
            issues = validate_resilience_config(get_config())
        except ConfigError as exc:
            error_type = type(exc).__name__
            _log.error(
                "jobs.worker.config_invalid", worker_id=self.worker_id, error_type=error_type
            )
            return 1
        if issues:
            paths = [issue.path for issue in issues]
            _log.error("jobs.worker.config_invalid", worker_id=self.worker_id, paths=paths)
            return 1
        self._t = boot.timings()
        cpu = self.options.concurrency
        cpu = get_config().resilience.resilience.jobs.cpu_slots if cpu is None else cpu
        has_gpu = any(c != "none" for c in self.options.gpu_classes)
        backend, now = require_jobs_backend(), clock.now()
        row = boot.worker_row(self.worker_id, self.host, gpu_slot=has_gpu, cpu_slots=cpu, now=now)
        backend.upsert_worker(row)
        if has_gpu:
            self._lock = boot.take_gpu_lock(self.worker_id)
            if self._lock is None:
                backend.update_worker(self.worker_id, status="stopped")
                return 1
            self.gpu = GpuSlot(self.worker_id, self.options.gpu_classes)
            self.gpu.controller.detect_loaded_class()
        self.slots = {"gpu": None} if has_gpu else {}
        self.slots.update({f"cpu{i}": None for i in range(cpu)})
        boot.lease_expired(backend.requeue_owned(boot.dead_owners(self.host), now), count=False)
        backend.update_worker(self.worker_id, status="running")
        _log.info("jobs.worker.started", worker_id=self.worker_id, slots=len(self.slots))
        return None

    def _on_signal(self, _signum: int, _frame: FrameType | None) -> None:
        self._signals += 1
        if self._signals == 1:
            self.draining = True
        else:
            self.terminate_now = True
        self._wake.set()

    def _loop(self) -> None:
        while not self.terminate_now:
            now = clock.now()
            self.tick(now)
            if self.loop_done(now):
                return
            self._wake.wait(self.timings.tick_s)
            self._wake.clear()

    def _runs(self) -> Iterator[ChildRun]:
        return (run for run in list(self.slots.values()) if run is not None)

    def _free(self, run: ChildRun) -> None:
        self.slots[run.slot] = None
        if self.options.once:
            self.draining = True  # step 7i: the one job is finished

    def loop_done(self, now: datetime) -> bool:
        """Step 7i and step 8: whether the loop ends after this tick."""
        runs = list(self._runs())
        idle = not runs and (self.gpu is None or self.gpu.idle())
        if self.options.once and idle and not self._claimed and not self.draining:
            _log.info("jobs.worker.nothing_to_do", worker_id=self.worker_id)
            return True
        if not self.draining:
            return False
        if self._drain_until is None:
            self._drain_until = now + self.timings.shutdown_grace
            for run in runs:
                run.stop("shutdown")
        return not runs or now >= self._drain_until

    def tick(self, now: datetime) -> None:
        """Step 7: one guarded tick; 10 failed ticks in a row end the worker with code 1."""
        started = clock.monotonic()
        try:
            self._tick_steps(now)
        except Exception as exc:  # noqa: BLE001 - F08-04: the tick stops, children keep running
            self.failed_ticks += 1
            fields = {"failed_ticks": self.failed_ticks, "error_type": type(exc).__name__}
            _log.error("jobs.supervisor.tick_failed", **fields)
            if self.failed_ticks == FAILED_TICKS_MAX:
                _log.critical("jobs.supervisor.store_unavailable", **fields)
                self.draining, self.exit_code = True, 1
            return
        self.failed_ticks = 0
        record_histogram(TICK_SECONDS, clock.monotonic() - started, component="jobs")

    def _submit(self) -> Callable[..., None]:
        return self.gpu.request if self.gpu is not None else reject_request

    def _tick_steps(self, now: datetime) -> None:
        if not self.draining and (self._next_reap is None or now >= self._next_reap):
            self._periodic(now)
        for run in self._runs():
            run.drain(now, self._submit())
            run.send_replies()
            if run.reap(now, self._submit()):
                self._free(run)
        if not self.draining:
            for run in self._runs():
                if self._watch(run, now):
                    self._free(run)
            self._claim(now)
        if self._next_row is None or now >= self._next_row:
            self._update_row(now)

    def _periodic(self, now: datetime) -> None:
        """Step 7a, every `reaper_interval_s`."""
        self._next_reap = now + self.timings.reaper
        boot.lease_expired(require_jobs_backend().reap_expired(now), count=True)
        run_scheduler(now)
        run_due_probes(now)
        if self.gpu is not None:
            self.gpu.restart_check()
        flush_metrics()

    def _watch(self, run: ChildRun, now: datetime) -> bool:
        """Steps 7d-7f for one child; True when it was finished."""
        t = self.timings
        if run.lease_step(now, t):
            return True
        large = self.gpu is not None and self.gpu.loaded == "large"
        if run.stall_step(now, t.stall_large if large else t.stall):
            return True
        if run.slot != "gpu" or self.gpu is None:
            return False
        ctl = self.gpu.controller
        deadline = preempt_deadline(ctl.loaded, ctl.class_since, now)
        return run.preempt_step(now, deadline, t.preempt_grace)

    def _owner(self, slot: str) -> str:
        return f"{self.worker_id}:{slot}"

    def _claim(self, now: datetime) -> None:
        """Step 7g: fill free CPU slots, then run the arbiter for a free GPU slot."""
        if self.options.once and self._claimed:
            return
        for slot in [name for name, run in self.slots.items() if run is None and name != "gpu"]:
            owner = self._owner(slot)
            row = _claim(
                owner=owner,
                allowed_classes=["none"],
                job_id=None,
                min_priority=None,
                priority_exempt_kinds=(),
            )
            if row is None:
                return  # the other free CPU slots would find nothing either
            self._start_child(slot, row, now)
            if self.options.once:
                return
        if self.gpu is None or self.slots.get("gpu", True) is not None or not self.gpu.idle():
            return
        workers = require_jobs_backend().list_workers()
        worker = next((w for w in workers if w.worker_id == self.worker_id), None)
        found = self.gpu.plan(now, worker, preload=not self.options.once)
        if found is None:
            return
        row = _claim(
            owner=self._owner("gpu"),
            allowed_classes=found.classes,
            job_id=None,
            min_priority=found.min_priority,
            priority_exempt_kinds=found.exempt_kinds,
        )
        if row is not None:
            self._start_child("gpu", row, now)

    def _start_child(self, slot: str, row: JobRow, now: datetime) -> None:
        self._claimed = True
        owner = self._owner(slot)
        try:
            self.slots[slot] = spawn(slot, owner, row, self.options.bootstrap, now)
        except OSError:  # the process could not start: a crash without a child
            finish_job(row, owner, child_crash(), attempt_started_at=now, stop_reason=None)

    def _update_row(self, now: datetime) -> None:
        """Step 7h, every `heartbeat_s`."""
        self._next_row = now + self.timings.heartbeat
        fields: dict[str, object] = {
            "heartbeat_at": now,
            "current_jobs": [run.current() for run in self._runs()],
            "status": "draining" if self.draining else "running",
        }
        if self.gpu is not None and self.gpu.idle():  # a running swap owns the column
            fields["gpu_class_loaded"] = self.gpu.loaded
        require_jobs_backend().update_worker(self.worker_id, **fields)

    def _quietly(self, action: Callable[[], object]) -> None:
        try:
            action()
        except HernessError as exc:
            fields = {"failed_ticks": self.failed_ticks, "error_type": type(exc).__name__}
            _log.error("jobs.supervisor.tick_failed", **fields)

    def _yield_now(self, run: ChildRun) -> None:
        if not run.reap(clock.now(), self._submit()):
            run.settle(JobOutcome(status="yield"), stop_reason="shutdown")

    def stop(self) -> int:
        """Step 8: requeue what still runs (unless terminating now), mark the row stopped."""
        for run in self._runs():
            if self.terminate_now:
                run.terminate()  # stays `running`; crash recovery requeues it at next start
            else:
                self._quietly(lambda run=run: self._yield_now(run))  # type: ignore[misc]
            self.slots[run.slot] = None
        now = clock.now()
        backend = require_jobs_backend()
        fields: dict[str, object] = {"status": "stopped", "current_jobs": [], "heartbeat_at": now}
        self._quietly(lambda: backend.update_worker(self.worker_id, **fields))
        flush_metrics()
        _log.info("jobs.worker.stopped", worker_id=self.worker_id, exit_code=self.exit_code)
        return self.exit_code

    def release(self) -> None:
        """Stop the GPU executor and release the GPU lock; GPU services are left as they are."""
        if self.gpu is not None:
            self.gpu.close()
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.__exit__(None, None, None)


def run_worker(
    *,
    gpu_classes: Sequence[GpuClass],
    concurrency: int | None = None,
    once: bool = False,
    bootstrap: str,
) -> int:
    """Run the worker until it ends (U08-89); the exit code is 0 or 1 (R-46)."""
    try:
        options = WorkerOptions(tuple(gpu_classes), concurrency, once, bootstrap)
    except ConfigError as exc:
        _log.error("jobs.worker.config_invalid", paths=["options"], error_type=type(exc).__name__)
        return 1
    return Supervisor(options).run()
