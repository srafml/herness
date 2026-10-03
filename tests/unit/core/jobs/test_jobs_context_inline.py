"""Controller-verification tests of U08-86 (InlineJobContext, InlineServiceControl).

IT08-12 (the spec test of U08-86 and U08-90) runs the inline context through the real
`run_inline` in tests/integration/jobs/test_jobs_inline.py (T08-22); these unit tests cover
the inline context on a fake `GpuController` and a fake jobs backend.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest
from structlog.testing import capture_logs

from herness.core.errors import ConfigError, JobStateError, ModelUnavailable, SchemaViolation
from herness.core.ids import IdKind, new_id
from herness.core.jobs import _context_base as base
from herness.core.jobs import context
from herness.core.jobs.context import InlineJobContext, InlineServiceControl
from herness.core.jobs.gpu import GpuController
from herness.core.jobs.ports import JobContext, JobRow, JobsBackend, ServiceControl
from herness.core.types import GpuClass

pytestmark = pytest.mark.unit


def make_row(result: dict[str, Any] | None = None) -> JobRow:
    return JobRow(
        job_id=new_id(IdKind.JOB), kind="build_pipeline", gpu_class="none", status="running",
        priority=50, attempts=1, max_attempts=3, payload={}, result=result,
    )  # fmt: skip


class FakeController:
    """Records swaps and service calls; `fail_on` makes `swap` to that class fail."""

    def __init__(self, loaded: GpuClass = "decider", fail_on: GpuClass | None = None) -> None:
        self.loaded: GpuClass = loaded
        self.fail_on = fail_on
        self.calls: list[tuple[Any, ...]] = []

    def swap(self, target: GpuClass, *, reason: str) -> float:
        self.calls.append(("swap", target, reason))
        if target == self.fail_on:
            msg = "vram_not_freed"
            raise ModelUnavailable(msg)
        self.loaded = target
        return 0.0

    def service_start(self, name: str, *, timeout_s: float | None = None) -> None:
        self.calls.append(("start", name, timeout_s))

    def service_stop(self, name: str) -> None:
        self.calls.append(("stop", name))

    def service_healthy(self, name: str) -> bool:
        self.calls.append(("healthy", name))
        return True


class FakeBackend:
    def __init__(self, saved: bool = True) -> None:
        self.saved = saved
        self.calls: list[tuple[str, str, bytes]] = []

    def save_job_state(self, job_id: str, owner: str, state_json: bytes) -> bool:
        self.calls.append((job_id, owner, state_json))
        return self.saved


OWNER = "host:123:cli"


@pytest.fixture(autouse=True)
def _redactor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "redact_text", lambda t: t.replace("sk-SECRET", "[KEY]"))


def inline(
    controller: FakeController | None = None,
    backend: FakeBackend | None = None,
    row: JobRow | None = None,
) -> InlineJobContext:
    return InlineJobContext(
        row or make_row(),
        owner=OWNER,
        controller=cast("GpuController | None", controller),
        backend=cast("JobsBackend | None", backend),
    )


def test_cv_t08_20_inline_satisfies_protocols() -> None:
    """CV-T08-20 the inline classes are usable where the U08-04 protocols are expected."""
    ctx: JobContext = inline(FakeController())
    services: ServiceControl = ctx.services
    assert isinstance(services, InlineServiceControl)


def test_cv_t08_20_inline_require_swaps_in_job() -> None:
    """CV-T08-20 `require_gpu_class` → `controller.swap(cls, reason="in_job")`."""
    ctl = FakeController()
    inline(ctl).require_gpu_class("large", timeout_s=5)
    assert ctl.calls == [("swap", "large", "in_job")]


def test_cv_t08_20_inline_gpu_scope_restores() -> None:
    """CV-T08-20 `gpu_scope` restores the previously loaded class on normal and raising exit."""
    ctl = FakeController("decider")
    ctx = inline(ctl)
    with ctx.gpu_scope("large"):
        assert ctl.loaded == "large"
    assert ctl.calls == [("swap", "large", "in_job"), ("swap", "decider", "in_job")]
    ctl.calls.clear()
    with pytest.raises(ValueError, match="body"), ctx.gpu_scope("reasoning"):
        _boom(ValueError("body"))
    assert ctl.calls == [("swap", "reasoning", "in_job"), ("swap", "decider", "in_job")]


def _boom(exc: Exception) -> None:
    raise exc


def test_cv_t08_20_inline_restore_failure_logged() -> None:
    """CV-T08-20 a failed restore during exception exit logs ERROR and the original propagates."""
    ctl = FakeController("decider", fail_on="decider")
    ctx = inline(ctl)
    with capture_logs() as logs, pytest.raises(KeyError), ctx.gpu_scope("large"):
        _boom(KeyError("original"))
    (entry,) = [e for e in logs if e["event"] == "jobs.gpu.restore_failed"]
    assert (entry["log_level"], entry["class"], entry["error_type"]) == (
        "error", "decider", "ModelUnavailable",
    )  # fmt: skip


def test_cv_t08_20_inline_scope_same_class_no_restore() -> None:
    """CV-T08-20 scope class equal to the loaded class → no restore swap, also when raising."""
    ctl = FakeController("large")
    ctx = inline(ctl)
    with pytest.raises(RuntimeError), ctx.gpu_scope("large"):
        _boom(RuntimeError("x"))
    assert ctl.calls == [("swap", "large", "in_job")]


def test_cv_t08_20_inline_services_direct_calls() -> None:
    """CV-T08-20 `services.*` → `controller.service_*`."""
    ctl = FakeController()
    ctx = inline(ctl)
    ctx.services.start("openjev", timeout_s=30.0)
    ctx.services.stop("openjev")
    assert ctx.services.healthy("openjev") is True
    assert ctl.calls == [("start", "openjev", 30.0), ("stop", "openjev"), ("healthy", "openjev")]


def test_cv_t08_20_inline_no_controller_config_error() -> None:
    """CV-T08-20 `controller is None` → every GPU call raises ConfigError."""
    ctx = inline(None)
    for call in (
        lambda: ctx.require_gpu_class("large"),
        lambda: ctx.gpu_scope("large").__enter__(),
        lambda: ctx.services.start("openjev"),
        lambda: ctx.services.stop("openjev"),
        lambda: ctx.services.healthy("openjev"),
    ):
        with pytest.raises(ConfigError, match="GPU control requires the GPU slot"):
            call()


def test_cv_t08_20_inline_save_state() -> None:
    """CV-T08-20 `save_state` → `save_job_state(job_id, owner, canonical JSON bytes)`."""
    backend = FakeBackend()
    ctx = inline(backend=backend)
    ctx.save_state({"b": 1, "a": [True, None]})
    ((job_id, owner, data),) = backend.calls
    assert (job_id, owner) == (ctx.job_id, OWNER)
    assert data == b'{"a":[true,null],"b":1}'


def test_cv_t08_20_inline_save_state_zero_rows() -> None:
    """CV-T08-20 0 rows updated → JobStateError naming the job."""
    ctx = inline(backend=FakeBackend(saved=False))
    with pytest.raises(JobStateError, match="job state not saved") as info:
        ctx.save_state({"a": 1})
    assert info.value.job_id == ctx.job_id


def test_cv_t08_20_inline_save_state_cap() -> None:
    """CV-T08-20 a 5 MB state → SchemaViolation before any write."""
    backend = FakeBackend()
    with pytest.raises(SchemaViolation, match="4 MiB"):
        inline(backend=backend).save_state({"blob": "x" * 5_000_000})
    assert backend.calls == []


def test_cv_t08_20_inline_save_state_bound_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """CV-T08-20 without an explicit backend the bound jobs backend is used."""
    backend = FakeBackend()
    monkeypatch.setattr(context, "require_jobs_backend", lambda: backend)
    inline().save_state({"k": "v"})
    assert json.loads(backend.calls[0][2]) == {"k": "v"}


def test_cv_t08_20_inline_heartbeat_note_in_memory() -> None:
    """CV-T08-20 `heartbeat` keeps the redacted, cut note; `None` keeps the last note."""
    ctx = inline()
    assert ctx.note is None
    ctx.heartbeat("token sk-SECRET " + "y" * 300)
    note = cast("str", ctx.note)
    assert len(note) == 200
    assert note.startswith("token [KEY] y")
    ctx.heartbeat(None)
    assert ctx.note == note


def test_cv_t08_20_inline_stop_event_first_wins() -> None:
    """CV-T08-20 `request_stop` from another thread sets the event; the first reason wins."""
    ctx = inline()
    assert ctx.should_yield() is False
    worker = threading.Thread(target=ctx.request_stop, args=("cancel",))
    worker.start()
    worker.join(2)
    ctx.request_stop("shutdown")
    assert ctx._stop_event.is_set()
    assert ctx.should_yield() is True
    assert ctx.stop_reason == "cancel"


def test_cv_t08_20_inline_row_and_state() -> None:
    """CV-T08-20 row shortcuts and `load_state` copy on the inline context."""
    row = make_row({"state": {"n": 1}})
    ctx = inline(row=row)
    assert (ctx.job, ctx.job_id, ctx.kind, ctx.attempt) == (row, row.job_id, "build_pipeline", 1)
    state = ctx.load_state()
    state["n"] = 2
    assert ctx.load_state() == {"n": 1}


def test_cv_t08_20_gpu_settings_read_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """CV-T08-20 the default wait reads `R.gpu` from the loaded config."""
    gpu = SimpleNamespace(stop_timeout_s=1.0, warmup_timeout_s=2.0, classes={})
    cfg = SimpleNamespace(resilience=SimpleNamespace(resilience=SimpleNamespace(gpu=gpu)))
    monkeypatch.setattr(base, "get_config", lambda: cfg)
    assert base.default_wait_s(3.0) == 1 + 60 + 3 + 2 + 60
    assert base.start_s(cls="large") == 0.0
