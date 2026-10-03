"""`herness resume` to first task running (impl 08 BT08-10, design 08 §8; T08-22).

Run: pytest -m "integration and slow" tests/bench/test_jobs_resume_bench.py -s
There is no `herness resume` CLI yet (T09-22), so the measured path is the library call
behind it: `enqueue_resume(run_id)` → the idle in-process supervisor (GPU slot, `reasoning`
already loaded on the `fake_gpu` services) claims the review job at its next tick and starts
the job child, whose handler marks the first task running. The handler is the test
bootstrap's fake handler, so no LLM call is made (the `fake_llm` of the setup row is not
reached by it). Two windows covering the whole day both allow `reasoning`, so the result does
not depend on the time of day.
"""

from __future__ import annotations

import sqlite3
import sys
import time

import pytest
import yaml
from tests.support import worker_env as _worker_env
from tests.support.fake_gpu import FakeGpu
from tests.support.fake_keyring import MemoryKeyring
from tests.support.worker_bootstrap import bootstrap
from tests.support.worker_env import WAIT_S, WorkerEnv

from herness.core import time as clock
from herness.core.ids import IdKind, new_id
from herness.core.jobs import gpu
from herness.core.jobs.status import enqueue_resume
from herness.store.ops.core import run_write

pytestmark = [pytest.mark.integration, pytest.mark.slow]
worker_env = _worker_env.worker_env  # fixture

LIMIT_S = 30.0
VLLM_KEY = "vllm-" + "stub-bearer-" + "value-4"  # built at runtime (detect-secrets)
ALL_DAY_WINDOWS = [
    {"name": "chat", "start": "00:00", "end": "12:00", "classes": ["reasoning"],
     "preload": "reasoning"},
    {"name": "rest", "start": "12:00", "end": "00:00", "classes": ["reasoning", "decider"]},
]  # fmt: skip


def _all_day_reasoning(env: WorkerEnv) -> None:
    path = env.cfg_dir / "resilience.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["schedule"]["windows"] = ALL_DAY_WINDOWS
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    bootstrap()


def _running_run() -> str:
    run_id = new_id(IdKind.RUN)

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at)"
            " VALUES (?, 'org_review', 'standard', 'default', 'h', 'running', ?)",
            (run_id, clock.format_utc(clock.now())),
        )

    run_write(insert, op="test_setup")
    return run_id


@pytest.mark.timeout(600)
def test_bt08_10_resume_to_first_task_running_under_30_s(
    worker_env: WorkerEnv,
    fake_gpu: FakeGpu,
    fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BT08-10 idle worker with `reasoning` loaded: `enqueue_resume` → the review job's
    handler running (first task) in < 30 s."""
    fake_keyring.store[("herness", "vllm.api_key")] = VLLM_KEY
    monkeypatch.setattr(gpu, "_gpu", lambda: fake_gpu.gpu)
    _all_day_reasoning(worker_env)
    fake_gpu.states["vllm-reasoning"] = "running"  # the class is loaded before the worker starts
    sup = worker_env.supervisor(gpu_classes=("reasoning", "decider", "large"), concurrency=0)
    assert sup.start() is None
    assert sup.gpu is not None
    for _ in range(5):  # idle ticks: nothing queued
        sup.tick(clock.now())
    assert sup.gpu.loaded == "reasoning"
    run_id = _running_run()

    started = time.perf_counter()
    res = enqueue_resume(run_id)
    assert res.job_id is not None
    job_id = res.job_id
    claimed_s: float | None = None
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        sup.tick(clock.now())
        row = worker_env.job(job_id)
        if claimed_s is None and row.status != "queued":
            claimed_s = time.perf_counter() - started
        if row.status == "done":
            break
        time.sleep(0.05)
    first_task_s = time.perf_counter() - started
    row = worker_env.job(job_id)
    assert row.status == "done", row
    assert row.result is not None
    assert row.result["attempt"] == 1  # the handler ran in the job child
    sys.stdout.write(
        f"BT08-10 resume -> job claimed {claimed_s:.3f} s, first task (handler) done"
        f" {first_task_s:.3f} s (limit {LIMIT_S:.0f} s)\n"
    )
    assert sup.stop() == 0
    assert claimed_s is not None
    assert first_task_s < LIMIT_S
