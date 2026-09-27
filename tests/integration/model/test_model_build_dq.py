"""Stage `dq` of `build_pipeline` on `lake_small` (impl 02 T02-20: U02-102, flow F02-02).

IT02-29: against the build named by `CURRENT`, a lake with 10 % fewer incidents fails the
gate with `row_count_drop:core.incident`; the build is `failed` and `CURRENT` unchanged.
IT02-30: only `warn` checks fail: the gate passes, the warnings are logged and the build is
not failed (promotion itself is T02-21's stage). IT02-25 / IT02-26 (T02-19 review M5): a
non-Herness error raised by a spec 03 / 04 hook fails the build as `FatalError`.

The spec 03 / 04 hooks are not on this tree: `run_enrichment` and `run_scoring` are fakes
behind the loader seams of `herness.model._build_stages`; the real `materialize_facts` runs.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path

import duckdb
import pytest
import structlog
from pydantic import BaseModel
from tests.support.build_harness import FakeJobContext
from tests.support.lake_small import Row, install, load_config, write_lake
from tests.support.ops_store import OpsStoreHandle

from herness.core.errors import FatalError, HernessError
from herness.core.jobs.handlers import run_handler
from herness.core.resilience import ProcessState
from herness.core.types import JobOutcome
from herness.metrics import facts
from herness.model import _build_stages as stages
from herness.model.build import make_build_pipeline_handler
from herness.model.errors import DqGateFailed
from herness.store import warehouse
from herness.store._warehouse_rw import write_current
from herness.store.layout import DataLayout

pytestmark = pytest.mark.integration

HOOK_TEXT = "zq-hook-text-8812"
DQ_PIPELINE = ["build", "enrich", "score", "dq"]


@dataclasses.dataclass(frozen=True)
class Env:
    layout: DataLayout


@pytest.fixture
def env(tmp_path: Path, ops_store: OpsStoreHandle, reset_process_state: ProcessState) -> Env:
    """`lake_small` under the ops store's data root and a loaded config pointing at it."""
    install(ops_store.data_root)
    load_config(tmp_path / "cfgroot", ops_store.data_root)
    return Env(DataLayout.from_root(ops_store.data_root))


class Report(BaseModel):
    """Stand-in for impl 03 `EnrichReport` / impl 04 `ScoringReport`."""

    name: str


def _enrichment(*_args: object, **_kwargs: object) -> Report:
    return Report(name="enrich")


def _scoring(*_args: object, **_kwargs: object) -> Report:
    return Report(name="scoring")


@pytest.fixture
def hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake enrichment and scoring hooks (T03-28, T04-13 absent); real facts."""
    monkeypatch.setattr(stages, "_load_run_enrichment", lambda: _enrichment)
    monkeypatch.setattr(stages, "_load_run_scoring", lambda: _scoring)


def _run(ctx: FakeJobContext) -> JobOutcome | HernessError:
    return run_handler(ctx, make_build_pipeline_handler(llm_factory=None))


def _incident(key: str) -> Row:
    return Row(
        key,
        0,
        {
            "number": f"INC9{key}",
            "opened_at": "2024-03-01 09:00:00",
            "priority": "3 - Moderate",
            "state": "1",
            "business_service": "s1",
            "assignment_group": "g3",
            "short_description": "Extra record",
        },
    )


def _status(env: Env, build_id: str) -> str:
    [info] = [b for b in warehouse.list_builds(layout=env.layout) if b.build_id == build_id]
    return info.status


def _query(env: Env, build_id: str, sql: str) -> list[tuple[object, ...]]:
    with warehouse.open_readonly(build_id, layout=env.layout) as con:
        return con.execute(sql).fetchall()


def test_it02_29_incident_drop_blocks_build(
    env: Env, fake_job_context: Callable[..., FakeJobContext], hooks: None
) -> None:
    """IT02-29 previous build (10 incidents) is `CURRENT`; the new lake has 9 (10 % fewer):
    `DqGateFailed` naming `row_count_drop:core.incident` only, status `failed`, `CURRENT`
    unchanged, `model.dq.check_failed` (ERROR) and `model.build.failed` logged."""
    write_lake(env.layout.raw, {("servicenow", "incident"): [_incident(f"x{n}") for n in range(6)]})
    first = _run(fake_job_context({"stages": ["build"]}))
    assert isinstance(first, JobOutcome), first
    prev_id = str(first.result["build_id"])
    assert first.result["row_counts"]["core.incident"] == 10  # type: ignore[call-overload,index]
    write_current(prev_id, layout=env.layout)
    write_lake(env.layout.raw, {("servicenow", "incident"): [Row("x0", 5, deleted=True)]})
    with structlog.testing.capture_logs() as logs:
        error = _run(fake_job_context({"stages": DQ_PIPELINE}))
    assert isinstance(error, DqGateFailed), error
    assert error.failed_checks == ("row_count_drop:core.incident",)
    assert "row_count_drop:core.incident" in str(error)
    assert warehouse.read_current(layout=env.layout) == prev_id
    [new] = [b for b in warehouse.list_builds(layout=env.layout) if b.build_id != prev_id]
    assert new.status == "failed"
    assert _status(env, prev_id) == "building"
    [(value, threshold, details)] = _query(
        env,
        new.build_id,
        "SELECT value, threshold, CAST(details AS VARCHAR) FROM meta.dq_result"
        " WHERE check_name = 'row_count_drop:core.incident'",
    )
    assert (value, threshold) == (0.1, 0.05)
    assert json.loads(str(details)) == {"producer": "dq900", "prev": 10, "cur": 9}
    errors = [
        e["check_name"]
        for e in logs
        if e["event"] == "model.dq.check_failed" and e["log_level"] == "error"
    ]
    assert errors == ["row_count_drop:core.incident"]
    [failed] = [e for e in logs if e["event"] == "model.build.failed"]
    assert (failed["stage"], failed["error_class"]) == ("dq", "DqGateFailed")


def test_it02_30_warn_failures_only_pass_the_gate(
    env: Env, fake_job_context: Callable[..., FakeJobContext], hooks: None
) -> None:
    """IT02-30 `lake_small` with no enrichment: `decision_coverage_incident` (warn) fails,
    no error fails; the gate passes, warnings are logged, the build is a completed
    unpromoted build; `dq` and `row_counts` (core, enrich, metrics, score) in the result."""
    ctx = fake_job_context({"stages": DQ_PIPELINE})
    with structlog.testing.capture_logs() as logs:
        outcome = _run(ctx)
    assert isinstance(outcome, JobOutcome), outcome
    build_id = str(outcome.result["build_id"])
    dq = outcome.result["dq"]
    assert isinstance(dq, dict)
    assert dq["failed_errors"] == []
    assert "decision_coverage_incident" in dq["failed_warnings"]  # type: ignore[operator]
    assert isinstance(dq["checks"], int)
    assert dq["checks"] > 0
    warned = [
        e["check_name"]
        for e in logs
        if e["event"] == "model.dq.check_failed" and e["log_level"] == "warning"
    ]
    assert warned == dq["failed_warnings"]
    assert not [e for e in logs if e["event"] == "model.build.failed"]
    [evaluated] = [e for e in logs if e["event"] == "model.dq.evaluated"]
    assert (evaluated["build_id"], evaluated["failed_errors"]) == (build_id, 0)
    assert ctx.load_state()["stages_done"] == DQ_PIPELINE
    assert outcome.result["promoted"] is False
    [info] = warehouse.list_builds(layout=env.layout)
    assert (info.status, info.finished_at is not None) == ("building", True)
    counts = outcome.result["row_counts"]
    assert isinstance(counts, dict)
    assert {k.split(".")[0] for k in counts} >= {"core", "enrich", "metrics"}
    [(stored,)] = _query(env, build_id, "SELECT CAST(row_counts AS VARCHAR) FROM meta.build")
    assert json.loads(str(stored)) == counts


def test_it02_30_dq_alone_on_existing_build(
    env: Env, fake_job_context: Callable[..., FakeJobContext], hooks: None
) -> None:
    """IT02-30 stage `dq` alone on a completed build (resumed in a later job): the render
    context is prepared, `CURRENT` naming this build gives no previous counts."""
    first = _run(fake_job_context({"stages": ["build", "enrich", "score"]}))
    assert isinstance(first, JobOutcome), first
    build_id = str(first.result["build_id"])
    write_current(build_id, layout=env.layout)
    outcome = _run(fake_job_context({"stages": ["dq"], "build_id": build_id}))
    assert isinstance(outcome, JobOutcome), outcome
    [(details,)] = _query(
        env,
        build_id,
        "SELECT CAST(details AS VARCHAR) FROM meta.dq_result"
        " WHERE check_name = 'row_count_drop:core.incident'",
    )
    assert json.loads(str(details))["no_previous_build"] is True


def test_it02_30_yield_before_dq_files(
    env: Env, fake_job_context: Callable[..., FakeJobContext], hooks: None
) -> None:
    """IT02-30 a yield request before file 900 returns `yield` without `dq` done."""
    first = fake_job_context({"stages": ["build", "enrich", "score"]})
    build_id = str(_run(first).result["build_id"])  # type: ignore[union-attr]
    ctx = fake_job_context({"stages": ["dq"], "build_id": build_id}, yield_after=1)
    outcome = _run(ctx)
    assert isinstance(outcome, JobOutcome), outcome
    assert outcome.status == "yield"
    assert "dq" not in ctx.load_state().get("stages_done", [])  # type: ignore[operator]


# --- T02-19 review M5: non-Herness errors from the spec 03 / 04 hooks --------------------


def _boom(*_args: object, **_kwargs: object) -> Report:
    msg = f"hook broke on '{HOOK_TEXT}'"
    raise RuntimeError(msg)


def _assert_fatal(env: Env, error: object, stage: str, logs: list[dict[str, object]]) -> None:
    assert type(error) is FatalError, error
    assert str(error) == f"build stage {stage} failed"
    assert error.context["error_type"] == "RuntimeError"
    assert isinstance(error.__cause__, RuntimeError)
    assert HOOK_TEXT not in json.dumps({"ctx": dict(error.context), **error.details}, default=str)
    [failed] = [e for e in logs if e["event"] == "model.build.failed"]
    assert (failed["stage"], failed["error_class"]) == (stage, "FatalError")
    assert HOOK_TEXT not in json.dumps(logs, default=str)
    assert [b.status for b in warehouse.list_builds(layout=env.layout)] == ["failed"]


def test_it02_25_enrich_hook_non_herness_error_fails_build(
    env: Env,
    fake_job_context: Callable[..., FakeJobContext],
    hooks: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IT02-25 (M5) `run_enrichment` raising RuntimeError: FatalError `build stage enrich
    failed` with the class name only and the cause kept; the build is marked failed."""
    monkeypatch.setattr(stages, "_load_run_enrichment", lambda: _boom)
    with structlog.testing.capture_logs() as logs:
        error = _run(fake_job_context({"stages": ["build", "enrich"]}))
    _assert_fatal(env, error, "enrich", logs)


@pytest.mark.parametrize("hook", ["materialize_facts", "run_scoring"])
def test_it02_26_score_hook_non_herness_error_fails_build(
    env: Env,
    fake_job_context: Callable[..., FakeJobContext],
    hooks: None,
    monkeypatch: pytest.MonkeyPatch,
    hook: str,
) -> None:
    """IT02-26 (M5) `materialize_facts` or `run_scoring` raising RuntimeError: FatalError
    `build stage score failed`, cause kept, build marked failed."""
    if hook == "materialize_facts":

        def facts_boom(con: duckdb.DuckDBPyConnection, build_id: str, /) -> list[str]:
            return [str(_boom(con, build_id))]

        monkeypatch.setattr(facts, "materialize_facts", facts_boom)
    else:
        monkeypatch.setattr(stages, "_load_run_scoring", lambda: _boom)
    with structlog.testing.capture_logs() as logs:
        error = _run(fake_job_context({"stages": ["build", "enrich", "score"]}))
    _assert_fatal(env, error, "score", logs)


def test_it02_26_hook_herness_error_passes_unchanged(
    env: Env,
    fake_job_context: Callable[..., FakeJobContext],
    hooks: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IT02-26 (M5) a HernessError from a hook keeps its class (not re-wrapped)."""

    def scoring_fails(*_args: object, **_kwargs: object) -> Report:
        msg = "scoring input missing"
        raise FatalError(msg, stage="rank")

    monkeypatch.setattr(stages, "_load_run_scoring", lambda: scoring_fails)
    error = _run(fake_job_context({"stages": ["build", "enrich", "score"]}))
    assert isinstance(error, FatalError)
    assert str(error) == "scoring input missing"
    assert error.__cause__ is None
