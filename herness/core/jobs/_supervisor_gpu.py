"""The supervisor's GPU slot (private sibling of `supervisor`, T08-21).

One `GpuController` and one single-thread executor (`herness-gpu`) that runs every swap,
service call and restart, so the tick loop never blocks on the GPU (U08-87). The arbiter
step of U08-87 7g lives here: an external class request the window does not allow is
rejected with WARNING `jobs.gpu.request_rejected` and cleared (TH08-09), a `swap` decision
is submitted (a `ModelUnavailable` swap postpones that class's due jobs by 10 minutes), and
`keep` returns the claim filter. Kept apart from `herness.core.jobs.supervisor` for the
impl 08 §2 line budget of that module.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, NamedTuple

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError, HernessError, ModelUnavailable
from herness.core.jobs.arbiter import arbiter_decide
from herness.core.jobs.gpu import GpuController
from herness.core.jobs.gpu_services import ComposeRunner, LoopbackHttp
from herness.core.jobs.pipe import REPLY_MESSAGE_CHARS, GpuReplyMsg, GpuRequestMsg
from herness.core.jobs.ports import require_jobs_backend
from herness.core.jobs.queue import GPU_SLOT_KINDS
from herness.core.jobs.windows import window_at
from herness.core.logging import get_logger
from herness.core.resilience._state import require_ops_backend

if TYPE_CHECKING:
    from collections.abc import Sequence

    from herness.core.jobs._supervisor_child import ChildRun
    from herness.core.jobs.ports import WorkerRow
    from herness.core.jobs.windows import ActiveWindow
    from herness.core.types import GpuClass, JobKind

__all__ = ["ClaimFilter", "GpuSlot", "chat_rule", "reject_request"]

POSTPONE: Final = timedelta(minutes=10)  # design 08 §5.8: jobs of a failed class wait 10 min
_NOT_GPU_SLOT: Final = "GPU control requires the GPU slot"

_log = get_logger("jobs")


class ClaimFilter(NamedTuple):
    """GPU-slot claim filter: allowed classes and the chat-window priority rule."""

    classes: list[GpuClass]
    min_priority: int | None
    exempt_kinds: list[JobKind]


def chat_rule(window: ActiveWindow) -> tuple[int | None, list[JobKind]]:
    """`(min_priority, exempt kinds)` of the chat window, else no filter (design 08 §5.10)."""
    if window.spec.name != "chat":
        return None, []
    return get_config().resilience.schedule.batch_in_chat_min_priority, ["chat"]


def _reply(request_id: str, error_class: str, text: str) -> GpuReplyMsg:
    return GpuReplyMsg(
        request_id=request_id,
        ok=False,
        error_class="ConfigError" if error_class == "ConfigError" else "ModelUnavailable",
        message=text[:REPLY_MESSAGE_CHARS],
    )


def _done(reply: GpuReplyMsg) -> Future[GpuReplyMsg]:
    future: Future[GpuReplyMsg] = Future()
    future.set_result(reply)
    return future


def reject_request(run: ChildRun, msg: GpuRequestMsg) -> None:
    """A `gpu_request` from a child without the GPU slot: `ConfigError` (U08-85)."""
    run.pending[msg.request_id] = _done(_reply(msg.request_id, "ConfigError", _NOT_GPU_SLOT))


class GpuSlot:
    """GPU controller plus its executor, for a worker with a GPU slot (U08-87 step 3)."""

    def __init__(self, worker_id: str, classes: Sequence[GpuClass]) -> None:
        http, runner = LoopbackHttp(), ComposeRunner()
        self.controller = GpuController(worker_id=worker_id, runner=runner, http=http)
        self.classes: tuple[GpuClass, ...] = tuple(c for c in classes if c != "none")
        self._worker_id = worker_id
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="herness-gpu")
        self._futures: set[Future[object]] = set()

    @property
    def loaded(self) -> GpuClass:
        return self.controller.loaded

    def idle(self) -> bool:
        """True when no GPU work is queued or running on the executor."""
        self._futures = {f for f in self._futures if not f.done()}
        return not self._futures

    def _submit(self, fn: Callable[[], object]) -> Future[object]:
        future = self._executor.submit(fn)
        self._futures.add(future)
        return future

    def close(self) -> None:
        """Stop taking work; a swap already running finishes in its thread."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def request(self, run: ChildRun, msg: GpuRequestMsg) -> None:
        """A child's `gpu_request` (U08-87 7b): run on the executor, reply when done."""
        if run.slot != "gpu":
            reject_request(run, msg)
            return
        future: Future[GpuReplyMsg] = self._executor.submit(self._serve, msg)
        self._futures.add(future)  # type: ignore[arg-type]  # Future is covariant in use
        run.pending[msg.request_id] = future

    def _serve(self, msg: GpuRequestMsg) -> GpuReplyMsg:
        rid = msg.request_id
        try:
            return self._call(msg)
        except ConfigError as exc:
            return _reply(rid, "ConfigError", exc.message)
        except ModelUnavailable as exc:
            return _reply(rid, "ModelUnavailable", exc.message)
        except Exception as exc:  # noqa: BLE001 - every other failure is reported, never raised
            return _reply(rid, "ModelUnavailable", f"gpu request failed: {type(exc).__name__}")

    def _call(self, msg: GpuRequestMsg) -> GpuReplyMsg:
        ctl, rid = self.controller, msg.request_id
        if msg.op == "require_class":
            if msg.cls is None:
                return _reply(rid, "ConfigError", "require_class needs a class")
            previous = ctl.loaded
            ctl.swap(msg.cls, reason="in_job")
            return GpuReplyMsg(request_id=rid, ok=True, previous_class=previous)
        if msg.service is None:
            return _reply(rid, "ConfigError", "service request needs a service")
        if msg.op == "service_start":
            ctl.service_start(msg.service, timeout_s=msg.timeout_s)
        elif msg.op == "service_stop":
            ctl.service_stop(msg.service)
        else:
            return GpuReplyMsg(request_id=rid, ok=True, healthy=ctl.service_healthy(msg.service))
        return GpuReplyMsg(request_id=rid, ok=True)

    def _swap_job(self, target: GpuClass, reason: str) -> None:
        try:
            self.controller.swap(target, reason=reason)
        except ModelUnavailable:
            now = clock.now()
            require_jobs_backend().postpone_class(target, now, now + POSTPONE)
        except HernessError as exc:
            _log.error("jobs.gpu.swap_failed", to=target, error_type=type(exc).__name__)

    def swap(self, target: GpuClass, reason: str) -> None:
        """Submit a swap to `target` (U08-87 7g `swap`)."""
        self._submit(lambda: self._swap_job(target, reason))

    def _restart_job(self, service: str) -> None:
        try:
            self.controller.restart_service(service)  # type: ignore[arg-type]  # configured name
        except HernessError as exc:
            _log.warning("jobs.gpu.restart_failed", service=service, error_type=type(exc).__name__)

    def restart_check(self) -> None:
        """U08-87 7a: restart the only service of the loaded class when a `model:` breaker of
        a client on that class is open (rate-limited by `GpuController.restart_service`)."""
        loaded, cfg = self.loaded, get_config()
        spec = cfg.resilience.resilience.gpu.classes.get(loaded) if loaded != "none" else None
        if spec is None or len(spec.services) != 1 or not self.idle():
            return
        (service,) = spec.services
        clients = cfg.models.models.clients
        for row in require_ops_backend().health_list(["open"]):
            client = clients.get(row.source.removeprefix("model:"))
            model_key = row.source.startswith("model:") and client is not None
            if model_key and client is not None and client.gpu_class == loaded:
                self._submit(lambda: self._restart_job(service))
                return

    def _reject(self, requested: GpuClass, window: ActiveWindow) -> None:
        _log.warning("jobs.gpu.request_rejected", requested=requested, window=window.spec.name)
        require_jobs_backend().set_requested_class(self._worker_id, None)

    def plan(self, now: datetime, worker: WorkerRow | None, *, preload: bool) -> ClaimFilter | None:
        """The arbiter step of U08-87 7g for a free slot and idle executor; None: no claim."""
        window, loaded = window_at(now), self.loaded
        min_priority, exempt = chat_rule(window)
        requested = worker.requested_class if worker is not None else None
        if requested is not None and requested == loaded:  # already served: clear it
            require_jobs_backend().set_requested_class(self._worker_id, None)
            requested = None
        if requested is not None and requested != "none" and requested not in self.classes:
            self._reject(requested, window)
            return None
        cfg = get_config().resilience.resilience.jobs
        claimable = require_jobs_backend().claimable_counts(
            now=now,
            classes=[*self.classes, "none"],
            exclusive_kinds=cfg.exclusive_kinds,
            min_priority=min_priority,
            priority_exempt_kinds=exempt,
            gpu_slot_kinds=sorted(GPU_SLOT_KINDS),
        )
        d = arbiter_decide(loaded, window, claimable, requested=requested)
        if d.reason == "request_not_allowed" and requested is not None:
            self._reject(requested, window)
            return None
        if d.action == "swap" and (d.target == "none" or d.target in self.classes):
            if preload or d.reason != "preload":
                self.swap(d.target, d.reason)
            return None
        if d.action != "keep":
            return None
        classes: list[GpuClass] = [loaded] if loaded == "none" else [loaded, "none"]
        return ClaimFilter(classes, min_priority, exempt)
