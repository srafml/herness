"""Tests of U08-85 (ChildJobContext, ChildServiceControl): UT08-98, UT08-99, ST08-13 (child
side) and controller-verification tests of the reader thread, reply handling and pipe loss.

A real `multiprocessing.Pipe` pair connects the context to a fake parent thread.
"""

from __future__ import annotations

import contextlib
import pickle  # noqa: TID251 - ST08-13 sends a pickle attack payload; nothing here unpickles
import threading
from collections.abc import Callable, Iterator
from multiprocessing import Pipe
from multiprocessing.connection import Connection
from types import SimpleNamespace
from typing import Any, cast

import pytest
from structlog.testing import capture_logs

from herness.core import time as clock
from herness.core.errors import ConfigError, ModelUnavailable, SchemaViolation
from herness.core.ids import IdKind, new_id
from herness.core.jobs import _context_base as base
from herness.core.jobs import context
from herness.core.jobs.context import ChildJobContext
from herness.core.jobs.pipe import (
    GpuReplyMsg,
    GpuRequestMsg,
    HeartbeatMsg,
    PipeMessage,
    SaveStateMsg,
    StopMsg,
    decode_message,
    encode_message,
)
from herness.core.jobs.ports import JobRow
from herness.core.types import GpuClass

pytestmark = pytest.mark.unit

type Replier = Callable[[GpuRequestMsg], GpuReplyMsg | None]


def _svc(name: str, start: float) -> SimpleNamespace:
    return SimpleNamespace(services={name: SimpleNamespace(start_timeout_s=start)})


GPU = SimpleNamespace(
    stop_timeout_s=120.0,
    warmup_timeout_s=120.0,
    classes={
        "reasoning": _svc("vllm-reasoning", 900.0),
        "decider": _svc("openjev", 300.0),
        "large": _svc("llamacpp-large", 600.0),
    },
)


def make_row(result: dict[str, Any] | None = None) -> JobRow:
    return JobRow(
        job_id=new_id(IdKind.JOB), kind="distill", gpu_class="large", status="running",
        priority=50, attempts=2, max_attempts=5, payload={"k": 1}, result=result,
    )  # fmt: skip


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deterministic redactor stub and fixed GPU settings (no config load needed)."""
    monkeypatch.setattr(base, "redact_text", lambda t: t.replace("sk-SECRET", "[KEY]"))
    monkeypatch.setattr(base, "_gpu", lambda: GPU)


class FakeParent:
    """The supervisor end: records every message, answers `gpu_request`s with `replier`."""

    def __init__(self, conn: Connection, replier: Replier) -> None:
        self.conn = conn
        self.replier = replier
        self.received: list[PipeMessage] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> FakeParent:
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.conn.poll(0.01):
                continue
            msg = decode_message(self.conn.recv_bytes())
            self.received.append(msg)
            if isinstance(msg, GpuRequestMsg) and (reply := self.replier(msg)) is not None:
                self.conn.send_bytes(encode_message(reply))

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2)

    def requests(self) -> list[tuple[str, str | None, str | None, float | None]]:
        return [
            (m.op, m.cls, m.service, m.timeout_s)
            for m in self.received
            if isinstance(m, GpuRequestMsg)
        ]


def ok_reply(previous: GpuClass | None = "decider", healthy: bool | None = None) -> Replier:
    def reply(req: GpuRequestMsg) -> GpuReplyMsg:
        return GpuReplyMsg(
            request_id=req.request_id, ok=True, previous_class=previous, healthy=healthy
        )

    return reply


@pytest.fixture
def pipe_pair() -> Iterator[tuple[Connection, Connection]]:
    parent, child = Pipe(duplex=True)
    yield cast("Connection", parent), cast("Connection", child)
    for conn in (parent, child):
        conn.close()


def child_ctx(child: Connection, slot: str = "gpu", row: JobRow | None = None) -> ChildJobContext:
    return ChildJobContext(child, row or make_row(), slot=slot)  # type: ignore[arg-type]


@pytest.fixture
def gpu_env(pipe_pair: tuple[Connection, Connection]) -> Iterator[Callable[..., Any]]:
    """Build (ctx, parent) on the GPU slot with a replier; both shut down afterwards."""
    made: list[tuple[ChildJobContext, FakeParent]] = []

    def build(replier: Replier | None = None) -> tuple[ChildJobContext, FakeParent]:
        parent_conn, child_conn = pipe_pair
        parent = FakeParent(parent_conn, replier or ok_reply()).start()
        ctx = child_ctx(child_conn)
        made.append((ctx, parent))
        return ctx, parent

    yield build
    for ctx, parent in made:
        parent.stop()
        ctx.close()


def drain(conn: Connection, wait_s: float = 0.2) -> list[PipeMessage]:
    out: list[PipeMessage] = []
    while conn.poll(wait_s):
        out.append(decode_message(conn.recv_bytes()))
        wait_s = 0.05
    return out


def _boom(exc: Exception) -> None:
    raise exc


# --- UT08-98 ---------------------------------------------------------------------------


def test_ut08_98_stop_sets_should_yield_first_reason_wins(
    gpu_env: Callable[..., Any], pipe_pair: tuple[Connection, Connection]
) -> None:
    """UT08-98 `stop` → `should_yield()` true with the reason; a later stop does not win."""
    parent_conn, _ = pipe_pair
    parent_conn.send_bytes(encode_message(StopMsg(reason="preempt")))
    parent_conn.send_bytes(encode_message(StopMsg(reason="cancel")))
    ctx, _parent = gpu_env()
    ctx.require_gpu_class("large", timeout_s=5)  # the reply follows both stops on the pipe
    assert ctx._stop_event.is_set()
    assert ctx.should_yield() is True
    assert ctx.stop_reason == "preempt"


def test_ut08_98_reader_thread_name_and_daemon(
    pipe_pair: tuple[Connection, Connection],
) -> None:
    """UT08-98 the only reader is a daemon thread named `herness-pipe-reader`."""
    ctx = child_ctx(pipe_pair[1])
    try:
        assert ctx._reader.name == "herness-pipe-reader"
        assert ctx._reader.daemon is True
        assert ctx.should_yield() is False
        assert ctx.stop_reason is None
    finally:
        ctx.close()
    assert not ctx._reader.is_alive()


def test_ut08_98_heartbeats_throttled_to_one_per_second(
    pipe_pair: tuple[Connection, Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-98 five heartbeats within one second send one message; the pending note follows."""
    now = [1000.0]
    monkeypatch.setattr(clock, "monotonic", lambda: now[0])
    parent_conn, child_conn = pipe_pair
    ctx = child_ctx(child_conn, slot="cpu")
    try:
        for i in range(5):
            ctx.heartbeat(f"step {i}")
            now[0] += 0.1
        sent = drain(parent_conn)
        assert sent == [HeartbeatMsg(note="step 0")]
        ctx.heartbeat(None)  # still inside the second (t = 0.5 s)
        assert drain(parent_conn, 0.05) == []
        now[0] = 1001.0
        ctx.heartbeat(None)  # the next allowed send carries the latest pending note
        assert drain(parent_conn) == [HeartbeatMsg(note="step 4")]
        now[0] = 1002.5
        ctx.heartbeat()
        assert drain(parent_conn) == [HeartbeatMsg(note=None)]
    finally:
        ctx.close()


def test_ut08_98_heartbeat_note_redacted_and_cut(
    pipe_pair: tuple[Connection, Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-98 the note is redacted and cut to 200 chars; a redaction failure drops it."""
    parent_conn, child_conn = pipe_pair
    ctx = child_ctx(child_conn, slot="cpu")
    now = [0.0]
    monkeypatch.setattr(clock, "monotonic", lambda: now[0])
    try:
        ctx.heartbeat("key sk-SECRET " + "x" * 400)
        (msg,) = drain(parent_conn)
        assert isinstance(msg, HeartbeatMsg)
        assert msg.note is not None
        assert len(msg.note) == 200
        assert msg.note.startswith("key [KEY] x")
        assert "sk-SECRET" not in msg.note
        monkeypatch.setattr(base, "redact_text", lambda t: None)
        now[0] = 5.0
        ctx.heartbeat("anything")
        assert drain(parent_conn) == [HeartbeatMsg(note=None)]
    finally:
        ctx.close()


def test_ut08_98_five_mb_state_rejected(pipe_pair: tuple[Connection, Connection]) -> None:
    """UT08-98 a 5 MB state → SchemaViolation and nothing is sent."""
    parent_conn, child_conn = pipe_pair
    ctx = child_ctx(child_conn, slot="cpu")
    try:
        with pytest.raises(SchemaViolation, match="4 MiB"):
            ctx.save_state({"blob": "x" * 5_000_000})
        assert drain(parent_conn, 0.05) == []
        ctx.save_state({"cursor": 7, "done": ["a"]})
        assert drain(parent_conn) == [SaveStateMsg(state={"cursor": 7, "done": ["a"]})]
    finally:
        ctx.close()


def test_ut08_98_gpu_call_on_cpu_slot_config_error(
    pipe_pair: tuple[Connection, Connection],
) -> None:
    """UT08-98 every GPU call on the CPU slot → ConfigError, nothing sent."""
    parent_conn, child_conn = pipe_pair
    ctx = child_ctx(child_conn, slot="cpu")
    calls: list[Callable[[], object]] = [
        lambda: ctx.require_gpu_class("large"),
        lambda: ctx.gpu_scope("large").__enter__(),
        lambda: ctx.services.start("openjev"),
        lambda: ctx.services.stop("openjev"),
        lambda: ctx.services.healthy("openjev"),
    ]
    try:
        for call in calls:
            with pytest.raises(ConfigError, match="GPU control requires the GPU slot"):
                call()
        assert drain(parent_conn, 0.05) == []
    finally:
        ctx.close()


# --- UT08-99 ---------------------------------------------------------------------------


def test_ut08_99_gpu_scope_restores_on_normal_exit(gpu_env: Callable[..., Any]) -> None:
    """UT08-99 `gpu_scope("large")` with previous `decider` → restore request for `decider`."""
    ctx, parent = gpu_env(ok_reply("decider"))
    with ctx.gpu_scope("large"):
        assert parent.requests() == [("require_class", "large", None, None)]
    assert parent.requests() == [
        ("require_class", "large", None, None),
        ("require_class", "decider", None, None),
    ]


def test_ut08_99_gpu_scope_restores_on_exception(gpu_env: Callable[..., Any]) -> None:
    """UT08-99 the scope body raising → restore to `decider`; the original exception propagates."""
    ctx, parent = gpu_env(ok_reply("decider"))
    with pytest.raises(RuntimeError, match="handler boom"), ctx.gpu_scope("large"):
        _boom(RuntimeError("handler boom"))
    assert [r[1] for r in parent.requests()] == ["large", "decider"]


def test_ut08_99_restore_failure_logged_original_propagates(
    gpu_env: Callable[..., Any],
) -> None:
    """UT08-99 a failing restore during exception exit logs ERROR `jobs.gpu.restore_failed`."""

    def reply(req: GpuRequestMsg) -> GpuReplyMsg:
        ok = req.cls == "large"
        return GpuReplyMsg(
            request_id=req.request_id, ok=ok, previous_class="decider" if ok else None,
            error_class=None if ok else "ModelUnavailable", message=None if ok else "swap failed",
        )  # fmt: skip

    ctx, _parent = gpu_env(reply)
    with capture_logs() as logs, pytest.raises(KeyError), ctx.gpu_scope("large"):
        _boom(KeyError("original"))
    (entry,) = [e for e in logs if e["event"] == "jobs.gpu.restore_failed"]
    assert entry["log_level"] == "error"
    assert entry["class"] == "decider"
    assert entry["error_type"] == "ModelUnavailable"
    assert entry["job_id"] == ctx.job_id


def test_ut08_99_restore_failure_on_normal_exit_raises(gpu_env: Callable[..., Any]) -> None:
    """UT08-99 a failing restore after a normal body raises the reply's error class."""

    def reply(req: GpuRequestMsg) -> GpuReplyMsg:
        if req.cls == "large":
            return GpuReplyMsg(request_id=req.request_id, ok=True, previous_class="reasoning")
        return GpuReplyMsg(request_id=req.request_id, ok=False, error_class="ConfigError")

    ctx, _parent = gpu_env(reply)
    with pytest.raises(ConfigError, match="gpu request failed"), ctx.gpu_scope("large"):
        pass


@pytest.mark.parametrize("previous", ["large", None])
def test_ut08_99_no_restore_when_previous_equals_class(
    gpu_env: Callable[..., Any], previous: GpuClass | None
) -> None:
    """UT08-99 previous class equal to the scope class (or unknown) → no restore request."""
    ctx, parent = gpu_env(ok_reply(previous))
    with ctx.gpu_scope("large"):
        pass
    assert parent.requests() == [("require_class", "large", None, None)]


# --- reply handling, services, timeouts ------------------------------------------------


def test_cv_t08_20_failed_reply_raises_named_class(gpu_env: Callable[..., Any]) -> None:
    """CV-T08-20 `ok == False` raises the named class with the message."""

    def reply(req: GpuRequestMsg) -> GpuReplyMsg:
        err = "ConfigError" if req.cls == "decider" else "ModelUnavailable"
        return GpuReplyMsg(request_id=req.request_id, ok=False, error_class=err, message="nope")  # type: ignore[arg-type]

    ctx, _parent = gpu_env(reply)
    with pytest.raises(ModelUnavailable, match="nope"):
        ctx.require_gpu_class("large", timeout_s=5)
    with pytest.raises(ConfigError, match="nope"):
        ctx.require_gpu_class("decider", timeout_s=5)


def test_cv_t08_20_services_send_requests(gpu_env: Callable[..., Any]) -> None:
    """CV-T08-20 services.start/stop/healthy send their op; `healthy` returns the reply's value."""
    ctx, parent = gpu_env(ok_reply(None, healthy=True))
    ctx.services.start("openjev", timeout_s=30.0)
    ctx.services.stop("openjev")
    assert ctx.services.healthy("openjev") is True
    assert parent.requests() == [
        ("service_start", None, "openjev", 30.0),
        ("service_stop", None, "openjev", None),
        ("service_healthy", None, "openjev", None),
    ]


def test_cv_t08_20_healthy_none_is_false(gpu_env: Callable[..., Any]) -> None:
    """CV-T08-20 a reply without `healthy` reads as not healthy."""
    ctx, _parent = gpu_env(ok_reply(None, healthy=None))
    assert ctx.services.healthy("vllm-reasoning") is False


def test_cv_t08_20_no_reply_times_out_and_late_reply_dropped(
    gpu_env: Callable[..., Any], pipe_pair: tuple[Connection, Connection]
) -> None:
    """CV-T08-20 no reply → ModelUnavailable("gpu request timed out"); a late reply is ignored."""
    held: list[GpuRequestMsg] = []
    answer = threading.Event()

    def reply_when_asked(req: GpuRequestMsg) -> GpuReplyMsg | None:
        held.append(req)
        return ok_reply("none")(req) if answer.is_set() else None

    ctx, _parent = gpu_env(reply_when_asked)
    with pytest.raises(ModelUnavailable, match="gpu request timed out"):
        ctx.require_gpu_class("large", timeout_s=0.05)
    assert ctx._replies == {}
    late = GpuReplyMsg(request_id=held[0].request_id, ok=True, previous_class="none")
    with capture_logs() as logs:
        pipe_pair[0].send_bytes(encode_message(late))
        answer.set()
        ctx.require_gpu_class("decider", timeout_s=5)  # its reply follows the late one
    assert [e["event"] for e in logs if e["event"].startswith("jobs.pipe")] == [
        "jobs.pipe.late_reply"
    ]
    assert ctx.should_yield() is False


def test_cv_t08_20_default_waits(
    pipe_pair: tuple[Connection, Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T08-20 default wait = stop + 60 + max start of the class + warm-up + 60."""
    assert base.start_s(cls="large") == 600.0
    assert base.start_s(cls="none") == 0.0
    assert base.start_s(service="openjev") == 300.0
    assert base.default_wait_s(600.0) == 120 + 60 + 600 + 120 + 60
    waits: list[float] = []

    def fake_wait(start: float) -> float:
        waits.append(start)
        return 0.01

    monkeypatch.setattr(context, "default_wait_s", fake_wait)
    ctx = child_ctx(pipe_pair[1])
    try:
        calls: list[Callable[[], object]] = [
            lambda: ctx.require_gpu_class("reasoning"),
            lambda: ctx.services.start("openjev"),
            lambda: ctx.services.start("openjev", timeout_s=42.0),
        ]
        for call in calls:
            with pytest.raises(ModelUnavailable, match="timed out"):
                call()
    finally:
        ctx.close()
    assert waits == [900.0, 300.0, 42.0]


# --- pipe loss and hostile parent messages ---------------------------------------------

_TRIGGERED: list[str] = []


class _Evil:
    """Unpickling this object would record a flag (it must never happen)."""

    def __reduce__(self) -> tuple[object, tuple[str]]:
        return (_TRIGGERED.append, ("unpickled",))


def _pending_call(ctx: ChildJobContext) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            ctx.require_gpu_class("large", timeout_s=30)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


def _wait_registered(ctx: ChildJobContext) -> None:
    for _ in range(400):
        if ctx._replies:
            return
        threading.Event().wait(0.005)
    pytest.fail("request never registered")


@pytest.mark.parametrize(
    "hostile",
    [
        pickle.dumps(_Evil()),
        b'{"type":"stop","reason":"cancel","extra":1}',
        encode_message(HeartbeatMsg(note="wrong direction")),
    ],
    ids=["pickle", "extra_field", "wrong_direction"],
)
def test_st08_13_hostile_parent_message_is_fatal_not_executed(
    pipe_pair: tuple[Connection, Connection], hostile: bytes
) -> None:
    """ST08-13 a bad message on the pipe is never unpickled; waiting calls fail, not hang."""
    parent_conn, child_conn = pipe_pair
    ctx = child_ctx(child_conn)
    try:
        with capture_logs() as logs:
            thread, errors = _pending_call(ctx)
            _wait_registered(ctx)
            parent_conn.send_bytes(hostile)
            thread.join(5)
            ctx._reader.join(5)
        assert not thread.is_alive()
        assert [type(e) for e in errors] == [ModelUnavailable]
        assert errors[0].args[0] == "supervisor pipe closed"
        assert _TRIGGERED == []
        assert ctx.should_yield() is True
        assert ctx.stop_reason == "shutdown"
        (entry,) = [e for e in logs if e["event"] == "jobs.pipe.lost"]
        assert entry == {
            "event": "jobs.pipe.lost", "log_level": "error", "component": "jobs",
            "job_id": ctx.job_id, "reason": "bad_message",
        }  # fmt: skip
        with pytest.raises(ModelUnavailable, match="supervisor pipe closed"):
            ctx.require_gpu_class("large", timeout_s=30)  # fails at once once the pipe is dead
    finally:
        ctx.close()


def test_cv_t08_20_parent_gone_fails_waiters_and_writes(
    pipe_pair: tuple[Connection, Connection],
) -> None:
    """CV-T08-20 EOF from the parent: the waiting call fails, later writes fail, reason kept."""
    parent_conn, child_conn = pipe_pair
    parent_conn.send_bytes(encode_message(StopMsg(reason="cancel")))
    ctx = child_ctx(child_conn)
    try:
        assert ctx._stop_event.wait(5)
        thread, errors = _pending_call(ctx)
        _wait_registered(ctx)
        with capture_logs() as logs:
            parent_conn.close()
            thread.join(5)
            ctx._reader.join(5)
        assert [str(e) for e in errors] == ["supervisor pipe closed"]
        assert ctx.stop_reason == "cancel"  # a stop that arrived first keeps its reason
        assert [e["reason"] for e in logs if e["event"] == "jobs.pipe.lost"] == ["closed"]
        with pytest.raises(ModelUnavailable, match="supervisor pipe closed"):
            ctx.heartbeat("after parent exit")
    finally:
        ctx.close()


def test_cv_t08_20_oversized_frame_is_fatal(pipe_pair: tuple[Connection, Connection]) -> None:
    """CV-T08-20 a frame over 8 MiB is refused by `recv_bytes(maxlength)` (TH08-10)."""
    parent_conn, child_conn = pipe_pair
    ctx = child_ctx(child_conn)
    try:

        def send_big() -> None:
            with contextlib.suppress(OSError):  # the child side stops reading
                parent_conn.send_bytes(b"x" * 9_000_000)

        sender = threading.Thread(target=send_big, daemon=True)
        sender.start()
        ctx._reader.join(10)
        assert not ctx._reader.is_alive()
        assert ctx.stop_reason == "shutdown"
        assert ctx._dead is True
    finally:
        ctx.close()


def test_cv_t08_20_close_is_quiet(pipe_pair: tuple[Connection, Connection]) -> None:
    """CV-T08-20 our own close does not report a lost pipe."""
    ctx = child_ctx(pipe_pair[1])
    with capture_logs() as logs:
        ctx.close()
        ctx._pipe_lost("closed")
    assert logs == []
    assert ctx.should_yield() is False


# --- row shortcuts and load_state ------------------------------------------------------


def test_cv_t08_20_row_shortcuts_and_load_state(
    pipe_pair: tuple[Connection, Connection],
) -> None:
    """CV-T08-20 `job` is the row (R-42); `load_state` returns a copy of `result.state`."""
    row = make_row({"state": {"cursor": [1, 2]}, "other": 1})
    ctx = child_ctx(pipe_pair[1], slot="cpu", row=row)
    try:
        assert ctx.job is row
        assert (ctx.job_id, ctx.kind, ctx.attempt) == (row.job_id, "distill", 2)
        state = ctx.load_state()
        assert state == {"cursor": [1, 2]}
        state["cursor"].append(3)  # type: ignore[union-attr]
        assert ctx.load_state() == {"cursor": [1, 2]}
    finally:
        ctx.close()


@pytest.mark.parametrize("result", [None, {}, {"state": "not-a-dict"}])
def test_cv_t08_20_load_state_empty(
    pipe_pair: tuple[Connection, Connection], result: dict[str, Any] | None
) -> None:
    """CV-T08-20 first attempt (no result or no state) → empty dict."""
    ctx = child_ctx(pipe_pair[1], slot="cpu", row=make_row(result))
    try:
        assert ctx.load_state() == {}
    finally:
        ctx.close()
