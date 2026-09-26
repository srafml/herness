"""Tests for herness.connectors.backfill.run_backfill and SyncRunner.run_backfill (U01-41,
U01-43; T01-07): bounded parallel slices, failure isolation, resume, stop and tool keys.

No test sleeps: concurrency is proven with a barrier (`Gate`) and time is a fixed clock."""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs
from tests.support.fake_lake import FakeLake
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors._backfill_data import (
    Gate,
    Source,
    WindowConnector,
    WindowToolConnector,
    backfill_runner,
    factory_of,
    no_rows,
    one_row,
    slices,
    stop_after,
    watermark,
)
from tests.unit.connectors._runner_data import NOW, FakeGuard, batch, monitoring_cfg, servicenow_cfg
from tests.unit.connectors._settings_data import files as files_section

import herness.connectors.backfill as backfill_module
from herness.connectors.base import split_range
from herness.connectors.runner import SyncRunner
from herness.connectors.settings import FilesSettings
from herness.core import time as clock
from herness.core.errors import AuthError, ConfigError, SourceUnavailable
from herness.store.ops import ensure_slices, mark_slice_running, set_watermark

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("guard")]

_SRC, _ENT = "servicenow", "incident"
_DAY = datetime.timedelta(days=1)
_SEC = datetime.timedelta(seconds=1)
_START = NOW - 70 * _DAY  # ServiceNow `incident` slices are 7 days wide: 10 slices
_WINDOWS = split_range(_START, NOW, 7 * _DAY)
_FILE = re.compile(r"raw/servicenow/incident/part-\d{4}\.parquet")


def _failing(bad: dict[int, BaseException]) -> Any:
    """Plan raising `bad[i]` in slice `i` (1-based) and one row elsewhere."""
    starts = [w[0] for w in _WINDOWS]

    def plan(source: str, since: datetime.datetime, until: datetime.datetime) -> list[Any]:
        number = starts.index(since) + 1
        return [bad[number]] if number in bad else one_row(source, _ENT, since, until)

    return plan


def _parallel(
    ops_store: OpsStoreHandle, lake: FakeLake, source: Source, *, workers: int = 3
) -> SyncRunner:
    conn = WindowConnector(source)
    factory = factory_of(lambda: WindowConnector(source), source)
    cfg = servicenow_cfg(max_concurrency=workers)
    return backfill_runner(conn, cfg, lake, ops_store.data_root, factory=factory)


# --- UT01-37: bounded parallel slices, all done, watermark min(end, max) --------------------


def test_ut01_37_parallel_slices_bounded_and_all_done(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-37 10 slices with a factory and max_concurrency 3: exactly 3 fetches overlap
    (barrier), never more; every slice done with its row and file; watermark = min(end, max)."""
    source = Source(plan=lambda n, s, u: one_row(n, _ENT, s, u), gate=Gate(meet=3))
    runner = _parallel(ops_store, lake, source)

    with capture_logs() as logs:
        result = runner.run_backfill(_ENT, _START, NOW)

    assert source.gate.peak == 3
    assert len(source.gate.threads) == 3
    assert all(name.startswith("backfill-servicenow") for name in source.gate.threads)
    assert source.instances == 10
    assert source.windows() == _WINDOWS
    rows = slices()
    assert [r["status"] for r in rows] == ["done"] * 10
    assert [r["rows"] for r in rows] == [1] * 10
    assert [r["attempts"] for r in rows] == [1] * 10
    paths = [p for r in rows for p in json.loads(r["files"])]
    assert len(set(paths)) == 10
    assert all(_FILE.fullmatch(p) for p in paths)
    top = _WINDOWS[-1][0]
    assert watermark(_SRC, _ENT) == top
    assert result.mode == "backfill"
    assert (result.rows, result.tombstones, result.skipped_deleted) == (10, 0, 0)
    assert len(result.files) == 10
    assert result.watermark_before is None
    assert result.watermark_after == clock.format_utc(top)
    assert runner.stopped is False
    done = [e for e in logs if e["event"] == "connectors.backfill.slice_completed"]
    assert len(done) == 10
    assert {k: done[0][k] for k in ("source", "entity", "stream", "rows")} == {
        "source": _SRC,
        "entity": _ENT,
        "stream": _SRC,
        "rows": 1,
    }
    spans = sorted((e["slice_start"], e["slice_end"]) for e in done)
    assert spans == [(clock.format_utc(a), clock.format_utc(b)) for a, b in _WINDOWS]
    completed = [e for e in logs if e["event"] == "connectors.sync.completed"]
    assert len(completed) == 1
    assert completed[0]["mode"] == "backfill"
    assert completed[0]["rows"] == 10


def test_ut01_37_watermark_capped_at_end(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-37 a committed row stamped after `end` caps the watermark at `end`."""

    def plan(name: str, since: datetime.datetime, until: datetime.datetime) -> list[Any]:
        return [batch(name, _ENT, ["late"], NOW + datetime.timedelta(hours=1))]

    runner = _parallel(ops_store, lake, Source(plan=plan))
    result = runner.run_backfill(_ENT, NOW - 7 * _DAY, NOW)

    assert watermark(_SRC, _ENT) == NOW
    assert result.watermark_after == clock.format_utc(NOW)


def test_ut01_37_without_factory_runs_one_slice_at_a_time(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-37 without a connector factory the slices run one at a time on the shared
    connector, whatever max_concurrency says."""
    source = Source(plan=lambda n, s, u: one_row(n, _ENT, s, u))
    cfg = servicenow_cfg(max_concurrency=3)
    runner = backfill_runner(WindowConnector(source), cfg, lake, ops_store.data_root)

    result = runner.run_backfill(_ENT, _START, NOW)

    assert source.gate.peak == 1
    assert len(source.gate.threads) == 1
    assert source.instances == 0
    assert result.rows == 10
    assert [r["status"] for r in slices()] == ["done"] * 10


def test_ut01_37_done_slices_are_never_rerun(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-37 a second backfill over a fully done range fetches nothing, commits no rows and
    so sets the watermark to `end`."""
    source = Source(plan=lambda n, s, u: one_row(n, _ENT, s, u))
    runner = _parallel(ops_store, lake, source)
    runner.run_backfill(_ENT, _START, NOW)
    source.calls.clear()

    result = runner.run_backfill(_ENT, _START, NOW)

    assert source.calls == []
    assert result.rows == 0
    assert watermark(_SRC, _ENT) == NOW
    assert [r["attempts"] for r in slices()] == [1] * 10


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (_START.replace(tzinfo=None), NOW),
        (_START, NOW.replace(tzinfo=None)),
        (NOW, NOW),
        (NOW, _START),
        (_START, NOW + _SEC),
    ],
    ids=["naive-start", "naive-end", "empty", "reversed", "future-end"],
)
def test_ut01_37_run_backfill_rejects_bad_range(
    ops_store: OpsStoreHandle, lake: FakeLake, start: datetime.datetime, end: datetime.datetime
) -> None:
    """UT01-37 run_backfill needs aware `start < end <= now`; violations raise ConfigError
    before any slice is planned or fetched."""
    source = Source()
    runner = _parallel(ops_store, lake, source)
    with pytest.raises(ConfigError):
        runner.run_backfill(_ENT, start, end)
    assert slices() == []
    assert source.calls == []


def test_ut01_37_run_backfill_rejects_unknown_entity_and_files(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-37 an unconfigured entity and the files connector raise ConfigError."""
    runner = _parallel(ops_store, lake, Source())
    with pytest.raises(ConfigError, match="unknown entity"):
        runner.run_backfill("nope", _START, NOW)

    cfg = FilesSettings.model_validate(files_section())
    conn = WindowConnector(Source(), name="files", entities=("roster",))
    files_runner = backfill_runner(conn, cfg, lake, ops_store.data_root)
    with pytest.raises(ConfigError, match="files does not backfill"):
        files_runner.run_backfill("roster", _START, NOW)
    assert slices() == []


def test_ut01_37_run_backfill_passes_stream_key_and_workers(
    ops_store: OpsStoreHandle, lake: FakeLake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-37 a single-stream source calls U01-43 with key = connector name and
    max_workers = cfg.max_concurrency; the fetch maps each instance to its `sync`."""
    seen: list[tuple[str, int, Any]] = []
    source = Source()

    def spy(runner: SyncRunner, entity: str, start: Any, end: Any, **kw: Any) -> Any:
        seen.append((kw["key"], kw["max_workers"], kw["fetch_of"]))
        return "sentinel"

    monkeypatch.setattr(backfill_module, "run_backfill", spy)
    runner = _parallel(ops_store, lake, source, workers=5)
    assert runner.run_backfill(_ENT, _START, NOW) == "sentinel"

    assert [(key, workers) for key, workers, _ in seen] == [(_SRC, 5)]
    other = WindowConnector(source)
    assert seen[0][2](other) == other.sync


# --- UT01-38: one failing slice ------------------------------------------------------------


def test_ut01_38_failed_slice_others_done_error_raised(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-38 slice 4 raising SourceUnavailable: slice 4 failed with its error text, the
    other nine done; the error is raised after all slices; the watermark stays unset."""
    err = SourceUnavailable("source down")
    source = Source(plan=_failing({4: err}))
    runner = _parallel(ops_store, lake, source)

    with capture_logs() as logs, pytest.raises(SourceUnavailable) as info:
        runner.run_backfill(_ENT, _START, NOW)

    assert info.value is err
    rows = slices()
    assert [r["status"] for r in rows] == ["done"] * 3 + ["failed"] + ["done"] * 6
    assert rows[3]["last_error"] == "SourceUnavailable: source down"
    assert rows[3]["rows"] == 0
    assert rows[3]["attempts"] == 1
    assert watermark(_SRC, _ENT) is None
    assert sum(w.abort_calls for w in lake.writers) >= 1
    failed = [e for e in logs if e["event"] == "connectors.backfill.slice_failed"]
    assert len(failed) == 1
    assert failed[0]["log_level"] == "warning"
    assert {k: failed[0][k] for k in ("source", "entity", "stream", "error_class")} == {
        "source": _SRC,
        "entity": _ENT,
        "stream": _SRC,
        "error_class": "SourceUnavailable",
    }
    assert failed[0]["attempts"] == 1
    assert failed[0]["slice_start"] == clock.format_utc(_WINDOWS[3][0])
    assert not [e for e in logs if e["event"] == "connectors.sync.completed"]


def test_ut01_38_first_fatal_error_wins(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-38 with a retryable failure in slice 2 and a fatal one in slice 6, the fatal
    error is raised."""
    fatal = AuthError("bad token")
    source = Source(plan=_failing({2: SourceUnavailable("down"), 6: fatal}))
    with pytest.raises(AuthError) as info:
        _parallel(ops_store, lake, source).run_backfill(_ENT, _START, NOW)
    assert info.value is fatal


def test_ut01_38_first_error_in_slice_order(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-38 without a fatal error the first failure in slice order is raised."""
    first = SourceUnavailable("first")
    source = Source(plan=_failing({3: first, 7: SourceUnavailable("second")}))
    with pytest.raises(SourceUnavailable) as again:
        _parallel(ops_store, lake, source).run_backfill(_ENT, _START, NOW)
    assert again.value is first


def test_ut01_38_resume_reruns_only_failed_and_running(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-38 a rerun fetches only the failed slice and a slice left running, from their
    starts; done slices are skipped; then the watermark is min(end, max of this call)."""
    source = Source(plan=_failing({4: SourceUnavailable("down")}))
    with pytest.raises(SourceUnavailable):
        _parallel(ops_store, lake, source).run_backfill(_ENT, _START, NOW)
    mark_slice_running(_SRC, _ENT, _WINDOWS[7][0], now=NOW)  # a crashed worker's slice

    healthy = Source(plan=lambda n, s, u: one_row(n, _ENT, s, u))
    result = _parallel(ops_store, lake, healthy).run_backfill(_ENT, _START, NOW)

    assert healthy.windows() == [_WINDOWS[3], _WINDOWS[7]]
    rows = slices()
    assert [r["status"] for r in rows] == ["done"] * 10
    assert rows[3]["attempts"] == 2
    assert rows[3]["last_error"] is None
    assert rows[7]["attempts"] == 3
    assert result.rows == 2
    assert watermark(_SRC, _ENT) == _WINDOWS[7][0]


# --- UT01-39: no rows, and stop ------------------------------------------------------------


def test_ut01_39_no_rows_sets_watermark_to_end(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-39 when no slice commits a row, every slice is done and the watermark is `end`."""
    runner = _parallel(ops_store, lake, Source(plan=no_rows))
    end = NOW - _DAY

    result = runner.run_backfill(_ENT, _START, end)

    assert [r["status"] for r in slices()] == ["done"] * 10
    assert [r["files"] for r in slices()] == ["[]"] * 10
    assert watermark(_SRC, _ENT) == end
    assert result.rows == 0
    assert result.files == ()
    assert result.watermark_after == clock.format_utc(end)


def test_ut01_39_stop_leaves_slices_pending_and_watermark(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-39 should_stop true after 2 slices: two slices done, the rest pending, `stopped`
    true, the watermark unchanged; the next run resumes the pending slices only."""
    earlier = _START - _DAY
    set_watermark(_SRC, _ENT, "sys_updated_on", earlier, now=NOW)
    source = Source(plan=no_rows)
    cfg = servicenow_cfg(max_concurrency=3)
    conn = WindowConnector(source)
    stopper = stop_after(2)
    runner = backfill_runner(conn, cfg, lake, ops_store.data_root, should_stop=stopper)

    with capture_logs() as logs:
        result = runner.run_backfill(_ENT, _START, NOW)

    assert runner.stopped is True
    assert [r["status"] for r in slices()] == ["done"] * 2 + ["pending"] * 8
    assert source.windows() == _WINDOWS[:2]
    assert watermark(_SRC, _ENT) == earlier
    assert result.mode == "backfill"
    assert result.watermark_before == result.watermark_after == clock.format_utc(earlier)
    assert not [e for e in logs if e["event"] == "connectors.sync.completed"]

    resumed = backfill_runner(conn, cfg, lake, ops_store.data_root, should_stop=lambda: False)
    resumed.run_backfill(_ENT, _START, NOW)
    assert resumed.stopped is False
    assert source.windows() == sorted(_WINDOWS)
    assert watermark(_SRC, _ENT) == NOW


def test_ut01_39_stopped_flag_resets_on_next_run(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-39 `stopped` is reset at the start of every run."""
    stops = iter([True, False])
    conn = WindowConnector(Source(plan=no_rows))
    runner = backfill_runner(
        conn, servicenow_cfg(), lake, ops_store.data_root, should_stop=lambda: next(stops, False)
    )
    runner.run_backfill(_ENT, NOW - 7 * _DAY, NOW)
    assert runner.stopped is True
    runner.run_backfill(_ENT, NOW - 7 * _DAY, NOW)
    assert runner.stopped is False
    assert watermark(_SRC, _ENT) == NOW


# --- UT01-93: monitoring slices keyed per tool ---------------------------------------------

_PROM, _DD = "monitoring:prometheus", "monitoring:datadog"
_MON_START = NOW - 60 * _DAY  # monitoring slices are 30 days wide: 2 per tool


def _event_row(name: str, since: datetime.datetime, until: datetime.datetime) -> list[Any]:
    return [batch(name, "event", [f"e{since:%Y%m%d}"], since)]


def test_ut01_93_monitoring_slices_keyed_per_tool(
    ops_store: OpsStoreHandle, lake: FakeLake, guard: FakeGuard
) -> None:
    """UT01-93 a monitoring backfill plans, runs and marks slices under monitoring:<tool>,
    guards each tool key per slice and sets each tool's watermark."""
    source = Source()
    plans = {"prometheus": _event_row, "datadog": no_rows}
    conn = WindowToolConnector(source, plans)
    factory = factory_of(lambda: WindowToolConnector(source, plans), source)
    runner = backfill_runner(conn, monitoring_cfg(), lake, ops_store.data_root, factory=factory)

    result = runner.run_backfill("event", _MON_START, NOW)

    assert {r["source"] for r in slices()} == {_PROM, _DD}
    assert [r["status"] for r in slices(_PROM)] == ["done", "done"]
    assert [r["rows"] for r in slices(_PROM)] == [1, 1]
    assert [r["status"] for r in slices(_DD)] == ["done", "done"]
    assert sorted(c[0] for c in source.calls) == ["datadog"] * 2 + ["prometheus"] * 2
    assert sorted(guard.calls) == [_DD, _DD, _PROM, _PROM]
    assert watermark(_PROM, "event") == NOW - 30 * _DAY
    assert watermark(_DD, "event") == NOW
    assert result.mode == "backfill"
    assert result.rows == 2
    assert result.watermark_after == clock.format_utc(NOW - 30 * _DAY)


def test_ut01_93_tool_workers_from_adapter(
    ops_store: OpsStoreHandle, lake: FakeLake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-93 each tool backfills with its adapter's max_concurrency and a fetch bound to
    that tool's `sync_tool`."""
    seen: list[tuple[str, int, Any]] = []
    real = backfill_module.run_backfill

    def spy(runner: SyncRunner, entity: str, start: Any, end: Any, **kw: Any) -> Any:
        seen.append((kw["key"], kw["max_workers"], kw["fetch_of"]))
        return real(runner, entity, start, end, **kw)

    monkeypatch.setattr(backfill_module, "run_backfill", spy)
    source = Source()
    plans = {"prometheus": no_rows, "datadog": no_rows}
    conn = WindowToolConnector(source, plans)
    runner = backfill_runner(conn, monitoring_cfg(), lake, ops_store.data_root)
    runner.run_backfill("event", _MON_START, NOW)

    assert [(k, w) for k, w, _ in seen] == [(_PROM, 4), (_DD, 2)]
    list(seen[1][2](conn)("event", _MON_START, NOW))
    assert source.calls[-1] == ("datadog", "event", _MON_START, NOW)


def test_ut01_93_tool_failure_isolated(
    ops_store: OpsStoreHandle, lake: FakeLake, guard: FakeGuard
) -> None:
    """UT01-93 a failing datadog slice and an open prometheus breaker: each tool's own slices
    are marked, the fatal-first error is raised after both tools, and the open tool is
    listed in skipped_open."""
    err = SourceUnavailable("datadog down")
    guard.open_keys.add(_PROM)
    plans: dict[str, Any] = {"prometheus": _event_row, "datadog": lambda *_: [err]}
    runner = backfill_runner(
        WindowToolConnector(Source(), plans), monitoring_cfg(), lake, ops_store.data_root
    )

    with capture_logs() as logs, pytest.raises(SourceUnavailable) as info:
        runner.run_backfill("event", _MON_START, NOW)

    assert info.value is err
    assert runner.skipped_open == (_PROM,)
    assert [r["status"] for r in slices(_DD)] == ["failed", "failed"]
    assert [r["last_error"] for r in slices(_PROM)] == [f"CircuitOpen: circuit open: {_PROM}"] * 2
    failed = [e for e in logs if e["event"] == "connectors.sync.failed"]
    assert [(e["stream"], e["mode"]) for e in failed] == [(_DD, "backfill")]
    assert watermark(_PROM, "event") is None
    assert watermark(_DD, "event") is None


def test_ut01_93_plan_reuse_via_ensure_slices(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-93 slices planned under a tool key are the rows ensure_slices returns for it."""
    runner = backfill_runner(
        WindowToolConnector(Source(), {"prometheus": no_rows}),
        monitoring_cfg(),
        lake,
        ops_store.data_root,
    )
    runner.run_backfill("event", _MON_START, NOW)
    windows = split_range(_MON_START, NOW, 30 * _DAY)
    plan = ensure_slices(_PROM, "event", windows, now=NOW)
    assert [(r.slice_start, r.slice_end, r.status) for r in plan] == [
        (a, b, "done") for a, b in windows
    ]


def test_ut01_37_files_are_relative_to_data_root(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-37 slice files are stored as POSIX paths relative to the runner's data root."""
    runner = _parallel(ops_store, lake, Source(plan=lambda n, s, u: one_row(n, _ENT, s, u)))
    result = runner.run_backfill(_ENT, NOW - 7 * _DAY, NOW)
    (stored,) = json.loads(slices()[0]["files"])
    assert Path(ops_store.data_root, stored) == result.files[0]
