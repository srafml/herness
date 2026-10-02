"""Shared fixture of the supervisor unit tests (T08-21): `sup_env`.

A migrated ops store, the fast worker config and the test bootstrap in this process, with
the spawn context of `_supervisor_child` replaced by `FakeCtx`: a "child" is a thread that
runs a script against the parent's pipe end, so no process is started (unit marker). The
pipe itself is a real `multiprocessing.Pipe`, so framing and `decode_message` are real.
"""

from __future__ import annotations

import multiprocessing
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.support.worker_bootstrap import (
    BOOTSTRAP,
    ENV_CONFIG,
    ENV_DB,
    bootstrap,
    write_worker_config,
)

from herness.core import config as c
from herness.core import time as clock
from herness.core.jobs import _supervisor_child
from herness.core.jobs.pipe import (
    HeartbeatMsg,
    OutcomeMsg,
    PipeMessage,
    decode_message,
    encode_message,
)
from herness.core.jobs.ports import JobRow, require_jobs_backend
from herness.core.jobs.queue import enqueue
from herness.core.jobs.supervisor import Supervisor, WorkerOptions
from herness.core.resilience import ProcessState
from herness.core.types import GpuClass

type Script = Callable[["FakeChild"], int | None]


class FakeChild:
    """What a script sees: the child pipe end, the spawn arguments and helpers."""

    def __init__(self, proc: FakeProc, conn: Any, args: tuple[Any, ...]) -> None:
        self.proc, self.conn = proc, conn
        self.job_id, self.owner, self.slot, self.bootstrap = args[:4]

    def send(self, msg: PipeMessage) -> None:
        self.conn.send_bytes(encode_message(msg))

    def raw(self, data: bytes) -> None:
        self.conn.send_bytes(data)

    def recv(self, timeout: float = 30.0) -> PipeMessage | None:
        if not self.conn.poll(timeout):
            return None
        return decode_message(self.conn.recv_bytes())

    def done(self, **result: Any) -> int:
        self.send(OutcomeMsg(status="done", result=result))
        return 0

    def heartbeat(self, note: str = "working") -> None:
        self.send(HeartbeatMsg(note=note))

    def wait_released(self, timeout: float = 30.0) -> None:
        """Block until the test sets `release` (or the process is terminated)."""
        self.proc.release.wait(timeout)


class FakeProc:
    """A `multiprocessing.Process` stand-in running `ctx.script` in a daemon thread."""

    def __init__(self, ctx: FakeCtx, target: Any, args: tuple[Any, ...], name: str) -> None:
        self.ctx, self.target, self.args, self.name = ctx, target, args, name
        self.exitcode: int | None = None
        self.terminated = self.killed = False
        self.stubborn = ctx.stubborn
        self.release = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.ctx.start_error is not None:
            raise self.ctx.start_error
        conn = self.args[4].conn
        self.ctx.procs.append(self)
        self._thread = threading.Thread(target=self._main, args=(conn,), daemon=True)
        self._thread.start()

    def _main(self, conn: Any) -> None:
        try:
            code = self.ctx.script(FakeChild(self, conn, self.args))
        except (EOFError, OSError):
            code = 1
        if self.exitcode is None:
            self.exitcode = 0 if code is None else code

    def is_alive(self) -> bool:
        if self.killed:
            return False
        if self.terminated and not self.stubborn:
            return False
        return self._thread is not None and self._thread.is_alive()

    def terminate(self) -> None:
        self.terminated = True
        self.release.set()
        if not self.stubborn:
            self.exitcode = -15

    def kill(self) -> None:
        self.killed = True
        self.exitcode = -9

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None and not self.stubborn:
            self._thread.join(timeout)


class _ChildEnd:
    """The child pipe end: `spawn` closes its copy after `start()`, as with a real child
    (whose handle was duplicated); here the thread keeps using the same connection."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def close(self) -> None:
        return None


class FakeCtx:
    """The spawn context stand-in: a real `Pipe`, a thread-backed `Process`."""

    def __init__(self) -> None:
        self.script: Script = lambda child: child.done(ok=True)
        self.procs: list[FakeProc] = []
        self.stubborn = False
        self.start_error: BaseException | None = None

    def Pipe(self, duplex: bool = True) -> Any:  # noqa: N802 - multiprocessing API name
        parent, child = multiprocessing.Pipe(duplex)
        return parent, _ChildEnd(child)

    def Process(self, target: Any, args: tuple[Any, ...], name: str) -> FakeProc:  # noqa: N802
        return FakeProc(self, target, args, name)


@dataclass
class SupEnv:
    """The store, config and fake spawn context of one supervisor unit test."""

    ctx: FakeCtx
    cfg_dir: Path
    state: ProcessState
    supervisors: list[Supervisor] = field(default_factory=list)
    _n: int = 0

    def enqueue(
        self,
        mode: str = "done",
        *,
        kind: Any = "sync",
        gpu_class: GpuClass = "none",
        priority: int | None = None,
        max_attempts: int | None = None,
        scheduled_for: datetime | None = None,
    ) -> str:
        self._n += 1
        payload: dict[str, Any] = {"mode": mode, "n": self._n}
        return enqueue(kind, payload, gpu_class, priority, scheduled_for, max_attempts=max_attempts)

    def job(self, job_id: str) -> JobRow:
        row = require_jobs_backend().get_job(job_id)
        assert row is not None
        return row

    def supervisor(
        self,
        *,
        gpu_classes: tuple[GpuClass, ...] = ("none",),
        concurrency: int | None = 1,
        once: bool = False,
        bootstrap_spec: str = BOOTSTRAP,
    ) -> Supervisor:
        sup = Supervisor(WorkerOptions(gpu_classes, concurrency, once, bootstrap_spec))
        self.supervisors.append(sup)
        return sup

    def drive(self, sup: Supervisor, until: Callable[[], bool], timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sup.tick(clock.now())
            if until():
                return
            time.sleep(0.02)
        msg = "condition not reached"
        raise AssertionError(msg)


@pytest.fixture
def sup_env(
    ops_store: OpsStoreHandle,
    fake_keyring: MemoryKeyring,
    reset_process_state: ProcessState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[SupEnv]:
    """Store, fast config, bootstrap in this process, and `FakeCtx` as the spawn context."""
    del fake_keyring
    cfg_dir = write_worker_config(tmp_path / "cfgroot")
    monkeypatch.setenv(ENV_CONFIG, str(cfg_dir))
    monkeypatch.setenv(ENV_DB, str(ops_store.db_path))
    bootstrap()
    ctx = FakeCtx()
    monkeypatch.setattr(_supervisor_child, "_CTX", ctx)
    env = SupEnv(ctx, cfg_dir, reset_process_state)
    try:
        yield env
    finally:
        for proc in ctx.procs:
            proc.release.set()
        for sup in env.supervisors:
            sup.release()
        c.reset_config()
