"""Stages ``enrich`` and ``score`` and the ``build_pipeline`` handler factory (T02-19).

Private sibling of ``herness.model.build`` (impl 02 §2 module-map note), split off for the
400-line budget of ``build.py``: U02-100 ``_stage_enrich``, U02-101 ``_stage_score`` and
U02-134 ``make_build_pipeline_handler`` (re-exported by ``build``). ``build`` imports this
module; this module reaches ``build`` only inside functions, so there is no import cycle at
module level. The spec 03 / 04 hooks are imported lazily (impl 02 §2.2 exception: upward
imports of the build job, listed as ``ignore_imports`` on the layers contract); a hook
module that is not installed makes the stage fail with ``ConfigError`` on the failure path;
any other non-Herness error from a hook becomes ``FatalError`` so the build fails too.
"""

from __future__ import annotations

import contextlib
import importlib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import duckdb

from herness.core.errors import ConfigError, FatalError, HernessError
from herness.model import meta
from herness.store import warehouse

if TYPE_CHECKING:
    from herness.core.jobs.ports import JobContext
    from herness.core.types import JobOutcome
    from herness.model.build import StageStatus, _BuildRun


class _Report(Protocol):
    """What the stage keeps of a spec 03 / 04 report: its JSON form (pydantic models)."""

    def model_dump(self, *, mode: str) -> dict[str, Any]: ...


class _RunEnrichment(Protocol):
    """T03-28: signature of ``herness.enrich.pipeline.run_enrichment`` (impl 03)."""

    def __call__(  # noqa: PLR0913 - T03-28 signature
        self,
        wh: duckdb.DuckDBPyConnection,
        build_id: str,
        *,
        depth: Literal["fast", "standard", "deep"],
        ctx: JobContext,
        prev_warehouse: Path | None,
        stages: Sequence[str] | None = None,
        llm_factory: object | None = None,
    ) -> _Report: ...


class _RunScoring(Protocol):
    """T04-13: signature of ``herness.metrics.scoring.run_scoring`` (impl 04 DD04-02)."""

    def __call__(
        self,
        build_id: str,
        *,
        steps: Sequence[str] | None,
        con: duckdb.DuckDBPyConnection,
        ctx: JobContext,
    ) -> _Report: ...


def _hook(module: str, name: str, stage: str) -> object:
    """``module.name`` imported lazily; ConfigError when that module or name is absent."""
    msg = f"build stage {stage} is not available"
    try:
        loaded = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name != module:  # a dependency of an installed hook: a real fault
            raise
        raise ConfigError(msg) from None
    # only the lookup is guarded: an AttributeError while importing the module propagates
    try:
        return getattr(loaded, name)
    except AttributeError:
        pass
    raise ConfigError(msg)


@contextlib.contextmanager
def _hook_errors(stage: str, passthrough: type[Exception] = HernessError) -> Iterator[None]:
    """Turn a non-Herness hook error into ``FatalError`` (class name only, cause kept)."""
    try:
        yield
    except (HernessError, passthrough):
        raise
    except Exception as exc:
        msg = f"build stage {stage} failed"
        raise FatalError(msg, error_type=type(exc).__name__) from exc


def _load_run_enrichment() -> _RunEnrichment:
    # T03-28: becomes `from herness.enrich.pipeline import run_enrichment` once impl 03 lands
    return cast("_RunEnrichment", _hook("herness.enrich.pipeline", "run_enrichment", "enrich"))


def _load_run_scoring() -> _RunScoring:
    # T04-13: becomes `from herness.metrics.scoring import run_scoring` once impl 04 lands
    return cast("_RunScoring", _hook("herness.metrics.scoring", "run_scoring", "score"))


def _prev_warehouse(run: _BuildRun) -> Path | None:
    """The ``CURRENT`` build file when ``CURRENT`` names another build (U02-100 step 2)."""
    current = warehouse.read_current(layout=run.layout)
    if current is None or current == run.build_id:
        return None
    return warehouse.build_path(current, layout=run.layout)


def stage_enrich(run: _BuildRun) -> StageStatus:
    """Stage ``enrich``: spec 03 enrichment, then files 300-399 (U02-100).

    No GPU scope is entered here (R-43): impl 03 takes ``decider`` itself, so the SQL of
    300-399 runs with no class held. ``YieldRequested`` becomes ``yield`` without adding
    ``enrich`` to ``stages_done``; the next run repeats the stage and impl 03 resumes from
    its own checkpoint. Any other error propagates to the failure path.
    """
    # T03-04: import YieldRequested from herness.enrich.pipeline once that module exists
    from herness.enrich.gpu import YieldRequested  # noqa: PLC0415 - §2.2 lazy upward
    from herness.model import build  # noqa: PLC0415 - build imports this module

    con = build._connection(run)
    meta.update_build_row(con, clear_finished=True)  # a completed unpromoted build resumes
    run_enrichment = _load_run_enrichment()
    stage = run.payload.enrich_stage
    try:
        with _hook_errors("enrich", YieldRequested):
            report = run_enrichment(
                con,
                run.build_id,
                depth=run.payload.depth,
                ctx=run.ctx,
                prev_warehouse=_prev_warehouse(run),
                stages=[stage] if stage else None,
                llm_factory=run.llm_factory,
            )
    except YieldRequested:
        return "yield"
    run.result["enrich"] = report.model_dump(mode="json")
    if build._sql(run, 300, 399) == "yield":
        return "yield"
    con.execute("CHECKPOINT")
    return "done"


def stage_score(run: _BuildRun) -> StageStatus:
    """Stage ``score``: facts through the spec 04 hook, then scoring (U02-101).

    ``materialize_facts`` opens its own transaction, so it is called with none open; the
    runner never renders 400-499 itself. ``run_scoring`` gets the build connection, which
    stays open (never closed and reopened). The scoring hook is resolved before the facts
    so a missing hook fails before the long facts step.
    """
    from herness.metrics.facts import materialize_facts  # noqa: PLC0415 - §2.2 lazy upward
    from herness.model import build  # noqa: PLC0415 - build imports this module

    con = build._connection(run)
    run_scoring = _load_run_scoring()
    with _hook_errors("score"):
        query_ids = materialize_facts(con, run.build_id)
    con.execute("CHECKPOINT")
    with _hook_errors("score"):
        report = run_scoring(run.build_id, steps=run.payload.score_steps, con=con, ctx=run.ctx)
    run.result["facts_queries"] = len(query_ids)
    run.result["scoring"] = report.model_dump(mode="json")
    con.execute("CHECKPOINT")
    return "done"


def make_build_pipeline_handler(
    *,
    llm_factory: object | None,  # T03-28: impl 03 LlmFactory | None
) -> Callable[[JobContext], JobOutcome]:
    """The one-argument ``build_pipeline`` handler carrying ``llm_factory`` (U02-134, R-42).

    The handler reads its payload from ``ctx.job.payload`` through ``run_build_pipeline``
    and holds no other state; the composition root registers it (T08-12).
    """

    def handler(ctx: JobContext) -> JobOutcome:
        from herness.model import build  # noqa: PLC0415 - build imports this module

        return build.run_build_pipeline(ctx, llm_factory=llm_factory)

    return handler
