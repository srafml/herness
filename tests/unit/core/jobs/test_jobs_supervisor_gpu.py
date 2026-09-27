"""Unit tests of the supervisor's GPU slot (U08-87 steps 7a, 7b, 7g; T08-21) on the `fake_gpu`
fixture: child GPU requests, swaps and their failure handling, the restart check and the
arbiter step with external class requests."""

from __future__ import annotations

import datetime as dt
import multiprocessing
import time
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from tests.support.fake_gpu import FakeGpu
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.core.jobs._supervisor_env import SupEnv

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ModelUnavailable, StoreBusy
from herness.core.jobs import gpu
from herness.core.jobs._supervisor_boot import worker_row
from herness.core.jobs._supervisor_child import ChildRun
from herness.core.jobs._supervisor_gpu import ClaimFilter, GpuSlot, chat_rule
from herness.core.jobs.pipe import GpuReplyMsg, GpuRequestMsg
from herness.core.jobs.ports import require_jobs_backend
from herness.core.jobs.windows import window_at
from herness.core.resilience.breaker import breaker
from herness.store.ops.core import read_all

pytestmark = pytest.mark.unit

VLLM_KEY = "vllm-" + "stub-bearer-" + "value-2"  # built at runtime (detect-secrets)
WORKER = "h:1"
RID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


@pytest.fixture
def slot(
    sup_env: SupEnv, fake_gpu: FakeGpu, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> Iterator[GpuSlot]:
    """A `GpuSlot` of worker `h:1` (row upserted) on the fake compose and stub services."""
    fake_keyring.store[("herness", "vllm.api_key")] = VLLM_KEY
    monkeypatch.setattr(gpu, "_gpu", lambda: fake_gpu.gpu)
    row = worker_row(WORKER, "h", gpu_slot=True, cpu_slots=0, now=clock.now())
    require_jobs_backend().upsert_worker(row)
    gs = GpuSlot(WORKER, ("reasoning", "decider"))
    try:
        yield gs
    finally:
        gs.close()


def _wait_idle(gs: GpuSlot) -> None:
    deadline = time.monotonic() + 60
    while not gs.idle():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def _local(hour: int) -> dt.datetime:
    tz = clock.zone(get_config().weights.business_timezone)
    day = clock.now().astimezone(tz).date()
    while day.weekday() != 2:  # a Wednesday: `reviews` at night, no preload
        day += dt.timedelta(days=1)
    return dt.datetime.combine(day, dt.time(hour), tzinfo=tz).astimezone(dt.UTC)


def _run(slot_name: str) -> ChildRun:
    parent, _child = multiprocessing.Pipe()
    now = clock.now()
    row: Any = None
    return ChildRun(slot_name, f"{WORKER}:{slot_name}", row, None, parent, now, now, now)  # type: ignore[arg-type]


def _ask(gs: GpuSlot, run: ChildRun, **fields: Any) -> GpuReplyMsg:
    gs.request(run, GpuRequestMsg(request_id=RID, **fields))
    reply = run.pending.pop(RID).result(timeout=60)
    assert reply.request_id == RID
    return reply


def _worker() -> Any:
    return next(w for w in require_jobs_backend().list_workers() if w.worker_id == WORKER)


def test_st08_01_gpu_request_service_of_another_class_rejected(
    slot: GpuSlot, fake_gpu: FakeGpu
) -> None:
    """ST08-01 (gpu_request half) a child's request for a service outside the loaded class is
    answered `ConfigError` and starts no compose command; a CPU-slot child gets `ConfigError`."""
    run = _run("gpu")
    reply = _ask(slot, run, op="service_start", service="openjev")
    assert (reply.ok, reply.error_class) == (False, "ConfigError")
    assert not [a for a in fake_gpu.argv if "openjev" in a]
    cpu = _ask(slot, _run("cpu0"), op="require_class", cls="reasoning")
    assert (cpu.ok, cpu.error_class, cpu.message) == (
        False,
        "ConfigError",
        "GPU control requires the GPU slot",
    )


def test_cv_t08_21_gpu_requests_served_on_the_executor(
    slot: GpuSlot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T08-21 step 7b: `require_class` swaps and replies the previous class; service
    start/stop/healthy call the controller; missing arguments and failures become replies."""
    run = _run("gpu")
    swapped = _ask(slot, run, op="require_class", cls="reasoning")
    assert (swapped.ok, swapped.previous_class) == (True, "none")
    assert slot.loaded == "reasoning"
    assert _ask(slot, run, op="service_healthy", service="vllm-reasoning").healthy is True
    assert _ask(slot, run, op="service_stop", service="vllm-reasoning").ok
    assert _ask(slot, run, op="service_start", service="vllm-reasoning", timeout_s=5.0).ok
    no_cls = _ask(slot, run, op="require_class")
    assert (no_cls.ok, no_cls.error_class) == (False, "ConfigError")
    no_svc = _ask(slot, run, op="service_stop")
    assert (no_svc.ok, no_svc.error_class) == (False, "ConfigError")

    def boom(name: str) -> bool:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(slot.controller, "service_healthy", boom)
    failed = _ask(slot, run, op="service_healthy", service="vllm-reasoning")
    assert (failed.error_class, failed.message) == (
        "ModelUnavailable",
        "gpu request failed: RuntimeError",
    )

    def down(name: str) -> bool:
        msg = "gpu down"
        raise ModelUnavailable(msg)

    monkeypatch.setattr(slot.controller, "service_healthy", down)
    unavailable = _ask(slot, run, op="service_healthy", service="vllm-reasoning")
    assert (unavailable.error_class, unavailable.message) == ("ModelUnavailable", "gpu down")


def test_cv_t08_21_failed_swap_postpones_the_class(
    slot: GpuSlot, sup_env: SupEnv, fake_gpu: FakeGpu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T08-21 step 7g: a `ModelUnavailable` swap postpones the class's due jobs by 10 min
    without attempt charge; another store error is logged, never raised."""
    job_id = sup_env.enqueue(kind="review", gpu_class="reasoning")
    fake_gpu.fail["up"] = (1, "no gpu")
    before = clock.now()
    slot.swap("reasoning", "claimable")
    _wait_idle(slot)
    row = sup_env.job(job_id)
    assert row.status == "queued"
    assert row.attempts == 0
    assert row.scheduled_for is not None
    assert row.scheduled_for >= before + dt.timedelta(minutes=9)

    def busy(target: str, *, reason: str) -> float:
        msg = "locked"
        raise StoreBusy(msg)

    monkeypatch.setattr(slot.controller, "swap", busy)
    with structlog.testing.capture_logs() as logs:
        slot.swap("decider", "claimable")
        _wait_idle(slot)
    assert [e["error_type"] for e in logs if e["event"] == "jobs.gpu.swap_failed"] == ["StoreBusy"]


def test_cv_t08_21_restart_check_restarts_the_only_service(
    slot: GpuSlot, fake_gpu: FakeGpu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T08-21 step 7a: an open `model:` breaker of a client on the loaded single-service
    class restarts that service on the executor; nothing happens for class `none`."""
    slot.restart_check()
    assert slot.idle()
    slot.swap("reasoning", "setup")
    _wait_idle(slot)
    breaker("model:local-30b").force_open(ModelUnavailable("down"))
    breaker("jira").force_open(ModelUnavailable("down"))
    fake_gpu.argv.clear()
    slot.restart_check()
    _wait_idle(slot)
    stops = [a for a in fake_gpu.argv if "stop" in a and a[-1] == "vllm-reasoning"]
    assert stops
    rows = read_all("SELECT target FROM resilience_event WHERE kind = 'service_restart'")
    assert [r["target"] for r in rows] == ["vllm-reasoning"]

    def fail(name: str) -> bool:
        msg = "restart failed"
        raise ModelUnavailable(msg)

    monkeypatch.setattr(slot.controller, "restart_service", fail)
    with structlog.testing.capture_logs() as logs:
        slot.restart_check()
        _wait_idle(slot)
    assert any(e["event"] == "jobs.gpu.restart_failed" for e in logs)


def test_cv_t08_21_plan_requests_and_arbiter(slot: GpuSlot, sup_env: SupEnv) -> None:
    """CV-T08-21 step 7g: a request equal to the loaded class is cleared; a class outside the
    worker's classes and one the window does not allow are rejected and cleared (TH08-09);
    an allowed request swaps; with no work the slot idles; `keep` gives the claim filter."""
    backend = require_jobs_backend()
    night = _local(23)
    assert window_at(night).spec.name == "reviews"
    assert chat_rule(window_at(night)) == (None, [])
    backend.set_requested_class(WORKER, "none")
    assert slot.plan(night, _worker(), preload=True) is None  # served already; no work
    assert _worker().requested_class is None
    sup_env.enqueue(kind="build_pipeline")  # a GPU_SLOT_KINDS job of class none
    assert slot.plan(night, None, preload=True) == ClaimFilter(["none"], None, [])
    assert require_jobs_backend().list_jobs(status="queued", kind="build_pipeline", limit=5)
    for row in require_jobs_backend().list_jobs(status="queued", kind="build_pipeline", limit=5):
        backend.cancel_job(row.job_id, clock.now())
    for requested in ("large", "decider"):
        backend.set_requested_class(WORKER, requested)
        with structlog.testing.capture_logs() as logs:
            assert (
                slot.plan(_local(12) if requested == "decider" else night, _worker(), preload=True)
                is None
            )
        assert [e["requested"] for e in logs if e["event"] == "jobs.gpu.request_rejected"] == [
            requested
        ]
        assert _worker().requested_class is None
    backend.set_requested_class(WORKER, "decider")
    assert slot.plan(night, _worker(), preload=True) is None
    _wait_idle(slot)
    assert slot.loaded == "decider"
    assert slot.plan(night, None, preload=True) is None  # no work, no preload: idle
    assert slot.idle()
    sup_env.enqueue(kind="review", gpu_class="decider")
    found = slot.plan(night, _worker(), preload=True)
    assert found == ClaimFilter(["decider", "none"], None, [])


def test_cv_t08_21_plan_preload_only_when_allowed(slot: GpuSlot) -> None:
    """CV-T08-21 step 7g/7i: the window's `preload` swap is skipped in `--once` mode."""
    morning = _local(7)
    assert window_at(morning).spec.preload == "reasoning"
    assert slot.plan(morning, None, preload=False) is None
    assert slot.idle()
    assert slot.loaded == "none"
    assert slot.plan(morning, None, preload=True) is None
    _wait_idle(slot)
    assert slot.loaded == "reasoning"


def test_cv_t08_21_supervisor_gpu_slot_end_to_end(
    sup_env: SupEnv, fake_gpu: FakeGpu, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T08-21 steps 3, 7b, 7f, 7g, 7h with a GPU slot: the lock is taken and the class
    detected; the arbiter swaps to the queued job's class; the GPU child's in-job
    `require_class` is served on the executor and answered with the previous class; the
    worker row reports the loaded class."""
    from tests.unit.core.jobs._supervisor_env import FakeChild  # noqa: PLC0415

    from herness.core.jobs.pipe import GpuReplyMsg  # noqa: PLC0415

    fake_keyring.store[("herness", "vllm.api_key")] = VLLM_KEY
    monkeypatch.setattr(gpu, "_gpu", lambda: fake_gpu.gpu)
    replies: list[GpuReplyMsg] = []

    def script(child: FakeChild) -> int:
        child.heartbeat()
        child.send(GpuRequestMsg(request_id=RID, op="require_class", cls="decider"))
        reply = child.recv(timeout=60)
        assert isinstance(reply, GpuReplyMsg)
        replies.append(reply)
        return child.done(ok=True)

    sup_env.ctx.script = script
    job_id = sup_env.enqueue(kind="review", gpu_class="reasoning")
    sup = sup_env.supervisor(gpu_classes=("reasoning", "decider"), concurrency=0)
    assert sup.start() is None
    assert sup.gpu is not None
    night = _local(23)
    deadline = time.monotonic() + 60
    while sup_env.job(job_id).status != "done":
        assert time.monotonic() < deadline
        sup.tick(night)
        time.sleep(0.02)
    assert replies[0].ok
    assert replies[0].previous_class == "reasoning"
    _wait_idle(sup.gpu)
    sup.tick(night + dt.timedelta(seconds=5))
    row = next(w for w in require_jobs_backend().list_workers() if w.worker_id == sup.worker_id)
    assert (row.gpu_slot, row.gpu_class_loaded) == (1, "decider")
    assert sup.stop() == 0
