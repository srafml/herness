"""The `build_pipeline` handler factory and the stage hook loaders (impl 02 T02-19).

UT02-78: `make_build_pipeline_handler` returns a one-argument handler that passes the job
context (payload read from `ctx.job.payload`, R-42) and the factory's `llm_factory` to
`run_build_pipeline`. The loader seams of the spec 03 / 04 hooks turn a missing hook module
into ConfigError and let any other import fault through.
"""

from __future__ import annotations

import importlib.util
import inspect
import types
from collections.abc import Callable

import pytest
from tests.support.build_harness import FakeJobContext

from herness.core.errors import ConfigError
from herness.core.jobs.ports import JobContext
from herness.core.types import JobOutcome
from herness.model import _build_stages as stages
from herness.model import build

pytestmark = pytest.mark.unit


def test_ut02_78_handler_is_one_argument_and_passes_llm_factory(
    fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-78 the handler takes one argument; payload comes from `ctx.job.payload` (R-42);
    `llm_factory` reaches `run_build_pipeline`; the factory is exported by `build`."""
    calls: list[tuple[JobContext, object]] = []
    outcome = JobOutcome(status="done", result={"build_id": "x"})

    def recording(ctx: JobContext, *, llm_factory: object | None = None) -> JobOutcome:
        calls.append((ctx, llm_factory))
        return outcome

    monkeypatch.setattr(build, "run_build_pipeline", recording)
    factory = object()
    handler = build.make_build_pipeline_handler(llm_factory=factory)
    assert list(inspect.signature(handler).parameters) == ["ctx"]
    ctx = fake_job_context({"stages": ["build"]})
    assert handler(ctx) is outcome
    assert calls == [(ctx, factory)]
    assert dict(calls[0][0].job.payload) == {"stages": ["build"]}
    assert build.make_build_pipeline_handler is stages.make_build_pipeline_handler


def test_ut02_78_handler_without_llm_factory(
    fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-78 `llm_factory=None` is passed on as None (degraded enrichment, impl 03)."""
    seen: list[object] = []

    def recording(ctx: JobContext, *, llm_factory: object | None = None) -> JobOutcome:
        seen.append(llm_factory)
        return JobOutcome(status="done", result={})

    monkeypatch.setattr(build, "run_build_pipeline", recording)
    build.make_build_pipeline_handler(llm_factory=None)(fake_job_context({"stages": ["build"]}))
    assert seen == [None]


def test_ut02_78_missing_hook_module_is_config_error() -> None:
    """UT02-78 (U02-100/101 seam) an absent hook module or name is ConfigError naming only
    the stage."""
    with pytest.raises(ConfigError, match=r"^build stage enrich is not available$"):
        stages._hook("herness.model._no_such_hook", "run_enrichment", "enrich")
    with pytest.raises(ConfigError, match=r"^build stage score is not available$"):
        stages._hook("herness.model.meta", "no_such_function", "score")
    assert stages._hook("herness.model.meta", "update_build_row", "score") is not None


def test_ut02_78_hook_dependency_fault_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-78 a missing dependency inside an installed hook module is not masked."""

    def broken(name: str) -> object:
        msg = "no module named torchish"
        raise ModuleNotFoundError(msg, name="torchish")

    monkeypatch.setattr(stages, "importlib", types.SimpleNamespace(import_module=broken))
    with pytest.raises(ModuleNotFoundError):
        stages._hook("herness.enrich.pipeline", "run_enrichment", "enrich")


def test_ut02_78_hook_import_attribute_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-78 an AttributeError raised while an installed hook module is imported is not
    turned into "not available" (only the name lookup is guarded)."""

    def broken(name: str) -> object:
        msg = "module attribute missing during import"
        raise AttributeError(msg)

    monkeypatch.setattr(stages, "importlib", types.SimpleNamespace(import_module=broken))
    with pytest.raises(AttributeError, match="during import"):
        stages._hook("herness.enrich.pipeline", "run_enrichment", "enrich")


def test_ut02_78_hook_loaders_until_hooks_land() -> None:
    """UT02-78 the loaders resolve the spec 03 / 04 hooks; absent on this tree -> ConfigError.

    T03-28 / T04-13: this test changes once `run_enrichment` / `run_scoring` exist."""
    for loader, module, stage in (
        (stages._load_run_enrichment, "herness.enrich.pipeline", "enrich"),
        (stages._load_run_scoring, "herness.metrics.scoring", "score"),
    ):
        if importlib.util.find_spec(module) is None:
            with pytest.raises(ConfigError, match=rf"^build stage {stage} is not available$"):
                loader()
        else:
            assert callable(loader())
