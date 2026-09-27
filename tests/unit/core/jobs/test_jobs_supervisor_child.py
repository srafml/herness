"""Unit tests of the supervisor's per-child bookkeeping (U08-87 steps 7b-7f, T08-21):
`ChildRun` on a real pipe with a stand-in process, and the error rebuild of `outcome`."""

from __future__ import annotations

import multiprocessing
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.unit.core.jobs._supervisor_env import SupEnv

from herness.core import errors
from herness.core import time as clock
from herness.core.jobs import _supervisor_child as sc
from herness.core.jobs._supervisor_boot import timings
from herness.core.jobs._supervisor_child import ChildRun, error_of, verdict_of
from herness.core.jobs.pipe import (
    GpuRequestMsg,
    HeartbeatMsg,
    OutcomeMsg,
    SaveStateMsg,
    StopMsg,
    decode_message,
    encode_message,
)
from herness.core.jobs.ports import require_jobs_backend
from herness.core.jobs.queue import cancel, claim
from herness.core.types import JobOutcome

pytestmark = pytest.mark.unit

OWNER = "h:1:cpu0"


class Proc:
    """A process stand-in whose liveness the test sets."""

    def __init__(self, *, stubborn: bool = False) -> None:
        self.alive, self.stubborn = True, stubborn
        self.terminated = self.killed = False
        self.exitcode: int | None = None

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated = True
        if not self.stubborn:
            self.alive, self.exitcode = False, -15

    def kill(self) -> None:
        self.killed, self.alive, self.exitcode = True, False, -9

    def join(self, timeout: float | None = None) -> None:
        return None


def _run(env: SupEnv, mode: str = "done", **proc: Any) -> tuple[ChildRun, Any]:
    job_id = env.enqueue(mode)
    row = claim(owner=OWNER, allowed_classes=["none"])
    assert row is not None
    assert row.job_id == job_id
    parent, child = multiprocessing.Pipe()
    now = clock.now()
    return ChildRun("cpu0", OWNER, row, Proc(**proc), parent, now, now, now), child  # type: ignore[arg-type]


def _no_gpu(run: ChildRun, msg: GpuRequestMsg) -> None:  # pragma: no cover - never called
    raise AssertionError


def test_cv_t08_21_drain_heartbeat_state_and_outcome(sup_env: SupEnv) -> None:
    """CV-T08-21 step 7b: heartbeats set `last_hb` and the note (a note-less one keeps the
    last), `save_state` is written with the owner guard, `outcome` is kept and ends reading."""
    run, child = _run(sup_env)
    later = clock.now() + timedelta(seconds=3)
    for msg in (HeartbeatMsg(note="a"), HeartbeatMsg(), SaveStateMsg(state={"n": 2})):
        child.send_bytes(encode_message(msg))
    child.send_bytes(encode_message(OutcomeMsg(status="done", result={"x": 1})))
    child.send_bytes(encode_message(HeartbeatMsg(note="after")))
    run.drain(later, _no_gpu)
    assert (run.last_hb, run.note) == (later, "a")
    assert require_jobs_backend().load_job_state(run.row.job_id) == {"n": 2}
    assert run.outcome is not None
    assert run.outcome.result == {"x": 1}
    assert run.conn.poll(0)  # the message after the outcome is left unread
    run.proc.alive = False  # type: ignore[attr-defined]
    assert run.reap(later, _no_gpu) is True
    assert sup_env.job(run.row.job_id).status == "done"


def test_cv_t08_21_drain_eof_is_not_a_crash(sup_env: SupEnv) -> None:
    """CV-T08-21 a closed child end is end of input, not a crash; the exit is reaped as
    `child_crash` only because no outcome came."""
    run, child = _run(sup_env)
    child.close()
    run.drain(clock.now(), _no_gpu)
    assert run.closed
    assert not run.crashed
    assert run.reap(clock.now(), _no_gpu) is False  # still alive
    run.proc.alive = False  # type: ignore[attr-defined]
    assert run.reap(clock.now(), _no_gpu) is True
    row = sup_env.job(run.row.job_id)
    assert (row.status, row.last_error["message"]) == ("queued", "child_crash")  # type: ignore[index]


def test_cv_t08_21_replies_and_stops_reach_the_child(sup_env: SupEnv) -> None:
    """CV-T08-21 finished GPU futures are sent as `gpu_reply`; each stop reason is sent once
    and the first one sent is the run's stop reason; a closed pipe is ignored."""
    from concurrent.futures import Future  # noqa: PLC0415

    from herness.core.jobs.pipe import GpuReplyMsg  # noqa: PLC0415

    run, child = _run(sup_env)
    done: Future[GpuReplyMsg] = Future()
    waiting: Future[GpuReplyMsg] = Future()
    rid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    done.set_result(GpuReplyMsg(request_id=rid, ok=True))
    run.pending = {rid: done, "other": waiting}
    run.send_replies()
    assert list(run.pending) == ["other"]
    assert decode_message(child.recv_bytes()) == GpuReplyMsg(request_id=rid, ok=True)
    run.stop("preempt")
    run.stop("preempt")
    run.stop("cancel")
    assert decode_message(child.recv_bytes()) == StopMsg(reason="preempt")
    assert decode_message(child.recv_bytes()) == StopMsg(reason="cancel")
    assert not child.poll(0.05)
    assert run.stop_sent == "preempt"
    run.conn.close()
    run.post(StopMsg(reason="shutdown"))  # OSError on a closed pipe is swallowed


def test_cv_t08_21_terminate_kills_a_stubborn_child(sup_env: SupEnv) -> None:
    """CV-T08-21 `terminate()` falls back to `kill()` when the process survives it."""
    run, _child = _run(sup_env, stubborn=True)
    run.terminate()
    proc = run.proc
    assert proc.terminated
    assert proc.killed  # type: ignore[attr-defined]
    assert run.closed


def test_cv_t08_21_lease_lost_cancel_then_terminate(sup_env: SupEnv) -> None:
    """CV-T08-21 step 7d: heartbeats extend the lease; once the job is canceled the heartbeat
    changes 0 rows, `stop("cancel")` is sent once, and after `cancel_grace_s` the child is
    terminated and the canceled job finalised."""
    run, child = _run(sup_env)
    t = timings()
    now = clock.now()
    assert run.lease_step(now, t) is False
    assert run.hb_due == now + t.heartbeat
    assert run.lease_step(now, t) is False  # not due again yet
    assert cancel(run.row.job_id) == "cancel_requested"
    later = now + t.heartbeat
    assert run.lease_step(later, t) is False
    assert run.cancel_at == later
    assert decode_message(child.recv_bytes()) == StopMsg(reason="cancel")
    assert run.lease_step(later + t.cancel_grace, t) is True
    assert run.proc.terminated  # type: ignore[attr-defined]
    row = sup_env.job(run.row.job_id)
    assert row.status == "canceled"
    assert row.finished_at is not None


def test_cv_t08_21_stall_terminates_and_requeues(sup_env: SupEnv) -> None:
    """CV-T08-21 step 7e: no heartbeat within the stall timeout → terminate,
    `ModelUnavailable("stalled")`, requeued with backoff."""
    run, _child = _run(sup_env)
    stall = timedelta(seconds=3)
    assert run.stall_step(run.last_hb + stall, stall) is False
    assert run.stall_step(run.last_hb + stall + timedelta(microseconds=1), stall) is True
    row = sup_env.job(run.row.job_id)
    assert (row.status, row.last_error["message"]) == ("queued", "stalled")  # type: ignore[index]


def test_cv_t08_21_preempt_stop_then_yield_after_grace(sup_env: SupEnv) -> None:
    """CV-T08-21 step 7f: past the deadline `stop("preempt")` is sent once; after
    `preempt_grace_min` the child is terminated and the job yields without charge."""
    run, child = _run(sup_env)
    now, grace = clock.now(), timedelta(minutes=15)
    assert run.preempt_step(now, None, grace) is False
    assert run.preempt_step(now, now + timedelta(seconds=1), grace) is False
    assert run.preempt_at is None
    assert run.preempt_step(now, now, grace) is False
    assert decode_message(child.recv_bytes()) == StopMsg(reason="preempt")
    assert run.preempt_step(now + timedelta(minutes=1), now, grace) is False
    assert run.preempt_step(now + grace, now, grace) is True
    row = sup_env.job(run.row.job_id)
    assert (row.status, row.attempts) == ("queued", 0)


def test_cv_t08_21_settle_failure_keeps_the_verdict(
    sup_env: SupEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T08-21 a finish that fails (store busy) keeps the first verdict for the retry at
    the next tick, so a stalled child is not re-classified as a crash."""
    run, _child = _run(sup_env)
    calls: list[Any] = []

    def busy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[2])
        msg = "locked"
        raise errors.StoreBusy(msg)

    monkeypatch.setattr(sc, "finish_job", busy)
    with pytest.raises(errors.StoreBusy):
        run.stall_step(run.last_hb + timedelta(hours=1), timedelta(seconds=1))
    monkeypatch.undo()
    assert run.reap(clock.now(), _no_gpu) is True
    assert str(calls[0]) == "stalled"
    assert sup_env.job(run.row.job_id).last_error["message"] == "stalled"  # type: ignore[index]


def test_cv_t08_21_verdict_and_error_rebuild() -> None:
    """CV-T08-21 `outcome` → `finish_job` input: done/yield outcomes, an invalid result
    (`SchemaViolation`), and each reported error class rebuilt with its attributes; an
    unknown class becomes `FatalError`."""
    assert verdict_of(OutcomeMsg(status="yield", result={})) == JobOutcome(status="yield")
    big = OutcomeMsg(status="done", result={"x": "y" * 1_100_000})
    assert isinstance(verdict_of(big), errors.SchemaViolation)

    def err(cls: str, result: dict[str, Any] | None = None) -> errors.HernessError:
        msg = OutcomeMsg(status="error", result=result or {}, error_class=cls, error_message="m")
        out = verdict_of(msg)
        assert isinstance(out, errors.HernessError)
        return out

    assert type(err("SourceUnavailable")) is errors.SourceUnavailable
    assert type(err("Nope")) is errors.FatalError
    assert type(error_of(OutcomeMsg(status="error", result={}))) is errors.FatalError
    at = datetime(2026, 9, 1, tzinfo=UTC)
    circuit = err("CircuitOpen", {"key": "model:x", "retry_at": clock.format_utc(at)})
    assert isinstance(circuit, errors.CircuitOpen)
    assert (circuit.key, circuit.retry_at) == ("model:x", at)
    loose = err("CircuitOpen", {"retry_at": "garbage"})
    assert isinstance(loose, errors.CircuitOpen)
    assert loose.key == "unknown"
    limited = err("RateLimited", {"retry_after": 12})
    assert isinstance(limited, errors.RateLimited)
    assert limited.retry_after == 12.0
    plain = err("RateLimited", {"retry_after": True})
    assert isinstance(plain, errors.RateLimited)
    assert plain.retry_after is None


def test_cv_t08_21_error_class_with_required_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    """CV-T08-21 a reported class that cannot be built from a message alone → `FatalError`."""

    class Needs(errors.HernessError):
        def __init__(self, message: str, *, extra: str) -> None:
            super().__init__(message)

    monkeypatch.setitem(sc._ERRORS, "Needs", Needs)
    out = error_of(OutcomeMsg(status="error", result={}, error_class="Needs", error_message="m"))
    assert type(out) is errors.FatalError
