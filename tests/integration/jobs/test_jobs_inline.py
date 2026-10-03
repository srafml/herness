"""`run_inline` on the real jobs store (impl 08 IT08-12, flow F08-14; T08-22).

No worker runs: the job is claimed as `<host>:<pid>:cli`, the GPU controller talks to the
`fake_gpu` compose and stub services, and the handlers are test functions put in the
process handler table. Ctrl+C is a real `SIGINT` raised in the main thread by the handler,
so its delivery point is deterministic. Nothing here depends on the time of day: inline runs
ignore schedule windows.
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from tests.support.fake_gpu import FakeGpu
from tests.support.fake_keyring import MemoryKeyring
from tests.support.worker_env import WorkerEnv

from herness.core.config import get_config
from herness.core.errors import (
    ConfigError,
    HernessError,
    JobStateError,
    ModelUnavailable,
    SchemaViolation,
    StoreBusy,
)
from herness.core.ids import IdKind, new_id
from herness.core.jobs import gpu, inline
from herness.core.jobs.gpu_lock import GpuLock
from herness.core.jobs.inline import inline_owner, run_inline
from herness.core.jobs.ports import JobContext, require_jobs_backend
from herness.core.jobs.queue import cancel
from herness.core.resilience._state import process_state
from herness.core.types import JobKind, JobOutcome

pytestmark = pytest.mark.integration

VLLM_KEY = "vllm-" + "stub-bearer-" + "value-3"  # built at runtime (detect-secrets)
WAIT_S = 30.0  # bound of every handler loop below


@pytest.fixture
def gpu_env(
    worker_env: WorkerEnv,
    fake_gpu: FakeGpu,
    fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> WorkerEnv:
    """`worker_env` whose GPU controller talks to the fake compose and stub services."""
    fake_keyring.store[("herness", "vllm.api_key")] = VLLM_KEY
    monkeypatch.setattr(gpu, "_gpu", lambda: fake_gpu.gpu)
    return worker_env


def _use(monkeypatch: pytest.MonkeyPatch, kind: JobKind, fn: Callable[[Any], Any]) -> None:
    """Make `fn` the handler of `kind` (replacing the bootstrap's fake handler)."""
    monkeypatch.setitem(process_state().handlers, kind, fn)


def _lock_free() -> bool:
    """True when this process can take the host GPU lock now (nobody holds it)."""
    try:
        with GpuLock(get_config().paths.data / "locks" / "gpu.lock"):
            return True
    except ConfigError:
        return False


def _wait_for_stop(ctx: JobContext) -> None:
    deadline = time.monotonic() + WAIT_S
    while not ctx.should_yield() and time.monotonic() < deadline:
        time.sleep(0.02)


# --- IT08-12 ------------------------------------------------------------------------------------


def test_it08_12_inline_reasoning_job_swaps_and_ends_done(
    gpu_env: WorkerEnv, fake_gpu: FakeGpu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-12 no worker; a queued `reasoning` job on `fake_gpu`: `run_inline` swaps to
    `reasoning` (compose `up` of `vllm-reasoning`) and the job ends `done`; the GPU lock is
    held while the handler runs and released afterwards."""
    seen: dict[str, Any] = {}

    def handler(ctx: JobContext) -> JobOutcome:
        seen["running"] = set(fake_gpu.running())
        seen["lock_free"] = _lock_free()
        seen["owner"] = ctx.job.lease_owner
        return JobOutcome(status="done", result={"ok": True})

    _use(monkeypatch, "review", handler)
    job_id = gpu_env.enqueue(kind="review", gpu_class="reasoning")
    assert require_jobs_backend().list_workers() == []

    outcome = run_inline(job_id)

    assert outcome == JobOutcome(status="done", result={"ok": True})
    assert seen == {"running": {"vllm-reasoning"}, "lock_free": False, "owner": inline_owner()}
    assert any(argv[-1] == "vllm-reasoning" and "--profile" in argv for argv in fake_gpu.argv)
    row = gpu_env.job(job_id)
    assert (row.status, row.attempts, row.lease_owner) == ("done", 1, None)
    assert [e["job_id"] for e in gpu_env.events("job_done")] == [job_id]
    assert gpu_env.events("gpu_swap")
    assert _lock_free()


def test_it08_12_sigint_mid_handler_yields_and_releases_the_lock(
    gpu_env: WorkerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-12 a real SIGINT mid-handler sets stop reason `shutdown`: the handler yields, the
    job is back to `queued` with no attempt charge, the lock is released and the previous
    SIGINT handler is restored."""
    before = signal.getsignal(signal.SIGINT)
    seen: dict[str, Any] = {}

    def handler(ctx: JobContext) -> JobOutcome:
        seen["lock_free"] = _lock_free()
        signal.raise_signal(signal.SIGINT)  # Ctrl+C, delivered to this (main) thread
        _wait_for_stop(ctx)
        seen["reason"] = ctx.stop_reason
        return JobOutcome(status="yield")

    _use(monkeypatch, "review", handler)
    job_id = gpu_env.enqueue(kind="review", gpu_class="reasoning")

    outcome = run_inline(job_id)

    assert outcome.status == "yield"
    assert seen == {"lock_free": False, "reason": "shutdown"}
    row = gpu_env.job(job_id)
    assert (row.status, row.attempts, row.lease_owner) == ("queued", 0, None)
    (event,) = gpu_env.events("job_yield")
    assert event["stop_reason"] == "shutdown"
    assert _lock_free()
    assert signal.getsignal(signal.SIGINT) is before


# --- resume from the saved checkpoint (controller carry-over b) -------------------------------


def test_it08_12_resume_continues_from_the_checkpoint_without_repeating(
    worker_env: WorkerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-12 resume: step 1 runs, `save_state` records it, then Ctrl+C stops the run; a
    second `run_inline` reads `load_state` and does only step 2 (no repeated side effect)."""
    effects: list[str] = []

    def handler(ctx: JobContext) -> JobOutcome:
        state = ctx.load_state()
        if state.get("done_step", 0) < 1:
            effects.append("step1")
            ctx.save_state({"done_step": 1})
            signal.raise_signal(signal.SIGINT)
        if ctx.should_yield():
            return JobOutcome(status="yield")
        effects.append("step2")
        return JobOutcome(status="done", result={"steps": 2})

    _use(monkeypatch, "sync", handler)
    job_id = worker_env.enqueue(kind="sync")

    assert run_inline(job_id).status == "yield"
    assert effects == ["step1"]
    assert worker_env.job(job_id).result == {"state": {"done_step": 1}}

    assert run_inline(job_id) == JobOutcome(status="done", result={"steps": 2})
    assert effects == ["step1", "step2"]
    row = worker_env.job(job_id)
    assert (row.status, row.attempts, row.result) == ("done", 1, {"steps": 2})


# --- controller verification: the other branches of U08-90 -------------------------------------


def test_cv_t08_22_cpu_job_runs_without_the_gpu_lock(
    worker_env: WorkerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-12 (cv) a class-`none` `sync` job takes no GPU lock and builds no controller:
    GPU calls from its handler raise ConfigError."""
    seen: dict[str, Any] = {}

    def handler(ctx: JobContext) -> JobOutcome:
        seen["lock_free"] = _lock_free()
        try:
            ctx.require_gpu_class("reasoning")
        except ConfigError as exc:
            seen["gpu"] = exc.message
        return JobOutcome(status="done")

    _use(monkeypatch, "sync", handler)
    job_id = worker_env.enqueue(kind="sync")
    assert run_inline(job_id).status == "done"
    assert seen == {"lock_free": True, "gpu": "GPU control requires the GPU slot"}


def test_cv_t08_22_gpu_slot_kind_takes_the_lock_without_a_swap(
    gpu_env: WorkerEnv, fake_gpu: FakeGpu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-12 (cv) `build_pipeline` (class `none`, R-43) holds the GPU lock and gets a
    controller, but no class is swapped in before the handler runs."""
    seen: dict[str, Any] = {}

    def handler(ctx: JobContext) -> JobOutcome:
        seen["lock_free"] = _lock_free()
        seen["healthy"] = ctx.services.healthy("vllm-reasoning")
        return JobOutcome(status="done")

    _use(monkeypatch, "build_pipeline", handler)
    job_id = gpu_env.enqueue(kind="build_pipeline")
    assert run_inline(job_id).status == "done"
    assert seen["lock_free"] is False
    assert not any("--profile" in argv for argv in fake_gpu.argv)
    assert _lock_free()


def test_cv_t08_22_loaded_class_is_not_swapped_again(
    gpu_env: WorkerEnv, fake_gpu: FakeGpu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-12 (cv) the class already running (detected at start) is not swapped again."""
    fake_gpu.states["vllm-reasoning"] = "running"
    _use(monkeypatch, "review", lambda ctx: JobOutcome(status="done"))
    job_id = gpu_env.enqueue(kind="review", gpu_class="reasoning")
    assert run_inline(job_id).status == "done"
    assert not any("--profile" in argv for argv in fake_gpu.argv)


def test_cv_t08_22_unclaimable_job_raises_job_state_error(worker_env: WorkerEnv) -> None:
    """IT08-12 (cv) an unknown job, or one that is already done, is not claimable."""
    with pytest.raises(JobStateError, match="job not claimable"):
        run_inline(new_id(IdKind.JOB))
    job_id = worker_env.enqueue(kind="sync")
    assert run_inline(job_id).status == "done"
    with pytest.raises(JobStateError) as info:
        run_inline(job_id)
    assert info.value.job_id == job_id


def test_cv_t08_22_held_gpu_lock_requeues_and_raises(
    gpu_env: WorkerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-12 (cv) another GPU owner holds the lock: the job is requeued at once with no
    attempt charge and the ConfigError propagates; the handler never runs."""
    ran: list[str] = []
    _use(monkeypatch, "review", lambda ctx: ran.append("x"))
    job_id = gpu_env.enqueue(kind="review", gpu_class="reasoning")
    with GpuLock(get_config().paths.data / "locks" / "gpu.lock"), pytest.raises(ConfigError):
        run_inline(job_id)
    row = gpu_env.job(job_id)
    assert (row.status, row.attempts, row.lease_owner) == ("queued", 0, None)
    assert ran == []


def test_cv_t08_22_failed_swap_is_applied_then_raised(
    gpu_env: WorkerEnv, fake_gpu: FakeGpu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-12 (cv) the swap fails: `finish_job` applies the ModelUnavailable (a retryable
    requeue with `last_error`), the error propagates and the lock is released."""
    fake_gpu.fail["up"] = (1, "no such image")
    ran: list[str] = []
    _use(monkeypatch, "review", lambda ctx: ran.append("x"))
    job_id = gpu_env.enqueue(kind="review", gpu_class="reasoning")
    with pytest.raises(ModelUnavailable):
        run_inline(job_id)
    row = gpu_env.job(job_id)
    assert row.status == "queued"
    assert row.last_error is not None
    assert row.last_error["class"] == "ModelUnavailable"
    assert ran == []
    assert _lock_free()


def test_cv_t08_22_handler_error_is_applied_then_raised(
    worker_env: WorkerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-12 (cv) a handler HernessError is applied first (`failed`, a fatal error) and
    then raised to the caller."""

    def handler(ctx: JobContext) -> JobOutcome:
        msg = "bad shape"
        raise SchemaViolation(msg)

    _use(monkeypatch, "sync", handler)
    job_id = worker_env.enqueue(kind="sync")
    with pytest.raises(SchemaViolation):
        run_inline(job_id)
    row = worker_env.job(job_id)
    assert row.status == "failed"
    assert row.last_error is not None
    assert row.last_error["class"] == "SchemaViolation"


def test_cv_t08_22_missing_handler_fails_the_job(
    worker_env: WorkerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-12 (cv) no handler registered for the kind: the ConfigError is the outcome."""
    monkeypatch.delitem(process_state().handlers, "sync")
    job_id = worker_env.enqueue(kind="sync")
    with pytest.raises(ConfigError, match="no handler"):
        run_inline(job_id)
    assert worker_env.job(job_id).status == "failed"


def test_cv_t08_22_cancel_stops_the_handler_through_the_heartbeat(
    worker_env: WorkerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-12 (cv) the job is canceled while it runs: the next lease heartbeat fails, the
    stop reason is `cancel`, and `finish_job` finalises the job as `canceled`."""
    seen: dict[str, Any] = {}

    def handler(ctx: JobContext) -> JobOutcome:
        assert cancel(ctx.job_id) == "cancel_requested"
        _wait_for_stop(ctx)
        seen["reason"] = ctx.stop_reason
        return JobOutcome(status="yield")

    _use(monkeypatch, "sync", handler)
    job_id = worker_env.enqueue(kind="sync")
    assert run_inline(job_id).status == "yield"
    assert seen == {"reason": "cancel"}
    assert worker_env.job(job_id).status == "canceled"


def test_cv_t08_22_heartbeat_extends_the_lease_and_survives_store_errors(
    worker_env: WorkerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-12 (cv) the heartbeat thread extends the lease every `heartbeat_s`; a store
    error is logged and retried at the next beat instead of stopping the handler."""
    backend = require_jobs_backend()
    real = backend.heartbeat_job
    calls: list[str] = []

    def flaky(job_id: str, owner: str, lease_until: Any) -> bool:
        calls.append(owner)
        if len(calls) == 1:
            msg = "store busy"
            raise StoreBusy(msg)
        return real(job_id, owner, lease_until)

    monkeypatch.setattr(backend, "heartbeat_job", flaky)
    seen: dict[str, Any] = {}

    def handler(ctx: JobContext) -> JobOutcome:
        deadline = time.monotonic() + WAIT_S
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        seen["stopped"] = ctx.should_yield()
        return JobOutcome(status="done")

    _use(monkeypatch, "sync", handler)
    job_id = worker_env.enqueue(kind="sync")
    assert run_inline(job_id).status == "done"
    assert calls[:2] == [inline_owner(), inline_owner()]
    assert seen == {"stopped": False}


def test_cv_t08_22_off_main_thread_installs_no_sigint_handler(
    worker_env: WorkerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-12 (cv) called from another thread, `run_inline` runs the job without touching
    the process SIGINT handler (only the main thread may set one)."""
    before = signal.getsignal(signal.SIGINT)
    seen: dict[str, Any] = {}

    def handler(ctx: JobContext) -> JobOutcome:
        seen["sigint"] = signal.getsignal(signal.SIGINT)
        return JobOutcome(status="done")

    _use(monkeypatch, "sync", handler)
    job_id = worker_env.enqueue(kind="sync")
    results: list[JobOutcome | BaseException] = []

    def target() -> None:
        try:
            results.append(run_inline(job_id))
        except HernessError as exc:  # pragma: no cover - reported by the assert below
            results.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(WAIT_S)
    assert [getattr(r, "status", r) for r in results] == ["done"]
    assert seen["sigint"] is before


@pytest.fixture(autouse=True)
def _restore_sigint() -> Iterator[None]:
    """Belt and braces: a failed test never leaves the inline SIGINT handler installed."""
    before = signal.getsignal(signal.SIGINT)
    yield
    if signal.getsignal(signal.SIGINT) is not before:
        signal.signal(signal.SIGINT, before)


def test_cv_t08_22_module_exports() -> None:
    """IT08-12 (cv) the module exports only `run_inline` (U08-98 maps it later)."""
    assert inline.__all__ == ["run_inline"]
