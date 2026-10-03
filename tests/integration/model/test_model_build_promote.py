"""Promotion and retention (impl 02 U02-103 … U02-105, T02-21; flow F02-02 step 9).

IT02-32: five promoted builds, one pinned by a running run; promoting a new build with
`keep_last=3` switches `CURRENT`, keeps the newest two others plus the pinned one, deletes
the rest and retires the previous build. Further IT02-32 cases run the real pipeline on
`lake_small`: a full pipeline promotes and the next one retires it; a yielded `enrich`
build survives the `pre` cleanup of another job while its job is queued (T02-21 spec note);
a failure after the build is marked `promoted` (StoreBusy at `write_current` or in the post
cleanup after the switch) is resumed by the retry at `promote` (fix round 1).
The spec 03 / 04 hooks are fakes behind the loader seams of `herness.model._build_stages`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
import structlog
from pydantic import BaseModel
from tests.support.build_harness import FakeJobContext
from tests.support.lake_small import install, load_config
from tests.support.ops_store import OpsStoreHandle
from tests.support.promote_builds import add_job, add_run, build_id, make_build, statuses

from herness.core import time as clock
from herness.core.errors import ConfigError, HernessError, StoreBusy
from herness.core.jobs.handlers import run_handler
from herness.core.resilience import ProcessState
from herness.core.types import JobOutcome
from herness.enrich.gpu import YieldRequested
from herness.model import _build_stages as stages
from herness.model import _build_support as support
from herness.model import promote
from herness.model.build import make_build_pipeline_handler
from herness.model.promote import promote_build
from herness.model.settings import BuildSettings
from herness.store import ops, warehouse
from herness.store._warehouse_rw import open_for_build, write_current
from herness.store.layout import DataLayout

pytestmark = pytest.mark.integration

FULL = ["build", "enrich", "score", "dq", "promote"]
_DB = support.DbSettings(threads=1, memory_limit="512MiB")


def _new_build(layout: DataLayout, build: str) -> duckdb.DuckDBPyConnection:
    make_build(layout, build, "building", finished=False)
    return open_for_build(build, create=False, cfg=_DB, layout=layout)


def test_it02_32_promote_applies_retention(ops_store: OpsStoreHandle) -> None:
    """IT02-32 five promoted builds (the newest `CURRENT`), the second oldest pinned by a
    running run: promoting a new build with `keep_last=3` makes it `CURRENT`, keeps the
    newest two others and the pinned one, deletes the rest and marks the previous retired."""
    layout = DataLayout.from_root(ops_store.data_root)
    olds = [build_id(day) for day in (1, 2, 3, 4, 5)]
    for build in olds:
        make_build(layout, build, "promoted")
    write_current(olds[-1], layout=layout)
    add_run("running", olds[1])
    new = build_id(6)
    con = _new_build(layout, new)
    with structlog.testing.capture_logs() as logs:
        previous = promote_build(
            con, new, build_cfg=BuildSettings(keep_last=3), layout=layout, now=clock.now()
        )
    assert previous == olds[-1]
    assert warehouse.read_current(layout=layout) == new
    assert statuses(layout) == {
        new: "promoted",
        olds[4]: "retired",
        olds[3]: "retired",
        olds[1]: "retired",
    }
    [info] = [b for b in warehouse.list_builds(layout=layout) if b.build_id == new]
    assert info.finished_at is not None
    with pytest.raises(duckdb.ConnectionException):
        con.execute("SELECT 1")  # closed by promote_build before CURRENT changed
    [promoted] = [e for e in logs if e["event"] == "model.build.promoted"]
    assert (promoted["build_id"], promoted["previous"]) == (new, olds[-1])
    [cleanup] = [e for e in logs if e["event"] == "model.build.cleanup"]
    assert (cleanup["deleted"], cleanup["deferred"], cleanup["retired"]) == (2, 0, 2)
    deleted = [e["build_id"] for e in logs if e["event"] == "store.warehouse.deleted"]
    assert sorted(deleted) == [olds[0], olds[2]]


def test_it02_32_first_promotion_and_unreadable_previous(ops_store: OpsStoreHandle) -> None:
    """IT02-32 the first promotion has no previous build; a later one whose previous
    `CURRENT` file is unreadable still promotes, logging `model.build.retire_failed`."""
    layout = DataLayout.from_root(ops_store.data_root)
    first = build_id(1)
    assert (
        promote_build(
            _new_build(layout, first),
            first,
            build_cfg=BuildSettings(),
            layout=layout,
            now=clock.now(),
        )
        is None
    )
    warehouse.build_path(first, layout=layout).write_bytes(b"corrupted")
    second = build_id(2)
    with structlog.testing.capture_logs() as logs:
        previous = promote_build(
            _new_build(layout, second),
            second,
            build_cfg=BuildSettings(),
            layout=layout,
            now=clock.now(),
        )
    assert previous == first
    assert warehouse.read_current(layout=layout) == second
    [failed] = [e for e in logs if e["event"] == "model.build.retire_failed"]
    assert (failed["build_id"], failed["error_class"]) == (first, "SchemaViolation")
    assert statuses(layout) == {second: "promoted", first: "unreadable"}  # young: kept


# --- pipeline level ----------------------------------------------------------------------


class Report(BaseModel):
    """Stand-in for impl 03 `EnrichReport` / impl 04 `ScoringReport`."""

    name: str


@dataclasses.dataclass(frozen=True)
class Env:
    layout: DataLayout


@pytest.fixture
def env(
    tmp_path: Path,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> Env:
    """`lake_small`, a loaded config and fake enrichment / scoring hooks."""
    install(ops_store.data_root)
    load_config(tmp_path / "cfgroot", ops_store.data_root)
    monkeypatch.setattr(stages, "_load_run_enrichment", lambda: lambda *_a, **_k: Report(name="e"))
    monkeypatch.setattr(stages, "_load_run_scoring", lambda: lambda *_a, **_k: Report(name="s"))
    return Env(DataLayout.from_root(ops_store.data_root))


def _run(ctx: FakeJobContext) -> JobOutcome:
    outcome = run_handler(ctx, make_build_pipeline_handler(llm_factory=None))
    assert isinstance(outcome, JobOutcome), outcome
    return outcome


def test_it02_32_pipeline_promotes_and_next_retires(
    env: Env, fake_job_context: Callable[..., FakeJobContext]
) -> None:
    """IT02-32 the full pipeline promotes its build (`CURRENT`, status `promoted`, result
    `promoted`, metric status `promoted`); the next full pipeline retires it."""
    first = _run(fake_job_context({"stages": FULL}))
    first_id = str(first.result["build_id"])
    assert first.result["promoted"] is True
    assert list(first.result["durations_ms"]) == FULL  # type: ignore[arg-type]
    assert warehouse.read_current(layout=env.layout) == first_id
    assert statuses(env.layout) == {first_id: "promoted"}
    second = _run(fake_job_context({"stages": FULL}))
    second_id = str(second.result["build_id"])
    assert warehouse.read_current(layout=env.layout) == second_id
    assert statuses(env.layout) == {second_id: "promoted", first_id: "retired"}
    rows = ops.read_all("SELECT labels FROM metric_sample WHERE name = 'herness_model_build_total'")
    assert [str(r["labels"]) for r in rows] == ['{"status":"promoted"}'] * 2


def test_it02_32_yielded_enrich_build_survives_other_jobs_cleanup(
    env: Env,
    fake_job_context: Callable[..., FakeJobContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IT02-32 (T02-21 spec note, T02-19 review M1) a job whose `enrich` yielded leaves its
    build `building` with `finished_at` NULL; while that job is queued, another job's `pre`
    cleanup keeps the build; once the job is finished the build is an orphan and deleted."""

    def yielding(*_args: object, **_kwargs: object) -> Report:
        stage = "decide"
        raise YieldRequested(stage)

    monkeypatch.setattr(stages, "_load_run_enrichment", lambda: yielding)
    yielded = fake_job_context({"stages": FULL})
    assert _run(yielded).status == "yield"
    state = yielded.load_state()
    kept = str(state["build_id"])
    assert state["stages_done"] == ["build"]
    [info] = warehouse.list_builds(layout=env.layout)
    assert (info.build_id, info.status, info.finished_at) == (kept, "building", None)
    job_id = add_job("queued", {"stages": FULL}, state)
    other = _run(fake_job_context({"stages": ["build"]}))
    assert kept in statuses(env.layout)
    ops.run_write(
        lambda conn: conn.execute("UPDATE job SET status = 'canceled' WHERE job_id = ?", (job_id,)),
        op="test_job",
    )
    third = _run(fake_job_context({"stages": ["build"]}))
    assert set(statuses(env.layout)) == {other.result["build_id"], third.result["build_id"]}


# --- fix round 1 (review I1): a failure after the status update is resumed by the retry ----


def _busy_once(real: Callable[..., Any], when: Callable[..., bool]) -> Callable[..., Any]:
    """``real`` raising StoreBusy on its first call that matches ``when``."""
    raised: list[bool] = []

    def flaky(*args: Any, **kwargs: Any) -> Any:
        if not raised and when(*args, **kwargs):
            raised.append(True)
            msg = "CURRENT is being replaced"
            raise StoreBusy(msg)
        return real(*args, **kwargs)

    return flaky


def _attempt(ctx: FakeJobContext) -> JobOutcome | HernessError:
    return run_handler(ctx, make_build_pipeline_handler(llm_factory=None))


def _always(*_args: Any, **_kwargs: Any) -> bool:
    return True


def test_it02_32_write_current_busy_full_pipeline_retry_resumes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-32 (U02-104 Errors) `write_current` raises StoreBusy once in a full pipeline: the
    build stays `promoted` (not failed), `CURRENT` unchanged; the retry of the same job
    resumes at `promote`, re-runs the gate (CURRENT does not name it yet) and promotes."""
    monkeypatch.setattr(promote, "write_current", _busy_once(promote.write_current, _always))
    first = FakeJobContext({"stages": FULL})
    error = _attempt(first)
    assert isinstance(error, StoreBusy), error
    state = first.load_state()
    build = str(state["build_id"])
    assert state["stages_done"] == ["build", "enrich", "score", "dq"]
    assert warehouse.read_current(layout=env.layout) is None
    assert statuses(env.layout) == {build: "promoted"}
    with structlog.testing.capture_logs() as logs:
        outcome = _run(FakeJobContext({"stages": FULL}, state=state, attempt=2))
    assert outcome.result["build_id"] == build
    assert list(outcome.result["durations_ms"]) == ["promote"]  # type: ignore[arg-type]
    assert [e["build_id"] for e in logs if e["event"] == "model.dq.evaluated"] == [build]
    assert warehouse.read_current(layout=env.layout) == build
    assert statuses(env.layout) == {build: "promoted"}


def test_it02_32_write_current_busy_from_stage_promote_retry_resumes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-32 (U02-104 Errors) `--from-stage promote` whose `write_current` raises
    StoreBusy once: the retry with the same payload resumes the `promoted` build, re-runs
    the gate and promotes (before fix round 1 it failed with `build … is promoted`)."""
    prepared = _run(FakeJobContext({"stages": ["build", "enrich", "score", "dq"]}))
    build = str(prepared.result["build_id"])
    payload = {"stages": ["promote"], "build_id": build}
    monkeypatch.setattr(promote, "write_current", _busy_once(promote.write_current, _always))
    first = FakeJobContext(payload)
    assert isinstance(_attempt(first), StoreBusy)
    assert statuses(env.layout) == {build: "promoted"}
    assert warehouse.read_current(layout=env.layout) is None
    outcome = _run(FakeJobContext(payload, state=first.load_state(), attempt=2))
    assert outcome.result["promoted"] is True
    assert outcome.result["dq"] is not None
    assert warehouse.read_current(layout=env.layout) == build


def test_it02_32_post_cleanup_busy_after_switch_retry_finishes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-32 (U02-104 Errors) the post cleanup raises StoreBusy once after `CURRENT`
    switched: the build is not marked failed; the retry finishes the promotion (retention
    and `model.build.promoted`) with no gate re-run, no second switch, no failure."""

    def post(*_args: Any, **kwargs: Any) -> bool:
        return bool(kwargs.get("mode") == "post")

    monkeypatch.setattr(promote, "cleanup_builds", _busy_once(promote.cleanup_builds, post))
    first = FakeJobContext({"stages": FULL})
    with structlog.testing.capture_logs() as first_logs:
        assert isinstance(_attempt(first), StoreBusy)
    state = first.load_state()
    build = str(state["build_id"])
    assert warehouse.read_current(layout=env.layout) == build
    assert statuses(env.layout) == {build: "promoted"}
    assert not [e for e in first_logs if e["event"] == "model.build.mark_failed_error"]
    with structlog.testing.capture_logs() as logs:
        outcome = _run(FakeJobContext({"stages": FULL}, state=state, attempt=2))
    assert outcome.result["build_id"] == build
    assert outcome.result["promoted"] is True
    events = [e["event"] for e in logs]
    assert "model.dq.evaluated" not in events
    assert "store.warehouse.current_switched" not in events
    assert "model.build.failed" not in events
    [cleanup] = [e for e in logs if e["event"] == "model.build.cleanup" and e["mode"] == "post"]
    assert cleanup["deleted"] == 0
    [done] = [e for e in logs if e["event"] == "model.build.promoted"]
    assert (done["build_id"], done["previous"]) == (build, None)
    assert warehouse.read_current(layout=env.layout) == build
    assert statuses(env.layout) == {build: "promoted"}


def test_it02_32_promoted_build_resume_rules(env: Env) -> None:
    """IT02-32 (U02-98 step 3, fix round 1) a `promoted` build is resumable only when
    `promote` is the only requested stage left; with more stages it is ConfigError."""
    prepared = _run(FakeJobContext({"stages": FULL}))
    build = str(prepared.result["build_id"])
    now = clock.now()
    assert support.resolve_build(build, {}, env.layout, now, ["promote"]) == (build, [])
    with pytest.raises(ConfigError, match="is promoted"):
        support.resolve_build(build, {}, env.layout, now, ["dq", "promote"])
    state = {"build_id": build, "stages_done": ["build", "enrich", "score", "dq"]}
    assert support.resolve_build(None, state, env.layout, now, FULL)[0] == build
    new_id, done = support.resolve_build(None, state, env.layout, now, ["build", "enrich"])
    assert (new_id != build, done) == (True, [])


def test_it02_32_promoted_build_older_than_current_not_resumed(ops_store: OpsStoreHandle) -> None:
    """IT02-32 (fix round 2, review M5) a `promoted` build older than `CURRENT` is never
    resumed for promotion (that would move `CURRENT` back to older data): a payload
    `build_id` is ConfigError, a saved state starts a new build. A promoted build not older
    than `CURRENT`, or with an invalid `CURRENT`, still resumes (the I1 recovery)."""
    layout = DataLayout.from_root(ops_store.data_root)
    older, current, newer = build_id(1), build_id(2), build_id(3)
    for build in (older, current, newer):
        make_build(layout, build, "promoted")
    write_current(current, layout=layout)
    now = clock.now()
    with pytest.raises(ConfigError, match=f"^build {older} is promoted$"):
        support.resolve_build(older, {}, layout, now, ["promote"])
    state = {"build_id": older, "stages_done": ["build", "enrich", "score", "dq"]}
    new_id, done = support.resolve_build(None, state, layout, now, FULL)
    assert (new_id not in (older, current, newer), done) == (True, [])
    for build in (current, newer):
        assert support.resolve_build(build, {}, layout, now, ["promote"]) == (build, [])
        resumed = {**state, "build_id": build}
        assert support.resolve_build(None, resumed, layout, now, FULL)[0] == build
    (layout.warehouse / "CURRENT").write_text("not-a-build-id\n", encoding="ascii")
    assert support.resolve_build(older, {}, layout, now, ["promote"]) == (older, [])
