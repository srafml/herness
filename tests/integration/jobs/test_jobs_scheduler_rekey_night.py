"""Planned rekey night on the real jobs store (impl 08 IT08-03, flow F08-12; design 08 §5.11).

The supervisor and `JobContext` are later cards, so the worker is a scoped harness: the GPU
slot claims through `queue.claim` with the active window's classes (plus `none`), fake
handlers finish their jobs through the real `SqliteJobsBackend`, and the scheduler tick runs
on a fake clock from Saturday 18:59 to 23:00.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.core.jobs import _sched_env
from tests.unit.core.jobs._sched_env import Clock, events, jobs, local

from herness.core import time as clock
from herness.core.jobs import queue, scheduler, windows
from herness.core.jobs.ports import JobRow, require_jobs_backend
from herness.core.types import GpuClass

pytestmark = pytest.mark.integration
jobs_db, sched_db = _sched_env.jobs_db, _sched_env.sched_db  # fixtures

GPU_OWNER = "worker-host:4242:gpu"
WED, SAT = (2026, 9, 23), (2026, 9, 26)


def _gpu_claim() -> JobRow | None:
    """One GPU-slot claim with the classes the active window allows (none always runs)."""
    allowed: list[GpuClass] = ["none", *windows.window_at(clock.now()).spec.classes]
    return queue.claim(owner=GPU_OWNER, allowed_classes=allowed)


def _handler_done(row: JobRow) -> None:
    """Fake handler: the job ends `done` at the current fake time."""
    assert require_jobs_backend().finish_done(row.job_id, GPU_OWNER, {}, clock.now())


def _tick(fake: Clock, at: datetime) -> scheduler.SchedulerReport:
    return scheduler.run_scheduler(fake.set(at))


def test_it08_03_rekey_night(sched_db: Clock, fake_keyring: MemoryKeyring) -> None:
    """IT08-03 the rekey job claims before `build_pipeline` on the GPU slot (the build has
    class `none`, R-43); the build payload has `rekey_night`; no preemption at 21:00 while the
    build runs; the standard reviews are `chain_skipped` with reason `rekey`."""
    fake_keyring.set_password("herness", scheduler.NEXT_KEY_REF, bytes(range(32, 64)).hex())
    sched_db.set(local(WED, "10:00"))
    rekey_id = scheduler.schedule_rekey()
    assert _gpu_claim() is None  # planned for Saturday 19:00, not due

    _tick(sched_db, local(SAT, "18:59"))
    assert _gpu_claim() is None
    assert [row.kind for row in jobs("nightly")] == []

    _tick(sched_db, local(SAT, "19:00"))
    (build,) = jobs("nightly")
    assert build.payload["rekey_night"] is True
    assert (build.kind, build.gpu_class, build.priority) == ("build_pipeline", "none", 60)
    first = _gpu_claim()
    assert first is not None
    assert (first.job_id, first.kind, first.gpu_class, first.priority) == (
        rekey_id,
        "maintenance",
        "decider",
        70,
    )
    assert _gpu_claim() is None  # `exclusive_kinds` keeps the build apart while rekey runs

    sched_db.set(local(SAT, "19:40"))
    _handler_done(first)
    second = _gpu_claim()
    assert second is not None
    assert (second.job_id, second.kind) == (build.job_id, "build_pipeline")
    assert second.payload["rekey_night"] is True
    class_since = clock.now()  # the build switches to decider in-job (gpu_scope: carry-over)

    at = local(SAT, "19:30")
    while at < local(SAT, "23:00"):
        at += timedelta(minutes=30)
        report = _tick(sched_db, at)
        assert (report.errors, report.chains_advanced) == (0, 0)
        assert windows.preempt_deadline("decider", class_since, at) is None
    assert windows.window_at(local(SAT, "21:00")).spec.name == "reviews"

    _handler_done(second)
    report = _tick(sched_db, local(SAT, "23:00") + timedelta(seconds=30))
    assert report.chains_advanced == 0
    skipped = [json.loads(row["detail"]) for row in events("chain_skipped")]
    assert [(d["step"], d["reason"]) for d in skipped] == [
        (1, "rekey"),
        (2, "rekey"),
        (3, "disabled"),
    ]
    assert [row.kind for row in jobs("nightly")] == ["build_pipeline"]
    assert _gpu_claim() is None
    rekey = require_jobs_backend().get_job(rekey_id)
    assert rekey is not None
    assert rekey.status == "done"
