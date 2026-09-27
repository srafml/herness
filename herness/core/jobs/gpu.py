"""GPU swap and service control, the GPU state reader and external class requests.

Impl 08 U08-81 (`GpuController`), U08-83 (`gpu_state`, `WorkerGpuState`) and U08-102
(`request_gpu_class`); design 08 §3.5, §5.3, §5.8. Every compose command, health GET,
warm-up and VRAM reading goes through `gpu_services` (argument lists, `shell=False`,
bounded output; TH08-01). Only the supervisor, or `run_inline` holding the GPU lock,
constructs a `GpuController`; this module never takes the GPU lock itself (TH08-09).
Failures fail closed: the loaded class becomes `none` and the error is `ModelUnavailable`.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal, cast

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError, HernessError, ModelUnavailable
from herness.core.jobs.gpu_services import LoopbackHttp, wait_vram_free
from herness.core.jobs.ports import require_jobs_backend
from herness.core.logging import get_logger
from herness.core.resilience._state import process_state, require_ops_backend
from herness.core.resilience.breaker import breaker
from herness.core.resilience.events import record_event
from herness.core.resilience.faults import fault_point
from herness.core.resilience.metrics import record_counter, record_histogram
from herness.core.resilience.policies import GPU_HEALTH_POLICY, full_jitter_delay

if TYPE_CHECKING:
    from herness.core.jobs.gpu_services import ComposeRunner
    from herness.core.jobs.ports import WorkerRow
    from herness.core.resilience.ports import GpuStateReader
    from herness.core.resilience.settings import GpuSettings, ServiceSettings
    from herness.core.types import GpuClass, ServiceName

__all__ = ["GpuController", "WorkerGpuState", "gpu_state", "request_gpu_class"]

type LoadedClass = GpuClass | Literal["swapping"]

HEALTH_TIMEOUT_S: Final = 5  # `service_healthy`: one health GET (design 08 §5.8)
STATE_TTL_S: Final = 5.0  # `WorkerGpuState` cache lifetime (U08-83)
STATE_HEALTH_TIMEOUT_S: Final = 2
RESTART_WINDOW: Final = timedelta(hours=1)
ALIVE_HEARTBEATS: Final = 3  # U08-54: alive while heartbeat_at > now - 3 x heartbeat_s
ALIVE_STATUSES: Final = frozenset({"starting", "running", "draining"})
SWAP_SECONDS: Final = "herness_jobs_gpu_swap_seconds"
SWAP_FAILED: Final = "herness_jobs_gpu_swap_failed_total"
OPENJEV_KEY: Final = "decider:openjev"

_log = get_logger("jobs")


def _gpu() -> GpuSettings:
    return get_config().resilience.resilience.gpu


def _services(cls: GpuClass) -> dict[ServiceName, ServiceSettings]:
    """`svc_of(cls)`: the configured services of `cls` (none for class `none`)."""
    spec = _gpu().classes.get(cls) if cls != "none" else None
    return dict(spec.services) if spec is not None else {}


def _all_services() -> dict[ServiceName, tuple[GpuClass, ServiceSettings]]:
    return {n: (c, s) for c, spec in _gpu().classes.items() for n, s in spec.services.items()}


def _endpoints(cls: GpuClass) -> list[str]:
    """Breaker keys of `cls`: `model:<k>` per `models.yaml` client, plus `decider:openjev`."""
    if cls == "none":
        return []
    clients = get_config().models.models.clients
    keys = [f"model:{k}" for k, client in clients.items() if client.gpu_class == cls]
    return [*keys, OPENJEV_KEY] if cls == "decider" else keys


def _unavailable(exc: BaseException, what: str) -> ModelUnavailable:
    """Every failure leaves as `ModelUnavailable`; no foreign error text is kept."""
    if isinstance(exc, ModelUnavailable):
        return exc
    msg = f"gpu {what} failed"
    return ModelUnavailable(msg)


def _alive(row: WorkerRow, now: datetime, heartbeat_s: float) -> bool:
    """The worker liveness rule of U08-54 (design 08 §3.4, R-44)."""
    fresh = row.heartbeat_at > now - timedelta(seconds=ALIVE_HEARTBEATS * heartbeat_s)
    return row.status in ALIVE_STATUSES and fresh


def _gpu_worker(workers: Sequence[WorkerRow]) -> WorkerRow | None:
    """The alive worker with `gpu_slot = 1` (latest heartbeat first), or None."""
    heartbeat_s = get_config().resilience.resilience.jobs.heartbeat_s
    now = clock.now()
    alive = [w for w in workers if w.gpu_slot == 1 and _alive(w, now, heartbeat_s)]
    return max(alive, key=lambda w: w.heartbeat_at, default=None)


class GpuController:
    """Swap, service start/stop/health, detection and restart for one GPU (U08-81)."""

    def __init__(self, *, worker_id: str | None, runner: ComposeRunner, http: LoopbackHttp) -> None:
        self._worker_id, self._runner, self._http = worker_id, runner, http
        self._loaded: GpuClass = "none"
        self._since = clock.now()
        self._lock = threading.Lock()

        def kill(name: str) -> None:  # the `kill_service:` fault action (U08-37)
            runner.kill(cast("ServiceName", name))  # the runner checks the name

        process_state().kill_service_hook = kill

    @property
    def loaded(self) -> GpuClass:
        """The loaded class; equals `worker.gpu_class_loaded` after every method returns."""
        return self._loaded

    @property
    def class_since(self) -> datetime:
        """When the loaded class was last set."""
        return self._since

    def _set_worker(self, loaded: LoadedClass, **fields: object) -> None:
        if self._worker_id is not None:
            backend = require_jobs_backend()
            backend.update_worker(self._worker_id, gpu_class_loaded=loaded, **fields)

    def _set_loaded(self, cls: GpuClass, **fields: object) -> None:
        self._loaded, self._since = cls, clock.now()
        self._set_worker(cls, **fields)

    def _running(self) -> list[ServiceName]:
        known = _all_services()
        states = self._runner.ps()
        return [n for n, st in states.items() if st == "running" and n in known]

    def _stop(self, name: ServiceName) -> None:
        """`compose stop`; `compose kill` when `ps` still shows it running (step 3)."""
        try:
            self._runner.stop(name)
        except ModelUnavailable as exc:
            _log.warning("jobs.gpu.stop_failed", service=name, error_type=type(exc).__name__)
        if self._runner.ps().get(name) == "running":
            self._runner.kill(name)

    def _stop_quietly(self, names: Iterable[ServiceName]) -> None:
        """Stop each service; errors are logged (error type only), never raised."""
        for name in names:
            try:
                self._stop(name)
            except HernessError as exc:
                _log.warning("jobs.gpu.stop_failed", service=name, error_type=type(exc).__name__)

    def _own(self, name: ServiceName) -> ServiceSettings:
        """Settings of `name` when it belongs to the loaded class, else `ConfigError`."""
        svc = _services(self._loaded).get(name)
        if svc is None:
            msg = f"service {str(name)[:64]} belongs to another class; use require_gpu_class"
            raise ConfigError(msg)
        return svc

    def _await_healthy(self, svc: ServiceSettings, max_elapsed_s: float) -> None:
        """Poll `http.healthy` under `GPU_HEALTH_POLICY` with `max_elapsed_s` (step 5)."""
        p = dataclasses.replace(GPU_HEALTH_POLICY, max_elapsed_s=float(max_elapsed_s))
        state, start = process_state(), clock.monotonic()
        for attempt in range(1, p.attempts + 1):
            if self._http.healthy(svc, timeout_s=p.timeout_s or HEALTH_TIMEOUT_S):
                return
            if clock.monotonic() - start >= p.max_elapsed_s:
                break
            state.sleep(full_jitter_delay(attempt, p, rng=state.rng))
        msg = "unhealthy"
        raise ModelUnavailable(msg)

    def _bring_up(self, cls: GpuClass, name: ServiceName, max_elapsed_s: float) -> None:
        """`compose up`, the health poll and the `gpu.after_start` fault point."""
        self._runner.up(cls, name)
        self._await_healthy(_services(cls)[name], max_elapsed_s)
        fault_point("gpu.after_start")

    def _warm(self, name: ServiceName) -> None:
        svc = _all_services()[name][1]
        self._http.warm_up(name, svc, timeout_s=_gpu().warmup_timeout_s)

    @staticmethod
    def _record_failures(keys: Iterable[str], err: ModelUnavailable) -> None:
        for key in keys:
            breaker(key).record_failure(err)

    def _entry_healthy(self, target: GpuClass) -> bool:
        entry = [(n, s) for n, s in _services(target).items() if s.start_on_entry]
        return all(self._http.healthy(s, timeout_s=HEALTH_TIMEOUT_S) for _, s in entry)

    def _swap_steps(self, target: GpuClass, step: list[str]) -> None:
        """Steps 3-6; `step[0]` names the step running, for the failure event."""
        # Every running service is stopped, the target's own too: the target is only reached
        # here while unhealthy, and the step 4 VRAM check cannot pass while it holds memory.
        for name in self._running():
            self._stop(name)
        fault_point("gpu.after_stop")
        step[0] = "vram"
        wait_vram_free(threshold_mb=_gpu().vram_free_threshold_mb)
        entry = [n for n, s in _services(target).items() if s.start_on_entry]
        step[0] = "start"
        for name in entry:
            self._bring_up(target, name, _services(target)[name].start_timeout_s)
        step[0] = "warmup"
        for name in entry:
            self._warm(name)

    def _swap_failed(
        self, source: GpuClass, target: GpuClass, step: str, err: ModelUnavailable
    ) -> None:
        """Step 8 (and step 4): fail closed to class `none`."""
        self._stop_quietly(_services(target))
        self._set_loaded("none")
        if step in {"start", "warmup"}:
            self._record_failures(_endpoints(target), err)
        detail = {"from": source, "to": target, "step": step, "error_type": type(err).__name__}
        record_event("gpu_swap_failed", component="jobs", target=target, detail=detail)
        record_counter(SWAP_FAILED, component="jobs", labels={"to": target})

    def swap(self, target: GpuClass, *, reason: str) -> float:
        """Load class `target`; the duration in seconds (0.0 when already loaded and healthy)."""
        with self._lock:
            if target == self._loaded and self._entry_healthy(target):
                return 0.0
            source, t0, step = self._loaded, clock.monotonic(), ["stop"]
            self._set_worker("swapping", requested_class=target)
            try:
                self._swap_steps(target, step)
            except Exception as exc:  # noqa: BLE001 - every failure fails closed (step 8)
                err = _unavailable(exc, "swap")
                self._swap_failed(source, target, step[0], err)
                raise err from None
            duration = clock.monotonic() - t0
            self._set_loaded(target, requested_class=None)
            keys = _endpoints(target)
            if keys:
                require_ops_backend().health_reset(keys, clock.now())
            detail = {"from": source, "to": target, "duration_s": duration, "reason": reason}
            record_event("gpu_swap", component="jobs", target=target, detail=detail)
            labels = {"from": source, "to": target}
            record_histogram(SWAP_SECONDS, duration, component="jobs", labels=labels)
            return duration

    def _service_event(self, kind: str, name: ServiceName, t0: float, reason: str) -> None:
        detail = {"service": name, "duration_s": clock.monotonic() - t0, "reason": reason}
        record_event(kind, component="jobs", target=name, detail=detail)

    def _start_one(self, name: ServiceName, max_elapsed_s: float, *, stop_first: bool) -> None:
        """Steps 4-6 for one service of the loaded class; failure stops it, fails its breakers."""
        try:
            if stop_first:
                self._stop(name)
            wait_vram_free(threshold_mb=_gpu().vram_free_threshold_mb)
            self._bring_up(self._loaded, name, max_elapsed_s)
            self._warm(name)
        except Exception as exc:  # noqa: BLE001 - every failure leaves as ModelUnavailable
            err = _unavailable(exc, "service start")
            self._stop_quietly([name])
            self._record_failures(_endpoints(self._loaded), err)
            raise err from None

    def service_start(self, name: ServiceName, *, timeout_s: float | None = None) -> None:
        """Start one service of the loaded class (the handler released in-process models)."""
        with self._lock:
            svc, t0 = self._own(name), clock.monotonic()
            self._start_one(name, timeout_s or svc.start_timeout_s, stop_first=False)
            if name == "openjev":
                require_ops_backend().health_reset([OPENJEV_KEY], clock.now())
            self._service_event("service_start", name, t0, "requested")

    def service_stop(self, name: ServiceName) -> None:
        """Stop one service of the loaded class (kill fallback) and wait for free VRAM."""
        with self._lock:
            self._own(name)
            t0 = clock.monotonic()
            self._stop(name)
            wait_vram_free(threshold_mb=_gpu().vram_free_threshold_mb)
            self._service_event("service_stop", name, t0, "requested")

    def service_healthy(self, name: ServiceName) -> bool:
        """One health GET of `name` with a 5 s timeout."""
        with self._lock:
            entry = _all_services().get(name)
            if entry is None:
                msg = "service is not a configured service name"
                raise ConfigError(msg)
            return self._http.healthy(entry[1], timeout_s=HEALTH_TIMEOUT_S)

    def _detected(self, running: Sequence[ServiceName]) -> GpuClass:
        known = _all_services()
        classes = {known[n][0] for n in running}
        if len(classes) != 1:
            return "none"
        (cls,) = classes
        if not all(self._http.healthy(known[n][1], timeout_s=HEALTH_TIMEOUT_S) for n in running):
            return "none"
        entry = [n for n, s in _services(cls).items() if s.start_on_entry]
        return cls if all(n in running for n in entry) else "none"

    def detect_loaded_class(self) -> GpuClass:
        """Class from `compose ps` plus health; two classes or unhealthy → stop all, `none`."""
        with self._lock:
            running = self._running()
            cls = self._detected(running)
            if cls == "none":
                self._stop_quietly(running)
            self._set_loaded(cls)
            return cls

    def restart_service(self, name: ServiceName) -> bool:
        """Restart a loaded service (open `model:` breaker); False when rate-limited (§5.3)."""
        with self._lock:
            svc = self._own(name)
            since = clock.now() - RESTART_WINDOW
            done = require_ops_backend().count_events("service_restart", target=name, since=since)
            if done >= get_config().resilience.resilience.breakers.restart_max_per_hour:
                return False
            t0 = clock.monotonic()
            try:
                self._start_one(name, svc.start_timeout_s, stop_first=True)
            except ModelUnavailable:
                self._service_event("service_restart", name, t0, "failed")  # counts toward the cap
                raise
            self._service_event("service_restart", name, t0, "breaker_open")
            return True


class WorkerGpuState:
    """`GpuStateReader` over the worker row and cached health GETs (U08-83)."""

    def __init__(self, http: LoopbackHttp | None = None) -> None:
        self._http = http if http is not None else LoopbackHttp()
        self._lock = threading.Lock()
        self._loaded: tuple[float, LoadedClass] | None = None
        self._health: dict[str, tuple[float, bool]] = {}

    def loaded_class(self) -> LoadedClass:
        """`gpu_class_loaded` of the alive GPU worker, else `none`; `list_workers` ≤ 1 per 5 s."""
        with self._lock:
            now = clock.monotonic()
            if self._loaded is not None and now - self._loaded[0] < STATE_TTL_S:
                return self._loaded[1]
            worker = _gpu_worker(require_jobs_backend().list_workers())
            value: LoadedClass = "none" if worker is None else worker.gpu_class_loaded
            self._loaded = (now, value)
            return value

    def service_healthy(self, name: ServiceName) -> bool:
        """One health GET (2 s timeout) per service at most every 5 s; the result is cached."""
        now = clock.monotonic()
        with self._lock:
            cached = self._health.get(name)
        if cached is not None and now - cached[0] < STATE_TTL_S:
            return cached[1]
        entry = _all_services().get(name)
        if entry is None:
            msg = "service is not a configured service name"
            raise ConfigError(msg)
        healthy = self._http.healthy(entry[1], timeout_s=STATE_HEALTH_TIMEOUT_S)
        with self._lock:
            self._health[name] = (now, healthy)
        return healthy


class _Holder:  # owns the process-wide reader (built on first use)
    reader: WorkerGpuState | None = None
    lock: Final = threading.Lock()


def gpu_state() -> GpuStateReader:
    """The process-wide `WorkerGpuState` (design 08 §3.5)."""
    with _Holder.lock:
        if _Holder.reader is None:
            _Holder.reader = WorkerGpuState()
        return _Holder.reader


def request_gpu_class(cls: GpuClass) -> Literal["requested", "no_worker"]:
    """Ask the alive GPU worker's arbiter for `cls` (`gpu load/unload`, `deploy up/down`)."""
    backend = require_jobs_backend()
    worker = _gpu_worker(backend.list_workers())
    if worker is None:
        return "no_worker"
    backend.set_requested_class(worker.worker_id, cls)
    _log.info("jobs.gpu.class_requested", worker_id=worker.worker_id, **{"class": cls})
    return "requested"
