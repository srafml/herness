"""Start-up helpers and timings of the supervisor (private sibling of `supervisor`, T08-21).

U08-87 steps 2-5: the `starting` worker row, the host GPU lock, crash recovery of leases held
by dead processes on this host, `lease_expired` events, and the signal handlers. Kept apart
from `herness.core.jobs.supervisor` for the impl 08 §2 line budget of that module.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import FrameType
from typing import Final

import psutil

import herness
from herness.core.config import get_config
from herness.core.errors import ConfigError
from herness.core.jobs.gpu_lock import GpuLock
from herness.core.jobs.ports import WorkerRow, require_jobs_backend
from herness.core.logging import get_logger
from herness.core.resilience._state import process_state
from herness.core.resilience.events import record_event
from herness.core.resilience.metrics import record_counter

__all__ = [
    "Timings",
    "dead_owners",
    "install_signals",
    "lease_expired",
    "restore_signals",
    "take_gpu_lock",
    "timings",
    "worker_row",
]

LEASE_EXPIRED: Final = "herness_jobs_lease_expired_total"
_OWNER_PARTS: Final = 3  # host, pid, slot

type SignalHandler = Callable[[int, FrameType | None], None]

_log = get_logger("jobs")


@dataclass(frozen=True, slots=True)
class Timings:
    """Supervisor timings of `R.jobs` and `S` as timedeltas."""

    heartbeat: timedelta
    lease: timedelta
    cancel_grace: timedelta
    stall: timedelta
    stall_large: timedelta
    preempt_grace: timedelta
    reaper: timedelta
    shutdown_grace: timedelta
    tick_s: float


def timings() -> Timings:
    """The supervisor timings of the loaded config."""
    cfg = get_config().resilience
    j = cfg.resilience.jobs
    return Timings(
        heartbeat=timedelta(seconds=j.heartbeat_s),
        lease=timedelta(seconds=j.lease_s),
        cancel_grace=timedelta(seconds=j.cancel_grace_s),
        stall=timedelta(seconds=j.stall_timeout_s),
        stall_large=timedelta(seconds=j.stall_timeout_large_s),
        preempt_grace=timedelta(minutes=cfg.schedule.preempt_grace_min),
        reaper=timedelta(seconds=j.reaper_interval_s),
        shutdown_grace=timedelta(seconds=j.shutdown_grace_s),
        tick_s=j.tick_s,
    )


def lease_expired(rows: Sequence[tuple[str, str, str]], *, count: bool) -> None:
    """One `lease_expired` event (and the counter when `count`) per requeued or failed job."""
    for job_id, kind, outcome in rows:
        detail = {"kind": kind, "outcome": outcome}
        record_event("lease_expired", component="jobs", target=kind, job_id=job_id, detail=detail)
        if count:
            record_counter(LEASE_EXPIRED, component="jobs", labels={"kind": kind})


def _dead(owner: str) -> bool:
    """True when `<host>:<pid>:<slot>` names a pid other than ours that no longer exists."""
    parts = owner.rsplit(":", _OWNER_PARTS - 1)
    if len(parts) != _OWNER_PARTS or not parts[1].isdigit():
        return False
    pid = int(parts[1])
    return pid != os.getpid() and not psutil.pid_exists(pid)


def dead_owners(host: str) -> list[str]:
    """Step 4: owners of running jobs on this host whose process is gone."""
    rows = require_jobs_backend().running_on_host(host)
    return sorted({r.lease_owner for r in rows if r.lease_owner and _dead(r.lease_owner)})


def worker_row(
    worker_id: str, host: str, *, gpu_slot: bool, cpu_slots: int, now: datetime
) -> WorkerRow:
    """Step 2: the `starting` row (`faults_enabled` once the fault plan was loaded)."""
    return WorkerRow(
        worker_id=worker_id,
        host=host,
        pid=os.getpid(),
        gpu_slot=int(gpu_slot),
        cpu_slots=cpu_slots,
        gpu_class_loaded="none",
        status="starting",
        started_at=now,
        heartbeat_at=now,
        version=herness.__version__,
        faults_enabled=process_state().faults_enabled,
    )


def take_gpu_lock(worker_id: str) -> GpuLock | None:
    """Step 3: `data/locks/gpu.lock`; held by another owner → ERROR log and None (TH08-09)."""
    lock = GpuLock(get_config().paths.data / "locks" / "gpu.lock")
    try:
        return lock.__enter__()
    except ConfigError:
        _log.error("jobs.worker.gpu_lock_held", worker_id=worker_id)
        return None


def install_signals(handler: SignalHandler) -> dict[int, object]:
    """Step 5: SIGINT plus SIGBREAK (Windows) or SIGTERM; returns the previous handlers."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    names = ("SIGINT", "SIGBREAK") if sys.platform == "win32" else ("SIGINT", "SIGTERM")
    return {s: signal.signal(s, handler) for s in (getattr(signal, n) for n in names)}


def restore_signals(previous: dict[int, object]) -> None:
    """Put back the handlers `install_signals` replaced."""
    for signum, handler in previous.items():
        signal.signal(signum, handler)  # type: ignore[arg-type]  # a handler signal.signal gave
