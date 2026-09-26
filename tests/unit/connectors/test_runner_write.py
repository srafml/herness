"""Tests for the sync runner's shared write loop `SyncRunner._write_stream` (U01-40, U01-35;
T01-06): checkpoints, deletion reloads, drift splits, aborts and unordered watermarks."""

from __future__ import annotations

import datetime

import pytest
from structlog.testing import capture_logs
from tests.support.fake_lake import FakeLake
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors._runner_data import (
    NOW,
    UNTIL,
    FakeConnector,
    Step,
    T,
    batch,
    make_runner,
    metrics,
    monitoring_cfg,
    servicenow_cfg,
)

import herness.connectors._write_loop as write_loop
import herness.connectors.backfill as backfill_module
from herness.core.errors import SourceUnavailable
from herness.store.ops import get_watermark, set_watermark
from herness.store.ops.privacy import create_deletion_request, set_deletion_status

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("guard")]

_SRC, _ENT = "servicenow", "incident"
_SEC = datetime.timedelta(seconds=1)


def _seed(source: str = _SRC, entity: str = _ENT, value: datetime.datetime = T) -> None:
    set_watermark(source, entity, "f", value, now=NOW)


def _wm(source: str = _SRC, entity: str = _ENT) -> datetime.datetime | None:
    wm = get_watermark(source, entity)
    return None if wm is None else wm.value


def _spy_watermarks(monkeypatch: pytest.MonkeyPatch, lake: FakeLake) -> None:
    """Record every `set_watermark` call into the lake's ordered event list."""
    real = write_loop.set_watermark

    def spy(
        source: str, entity: str, field: str, value: datetime.datetime, *, now: datetime.datetime
    ) -> bool:
        lake.events.append(("set_watermark", source, value))
        return real(source, entity, field, value, now=now)

    monkeypatch.setattr(write_loop, "set_watermark", spy)
    monkeypatch.setattr(backfill_module, "set_watermark", spy)


def _request_deletion(record_id: str) -> None:
    request = create_deletion_request(
        record_id=record_id, requested_by="a" * 32, reason_ref="ticket-1", now=NOW
    )
    set_deletion_status(request.request_id, "running")


def _tens(n: int) -> list[str]:
    return [f"k{n * 10 + i:02d}" for i in range(10)]


# --- UT01-31: checkpoints, call order, deletion reload -------------------------------------


def test_ut01_31_checkpoints_commit_before_watermark(
    ops_store: OpsStoreHandle, lake: FakeLake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-31 checkpoint_rows 20 and 50 ordered rows give 3 commits, each followed by a
    set_watermark (call order asserted); a deletion request added after the first
    checkpoint drops its id once the next checkpoint reloaded the set."""
    _seed()
    _spy_watermarks(monkeypatch, lake)
    starts = [T + datetime.timedelta(minutes=n) for n in range(5)]
    batches = [batch(_SRC, _ENT, _tens(n), starts[n]) for n in range(5)]
    steps: list[Step] = [batches[0], batches[1]]
    steps += [lambda: _request_deletion(f"{_SRC}:{_ENT}:k45"), batches[2], batches[3], batches[4]]
    conn = FakeConnector(steps=steps)
    notes: list[str] = []
    runner = make_runner(
        conn, servicenow_cfg(checkpoint_rows=20), lake, ops_store.data_root, progress=notes.append
    )

    with capture_logs() as logs:
        result = runner.run_incremental(_ENT)

    wm1, wm2, wm3 = starts[1] + 9 * _SEC, starts[3] + 9 * _SEC, starts[4] + 9 * _SEC
    assert [e for e in lake.events if e[0] != "write"] == [
        ("open", 0),
        ("commit", 0, 20),
        ("set_watermark", _SRC, wm1),
        ("open", 1),
        ("commit", 1, 20),
        ("set_watermark", _SRC, wm2),
        ("open", 2),
        ("commit", 2, 9),
        ("set_watermark", _SRC, wm3),
    ]
    written = lake.writers[2].batches[0]["_source_key"].to_pylist()
    assert "k45" not in written
    assert len(written) == 9
    assert result.rows == 49
    assert result.skipped_deleted == 1
    assert len(result.files) == 3
    assert _wm() == wm3
    assert notes == ["servicenow/incident rows=20", "servicenow/incident rows=40"]
    cps = [e for e in logs if e["event"] == "connectors.sync.checkpoint_committed"]
    assert [(e["stream"], e["rows"], e["files"]) for e in cps] == [(_SRC, 20, 1), (_SRC, 40, 1)]


def test_ut01_31_time_based_checkpoint(
    ops_store: OpsStoreHandle, lake: FakeLake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-31 a writer open for 600 s is checkpointed even below checkpoint_rows."""
    _seed()
    now = [NOW]

    def advance() -> None:
        now[0] += datetime.timedelta(seconds=600)

    conn = FakeConnector(steps=[batch(_SRC, _ENT, ["a"], T), advance, batch(_SRC, _ENT, ["b"], T)])
    runner = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root, clock=lambda: now[0])

    result = runner.run_incremental(_ENT)

    assert [e[:2] for e in lake.events if e[0] == "commit"] == [("commit", 0), ("commit", 1)]
    assert result.rows == 2


def test_ut01_31_fully_deleted_batch_is_skipped(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-31 a batch whose rows are all under a deletion request writes nothing."""
    _seed()
    _request_deletion(f"{_SRC}:{_ENT}:gone")
    conn = FakeConnector(steps=[batch(_SRC, _ENT, ["gone"], T)])
    result = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root).run_incremental(_ENT)

    assert lake.kinds() == ["open", "commit"]
    assert result.rows == 0
    assert result.skipped_deleted == 1
    assert _wm() == T


# --- UT01-33: abort on failure -----------------------------------------------------------


def test_ut01_33_connector_error_aborts_writer(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-33 a connector raising mid-stream aborts the writer once and re-raises; the
    watermark is unchanged."""
    _seed()
    err = SourceUnavailable("source down")
    conn = FakeConnector(steps=[batch(_SRC, _ENT, ["a"], T + _SEC), err])
    runner = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root)

    with pytest.raises(SourceUnavailable) as info:
        runner.run_incremental(_ENT)

    assert info.value is err
    assert lake.writers[0].abort_calls == 1
    assert lake.kinds() == ["open", "write", "abort"]
    assert _wm() == T


def test_ut01_33_keyboard_interrupt_aborts_writer(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-33 a KeyboardInterrupt also aborts the writer before propagating."""
    _seed()
    conn = FakeConnector(steps=[KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        make_runner(conn, servicenow_cfg(), lake, ops_store.data_root).run_incremental(_ENT)
    assert lake.writers[0].abort_calls == 1


def test_ut01_33_failing_abort_is_logged_and_original_raised(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-33 when abort() itself raises, connectors.lake.abort_failed is logged and the
    original error is re-raised."""
    _seed()
    lake.abort_error = OSError("disk gone")
    err = SourceUnavailable("source down")
    conn = FakeConnector(steps=[err])

    with capture_logs() as logs, pytest.raises(SourceUnavailable) as info:
        make_runner(conn, servicenow_cfg(), lake, ops_store.data_root).run_incremental(_ENT)

    assert info.value is err
    failed = [e for e in logs if e["event"] == "connectors.lake.abort_failed"]
    assert len(failed) == 1
    assert failed[0]["log_level"] == "error"
    assert failed[0]["error_class"] == "OSError"
    assert (failed[0]["source"], failed[0]["entity"]) == (_SRC, _ENT)


def test_ut01_33_commit_error_aborts(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-33 a failing final commit aborts the writer and leaves the watermark."""
    _seed()
    lake.fail_on_commit = SourceUnavailable("lake busy")
    conn = FakeConnector(steps=[batch(_SRC, _ENT, ["a"], T + _SEC)])
    with pytest.raises(SourceUnavailable):
        make_runner(conn, servicenow_cfg(), lake, ops_store.data_root).run_incremental(_ENT)
    assert lake.writers[0].abort_calls == 1
    assert _wm() == T


# --- UT01-34: schema drift ---------------------------------------------------------------


def test_ut01_34_drift_commits_before_new_schema(ops_store: OpsStoreHandle, lake: FakeLake) -> None:
    """UT01-34 batch 2 adds a column: commit before batch 2, two writers, and
    connectors.schema_drift.detected logged with the added column and a drift metric."""
    _seed()
    conn = FakeConnector(
        steps=[batch(_SRC, _ENT, ["a"], T), batch(_SRC, _ENT, ["b"], T + _SEC, extra="u_new")]
    )
    with capture_logs() as logs:
        result = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root).run_incremental(
            _ENT
        )

    assert lake.kinds() == ["open", "write", "commit", "open", "write", "commit"]
    assert len(lake.writers) == 2
    assert result.rows == 2
    drift = [e for e in logs if e["event"] == "connectors.schema_drift.detected"]
    assert len(drift) == 1
    assert drift[0]["log_level"] == "warning"
    assert (drift[0]["added"], drift[0]["removed"], drift[0]["changed"]) == (("u_new",), (), ())
    assert (drift[0]["source"], drift[0]["entity"]) == (_SRC, _ENT)
    drift_metrics = [m for m in metrics() if m[0] == "herness_connectors_schema_drift_total"]
    assert drift_metrics == [
        ("herness_connectors_schema_drift_total", {"source": _SRC, "entity": _ENT}, 1.0)
    ]


def test_ut01_34_drift_right_after_checkpoint_needs_no_commit(
    ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """UT01-34 drift on the first batch after a checkpoint opens no extra file."""
    _seed()
    steps = [
        batch(_SRC, _ENT, [f"a{i}" for i in range(1000)], T),
        batch(_SRC, _ENT, ["b"], T + 2000 * _SEC, extra="u_new"),
    ]
    conn = FakeConnector(steps=steps)
    cfg = servicenow_cfg(checkpoint_rows=1000)
    make_runner(conn, cfg, lake, ops_store.data_root).run_incremental(_ENT)

    assert [e[0] for e in lake.events if e[0] != "write"] == ["open", "commit", "open", "commit"]


# --- UT01-36: unordered streams ----------------------------------------------------------


def test_ut01_36_unordered_watermark_set_once_capped(
    ops_store: OpsStoreHandle, lake: FakeLake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-36 an unordered (monitoring) stream with rows past until sets the watermark
    once, after the last commit, capped at until."""
    _seed("monitoring", "event")
    _spy_watermarks(monkeypatch, lake)
    late = UNTIL + datetime.timedelta(minutes=5)
    rows = [
        batch("monitoring", "event", [f"e{i}" for i in range(10)], late, step=-_SEC),
        batch("monitoring", "event", [f"f{i}" for i in range(10)], T + _SEC),
        batch("monitoring", "event", [f"g{i}" for i in range(10)], T + 2 * _SEC),
    ]
    conn = FakeConnector(name="monitoring", entities=("event",), steps=rows)
    cfg = monitoring_cfg(checkpoint_rows=20)

    result = make_runner(conn, cfg, lake, ops_store.data_root).run_incremental("event")

    kinds = [e[0] for e in lake.events if e[0] != "write"]
    assert kinds == ["open", "commit", "open", "commit", "set_watermark"]
    assert lake.events[-1] == ("set_watermark", "monitoring", UNTIL)
    assert _wm("monitoring", "event") == UNTIL
    assert result.rows == 30
