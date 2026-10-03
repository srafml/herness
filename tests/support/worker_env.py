"""`worker_env` fixture (impl 08 T08-21): a migrated ops store, the fast worker config and the
test bootstrap run in this process, plus helpers to enqueue jobs, drive an in-process
`Supervisor` tick by tick and start the worker as a subprocess (list argv, no shell)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
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
from herness.core.jobs.ports import JobRow, require_jobs_backend
from herness.core.jobs.queue import enqueue
from herness.core.jobs.supervisor import Supervisor, WorkerOptions
from herness.core.resilience import ProcessState
from herness.core.types import GpuClass
from herness.store.ops.core import read_all

REPO = Path(__file__).resolve().parents[2]
WAIT_S = 120.0  # generous: the suite may run on a loaded host


@dataclass
class WorkerEnv:
    """The shared store and config of one worker test."""

    cfg_dir: Path
    db: Path
    tmp: Path
    supervisors: list[Supervisor] = field(default_factory=list)
    procs: list[subprocess.Popen[bytes]] = field(default_factory=list)
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
        """Enqueue one fake job; every call has its own payload (no dedupe). `scheduled_for`
        defaults to now (`clock.now()` at submit)."""
        self._n += 1
        payload: dict[str, Any] = {"mode": mode, "n": self._n}
        return enqueue(kind, payload, gpu_class, priority, scheduled_for, max_attempts=max_attempts)

    def configure(self, **jobs: Any) -> None:
        """Change `resilience.jobs` timings in the config tree and bootstrap again."""
        path = self.cfg_dir / "resilience.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["resilience"]["jobs"].update(jobs)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        bootstrap()

    def job(self, job_id: str) -> JobRow:
        row = require_jobs_backend().get_job(job_id)
        assert row is not None
        return row

    def events(self, kind: str) -> list[dict[str, Any]]:
        rows = read_all(
            "SELECT job_id, target, detail FROM resilience_event WHERE kind = ? ORDER BY ts",
            (kind,),
        )
        return [
            {"job_id": r["job_id"], "target": r["target"], **json.loads(r["detail"])} for r in rows
        ]

    def metric_names(self) -> set[str]:
        return {str(r["name"]) for r in read_all("SELECT DISTINCT name FROM metric_sample")}

    def supervisor(
        self,
        *,
        gpu_classes: tuple[GpuClass, ...] = ("none",),
        concurrency: int = 1,
        once: bool = False,
    ) -> Supervisor:
        sup = Supervisor(WorkerOptions(gpu_classes, concurrency, once, BOOTSTRAP))
        self.supervisors.append(sup)
        return sup

    def drive(self, sup: Supervisor, until: Callable[[], bool], timeout: float = WAIT_S) -> None:
        """Tick `sup` (real clock) until `until()` holds; fail after `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sup.tick(clock.now())
            if until():
                return
            time.sleep(0.1)
        msg = "condition not reached while driving the supervisor"
        raise AssertionError(msg)

    def spawn_worker(self, *args: str, name: str = "worker") -> subprocess.Popen[bytes]:
        """The worker as a subprocess: `python -m tests.support.worker_bootstrap <args>`."""
        env = dict(os.environ, PYTHONUTF8="1")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        log = (self.tmp / f"{name}-{len(self.procs)}.log").open("wb")
        argv = [sys.executable, "-m", "tests.support.worker_bootstrap", *args]
        proc = subprocess.Popen(  # noqa: S603 - fixed argv list, no shell
            argv, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags
        )
        self.procs.append(proc)
        return proc

    def output(self, index: int = -1, name: str = "worker") -> str:
        path = self.tmp / f"{name}-{index % len(self.procs)}.log"
        return path.read_text(encoding="utf-8", errors="replace")

    def cleanup(self) -> None:
        for sup in self.supervisors:
            for run in [r for r in sup.slots.values() if r is not None]:
                run.terminate()
            sup.release()
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)


def interrupt(proc: subprocess.Popen[bytes]) -> None:
    """SIGINT on POSIX, `CTRL_BREAK_EVENT` to the worker's process group on Windows."""
    if sys.platform == "win32":
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)


def wait_for(until: Callable[[], bool], timeout: float = WAIT_S, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if until():
            return
        time.sleep(0.1)
    msg = f"{what} not reached within {timeout} s"
    raise AssertionError(msg)


@pytest.fixture
def worker_env(
    ops_store: OpsStoreHandle,
    fake_keyring: MemoryKeyring,
    reset_process_state: ProcessState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[WorkerEnv]:
    """Store, fast config and the test bootstrap in this process (children inherit the env)."""
    del fake_keyring, reset_process_state
    cfg_dir = write_worker_config(tmp_path / "cfgroot")
    monkeypatch.setenv(ENV_CONFIG, str(cfg_dir))
    monkeypatch.setenv(ENV_DB, str(ops_store.db_path))
    bootstrap()
    env = WorkerEnv(cfg_dir, ops_store.db_path, tmp_path)
    try:
        yield env
    finally:
        env.cleanup()
        c.reset_config()
