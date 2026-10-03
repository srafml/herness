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
from typing import cast

import duckdb
import pytest
from pydantic import BaseModel
from tests.support.build_harness import FakeJobContext

from herness.core.errors import ConfigError
from herness.core.jobs.ports import JobContext
from herness.core.types import JobOutcome
from herness.metrics import facts
from herness.model import _build_stages as stages
from herness.model import _build_state, build

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


class _Scored(BaseModel):
    """Stand-in for impl 04 `ScoringReport`: only `flags` matters to the stage."""

    steps_done: list[str]
    flags: list[str]


def _score_run(
    monkeypatch: pytest.MonkeyPatch, ctx: FakeJobContext, flags: list[str]
) -> tuple[build._BuildRun, list[JobContext]]:
    """A minimal run for `stage_score` on an in-memory `meta.build` with `finished_at` set;
    fake spec 04 hooks; the fake scoring checkpoints like `run_scoring` (key `scoring`)."""
    con = duckdb.connect()
    con.execute("CREATE SCHEMA meta")
    con.execute("CREATE TABLE meta.build (finished_at TIMESTAMPTZ)")
    con.execute("INSERT INTO meta.build VALUES (TIMESTAMPTZ '2026-09-01 00:00:00+00')")
    seen: list[JobContext] = []

    def scoring(build_id: str, *, steps: object, con: object, ctx: JobContext) -> _Scored:
        seen.append(ctx)
        ctx.save_state({**ctx.load_state(), "scoring": {"steps_done": ["metrics"]}})
        return _Scored(steps_done=["validate", "metrics"], flags=flags)

    monkeypatch.setattr(facts, "materialize_facts", lambda con, build_id, /: ["q1"])
    monkeypatch.setattr(stages, "_load_run_scoring", lambda: scoring)
    monkeypatch.setattr(build, "_connection", lambda run: con)
    run = types.SimpleNamespace(
        ctx=ctx,
        payload=types.SimpleNamespace(score_steps=None),
        build_id="b1",
        stages_done=["build", "enrich"],
        result={},
        state={"build_id": "b1", "stages_done": ["build", "enrich"], "other": 1},
        con=con,
    )
    return cast("build._BuildRun", run), seen


@pytest.mark.parametrize(("flags", "status"), [(["yielded"], "yield"), ([], "done")])
def test_ut02_78_stage_score_returns_yield_when_scoring_yielded(
    fake_job_context: Callable[..., FakeJobContext],
    monkeypatch: pytest.MonkeyPatch,
    flags: list[str],
    status: str,
) -> None:
    """UT02-78 (U02-101, T02-19b) `stage_score` returns `yield` when the scoring report
    flags `yielded` (so the build is neither done nor promotable) and `done` otherwise; the
    report is kept in the result and `finished_at` is cleared like stage enrich."""
    ctx = fake_job_context({"stages": ["score"], "build_id": "b1"})
    run, seen = _score_run(monkeypatch, ctx, flags)
    assert stages.stage_score(run) == status
    assert run.result["scoring"] == {"steps_done": ["validate", "metrics"], "flags": flags}
    assert run.con is not None
    assert run.con.execute("SELECT finished_at FROM meta.build").fetchone() == (None,)
    assert seen[0].job is ctx.job


def test_ut02_78_hook_context_keeps_scoring_checkpoint(
    fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-78 (U02-98, T02-19b) the hook's checkpoint key is merged into the build's job
    state, and the build's own later save (a yield or a stage done) keeps it."""
    ctx = fake_job_context({"stages": ["score"], "build_id": "b1"})
    run, seen = _score_run(monkeypatch, ctx, ["yielded"])
    stages.stage_score(run)
    hook_ctx = seen[0]
    assert hook_ctx.load_state() == ctx.saved_states[-1]
    assert hook_ctx.stop_reason is None
    run.stages_done.append("score")
    _build_state.save_state(run)
    assert ctx.saved_states[-1] == {
        "build_id": "b1",
        "stages_done": ["build", "enrich", "score"],
        "other": 1,
        "scoring": {"steps_done": ["metrics"]},
    }
    loaded = hook_ctx.load_state()
    loaded["scoring"] = None  # a copy: the hook cannot change the build's state in place
    assert run.state["scoring"] == {"steps_done": ["metrics"]}
