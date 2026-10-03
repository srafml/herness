"""In-process run of one job (impl 08 U08-90, flow F08-14; design 08 §5.7, R-45).

CLI commands enqueue by default; the admin-only `--inline` flag of impl 09 calls `run_inline`,
which claims the named job as `<host>:<pid>:cli`, takes the host GPU lock when the job needs
the GPU (TH08-09), heartbeats its lease from a daemon thread, turns Ctrl+C into a `shutdown`
stop (the handler yields), runs the handler with an `InlineJobContext` and applies the outcome
through the same `finish_job` the worker uses (U08-50).
"""

from __future__ import annotations

import os
import signal
import socket
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError, HernessError, JobStateError, ModelUnavailable
from herness.core.jobs.context import InlineJobContext
from herness.core.jobs.gpu import GpuController
from herness.core.jobs.gpu_lock import GpuLock
from herness.core.jobs.gpu_services import ComposeRunner, LoopbackHttp
from herness.core.jobs.handlers import resolve_handler, run_handler
from herness.core.jobs.outcomes import finish_job
from herness.core.jobs.ports import require_jobs_backend
from herness.core.jobs.queue import GPU_SLOT_KINDS, claim
from herness.core.logging import bind_ids, get_logger

if TYPE_CHECKING:
    from types import FrameType

    from herness.core.jobs.ports import JobRow
    from herness.core.types import GpuClass, JobOutcome

__all__ = ["run_inline"]

ALL_CLASSES: Final[tuple[GpuClass, ...]] = ("none", "reasoning", "decider", "large")
JOIN_S: Final = 5.0  # the heartbeat thread wakes on its stop event; this only bounds the join
_ID_CHARS: Final = 64

_log = get_logger("jobs")


def inline_owner() -> str:
    """`<host>:<pid>:cli`, the lease owner of an inline run (U08-90 step 1, U08-48)."""
    return f"{socket.gethostname().lower()}:{os.getpid()}:cli"


def _needs_gpu(row: JobRow) -> bool:
    return row.gpu_class != "none" or row.kind in GPU_SLOT_KINDS  # R-43


def _gpu_controller(
    row: JobRow, owner: str, started: datetime, stack: ExitStack
) -> GpuController | None:
    """Step 2: the host GPU lock, the loaded class and the swap; None for CPU-only jobs.

    A failed swap is applied by `finish_job`; any other exit (a held lock, Ctrl+C, a
    detection error) requeues the job at once with no attempt charge, then re-raises.
    """
    if not _needs_gpu(row):
        return None
    applied = False  # the swap failure is applied by `finish_job`; all else yields the job
    try:
        stack.enter_context(GpuLock(get_config().paths.data / "locks" / "gpu.lock"))
        controller = GpuController(worker_id=None, runner=ComposeRunner(), http=LoopbackHttp())
        loaded = controller.detect_loaded_class()
        if row.gpu_class not in {"none", loaded}:
            try:
                controller.swap(row.gpu_class, reason="inline")
            except ModelUnavailable as exc:
                applied = True
                finish_job(row, owner, exc, attempt_started_at=started, stop_reason=None)
                raise
    except BaseException:  # held lock, Ctrl+C (no SIGINT handler yet) or any failure: yield
        if not applied:
            now = clock.now()
            require_jobs_backend().finish_yield(row.job_id, owner, now, now)  # no attempt charge
        raise
    return controller


def _beat(ctx: InlineJobContext, owner: str, stop: threading.Event) -> None:
    """Every `heartbeat_s`: extend the lease; a lost lease (or cancel) stops the handler."""
    jobs = get_config().resilience.resilience.jobs
    backend = require_jobs_backend()
    while not stop.wait(jobs.heartbeat_s):
        lease_until = clock.now() + timedelta(seconds=jobs.lease_s)
        try:
            kept = backend.heartbeat_job(ctx.job_id, owner, lease_until)
        except Exception as exc:  # noqa: BLE001 - thread boundary (ENG §3.4): retry next beat
            _log.warning(
                "jobs.inline.heartbeat_failed", job_id=ctx.job_id, error_type=type(exc).__name__
            )
            continue
        if not kept:
            ctx.request_stop("cancel")
            return


@contextmanager
def _heartbeat(ctx: InlineJobContext, owner: str) -> Iterator[None]:
    """Step 3: the daemon heartbeat thread, stopped and joined on exit."""
    stop = threading.Event()
    thread = threading.Thread(
        target=_beat, args=(ctx, owner, stop), name="herness-inline-heartbeat", daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(JOIN_S)


@contextmanager
def _sigint(ctx: InlineJobContext) -> Iterator[None]:
    """Step 4: Ctrl+C sets the stop reason `shutdown` (the handler yields); restored on exit.

    Signal handlers can only be set from the main thread; elsewhere nothing is installed.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def on_sigint(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        ctx.request_stop("shutdown")

    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _run(ctx: InlineJobContext) -> JobOutcome | HernessError:
    """Step 5: the handler of the job's kind; a missing handler becomes the outcome."""
    try:
        handler = resolve_handler(ctx.kind)
    except ConfigError as exc:
        return exc
    return run_handler(ctx, handler)


def run_inline(job_id: str) -> JobOutcome:
    """Claim and run job `job_id` in this process (U08-90; R-45).

    Returns the handler's `JobOutcome` (`yield` after Ctrl+C or a lost lease) once the
    outcome is applied. Raises JobStateError when the job cannot be claimed, the
    ConfigError of a held GPU lock (the job is requeued at once), the ModelUnavailable of a
    failed swap, and the handler's HernessError after it was applied.
    """
    owner = inline_owner()
    row = claim(owner=owner, allowed_classes=ALL_CLASSES, job_id=job_id)
    if row is None:
        msg = "job not claimable"
        raise JobStateError(msg, job_id=job_id[:_ID_CHARS])
    started = clock.now()
    with bind_ids(job_id=row.job_id), ExitStack() as stack:  # step 7: unwound in reverse
        controller = _gpu_controller(row, owner, started, stack)
        ctx = InlineJobContext(row, owner=owner, controller=controller)
        stack.enter_context(_heartbeat(ctx, owner))
        stack.enter_context(_sigint(ctx))
        res = _run(ctx)
        finish_job(row, owner, res, attempt_started_at=started, stop_reason=ctx.stop_reason)
    if isinstance(res, HernessError):
        raise res
    return res
