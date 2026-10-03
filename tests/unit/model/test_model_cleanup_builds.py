"""Unit tests of `herness.model.promote.cleanup_builds` (impl 02 U02-105, T02-21).

UT02-48: retention after a promotion keeps the build of a non-terminal run (read through
impl 06 `select_runs`, R-68) and deletes the rest beyond `keep_last`. Further UT02-48 cases
cover the orphan rules of both modes and the T02-21 spec note: a build a queued (yielded)
or running `build_pipeline` job will resume is never deleted by another job's `pre` cleanup.
Builds are synthetic files holding only `meta.build` (tests/support/promote_builds.py).
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Collection

import pytest
import structlog
from tests.support.ops_store import OpsStoreHandle
from tests.support.promote_builds import (
    add_job,
    add_run,
    build_id,
    make_build,
    statuses,
)

from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.model import promote
from herness.model.promote import NON_TERMINAL_RUN_STATUSES, CleanupReport, cleanup_builds
from herness.store import ops, warehouse
from herness.store._warehouse_rw import write_current
from herness.store.layout import DataLayout

pytestmark = pytest.mark.unit

CURRENT = build_id(20)
NEW = build_id(25)


@pytest.fixture
def layout(ops_store: OpsStoreHandle) -> DataLayout:
    """The data layout of the migrated ops store's data root."""
    return DataLayout.from_root(ops_store.data_root)


def _current(layout: DataLayout) -> None:
    make_build(layout, CURRENT, "promoted")
    write_current(CURRENT, layout=layout)


def test_ut02_48_running_run_pins_its_build(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-48 runs `running`, `done`, `partial` each on its own build plus a fourth
    unprotected promoted build: `post` with `keep_last=1` leaves only the running run's build
    besides `CURRENT`; the runs are read through `select_runs` with the non-terminal set."""
    _current(layout)
    builds = [build_id(day) for day in (1, 2, 3, 4)]
    for build in builds:
        make_build(layout, build, "promoted")
    for status, build in zip(("running", "done", "partial"), builds, strict=False):
        add_run(status, build)
    add_run("running", None)  # a run without a build pins nothing
    asked: list[Collection[str]] = []
    real = ops.select_runs

    def spy(*, statuses: Collection[str], kinds: Collection[str] | None = None) -> object:
        asked.append(statuses)
        return real(statuses=statuses, kinds=kinds)

    monkeypatch.setattr(ops, "select_runs", spy)
    with structlog.testing.capture_logs() as logs:
        report = cleanup_builds(
            mode="post", keep_last=1, protect=frozenset(), layout=layout, now=clock.now()
        )
    assert asked == [NON_TERMINAL_RUN_STATUSES]
    assert sorted(report.deleted) == builds[1:]
    assert (report.deferred, report.retired) == ((), (builds[0],))
    assert statuses(layout) == {CURRENT: "promoted", builds[0]: "retired"}
    [event] = [e for e in logs if e["event"] == "model.build.cleanup"]
    assert (event["mode"], event["deleted"], event["deferred"], event["retired"]) == (
        "post",
        3,
        0,
        1,
    )


def test_ut02_48_non_terminal_statuses_constant() -> None:
    """UT02-48 `NON_TERMINAL_RUN_STATUSES` is the `run.status` CHECK list minus the terminal
    set `done`, `partial`, `failed`, `canceled`."""
    assert NON_TERMINAL_RUN_STATUSES == (
        "created",
        "planning",
        "running",
        "challenging",
        "verifying",
        "writing",
        "recording",
    )
    assert dataclasses.fields(CleanupReport)[0].name == "deleted"


def _junk(layout: DataLayout, build: str, age_s: float) -> None:
    path = warehouse.build_path(build, layout=layout)
    path.write_bytes(b"not a duckdb file")
    stamp = clock.now().timestamp() - age_s
    os.utime(path, (stamp, stamp))


def test_ut02_48_orphan_and_retention_rules(layout: DataLayout) -> None:
    """UT02-48 (U02-105 rules 2-4) `pre` deletes killed builds, unreadable files older than
    one hour and every failed build but the newest; it keeps `protect`, young unreadable
    files, completed unpromoted builds and old promoted builds. `post` then also deletes
    the completed build older than `CURRENT` and retires the remaining promoted build."""
    _current(layout)
    killed, protected = build_id(10), build_id(11)
    failed_old, failed_new = build_id(3), build_id(4)
    old_junk, young_junk = build_id(5), build_id(6)
    completed, promoted_old = build_id(7), build_id(2)
    make_build(layout, killed, "building", finished=False)
    make_build(layout, protected, "building", finished=False)
    make_build(layout, failed_old, "failed")
    make_build(layout, failed_new, "failed")
    make_build(layout, completed, "building")
    make_build(layout, promoted_old, "promoted")
    _junk(layout, old_junk, promote.ORPHAN_UNREADABLE_AGE_S + 60)
    _junk(layout, young_junk, 60)
    pre = cleanup_builds(
        mode="pre", keep_last=3, protect=frozenset({protected}), layout=layout, now=clock.now()
    )
    assert sorted(pre.deleted) == sorted([killed, failed_old, old_junk])
    assert (pre.deferred, pre.retired) == ((), ())
    assert set(statuses(layout)) == {
        CURRENT,
        protected,
        failed_new,
        young_junk,
        completed,
        promoted_old,
    }
    post = cleanup_builds(
        mode="post", keep_last=3, protect=frozenset({protected}), layout=layout, now=clock.now()
    )
    assert post.deleted == (completed,)
    assert post.retired == (promoted_old,)
    assert statuses(layout)[promoted_old] == "retired"


def test_ut02_48_locked_build_never_deleted(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-48 a `locked` file is never handed to `delete_build_files` in either mode."""
    locked = build_id(9)
    make_build(layout, locked, "building", finished=False)
    real_list = warehouse.list_builds

    def as_locked(*, layout: DataLayout | None = None) -> list[warehouse.BuildInfo]:
        return [dataclasses.replace(b, status="locked") for b in real_list(layout=layout)]

    calls: list[str] = []
    monkeypatch.setattr(warehouse, "list_builds", as_locked)
    monkeypatch.setattr(warehouse, "delete_build_files", lambda b, **_k: calls.append(b))
    for mode in ("pre", "post"):
        report = cleanup_builds(
            mode=mode,  # type: ignore[arg-type]
            keep_last=1,
            protect=frozenset(),
            layout=layout,
            now=clock.now(),
        )
        assert report == CleanupReport((), (), ())
    assert calls == []


@pytest.mark.parametrize(("mode", "keep_last"), [("pre", 0), ("post", 21), ("both", 3)])
def test_ut02_48_rejects_bad_arguments(layout: DataLayout, mode: str, keep_last: int) -> None:
    """UT02-48 `keep_last` outside 1-20 or an unknown `mode` is ConfigError."""
    with pytest.raises(ConfigError, match="keep_last 1-20"):
        cleanup_builds(
            mode=mode,  # type: ignore[arg-type]
            keep_last=keep_last,
            protect=frozenset(),
            layout=layout,
            now=clock.now(),
        )


def test_ut02_48_yielded_build_survives_pre_cleanup_from_another_job(layout: DataLayout) -> None:
    """UT02-48 (T02-21 spec note, review M1 of T02-19) builds a queued (yielded) or running
    `build_pipeline` job will resume survive another job's `pre` cleanup: the state's build
    when `build` is done, or the payload `build_id`. A yield inside `build`, a finished job
    and a job of a removed build leave true orphans that are deleted."""
    yielded_score = build_id(12, "AAAAA1")
    payload_named = build_id(12, "AAAAA2")
    yielded_in_build = build_id(12, "AAAAA3")
    finished_job = build_id(12, "AAAAA4")
    for build in (yielded_score, payload_named, yielded_in_build, finished_job):
        make_build(layout, build, "building", finished=False)
    add_job(
        "queued",
        {"stages": ["build", "enrich", "score", "dq", "promote"]},
        {"build_id": yielded_score, "stages_done": ["build", "enrich"], "scoring": {}},
    )
    add_job("running", {"stages": ["score", "dq"], "build_id": payload_named})
    add_job("queued", {"stages": ["build"]}, {"build_id": yielded_in_build, "stages_done": []})
    add_job(
        "done",
        {"stages": ["build", "enrich"]},
        {"build_id": finished_job, "stages_done": ["build", "enrich"]},
    )
    add_job(
        "queued", {"stages": ["build"]}, {"build_id": ["not", "an", "id"], "stages_done": ["build"]}
    )
    report = cleanup_builds(
        mode="pre", keep_last=3, protect=frozenset({NEW}), layout=layout, now=clock.now()
    )
    assert sorted(report.deleted) == sorted([yielded_in_build, finished_job])
    assert set(statuses(layout)) == {yielded_score, payload_named}
