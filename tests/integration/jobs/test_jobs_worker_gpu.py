"""Worker GPU slot on the `fake_gpu` fixture (impl 08 IT08-13, ST08-09; T08-21): the arbiter
step with the chat-window priority rule, the host GPU lock against a second worker process,
and an external class request the chat window does not allow (TH08-09)."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable

import pytest
import structlog
from tests.support.fake_gpu import FakeGpu
from tests.support.fake_keyring import MemoryKeyring
from tests.support.worker_env import WAIT_S, WorkerEnv

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.jobs import gpu
from herness.core.jobs.gpu import request_gpu_class
from herness.core.jobs.ports import require_jobs_backend
from herness.core.jobs.queue import GPU_SLOT_KINDS
from herness.core.jobs.supervisor import Supervisor
from herness.core.jobs.windows import window_at

pytestmark = pytest.mark.integration

VLLM_KEY = "vllm-" + "stub-bearer-" + "value-2"  # built at runtime (detect-secrets)


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


def _chat_noon() -> dt.datetime:
    """Today 12:00 in the business time zone: inside the `chat` window."""
    tz = clock.zone(get_config().weights.business_timezone)
    local = dt.datetime.combine(clock.now().astimezone(tz).date(), dt.time(12), tzinfo=tz)
    at = local.astimezone(dt.UTC)
    assert window_at(at).spec.name == "chat"
    return at


def _drive_at(sup: Supervisor, at: dt.datetime, until: Callable[[], bool]) -> None:
    """Tick `sup` at the fixed instant `at` until `until()` holds."""
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        sup.tick(at)
        if until():
            return
        time.sleep(0.1)
    msg = "condition not reached while driving the supervisor"
    raise AssertionError(msg)


def test_it08_13_chat_window_claims_chat_and_high_priority_only(gpu_env: WorkerEnv) -> None:
    """IT08-13 chat window; queued `review` priority 40 and 75 and `chat` priority 75: the
    arbiter swaps to `reasoning`, the chat job and the priority-75 review are claimed and
    done, and the priority-40 review stays queued."""
    low = gpu_env.enqueue(kind="review", gpu_class="reasoning", priority=40)
    high = gpu_env.enqueue(kind="review", gpu_class="reasoning", priority=75)
    chat = gpu_env.enqueue(kind="chat", gpu_class="reasoning", priority=75)
    sup = gpu_env.supervisor(gpu_classes=("reasoning", "decider", "large"), concurrency=0)
    assert sup.start() is None
    assert sup.gpu is not None
    noon = _chat_noon()
    _drive_at(
        sup, noon, lambda: gpu_env.job(chat).status == "done" and gpu_env.job(high).status == "done"
    )
    assert sup.gpu.loaded == "reasoning"
    assert gpu_env.job(low).status == "queued"
    counts = require_jobs_backend().claimable_counts(
        now=clock.now(),
        classes=["reasoning"],
        exclusive_kinds=[],
        min_priority=70,
        priority_exempt_kinds=["chat"],
        gpu_slot_kinds=sorted(GPU_SLOT_KINDS),
    )
    assert counts == {"reasoning": 0}
    owners = {gpu_env.job(i).result["attempt"] for i in (chat, high)}  # type: ignore[index]
    assert owners == {1}
    assert sup.stop() == 0


def test_st08_09_second_gpu_worker_exits_1_and_request_rejected(
    gpu_env: WorkerEnv, fake_gpu: FakeGpu
) -> None:
    """ST08-09 a second GPU worker process exits 1 (R-46) while this one holds the GPU lock;
    `request_gpu_class("large")` (the `deploy up large` path) during the chat window is
    rejected by the arbiter, logged `jobs.gpu.request_rejected` and cleared."""
    sup = gpu_env.supervisor(gpu_classes=("reasoning", "decider", "large"), concurrency=0)
    assert sup.start() is None
    second = gpu_env.spawn_worker("--gpu-classes", "reasoning", "--concurrency", "0")
    assert second.wait(timeout=WAIT_S) == 1
    assert "jobs.worker.gpu_lock_held" in gpu_env.output()
    backend = require_jobs_backend()
    backend.update_worker(sup.worker_id, heartbeat_at=clock.now())  # alive GPU worker
    assert request_gpu_class("large") == "requested"
    fake_gpu.argv.clear()
    with structlog.testing.capture_logs() as logs:
        sup.tick(_chat_noon())
    rejected = [e for e in logs if e["event"] == "jobs.gpu.request_rejected"]
    assert len(rejected) == 1
    assert (rejected[0]["log_level"], rejected[0]["requested"]) == ("warning", "large")
    assert rejected[0]["window"] == "chat"
    row = next(w for w in backend.list_workers() if w.worker_id == sup.worker_id)
    assert row.requested_class is None
    assert sup.gpu is not None
    assert sup.gpu.idle()
    assert not [argv for argv in fake_gpu.argv if "llamacpp-large" in argv]
    assert sup.stop() == 0
