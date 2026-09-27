"""Tests of U08-81 (GpuController), U08-83 (gpu_state, WorkerGpuState) and U08-102
(request_gpu_class) on the `fake_gpu` fixture and a real migrated ops store (T08-18)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.support.config_tree import write_full_config
from tests.support.fake_clock import FakeClock
from tests.support.fake_gpu import FakeGpu
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core import redact as r
from herness.core import time as clock
from herness.core.errors import ConfigError, ModelUnavailable, QueryError
from herness.core.ids import new_ulid
from herness.core.jobs import gpu
from herness.core.jobs.gpu import GpuController, WorkerGpuState, gpu_state, request_gpu_class
from herness.core.jobs.gpu_services import LoopbackHttp
from herness.core.jobs.ports import WorkerRow, bind_jobs_backend
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState, bind_ops_backend, breaker, process_state
from herness.core.resilience.ports import EventRow
from herness.core.resilience.settings import ServiceSettings
from herness.core.settings import RedactionConfig
from herness.core.types import GpuClass
from herness.store.ops.core import read_all, run_write
from herness.store.ops.jobs import SqliteJobsBackend
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.unit

OPENJEV_KEY = "openjev-" + "stub-bearer-" + "value-1"  # built at runtime (detect-secrets)
VLLM_KEY = "vllm-" + "stub-bearer-" + "value-2"
REASONING_KEYS = ("model:local-30b", "model:local-lora-14b")


class RecordingJobs(SqliteJobsBackend):
    """The real jobs backend, recording `update_worker` and `set_requested_class` calls."""

    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self.requests: list[tuple[str, object]] = []

    def update_worker(self, worker_id: str, **fields: object) -> None:
        self.updates.append(dict(fields))
        super().update_worker(worker_id, **fields)

    def set_requested_class(self, worker_id: str, cls: Any) -> None:
        self.requests.append((worker_id, cls))
        super().set_requested_class(worker_id, cls)


@dataclass
class Env:
    gpu: FakeGpu
    jobs: RecordingJobs
    ops: SqliteResilienceBackend
    state: ProcessState

    def controller(
        self, http: LoopbackHttp | None = None, worker: str | None = "w1"
    ) -> GpuController:
        return GpuController(worker_id=worker, runner=self.gpu.runner(), http=http or self.gpu.http)

    def worker(self) -> WorkerRow:
        return next(w for w in self.jobs.list_workers() if w.worker_id == "w1")

    def events(self, kind: str) -> list[dict[str, Any]]:
        rows = read_all("SELECT target, detail FROM resilience_event WHERE kind = ?", (kind,))
        return [{"target": row["target"], **json.loads(row["detail"])} for row in rows]

    def verbs(self) -> list[tuple[str, str]]:
        """(verb, service) of every compose command but `ps`."""
        tails = [argv[len(self.gpu.prefix) :] for argv in self.gpu.argv]
        out = []
        for tail in tails:
            verb = "up" if tail[0] == "--profile" else tail[0]
            if verb != "ps":
                out.append((verb, tail[-1]))
        return out


def _worker_row(
    now: datetime, *, loaded: str = "none", slot: int = 1, status: str = "running"
) -> WorkerRow:
    return WorkerRow.model_validate(
        {
            "worker_id": "w1", "host": "h", "pid": 1, "gpu_slot": slot, "cpu_slots": 2,
            "gpu_class_loaded": loaded, "status": status, "started_at": now,
            "heartbeat_at": now, "version": "t",
        }
    )  # fmt: skip


@pytest.fixture
def env(
    fake_gpu: FakeGpu,
    ops_store: OpsStoreHandle,
    fake_keyring: MemoryKeyring,
    reset_process_state: ProcessState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Env]:
    """Full config, keyring keys, a test redactor, both backends bound, worker `w1` alive."""
    config_dir = write_full_config(tmp_path / "cfgroot")
    c.init_config(
        "local", config_dir=config_dir, env={"HERNESS_PATHS__DATA": str(ops_store.data_root)}
    )
    fake_keyring.store[("herness", "openjev_api_key")] = OPENJEV_KEY
    fake_keyring.store[("herness", "vllm.api_key")] = VLLM_KEY
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    monkeypatch.setattr(gpu, "_gpu", lambda: fake_gpu.gpu)
    ops, jobs = SqliteResilienceBackend(), RecordingJobs()
    bind_ops_backend(ops)
    bind_jobs_backend(jobs)
    jobs.upsert_worker(_worker_row(clock.now()))
    yield Env(fake_gpu, jobs, ops, reset_process_state)
    c.reset_config()


def _loaded(env: Env, cls: GpuClass, http: LoopbackHttp | None = None) -> GpuController:
    """A controller with `cls` loaded through a real swap (history then cleared)."""
    ctl = env.controller(http)
    if cls != "none":
        ctl.swap(cls, reason="setup")
    env.gpu.argv.clear()
    env.jobs.updates.clear()
    for stub in env.gpu.stubs.values():
        stub.requests.clear()
    return ctl


def _paths(env: Env, name: str) -> list[tuple[str, str]]:
    return [(q.method, q.path) for q in env.gpu.stubs[name].requests]


@dataclass
class FakeHttp(LoopbackHttp):
    """Health answers from `healthy_names`; counts calls; warm-up always succeeds."""

    healthy_names: set[str] = field(default_factory=set)
    script: list[bool] = field(default_factory=list)  # answered first, in order
    calls: list[tuple[str, float]] = field(default_factory=list)

    def healthy(self, svc: ServiceSettings, *, timeout_s: float = 5) -> bool:
        self.calls.append((svc.url, timeout_s))
        if self.script:
            return self.script.pop(0)
        return svc.url in self.healthy_names

    def warm_up(self, name: Any, svc: ServiceSettings, *, timeout_s: float) -> None:
        return None


# --- UT08-91 swap ---


def test_ut08_91_swap_decider_to_reasoning(env: Env) -> None:
    """UT08-91 decider (openjev running) → reasoning: stop, VRAM, up, health, warm-up, reset."""
    ctl = _loaded(env, "decider")
    ctl.service_start("openjev")
    env.gpu.argv.clear()
    env.jobs.updates.clear()
    breaker("model:local-30b").force_open(ModelUnavailable("down"))
    vram_before = len(env.gpu.vram_argv)

    duration = ctl.swap("reasoning", reason="claimable")

    assert duration >= 0
    assert env.verbs() == [("stop", "openjev"), ("up", "vllm-reasoning")]
    assert len(env.gpu.vram_argv) > vram_before
    assert _paths(env, "vllm-reasoning")[0] == ("GET", "/health")
    assert ("POST", "/v1/chat/completions") in _paths(env, "vllm-reasoning")
    assert env.jobs.updates[0] == {"gpu_class_loaded": "swapping", "requested_class": "reasoning"}
    assert env.jobs.updates[-1] == {"gpu_class_loaded": "reasoning", "requested_class": None}
    assert ctl.loaded == "reasoning" == env.worker().gpu_class_loaded
    assert env.worker().requested_class is None
    swap = env.events("gpu_swap")[-1]
    assert (swap["from"], swap["to"], swap["reason"]) == ("decider", "reasoning", "claimable")
    row = env.ops.health_get("model:local-30b")
    assert row is not None
    assert (row.state, row.failures) == ("closed", 0)
    hist = [k for k, _ in env.state.metric_buffer.histograms]
    assert (
        "herness_jobs_gpu_swap_seconds",
        (("from", "decider"), ("to", "reasoning")),
        "jobs",
    ) in hist
    env.gpu.assert_single_class()


def test_ut08_91_swap_noop_when_loaded_and_healthy(env: Env) -> None:
    """UT08-91 target already loaded and healthy → 0.0 and no compose command."""
    ctl = _loaded(env, "reasoning")
    before = ctl.class_since
    assert ctl.swap("reasoning", reason="again") == 0.0
    assert env.verbs() == []
    assert ctl.class_since == before
    env.gpu.assert_single_class()


def test_ut08_91_swap_reenters_unhealthy_loaded_class(env: Env) -> None:
    """UT08-91 loaded but unhealthy → its service is stopped (VRAM freed) and started again."""
    vllm = env.gpu.service("vllm-reasoning").url
    http = FakeHttp(healthy_names={vllm})
    ctl = _loaded(env, "reasoning", http)
    http.script.append(False)  # the step 1 health check fails once
    assert ctl.swap("reasoning", reason="unhealthy") >= 0
    assert env.verbs() == [("stop", "vllm-reasoning"), ("up", "vllm-reasoning")]
    assert ctl.loaded == "reasoning"
    env.gpu.assert_single_class()


def test_ut08_91_swap_to_none_and_no_worker(env: Env) -> None:
    """UT08-91 swap to `none` stops everything and resets no breaker; worker_id None skips rows."""
    ctl = _loaded(env, "reasoning")
    ctl2 = env.controller(worker=None)
    assert ctl2.detect_loaded_class() == "reasoning"
    env.jobs.updates.clear()
    ctl2.swap("none", reason="unload")
    assert ctl2.loaded == "none"
    assert env.jobs.updates == []
    assert env.gpu.running() == set()
    assert ctl.loaded == "reasoning"  # a separate instance
    env.gpu.assert_single_class()


def test_ut08_91_stop_falls_back_to_kill(env: Env) -> None:
    """UT08-91 step 3: `stop` fails and the service still runs → `compose kill`."""
    ctl = _loaded(env, "reasoning")
    env.gpu.fail["stop"] = (1, "boom")
    ctl.swap("large", reason="claimable")
    assert env.verbs()[:3] == [
        ("stop", "vllm-reasoning"), ("kill", "vllm-reasoning"), ("up", "llamacpp-large"),
    ]  # fmt: skip
    assert ctl.loaded == "large"
    env.gpu.assert_single_class()


def test_ut08_91_construction_sets_kill_hook(env: Env) -> None:
    """UT08-91 construction installs `process_state().kill_service_hook` = compose kill."""
    env.controller()
    hook = process_state().kill_service_hook
    assert hook is not None
    hook("openjev")
    assert env.verbs() == [("kill", "openjev")]


# --- UT08-92 swap failures ---


def test_ut08_92_warmup_failure_fails_closed(env: Env) -> None:
    """UT08-92 warm-up fails → vllm stopped, class none, breaker failure, event, error."""
    ctl = _loaded(env, "decider")
    env.gpu.stubs["vllm-reasoning"].answers["/v1/chat/completions"] = 500
    with pytest.raises(ModelUnavailable):
        ctl.swap("reasoning", reason="claimable")
    assert env.gpu.states["vllm-reasoning"] == "exited"
    assert ctl.loaded == "none" == env.worker().gpu_class_loaded
    for key in REASONING_KEYS:
        row = env.ops.health_get(key)
        assert row is not None
        assert row.failures == 1
    failed = env.events("gpu_swap_failed")[-1]
    assert (failed["from"], failed["to"], failed["step"]) == ("decider", "reasoning", "warmup")
    assert failed["error_type"] == "ModelUnavailable"
    counters = env.state.metric_buffer.counters
    assert counters[("herness_jobs_gpu_swap_failed_total", (("to", "reasoning"),), "jobs")] == 1
    env.gpu.assert_single_class()


def test_ut08_92_health_timeout_fails_start(env: Env, fake_clock: FakeClock) -> None:
    """UT08-92 health never passes within `start_timeout_s` → step `start`, class none."""
    env.state.sleep = fake_clock.sleep
    ctl = _loaded(env, "none", FakeHttp())
    with pytest.raises(ModelUnavailable, match="unhealthy"):
        ctl.swap("large", reason="claimable")
    assert env.events("gpu_swap_failed")[-1]["step"] == "start"
    assert env.gpu.states["llamacpp-large"] == "exited"
    assert ctl.loaded == "none"
    env.gpu.assert_single_class()


def test_ut08_92_vram_not_freed(env: Env, fake_clock: FakeClock) -> None:
    """UT08-92 step 4: VRAM stays busy → `vram_not_freed`, class none, no breaker failure."""
    del fake_clock
    env.gpu.states["vllm-reasoning"] = "running"
    ctl = env.controller()
    env.gpu.sticky.add("vllm-reasoning")
    env.gpu.fail["kill"] = (0, "")  # kill "works" but the service stays up
    with pytest.raises(ModelUnavailable, match="vram_not_freed"):
        ctl.swap("large", reason="claimable")
    failed = env.events("gpu_swap_failed")[-1]
    assert (failed["step"], failed["error_type"]) == ("vram", "ModelUnavailable")
    assert ctl.loaded == "none" == env.worker().gpu_class_loaded
    assert env.ops.health_get("model:local-large-offload") is None


def test_ut08_92_foreign_error_becomes_model_unavailable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-92 any failure (here a fault point raising a foreign error) fails closed."""
    ctl = _loaded(env, "reasoning")

    def boom(name: str, **labels: str) -> None:
        if name == "gpu.after_stop":
            msg = "secret-ish text"
            raise RuntimeError(msg)

    monkeypatch.setattr(gpu, "fault_point", boom)
    with pytest.raises(ModelUnavailable, match="gpu swap failed") as info:
        ctl.swap("large", reason="claimable")
    assert "secret" not in str(info.value)
    assert env.events("gpu_swap_failed")[-1]["step"] == "stop"
    assert ctl.loaded == "none"


def test_ut08_92_stop_errors_logged_not_raised(env: Env) -> None:
    """UT08-92 step 8 stops the target's services; their errors are logged, not raised."""
    ctl = _loaded(env, "decider")
    env.gpu.stubs["vllm-reasoning"].answers["/v1/chat/completions"] = 500
    env.gpu.sticky.add("vllm-reasoning")
    env.gpu.fail["kill"] = (1, "nope")
    with structlog.testing.capture_logs() as logs, pytest.raises(ModelUnavailable):
        ctl.swap("reasoning", reason="claimable")
    stops = [e for e in logs if e["event"] == "jobs.gpu.stop_failed"]
    assert stops
    assert all(set(e) >= {"service", "error_type"} for e in stops)
    assert ctl.loaded == "none"


# --- UT08-93 service start/stop ---


def test_ut08_93_service_start_class_check_and_openjev(env: Env) -> None:
    """UT08-93 decider loaded: other class → ConfigError; openjev: VRAM, health, warm-up."""
    ctl = _loaded(env, "decider")
    breaker("decider:openjev").force_open(ModelUnavailable("down"))
    with pytest.raises(ConfigError, match="belongs to another class; use require_gpu_class"):
        ctl.service_start("vllm-reasoning")
    vram_before = len(env.gpu.vram_argv)
    ctl.service_start("openjev")
    assert env.verbs() == [("up", "openjev")]
    assert len(env.gpu.vram_argv) > vram_before
    assert _paths(env, "openjev") == [("GET", "/v1/models"), ("POST", "/v1/systemone")]
    started = env.events("service_start")[-1]
    assert (started["target"], started["service"]) == ("openjev", "openjev")
    row = env.ops.health_get("decider:openjev")
    assert row is not None
    assert row.state == "closed"
    assert ctl.loaded == "decider" == env.worker().gpu_class_loaded
    env.gpu.assert_single_class()


def test_ut08_93_service_start_failure(env: Env) -> None:
    """UT08-93 openjev warm-up fails → it is stopped, its breaker fails, ModelUnavailable."""
    ctl = _loaded(env, "decider")
    env.gpu.stubs["openjev"].answers["/v1/systemone"] = 500
    with pytest.raises(ModelUnavailable):
        ctl.service_start("openjev", timeout_s=30)
    assert env.gpu.states["openjev"] == "exited"
    row = env.ops.health_get("decider:openjev")
    assert row is not None
    assert row.failures == 1
    assert ctl.loaded == "decider"


def test_ut08_93_service_stop_and_healthy(env: Env) -> None:
    """UT08-93 `service_stop` class check, stop, VRAM wait, event; `service_healthy` GET."""
    ctl = _loaded(env, "decider")
    ctl.service_start("openjev")
    assert ctl.service_healthy("openjev") is True
    with pytest.raises(ConfigError):
        ctl.service_stop("llamacpp-large")
    with pytest.raises(ConfigError):
        ctl.service_healthy("nope")  # type: ignore[arg-type]
    ctl.service_stop("openjev")
    assert env.gpu.states["openjev"] == "exited"
    assert env.events("service_stop")[-1]["service"] == "openjev"


# --- UT08-94 detection ---


def test_ut08_94_two_classes_stopped(env: Env) -> None:
    """UT08-94 vllm and openjev both running → both stopped, class none."""
    env.gpu.states.update({"vllm-reasoning": "running", "openjev": "running"})
    ctl = env.controller()
    assert ctl.detect_loaded_class() == "none"
    assert env.gpu.running() == set()
    assert ctl.loaded == "none" == env.worker().gpu_class_loaded
    # The two-class state is the test's setup; from the first stop on it never recurs.
    first_stop = next(i for i, argv in enumerate(env.gpu.argv) if "stop" in argv)
    assert all(len(cls) <= 1 for cls in env.gpu.running_classes[first_stop + 1 :])


@pytest.mark.parametrize(
    ("running", "unhealthy", "expected"),
    [
        ((), None, "none"),
        (("openjev",), None, "decider"),
        (("vllm-reasoning",), None, "reasoning"),
        (("vllm-reasoning",), "/health", "none"),
    ],
)
def test_ut08_94_detect_single_class(
    env: Env, running: tuple[str, ...], unhealthy: str | None, expected: str
) -> None:
    """UT08-94 one healthy class is kept; unhealthy services are stopped; nothing → none."""
    for name in running:
        env.gpu.states[name] = "running"
        if unhealthy is not None:
            env.gpu.stubs[name].answers[unhealthy] = 503
    ctl = env.controller()
    assert ctl.detect_loaded_class() == expected
    assert env.worker().gpu_class_loaded == expected
    if expected == "none":
        assert env.gpu.running() == set()
    env.gpu.assert_single_class()


def test_ut08_94_entry_service_missing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-94 a class whose start_on_entry services are not all running → none."""
    extra = env.gpu.gpu.classes["reasoning"].services["vllm-reasoning"].model_copy()
    classes = dict(env.gpu.gpu.classes)
    large = classes["large"].model_copy(
        update={"services": {**classes["large"].services, "vllm-reasoning": extra}}
    )
    classes["large"] = large
    patched = env.gpu.gpu.model_copy(update={"classes": classes})
    monkeypatch.setattr(gpu, "_gpu", lambda: patched)
    monkeypatch.setattr(gpu, "_all_services", lambda: {"llamacpp-large": ("large", extra)})
    env.gpu.states["llamacpp-large"] = "running"
    assert env.controller().detect_loaded_class() == "none"


# --- UT08-96 restart rate limit ---


def _restart_event(env: Env, minutes_ago: int) -> None:
    ts = clock.now() - timedelta(minutes=minutes_ago)
    detail: dict[str, Any] = {"service": "vllm-reasoning"}
    env.ops.insert_event(
        EventRow("evt_" + new_ulid(), ts, "service_restart", "jobs", "vllm-reasoning",
                 None, None, None, detail)
    )  # fmt: skip


def test_ut08_96_restart_rate_limited(env: Env) -> None:
    """UT08-96 one `service_restart` 30 min ago → False, nothing restarted."""
    ctl = _loaded(env, "reasoning")
    _restart_event(env, 30)
    assert ctl.restart_service("vllm-reasoning") is False
    assert env.verbs() == []


def test_ut08_96_restart_after_an_hour(env: Env) -> None:
    """UT08-96 the event 61 min ago → True: stop, up, health, warm-up, `service_restart`."""
    ctl = _loaded(env, "reasoning")
    _restart_event(env, 61)
    assert ctl.restart_service("vllm-reasoning") is True
    assert env.verbs() == [("stop", "vllm-reasoning"), ("up", "vllm-reasoning")]
    assert ("POST", "/v1/chat/completions") in _paths(env, "vllm-reasoning")
    restarted = env.events("service_restart")[-1]
    assert (restarted["target"], restarted["reason"]) == ("vllm-reasoning", "breaker_open")
    assert env.gpu.running() == {"vllm-reasoning"}
    env.gpu.assert_single_class()


def test_ut08_96_restart_failure_counts(env: Env) -> None:
    """UT08-96 a failed restart is recorded (it counts toward the cap) and raises."""
    ctl = _loaded(env, "reasoning")
    env.gpu.stubs["vllm-reasoning"].answers["/v1/chat/completions"] = 500
    with pytest.raises(ModelUnavailable):
        ctl.restart_service("vllm-reasoning")
    assert env.events("service_restart")[-1]["reason"] == "failed"
    assert ctl.restart_service("vllm-reasoning") is False
    with pytest.raises(ConfigError):
        ctl.restart_service("openjev")


# --- UT08-108 request_gpu_class ---


def test_ut08_108_request_gpu_class(env: Env) -> None:
    """UT08-108 no worker → no_worker (no write); alive → requested + INFO; stale → no_worker."""
    run_write(lambda conn: conn.execute("DELETE FROM worker"), op="test")
    assert request_gpu_class("large") == "no_worker"
    assert env.jobs.requests == []

    env.jobs.upsert_worker(_worker_row(clock.now()))
    with structlog.testing.capture_logs() as logs:
        assert request_gpu_class("large") == "requested"
    assert env.worker().requested_class == "large"
    info = [e for e in logs if e["event"] == "jobs.gpu.class_requested"]
    assert len(info) == 1
    assert (info[0]["log_level"], info[0]["worker_id"], info[0]["class"]) == ("info", "w1", "large")
    assert request_gpu_class("none") == "requested"
    assert env.worker().requested_class == "none"

    env.jobs.upsert_worker(_worker_row(clock.now() - timedelta(seconds=91)))
    env.jobs.requests.clear()
    assert request_gpu_class("large") == "no_worker"
    assert env.jobs.requests == []


@pytest.mark.parametrize(("slot", "status"), [(0, "running"), (1, "stopped")])
def test_ut08_108_not_a_gpu_worker(env: Env, slot: int, status: str) -> None:
    """UT08-108 a worker without the GPU slot, or stopped, is not asked."""
    env.jobs.upsert_worker(_worker_row(clock.now(), slot=slot, status=status))
    assert request_gpu_class("reasoning") == "no_worker"


def test_ut08_108_heartbeat_89s_alive(env: Env) -> None:
    """UT08-108 heartbeat 89 s old (heartbeat_s 30) is alive; 91 s is not (U08-54)."""
    env.jobs.upsert_worker(_worker_row(clock.now() - timedelta(seconds=89)))
    assert request_gpu_class("reasoning") == "requested"


def test_ut08_108_unbound_backend_raises(reset_process_state: ProcessState) -> None:
    """UT08-108 no jobs backend bound → ConfigError (U08-41)."""
    del reset_process_state
    with pytest.raises(ConfigError):
        request_gpu_class("none")


# --- CV T08-18 gpu_state / WorkerGpuState ---


class CountingJobs(RecordingJobs):
    def __init__(self) -> None:
        super().__init__()
        self.lists = 0

    def list_workers(self) -> list[WorkerRow]:
        self.lists += 1
        return super().list_workers()


def test_cv_t08_18_loaded_class_cached_5s(env: Env, fake_clock: FakeClock) -> None:
    """CV T08-18 `loaded_class` reads `list_workers` at most every 5 s; alive gpu_slot=1 worker."""
    jobs = CountingJobs()
    bind_jobs_backend(jobs)
    jobs.upsert_worker(_worker_row(clock.now(), loaded="reasoning"))
    state = WorkerGpuState(http=FakeHttp())
    assert state.loaded_class() == "reasoning"
    jobs.update_worker("w1", gpu_class_loaded="swapping")
    fake_clock.advance(4.9)
    assert state.loaded_class() == "reasoning"
    assert jobs.lists == 1
    fake_clock.advance(0.2)
    assert state.loaded_class() == "swapping"
    assert jobs.lists == 2


def test_cv_t08_18_loaded_class_none_without_alive_worker(env: Env, fake_clock: FakeClock) -> None:
    """CV T08-18 no alive GPU worker (stale heartbeat, or gpu_slot 0) → `none`."""
    env.jobs.upsert_worker(_worker_row(clock.now() - timedelta(seconds=91), loaded="large"))
    assert WorkerGpuState(http=FakeHttp()).loaded_class() == "none"
    env.jobs.upsert_worker(_worker_row(clock.now(), loaded="large", slot=0))
    fake_clock.advance(1)
    assert WorkerGpuState(http=FakeHttp()).loaded_class() == "none"


def test_cv_t08_18_service_healthy_cached_per_service(env: Env, fake_clock: FakeClock) -> None:
    """CV T08-18 `service_healthy` GETs (2 s timeout) at most every 5 s per service."""
    vllm = env.gpu.service("vllm-reasoning").url
    http = FakeHttp(healthy_names={vllm})
    state = WorkerGpuState(http=http)
    assert state.service_healthy("vllm-reasoning") is True
    assert state.service_healthy("openjev") is False
    http.healthy_names.clear()
    fake_clock.advance(4.9)
    assert state.service_healthy("vllm-reasoning") is True
    assert len(http.calls) == 2
    assert {t for _, t in http.calls} == {2}
    fake_clock.advance(0.2)
    assert state.service_healthy("vllm-reasoning") is False
    assert len(http.calls) == 3
    with pytest.raises(ConfigError):
        state.service_healthy("nope")  # type: ignore[arg-type]


def test_cv_t08_18_gpu_state_is_process_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    """CV T08-18 `gpu_state()` returns one process-wide `WorkerGpuState`."""
    monkeypatch.setattr(gpu._Holder, "reader", None)
    first = gpu_state()
    assert isinstance(first, WorkerGpuState)
    assert gpu_state() is first


def test_cv_t08_18_store_error_propagates(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """CV T08-18 a store failure writing the worker row is not swallowed by the swap."""
    ctl = _loaded(env, "none")

    def broken(worker_id: str, **fields: object) -> None:
        msg = "db"
        raise QueryError(msg)

    monkeypatch.setattr(env.jobs, "update_worker", broken)
    with pytest.raises(QueryError):
        ctl.swap("decider", reason="x")
