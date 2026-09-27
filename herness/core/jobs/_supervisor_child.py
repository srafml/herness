"""One running job child of the supervisor (private sibling of `supervisor`, T08-21).

Spawn, pipe draining (U08-87 step 7b), reaping (7c), lease heartbeat and cancel (7d), stall
(7e) and preemption (7f) of one child, and the rebuild of the error a child reports. Kept
apart from `herness.core.jobs.supervisor` for the impl 08 §2 line budget of that module.
A frame that fails `decode_message` or has the parent → child direction marks the child
as crashed (ST08-13): it is terminated and finished with `ModelUnavailable("child_crash")`.
"""

from __future__ import annotations

import multiprocessing
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal, Protocol

from pydantic import ValidationError

from herness.core import errors
from herness.core import time as clock
from herness.core.errors import CircuitOpen, FatalError, HernessError, RateLimited, SchemaViolation
from herness.core.ids import canonical_json
from herness.core.jobs.child import child_main
from herness.core.jobs.outcomes import finish_job
from herness.core.jobs.pipe import (
    MAX_PIPE_MSG_BYTES,
    GpuReplyMsg,
    GpuRequestMsg,
    HeartbeatMsg,
    OutcomeMsg,
    PipeMessage,
    SaveStateMsg,
    StopMsg,
    decode_message,
    encode_message,
)
from herness.core.jobs.ports import require_jobs_backend
from herness.core.logging import get_logger
from herness.core.types import JobOutcome

if TYPE_CHECKING:
    from multiprocessing.process import BaseProcess

    from herness.core.jobs._supervisor_boot import Timings
    from herness.core.jobs.outcomes import FinishResult
    from herness.core.jobs.ports import JobRow, StopReason

__all__ = ["ChildRun", "error_of", "spawn"]

JOIN_TIMEOUT_S: Final = 5.0
CHILD_CRASH: Final = "child_crash"
STALLED: Final = "stalled"
NOT_STOPPED: Final = "child did not stop"
_ID_CHARS: Final = 64
_ERRORS: Final[dict[str, type[HernessError]]] = {
    name: value
    for name, value in vars(errors).items()
    if isinstance(value, type) and issubclass(value, HernessError)
}

type Verdict = JobOutcome | HernessError
type GpuSubmit = Callable[[ChildRun, GpuRequestMsg], None]

_log = get_logger("jobs")
_CTX: Final = multiprocessing.get_context("spawn")


class Conn(Protocol):
    """The parent end of a child's pipe (`Connection`, or `PipeConnection` on Windows)."""

    def poll(self, timeout: float | None = ...) -> bool: ...
    def recv_bytes(self, maxlength: int | None = ...) -> bytes: ...
    def send_bytes(self, buf: bytes) -> None: ...
    def close(self) -> None: ...


@dataclass(eq=False)
class ChildRun:
    """A claimed job running in a child process, and what the supervisor knows of it."""

    slot: str  # "gpu" or "cpu<i>"
    owner: str
    row: JobRow
    proc: BaseProcess
    conn: Conn
    started_at: datetime
    last_hb: datetime
    hb_due: datetime
    note: str | None = None
    outcome: OutcomeMsg | None = None
    verdict: Verdict | None = None  # set before a finish that may be retried next tick
    crashed: bool = False
    closed: bool = False
    stop_sent: StopReason | None = None
    stops: set[StopReason] = field(default_factory=set)
    cancel_at: datetime | None = None
    preempt_at: datetime | None = None
    pending: dict[str, Future[GpuReplyMsg]] = field(default_factory=dict)

    def current(self) -> dict[str, str | None]:
        """The `worker.current_jobs` entry of this child (U08-87 step 7h)."""
        return {
            "job_id": self.row.job_id,
            "slot": self.slot,
            "kind": self.row.kind,
            "started_at": clock.format_utc(self.started_at),
            "note": self.note,
        }

    def post(self, msg: PipeMessage) -> None:
        """Send one message; a closed pipe is ignored (the child is reaped at exit)."""
        with suppress(OSError):
            self.conn.send_bytes(encode_message(msg))

    def stop(self, reason: StopReason) -> None:
        """Send `stop(reason)` once per reason; the first reason sent is the stop reason."""
        if reason in self.stops:
            return
        self.stops.add(reason)
        self.stop_sent = self.stop_sent or reason
        self.post(StopMsg(reason=reason))

    def drain(self, now: datetime, gpu: GpuSubmit) -> None:
        """Handle every waiting child message (step 7b); a bad frame marks a crash."""
        try:
            while not self.closed and self.outcome is None and self.conn.poll(0):
                self._handle(decode_message(self.conn.recv_bytes(MAX_PIPE_MSG_BYTES)), now, gpu)
        except (EOFError, BrokenPipeError):  # the child closed its end (Windows: broken pipe)
            self.closed = True
        except (OSError, SchemaViolation):  # oversized or invalid frame (TH08-13, ST08-13)
            self.closed = self.crashed = True
            _log.error("jobs.pipe.lost", job_id=self.row.job_id, reason="bad_message")

    def _handle(self, msg: PipeMessage, now: datetime, gpu: GpuSubmit) -> None:
        if isinstance(msg, HeartbeatMsg):
            self.last_hb = now
            self.note = msg.note if msg.note is not None else self.note
        elif isinstance(msg, SaveStateMsg):
            data = canonical_json(msg.state).encode("utf-8")
            require_jobs_backend().save_job_state(self.row.job_id, self.owner, data)
        elif isinstance(msg, GpuRequestMsg):
            gpu(self, msg)
        elif isinstance(msg, OutcomeMsg):
            self.outcome = msg
        else:  # stop / gpu_reply travel parent → child only
            msg_text = "bad pipe message"
            raise SchemaViolation(msg_text)

    def send_replies(self) -> None:
        """Send the `gpu_reply` of every finished GPU request (step 7b)."""
        for request_id, future in list(self.pending.items()):
            if future.done():
                del self.pending[request_id]
                self.post(future.result())

    def terminate(self) -> None:
        """Terminate (then kill) the process if alive, join it and close the pipe."""
        if self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(JOIN_TIMEOUT_S)
            if self.proc.is_alive():
                self.proc.kill()
        self.proc.join(JOIN_TIMEOUT_S)
        self.closed = True
        with suppress(OSError):
            self.conn.close()

    def settle(self, verdict: Verdict, stop_reason: StopReason | None = None) -> FinishResult:
        """Stop the child and apply `verdict` (U08-50); a failed finish is retried next tick."""
        self.verdict = self.verdict or verdict
        self.terminate()
        return finish_job(
            self.row,
            self.owner,
            self.verdict,
            attempt_started_at=self.started_at,
            stop_reason=stop_reason or self.stop_sent,
        )

    def reap(self, now: datetime, gpu: GpuSubmit) -> bool:
        """Step 7c: finish an exited (or crashed) child; True when the slot is free again."""
        if not self.crashed and self.proc.is_alive():
            return False
        if not self.crashed:
            self.drain(now, gpu)  # the outcome may have arrived after this tick's drain
        verdict: Verdict = child_crash()
        if not self.crashed and self.outcome is not None:
            verdict = verdict_of(self.outcome)
        self.settle(verdict)
        return True

    def lease_step(self, now: datetime, t: Timings) -> bool:
        """Step 7d: lease heartbeat; 0 rows → `stop("cancel")`, terminate after the grace."""
        if now >= self.hb_due:
            self.hb_due = now + t.heartbeat
            ok = require_jobs_backend().heartbeat_job(self.row.job_id, self.owner, now + t.lease)
            if not ok and self.cancel_at is None:
                self.cancel_at = now
                self.stop("cancel")
        if self.cancel_at is None or now < self.cancel_at + t.cancel_grace:
            return False
        self.settle(errors.ModelUnavailable(NOT_STOPPED))
        return True

    def stall_step(self, now: datetime, stall: timedelta) -> bool:
        """Step 7e: no heartbeat within `stall` → terminate, `ModelUnavailable("stalled")`."""
        if now - self.last_hb <= stall:
            return False
        _log.warning("jobs.job.stalled", job_id=self.row.job_id, owner=self.owner[:_ID_CHARS])
        self.settle(errors.ModelUnavailable(STALLED))
        return True

    def preempt_step(self, now: datetime, deadline: datetime | None, grace: timedelta) -> bool:
        """Step 7f: `stop("preempt")` past the deadline; terminate and yield after the grace."""
        if deadline is not None and now >= deadline and self.preempt_at is None:
            self.preempt_at = now
            self.stop("preempt")
        if self.preempt_at is None or now < self.preempt_at + grace:
            return False
        self.settle(JobOutcome(status="yield"), stop_reason="preempt")
        return True


def child_crash() -> HernessError:
    """`ModelUnavailable("child_crash")`: a child that ended without an outcome (F08-05)."""
    return errors.ModelUnavailable(CHILD_CRASH)


def verdict_of(msg: OutcomeMsg) -> Verdict:
    """The `finish_job` input of an `outcome` message."""
    if msg.status == "error":
        return error_of(msg)
    try:
        return JobOutcome(status=msg.status, result=msg.result)
    except ValidationError:
        return SchemaViolation("job outcome invalid")


def _circuit_open(text: str, extra: dict[str, object]) -> HernessError:
    key, at = extra.get("key"), extra.get("retry_at")
    try:
        retry_at = clock.parse_utc(at)
    except SchemaViolation:
        retry_at = clock.now()
    return CircuitOpen(text, key=key if isinstance(key, str) else "unknown", retry_at=retry_at)


def error_of(msg: OutcomeMsg) -> HernessError:
    """Rebuild the handler error a child reported; an unknown class becomes `FatalError`."""
    cls = _ERRORS.get(msg.error_class or "", FatalError)
    text = msg.error_message or "handler error"
    extra: dict[str, object] = dict(msg.result)
    if cls is CircuitOpen:
        return _circuit_open(text, extra)
    if cls is RateLimited:
        after = extra.get("retry_after")
        plain = isinstance(after, int | float) and not isinstance(after, bool)
        seconds = float(after) if plain and isinstance(after, int | float) else None
        return RateLimited(text, retry_after=seconds)
    try:
        return cls(text)
    except TypeError:  # a subclass with required attributes
        return FatalError(text)


def spawn(slot: str, owner: str, row: JobRow, bootstrap: str, now: datetime) -> ChildRun:
    """Start `child_main` for a claimed row with plain-string arguments (TH08-13)."""
    parent, child = _CTX.Pipe(duplex=True)
    kind: Literal["gpu", "cpu"] = "gpu" if slot == "gpu" else "cpu"
    proc = _CTX.Process(
        target=child_main,
        args=(row.job_id, owner, kind, bootstrap, child),
        name=f"herness-job-{slot}",
    )
    proc.start()
    child.close()
    return ChildRun(slot, owner, row, proc, parent, now, now, now)
