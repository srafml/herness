"""FT08-08, FT08-09, FT08-10 and FT08-12 of the impl 08 fault suite (T08-24).

Private sibling of `tests/fault/test_resilience_f01_f12.py`, which imports and collects these
tests (the ENG 400-line limit splits the suite); they run under its `fault` marker.

Not run on real containers (open-questions (b)9/(b)15): the GPU side is the `fake_gpu`
compose host and its loopback stub services, and the build stages that need models are the
`build_kill` stand-ins; the rows run on CPU in the normal fault run. Workers are real
`Supervisor`s whose jobs run in spawned child processes; a supervisor is ticked at fixed
instants of the business day (`herness.core.time.now` pinned to the same instant), so window
boundaries and grace periods are asserted on that fake clock. Waiting for a child to report is
the polling `drive_at` (synchronisation only, no timing assertion rests on it).
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import pytest
import structlog
import yaml
from tests.support import _f08_jobs as fj
from tests.support._f08_gpu_handlers import BOOTSTRAP, WAITING_NOTE
from tests.support.build_kill import RESULT_PREFIX as BUILD_PREFIX
from tests.support.fake_gpu import FakeGpu
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fault_env import FaultEnv
from tests.support.lake_small import install, load_config
from tests.support.ops_store import OpsStoreHandle
from tests.support.promote_builds import statuses
from tests.support.worker_bootstrap import ENV_CONFIG, ENV_DB, write_worker_config
from tests.support.worker_env import WAIT_S, WorkerEnv

from herness.core import config as c
from herness.core import time as clock
from herness.core.config import get_config
from herness.core.jobs import gpu
from herness.core.jobs.ports import bind_jobs_backend, require_jobs_backend
from herness.core.jobs.queue import enqueue
from herness.core.jobs.supervisor import Supervisor, WorkerOptions
from herness.core.jobs.windows import window_at
from herness.core.resilience import ProcessState
from herness.core.types import GpuClass
from herness.store import warehouse
from herness.store.layout import DataLayout

OPENJEV_KEY: Final = "openjev-" + "stub-bearer-" + "value-1"  # built at runtime (detect-secrets)
VLLM_KEY: Final = "vllm-" + "stub-bearer-" + "value-2"
LONG_LEASES: Final = {"lease_s": 86_400, "stall_timeout_s": 86_400, "stall_timeout_large_s": 86_400}
DAY: Final = dt.date(2026, 6, 17)  # a past Wednesday: claims on the real clock find the jobs


def local(hour: int, minute: int = 0) -> dt.datetime:
    """`hour:minute` on `DAY` in the business time zone, as aware UTC."""
    tz = clock.zone(get_config().weights.business_timezone)
    return dt.datetime.combine(DAY, dt.time(hour, minute), tzinfo=tz).astimezone(dt.UTC)


class Gpu(WorkerEnv):
    """`WorkerEnv` whose supervisors use the suite's bootstrap (`_f08_gpu_handlers`)."""

    def gpu_supervisor(self, classes: tuple[GpuClass, ...], concurrency: int = 0) -> Supervisor:
        sup = Supervisor(WorkerOptions(classes, concurrency, False, BOOTSTRAP))
        self.supervisors.append(sup)
        return sup

    def submit(self, mode: str, at: dt.datetime) -> str:
        return enqueue("build_pipeline", {"mode": mode}, "none", None, at)

    def long_leases(self) -> None:
        """Leases and stall limits longer than the jumps of the pinned clock (the real worker
        renews them every tick; here time jumps between ticks)."""
        path = self.cfg_dir / "resilience.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["resilience"]["jobs"].update(LONG_LEASES)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture
def gpu_worker(
    ops_store: OpsStoreHandle,
    fake_keyring: MemoryKeyring,
    reset_process_state: ProcessState,
    fake_gpu: FakeGpu,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Gpu]:
    """The `worker_env` set-up with the suite's bootstrap, on the `fake_gpu` compose host."""
    del reset_process_state
    cfg_dir = write_worker_config(tmp_path / "cfgroot")
    monkeypatch.setenv(ENV_CONFIG, str(cfg_dir))
    monkeypatch.setenv(ENV_DB, str(ops_store.db_path))
    from tests.support._f08_gpu_handlers import bootstrap  # noqa: PLC0415 - after the env

    bootstrap()
    fake_keyring.store[("herness", "openjev_api_key")] = OPENJEV_KEY
    fake_keyring.store[("herness", "vllm.api_key")] = VLLM_KEY
    monkeypatch.setattr(gpu, "_gpu", lambda: fake_gpu.gpu)
    env = Gpu(cfg_dir, ops_store.db_path, tmp_path)
    try:
        yield env
    finally:
        env.cleanup()
        c.reset_config()


class Loaded:
    """The jobs backend, recording every `gpu_class_loaded` the GPU controller writes."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.classes = inner, list[str]()

    def update_worker(self, worker_id: str, **fields: Any) -> None:
        if "gpu_class_loaded" in fields:
            self.classes.append(str(fields["gpu_class_loaded"]))
        self.inner.update_worker(worker_id, **fields)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def drive_at(sup: Supervisor, until: Callable[[], bool]) -> None:
    """Tick `sup` at the pinned instant until `until()` holds (children run meanwhile)."""
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        sup.tick(clock.now())
        if until():
            return
        time.sleep(0.05)
    msg = "condition not reached while driving the supervisor"
    raise AssertionError(msg)


def pin_clock(monkeypatch: pytest.MonkeyPatch, at: dt.datetime) -> list[dt.datetime]:
    """Pin `herness.core.time.now` (this process) to a settable instant; returns its box."""
    box = [at]
    monkeypatch.setattr(clock, "now", lambda: box[0])
    return box


def test_ft08_08_kill_before_promote_current_unchanged_rerun_promotes(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fault_env: FaultEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FT08-08 F8: `pipeline.before_promote kill` ends a `build_pipeline` job process at the
    promotion: `CURRENT` still names the previous build and the killed build stays
    `building`; the rerun of the same job (its saved state) promotes that build.

    Not run on real containers (open-questions (b)9/(b)15): the enrich and score stages are
    the `build_kill` stand-ins, so no GPU class or service is involved."""
    data, layout = ops_store.data_root, DataLayout.from_root(ops_store.data_root)
    install(data)
    load_config(tmp_path / "cfgroot", data)
    argv = (str(tmp_path / "cfgroot" / "config"), str(data))
    build = "tests.support.build_kill"
    clean_env = fj.child_env_without_plan()
    first = fj.result(fj.child(clean_env, *argv, str(tmp_path / "job1.json"), module=build),
                      BUILD_PREFIX)["result"]["build_id"]  # fmt: skip
    assert warehouse.read_current(layout=layout) == first
    fault_env([{"point": "pipeline.before_promote", "action": "kill"}])
    env = fj.child_env(monkeypatch, reset_process_state)

    fj.killed(fj.child(env, *argv, str(tmp_path / "job2.json"), module=build), BUILD_PREFIX)
    assert warehouse.read_current(layout=layout) == first
    killed_id = next(b for b, s in statuses(layout).items() if b != first)
    assert statuses(layout)[killed_id] == "building"
    rerun = fj.result(fj.child(clean_env, *argv, str(tmp_path / "job2.json"), module=build),
                      BUILD_PREFIX)  # fmt: skip

    assert rerun["status"] == "done"
    assert rerun["result"]["build_id"] == killed_id
    assert rerun["result"]["promoted"] is True
    assert warehouse.read_current(layout=layout) == killed_id
    assert statuses(layout) == {killed_id: "promoted", first: "retired"}


@pytest.mark.parametrize("sticky", [False, True], ids=["stopped", "sticky"])
def test_ft08_09_kill_after_stop_restart_detects_one_class_or_none(
    gpu_worker: Gpu,
    fake_gpu: FakeGpu,
    reset_process_state: ProcessState,
    fault_env: FaultEnv,
    monkeypatch: pytest.MonkeyPatch,
    sticky: bool,
) -> None:
    """FT08-09 F9: a worker process swapping `reasoning` → `large` dies at `gpu.after_stop
    kill` (the old class stopped, or, `sticky`, refusing to stop); the restarted worker's
    detection leaves exactly one class (`reasoning`, still running and healthy) or none, and
    over the whole history, the dead process included, two classes never ran at once. From
    `none` the restarted worker then loads `large` cleanly.

    Not run on real containers (open-questions (b)9/(b)15): `fake_gpu` compose host shared
    with the child through `FakeGpu.share`."""
    expected = "reasoning" if sticky else "none"  # the old class still healthy, or nothing
    fake_gpu.states["vllm-reasoning"] = "running"
    if sticky:
        fake_gpu.sticky.add("vllm-reasoning")
    state_file = fake_gpu.share(gpu_worker.tmp / "compose_state.json")
    fault_env([{"point": "gpu.after_stop", "action": "kill"}])
    env = fj.child_env(monkeypatch, reset_process_state)

    fj.killed(fj.child(env, "gpu", str(state_file), "large"))
    fake_gpu.load_shared()
    assert fake_gpu.running() == ({"vllm-reasoning"} if sticky else set())  # what the crash left
    assert "llamacpp-large" not in fake_gpu.states  # killed before the target was started
    sup = gpu_worker.gpu_supervisor(("reasoning", "large"))
    assert sup.start() is None
    assert sup.gpu is not None

    assert sup.gpu.loaded == expected
    [row] = require_jobs_backend().list_workers()
    assert row.gpu_class_loaded == expected
    fake_gpu.load_shared()
    assert {fake_gpu.class_of(s) for s in fake_gpu.running()} == (
        set() if expected == "none" else {expected}
    )
    if not sticky:
        sup.gpu.controller.swap("large", reason="claimable")
        assert fake_gpu.running() == {"llamacpp-large"}
    fake_gpu.assert_single_class()
    assert sup.stop() == 0


def test_ft08_10_preempted_at_08_00_yields_then_resumes_at_19_00(
    gpu_worker: Gpu, fake_gpu: FakeGpu, fault_env: FaultEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT08-10 F10: a `build_pipeline` job switches to `decider` at 07:50 (in-job, allowed
    until the next boundary) and finishes 2 of 4 checkpointed batches. At the 08:00 `chat`
    boundary (hard start) it gets `stop("preempt")` and yields while the clock still reads
    08:00, inside `preempt_grace_min` (15 min); the yield charges no attempt and keeps its
    state. The chat window keeps it queued (priority 60 < 70); at 19:00 it resumes and runs
    only batches 2 and 3: finished batches are not redone.

    Not run on real containers (open-questions (b)9/(b)15): `fake_gpu` compose host."""
    fault_env([])  # HERNESS_ENV=test with the plan loader on in every process; no rule
    box = pin_clock(monkeypatch, local(7, 50))
    assert [window_at(local(h, m)).spec.name for h, m in ((7, 50), (8, 0), (19, 0))] == [
        "morning_prep", "chat", "enrichment"
    ]  # fmt: skip
    job_id = gpu_worker.submit("f10", local(7, 50))
    gpu_worker.long_leases()
    sup = gpu_worker.gpu_supervisor(("reasoning", "decider"))
    assert sup.start() is None

    drive_at(sup, lambda: _note(sup) == WAITING_NOTE)
    assert gpu_worker.job(job_id).attempts == 1
    box[0] = local(8)
    drive_at(sup, lambda: gpu_worker.job(job_id).status == "queued" and sup.slots["gpu"] is None)
    queued = gpu_worker.job(job_id)
    assert queued.attempts == 0  # the preempted attempt is not charged
    assert queued.result is not None
    assert queued.result["state"] == {"done": [0, 1], "runs": [[0, 1]]}
    [yielded] = [e for e in fj.job_events("job_yield") if e["job_id"] == job_id]
    assert yielded["stop_reason"] == "preempt"
    preempted_at = clock.parse_utc(yielded["ts"])
    assert local(8) <= preempted_at < local(8, 15)  # within preempt_grace_min
    box[0] = local(18, 59)
    for _ in range(3):
        sup.tick(clock.now())
        assert gpu_worker.job(job_id).status == "queued"  # chat window: priority 60 < 70
    box[0] = local(19)
    drive_at(sup, lambda: gpu_worker.job(job_id).status == "done")

    done = gpu_worker.job(job_id)
    assert done.attempts == 1
    assert done.result is not None
    assert done.result["runs"] == [[0, 1], [2, 3]]  # batches 0 and 1 not redone
    fake_gpu.assert_single_class()
    assert sup.stop() == 0


def _note(sup: Supervisor) -> str | None:
    run = sup.slots.get("gpu")
    return None if run is None else run.note


def test_ft08_12_build_pipeline_in_job_switches_on_the_gpu_slot(
    gpu_worker: Gpu, fake_gpu: FakeGpu, fault_env: FaultEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT08-12 F12: a `build_pipeline` job (class `none`, R-43) enters
    `ctx.gpu_scope("decider")`, calls `ctx.services.start("openjev")`, then
    `ctx.require_gpu_class("reasoning")` on `fake_gpu`: the GPU slot (not the free CPU slot)
    claims it; `gpu_class_loaded` goes none → decider → reasoning (then back to none when the
    scope restores the class it entered from); never two services up; one `gpu_swap` event
    per switch.

    Not run on real containers (open-questions (b)9/(b)15): `fake_gpu` compose host."""
    fault_env([])  # HERNESS_ENV=test with the plan loader on in every process; no rule
    pin_clock(monkeypatch, local(22))  # `reviews`: decider and reasoning, no preload
    assert window_at(local(22)).spec.name == "reviews"
    job_id = gpu_worker.submit("f12", local(22))
    sup = gpu_worker.gpu_supervisor(("reasoning", "decider"), concurrency=1)
    with structlog.testing.capture_logs() as logs:
        assert sup.start() is None  # start-up detection: `none` (nothing runs)
        loaded = Loaded(require_jobs_backend())
        bind_jobs_backend(loaded)  # type: ignore[arg-type]  # the real backend, recorded
        drive_at(sup, lambda: gpu_worker.job(job_id).status == "done")

    claims = [e["owner"] for e in logs if e["event"] == "jobs.job.claimed"]
    assert claims == [f"{sup.worker_id}:gpu"]
    changes = [cls for cls in loaded.classes if cls != "swapping"]
    steps = [cls for i, cls in enumerate(changes) if i == 0 or cls != changes[i - 1]]
    assert steps == ["none", "decider", "reasoning", "none"]  # row refreshes repeat a value
    swaps = [(e["from"], e["to"]) for e in fj.job_events("gpu_swap")]
    assert swaps == [("none", "decider"), ("decider", "reasoning"), ("reasoning", "none")]
    [started] = fj.job_events("service_start")
    assert started["service"] == "openjev"
    fake_gpu.assert_single_service()
    fake_gpu.assert_single_class()
    assert {s for names in fake_gpu.running_services for s in names} == {
        "openjev", "vllm-reasoning"
    }  # fmt: skip
    assert sup.stop() == 0
