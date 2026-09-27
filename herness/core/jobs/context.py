"""`JobContext` implementations: child over the job pipe and inline (impl 08 U08-85, U08-86).

`ChildJobContext` runs inside a job child process; every effect goes to the supervisor as a JSON
pipe message (design 08 §5.8 last paragraph, §5.9; TH08-13). A daemon reader thread
(`herness-pipe-reader`) is the only reader of the connection and polls with a timeout, so
`close()` stops it. Pipe loss (EOF, `OSError`, an oversized frame) or a message that fails
validation or has the wrong direction is fatal for the pipe: nothing is executed, the reader
logs ERROR `jobs.pipe.lost` (ids only) and stops, waiting GPU calls fail at once with
`ModelUnavailable("supervisor pipe closed")` instead of hanging, and `should_yield()` becomes
true with reason `shutdown` (unless a `stop` arrived first) so the handler winds down.
`InlineJobContext` makes the same calls directly on a `GpuController` and the jobs backend.
"""

from __future__ import annotations

import json
import queue
import threading
from contextlib import suppress
from typing import TYPE_CHECKING, Final, Literal

from herness.core import time as clock
from herness.core.errors import ConfigError, JobStateError, ModelUnavailable, SchemaViolation
from herness.core.ids import new_ulid
from herness.core.jobs._context_base import (
    NO_GPU_SLOT,
    ContextBase,
    clean_note,
    default_wait_s,
    start_s,
    state_json,
)
from herness.core.jobs.pipe import (
    MAX_PIPE_MSG_BYTES,
    GpuOp,
    GpuReplyMsg,
    GpuRequestMsg,
    HeartbeatMsg,
    PipeMessage,
    SaveStateMsg,
    StopMsg,
    decode_message,
    encode_message,
)
from herness.core.jobs.ports import require_jobs_backend
from herness.core.logging import get_logger

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

    from pydantic import JsonValue

    from herness.core.jobs.gpu import GpuController
    from herness.core.jobs.ports import (
        JobContext,
        JobRow,
        JobsBackend,
        ServiceControl,
        StopReason,
    )
    from herness.core.types import GpuClass, ServiceName

__all__ = ["ChildJobContext", "ChildServiceControl", "InlineJobContext", "InlineServiceControl"]

HEARTBEAT_MIN_INTERVAL_S: Final = 1.0  # at most one heartbeat message per second
READER_POLL_S: Final = 0.2  # reader poll slice; `close()` is noticed within this time
PIPE_CLOSED: Final = "supervisor pipe closed"
GPU_TIMED_OUT: Final = "gpu request timed out"

_log = get_logger("jobs")


class ChildServiceControl:
    """`ServiceControl` of a child: each call is one `gpu_request` to the supervisor."""

    def __init__(self, ctx: ChildJobContext) -> None:
        self._ctx = ctx

    def start(self, name: ServiceName, *, timeout_s: float | None = None) -> None:
        self._ctx._service_call("service_start", name, timeout_s)

    def stop(self, name: ServiceName) -> None:
        self._ctx._service_call("service_stop", name, None)

    def healthy(self, name: ServiceName) -> bool:
        return self._ctx._service_call("service_healthy", name, None).healthy is True


def _raise_failed(reply: GpuReplyMsg) -> None:
    if reply.ok:
        return
    text = reply.message or "gpu request failed"
    if reply.error_class == "ConfigError":
        raise ConfigError(text)
    raise ModelUnavailable(text)


class ChildJobContext(ContextBase):
    """`JobContext` inside a child process; effects go through `conn` (U08-85)."""

    def __init__(self, conn: Connection, row: JobRow, *, slot: Literal["gpu", "cpu"]) -> None:
        super().__init__(row)
        self._conn = conn
        self._slot = slot
        self._write_lock = threading.Lock()
        self._replies: dict[str, queue.Queue[GpuReplyMsg | None]] = {}
        self._dead = False
        self._closing = threading.Event()
        self._last_hb: float | None = None
        self._pending_note: str | None = None
        self._services = ChildServiceControl(self)
        self._reader = threading.Thread(
            target=self._read_loop, name="herness-pipe-reader", daemon=True
        )
        self._reader.start()

    @property
    def services(self) -> ChildServiceControl:
        return self._services

    def close(self, timeout_s: float = 1.0) -> None:
        """Stop the reader (it polls, so it ends within `READER_POLL_S`); later GPU calls fail."""
        self._closing.set()
        self._reader.join(timeout_s)
        self._fail_waiters()

    def _read_loop(self) -> None:
        try:
            while not self._closing.is_set():
                if self._conn.poll(READER_POLL_S):
                    self._dispatch(decode_message(self._conn.recv_bytes(MAX_PIPE_MSG_BYTES)))
        except (EOFError, OSError):
            self._pipe_lost("closed")
        except SchemaViolation:
            self._pipe_lost("bad_message")

    def _dispatch(self, msg: PipeMessage) -> None:
        if isinstance(msg, StopMsg):
            self._set_stop(msg.reason)
            return
        if not isinstance(msg, GpuReplyMsg):  # a child → parent type from the parent
            msg_text = "bad pipe message"
            raise SchemaViolation(msg_text)
        with self._lock:
            box = self._replies.get(msg.request_id)
        if box is None:  # the caller timed out already
            _log.debug("jobs.pipe.late_reply", job_id=self.job_id)
            return
        with suppress(queue.Full):
            box.put_nowait(msg)

    def _pipe_lost(self, reason: str) -> None:
        if self._closing.is_set() and reason == "closed":
            return  # our own shutdown closed the connection
        _log.error("jobs.pipe.lost", job_id=self.job_id, reason=reason)
        self._fail_waiters()
        self._set_stop("shutdown")

    def _fail_waiters(self) -> None:
        """No reader any more: wake every waiting GPU call and refuse new ones."""
        with self._lock:
            self._dead = True
            boxes = list(self._replies.values())
        for box in boxes:
            with suppress(queue.Full):
                box.put_nowait(None)

    def _send(self, msg: PipeMessage) -> None:
        data = encode_message(msg)
        with self._write_lock, suppress(OSError):
            self._conn.send_bytes(data)
            return
        raise ModelUnavailable(PIPE_CLOSED)

    def heartbeat(self, note: str | None = None) -> None:
        """At most one message per second; a throttled note waits for the next send."""
        clean = clean_note(note)
        now = clock.monotonic()
        with self._lock:
            if clean is not None:
                self._pending_note = clean
            if self._last_hb is not None and now - self._last_hb < HEARTBEAT_MIN_INTERVAL_S:
                return
            self._last_hb = now
            out, self._pending_note = self._pending_note, None
        self._send(HeartbeatMsg(note=out))

    def save_state(self, state: dict[str, JsonValue]) -> None:
        """Send the checkpoint (canonical JSON ≤ 4 MiB, else `SchemaViolation`)."""
        self._send(SaveStateMsg(state=json.loads(state_json(state))))

    def _gpu_call(self, request: GpuRequestMsg, wait_s: float) -> GpuReplyMsg:
        box: queue.Queue[GpuReplyMsg | None] = queue.Queue(maxsize=1)
        with self._lock:
            dead = self._dead
            if not dead:
                self._replies[request.request_id] = box
        if dead:
            raise ModelUnavailable(PIPE_CLOSED)
        try:
            self._send(request)
            reply = box.get(timeout=wait_s)
        except queue.Empty:
            raise ModelUnavailable(GPU_TIMED_OUT) from None
        finally:
            with self._lock:
                self._replies.pop(request.request_id, None)
        if reply is None:
            raise ModelUnavailable(PIPE_CLOSED)
        _raise_failed(reply)
        return reply

    def _check_slot(self) -> None:
        if self._slot != "gpu":
            raise ConfigError(NO_GPU_SLOT)

    def _require(self, cls: GpuClass, timeout_s: float | None) -> GpuClass | None:
        self._check_slot()
        wait_s = timeout_s if timeout_s is not None else default_wait_s(start_s(cls=cls))
        request = GpuRequestMsg(
            request_id=new_ulid(), op="require_class", cls=cls, timeout_s=timeout_s
        )
        return self._gpu_call(request, wait_s).previous_class

    def _service_call(self, op: GpuOp, name: ServiceName, timeout_s: float | None) -> GpuReplyMsg:
        """One `service_*` request; same slot check and reply handling as GPU swaps."""
        self._check_slot()
        wait_s = default_wait_s(timeout_s or start_s(service=name))
        request = GpuRequestMsg(request_id=new_ulid(), op=op, service=name, timeout_s=timeout_s)
        return self._gpu_call(request, wait_s)


def _need(controller: GpuController | None) -> GpuController:
    if controller is None:
        raise ConfigError(NO_GPU_SLOT)
    return controller


class InlineServiceControl:
    """`ServiceControl` of an inline run: direct `GpuController.service_*` calls."""

    def __init__(self, controller: GpuController | None) -> None:
        self._controller = controller

    def start(self, name: ServiceName, *, timeout_s: float | None = None) -> None:
        _need(self._controller).service_start(name, timeout_s=timeout_s)

    def stop(self, name: ServiceName) -> None:
        _need(self._controller).service_stop(name)

    def healthy(self, name: ServiceName) -> bool:
        return _need(self._controller).service_healthy(name)


class InlineJobContext(ContextBase):
    """`JobContext` of `run_inline` (U08-86); `controller is None` → GPU calls `ConfigError`."""

    def __init__(
        self,
        row: JobRow,
        *,
        owner: str,
        controller: GpuController | None,
        backend: JobsBackend | None = None,
    ) -> None:
        super().__init__(row)
        self._owner = owner
        self._controller = controller
        self._backend = backend
        self._note: str | None = None
        self._services = InlineServiceControl(controller)

    @property
    def services(self) -> InlineServiceControl:
        return self._services

    @property
    def note(self) -> str | None:
        """The last heartbeat note (kept in memory; inline runs have no worker row)."""
        return self._note

    def request_stop(self, reason: StopReason) -> None:
        """Set by the inline SIGINT handler or heartbeat thread; the first reason wins."""
        self._set_stop(reason)

    def heartbeat(self, note: str | None = None) -> None:
        clean = clean_note(note)
        if clean is not None:
            self._note = clean

    def _require(self, cls: GpuClass, timeout_s: float | None) -> GpuClass | None:
        # `timeout_s` is not used inline: the controller applies its configured timeouts.
        controller = _need(self._controller)
        previous = controller.loaded
        controller.swap(cls, reason="in_job")
        return previous

    def save_state(self, state: dict[str, JsonValue]) -> None:
        """`save_job_state` with canonical JSON ≤ 4 MiB; 0 rows → `JobStateError`."""
        data = state_json(state)
        backend = self._backend if self._backend is not None else require_jobs_backend()
        if not backend.save_job_state(self.job_id, self._owner, data):
            msg = "job state not saved"
            raise JobStateError(msg, job_id=self.job_id)


if TYPE_CHECKING:  # U08-04: both classes satisfy the protocols structurally (checked by mypy)

    def _conforms(
        child: ChildJobContext, inline: InlineJobContext
    ) -> tuple[JobContext, JobContext, ServiceControl, ServiceControl]:
        return child, inline, child.services, inline.services
