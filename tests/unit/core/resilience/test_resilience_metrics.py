"""Tests for herness.core.resilience.metrics: record_counter, record_histogram, record_gauge,
timed and flush_metrics (impl 08 U08-19 … U08-22, U08-103; T08-05)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
import structlog
from tests.support.fake_clock import FakeClock
from tests.support.ops_store import OpsStoreHandle

from herness.core import audit, redact
from herness.core import time as clock
from herness.core.errors import ConfigError, StoreBusy
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState, bind_ops_backend, process_state
from herness.core.resilience import metrics as m
from herness.core.settings import RedactionConfig
from herness.core.types import MetricSample
from herness.store.ops.core import read_all, run_write
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.unit


class _Mono:
    """Controllable `herness.core.time.monotonic`."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def mono(monkeypatch: pytest.MonkeyPatch) -> _Mono:
    fake = _Mono()
    monkeypatch.setattr(clock, "monotonic", fake)
    return fake


class _FailingBackend(SqliteResilienceBackend):
    def insert_metric_samples(self, rows: Any) -> int:
        del rows
        msg = "ops store busy in metric_samples"
        raise StoreBusy(msg, op="metric_samples")


class _SecondChunkFails(SqliteResilienceBackend):
    def __init__(self) -> None:
        self.calls = 0

    def insert_metric_samples(self, rows: Any) -> int:
        self.calls += 1
        if self.calls == 2:
            msg = "ops store busy in metric_samples"
            raise StoreBusy(msg, op="metric_samples")
        return super().insert_metric_samples(rows)


def _rows(kind: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM metric_sample"
    params: tuple[str, ...] = ()
    if kind is not None:
        sql += " WHERE kind = ?"
        params = (kind,)
    return [dict(row) for row in read_all(sql + " ORDER BY rowid", params)]


@pytest.mark.parametrize(
    ("name", "component", "labels"),
    [
        ("jobs_enqueued_total", "jobs", None),  # no herness_ prefix
        ("herness_jobs_enqueued_things", "jobs", None),  # unit not allowed
        ("herness_jobs", "jobs", None),  # no unit segment
        ("herness_jobs_enqueued_total", "resilience", None),  # component mismatch
        ("herness_" + "a" * 40 + "_total", "a" * 40, None),  # component too long
        ("herness_jobs_enqueued_total", "jobs", {f"k{i}": "v" for i in range(7)}),  # 7 labels
        ("herness_jobs_enqueued_total", "jobs", {"kind": "has space"}),
        ("herness_jobs_enqueued_total", "jobs", {"Kind": "sync"}),  # bad key
        ("herness_jobs_enqueued_total", "jobs", {"kind": 3}),  # non-string value
    ],
)
def test_ut08_31_invalid_name_or_labels(
    reset_process_state: ProcessState, name: str, component: str, labels: Any
) -> None:
    """UT08-31 bad names, component mismatch, 7 labels, a label value with a space → ConfigError."""
    for record in (m.record_counter, m.record_histogram):
        with pytest.raises(ConfigError, match="invalid metric"):
            record(name, 1.0, component=component, labels=labels)
    assert reset_process_state.metric_buffer.counters == {}
    assert reset_process_state.metric_buffer.histograms == []


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), True, "1"])
def test_ut08_31_invalid_value(reset_process_state: ProcessState, value: Any) -> None:
    """UT08-31 negative, non-finite, bool or non-number values → ConfigError naming the metric."""
    del reset_process_state
    with pytest.raises(ConfigError, match="herness_jobs_enqueued_total"):
        m.record_counter("herness_jobs_enqueued_total", value, component="jobs")
    with pytest.raises(ConfigError, match="herness_jobs_run_seconds"):
        m.record_histogram("herness_jobs_run_seconds", value, component="jobs")


def test_ut08_32_counters_histograms_timed_and_flush(
    ops_db: OpsStoreHandle,
    fake_clock: FakeClock,
    mono: _Mono,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT08-32 1 000 increments on 2 label sets → 2 summed counter rows; 5 histogram values
    and a `timed` block → raw histogram rows."""
    del ops_db, mono
    for i in range(1000):
        m.record_counter(
            "herness_jobs_enqueued_total", component="jobs", labels={"kind": f"k{i % 2}"}
        )
    m.record_counter("herness_jobs_enqueued_total", 2.5, component="jobs", labels={"kind": "k0"})
    for value in (0.1, 0.2, 0.3, 0.4, 0.5):
        m.record_histogram("herness_jobs_run_seconds", value, component="jobs")
    ticks = iter([10.0, 12.5])
    monkeypatch.setattr(m.time, "perf_counter", lambda: next(ticks))
    with m.timed("herness_jobs_supervisor_tick_seconds", component="jobs"):
        pass
    assert _rows() == []  # nothing flushed before the explicit call
    assert m.flush_metrics() == 8
    counters = _rows("counter")
    assert [(json.loads(r["labels"]), r["value"]) for r in counters] == [
        ({"kind": "k0"}, 502.5),
        ({"kind": "k1"}, 500.0),
    ]
    assert {r["ts"] for r in _rows()} == {clock.format_utc(fake_clock.now())}
    histograms = [(r["name"], r["value"]) for r in _rows("histogram")]
    assert histograms == [
        *(("herness_jobs_run_seconds", v) for v in (0.1, 0.2, 0.3, 0.4, 0.5)),
        ("herness_jobs_supervisor_tick_seconds", 2.5),
    ]
    assert m.flush_metrics() == 0


def test_ut08_32_timed_records_on_exception_and_checks_unit(
    reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-32 `timed` records the elapsed time when the block raises; a non-seconds name
    is refused on entry."""
    ticks = iter([1.0, 4.0])
    monkeypatch.setattr(m.time, "perf_counter", lambda: next(ticks))

    def fail() -> None:
        with m.timed("herness_jobs_run_seconds", component="jobs"):
            msg = "block fails"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError):
        fail()
    assert [v for _, v in reset_process_state.metric_buffer.histograms] == [3.0]
    with pytest.raises(ConfigError), m.timed("herness_jobs_run_total", component="jobs"):
        pass


def test_ut08_32_overflow_dropped_and_logged(
    reset_process_state: ProcessState, ops_store: OpsStoreHandle
) -> None:
    """UT08-32 10 001 histogram values while unbound: 10 000 kept, 1 dropped and logged at
    the next flush; flush while unbound returns 0 and keeps the buffer."""
    del ops_store
    for i in range(10_001):
        m.record_histogram("herness_jobs_queue_wait_seconds", float(i), component="jobs")
    assert m.flush_metrics() == 0
    assert len(reset_process_state.metric_buffer.histograms) == m.METRIC_BUFFER_MAX
    assert reset_process_state.metric_buffer.dropped == 1
    bind_ops_backend(SqliteResilienceBackend())
    with structlog.testing.capture_logs() as logs:
        assert m.flush_metrics() == 10_000
    dropped = next(e for e in logs if e["event"] == "resilience.metrics.dropped")
    assert (dropped["log_level"], dropped["count"]) == ("warning", 1)
    assert len(_rows("histogram")) == 10_000
    assert process_state().metric_buffer.dropped == 0


def test_ut08_32_auto_flush_rules(ops_db: OpsStoreHandle, mono: _Mono) -> None:
    """UT08-32 a record call flushes once the last flush is ≥ 10 s old (a fresh buffer's
    interval starts at its first recording, on `clock.monotonic`); the 1 000th histogram
    observation triggers a flush."""
    del ops_db
    assert process_state().metric_buffer.last_flush is None
    m.record_counter("herness_jobs_enqueued_total", component="jobs")
    mono.now += 9.9
    m.record_counter("herness_jobs_enqueued_total", component="jobs")
    assert _rows() == []
    mono.now += 0.1
    m.record_counter("herness_jobs_enqueued_total", component="jobs")
    assert [r["value"] for r in _rows("counter")] == [3.0]
    assert process_state().metric_buffer.last_flush == mono.now
    mono.now += 9.9
    m.record_counter("herness_jobs_enqueued_total", component="jobs")
    assert len(_rows("counter")) == 1
    for i in range(999):
        m.record_histogram("herness_jobs_run_seconds", float(i), component="jobs")
    assert _rows("histogram") == []
    m.record_histogram("herness_jobs_run_seconds", 999.0, component="jobs")
    assert len(_rows("histogram")) == 1000


def test_ut08_32_flush_failure_is_logged_and_discarded(
    reset_process_state: ProcessState,
) -> None:
    """UT08-32 a store failure logs resilience.metrics.flush_failed with the row count,
    discards the rows and never raises into the recording call."""
    bind_ops_backend(_FailingBackend())
    m.record_counter("herness_jobs_enqueued_total", component="jobs")
    m.record_gauge("herness_jobs_gpu_vram_used_mb", 812.0, component="jobs")
    reset_process_state.metric_buffer.last_flush = clock.monotonic() - 60  # next call is due
    with structlog.testing.capture_logs() as logs:
        m.record_counter("herness_jobs_enqueued_total", component="jobs")
    failed = next(e for e in logs if e["event"] == "resilience.metrics.flush_failed")
    assert (failed["log_level"], failed["rows"], failed["error_type"]) == (
        "warning",
        2,
        "StoreBusy",
    )
    assert process_state().metric_buffer.counters == {}


def test_ut08_111_gauges_last_write_wins_and_key_cap(
    ops_db: OpsStoreHandle, fake_clock: FakeClock, mono: _Mono
) -> None:
    """UT08-111 one gauge row with the latest value and time; negative accepted; `_total`
    and NaN refused; key 1 001 dropped and counted."""
    del ops_db, mono
    name = "herness_jobs_queue_depth_count"
    m.record_gauge(name, 5, component="jobs", labels={"gpu_class": "none"})
    at = fake_clock.advance(5)
    m.record_gauge(name, 3, component="jobs", labels={"gpu_class": "none"})
    m.record_gauge("herness_jobs_gpu_vram_used_mb", -2.0, component="jobs")
    with pytest.raises(ConfigError):
        m.record_gauge("herness_jobs_enqueued_total", 1, component="jobs")
    with pytest.raises(ConfigError):
        m.record_gauge(name, float("nan"), component="jobs")
    for i in range(999):  # keys 3 … 1 001; key 1 001 is refused
        m.record_gauge(name, float(i), component="jobs", labels={"gpu_class": f"c{i}"})
    buffer = process_state().metric_buffer
    assert (len(buffer.gauges), buffer.dropped) == (1000, 1)
    assert (name, (("gpu_class", "c998"),), "jobs") not in buffer.gauges
    m.record_gauge(name, 7, component="jobs", labels={"gpu_class": "none"})  # existing key
    assert buffer.dropped == 1
    with structlog.testing.capture_logs() as logs:
        assert m.flush_metrics() == 1000
    assert next(e for e in logs if e["event"] == "resilience.metrics.dropped")["count"] == 1
    first = [r for r in _rows("gauge") if r["labels"] == '{"gpu_class":"none"}']
    assert [(r["value"], r["ts"]) for r in first] == [(7.0, clock.format_utc(at))]
    assert [r["value"] for r in _rows("gauge") if r["name"].endswith("_mb")] == [-2.0]


def test_ut08_111_gauge_row_uses_last_call_time(
    ops_db: OpsStoreHandle, fake_clock: FakeClock, mono: _Mono
) -> None:
    """UT08-111 the gauge row's ts is the time of the last call, not the flush time."""
    del ops_db, mono
    set_at = fake_clock.now()
    m.record_gauge("herness_jobs_queue_depth_count", 3, component="jobs")
    fake_clock.advance(30)
    assert m.flush_metrics() == 1
    assert _rows("gauge")[0]["ts"] == clock.format_utc(set_at)
    sample = MetricSample(
        ts=set_at,
        name="herness_jobs_queue_depth_count",
        kind="gauge",
        value=-1.0,
        component="jobs",
    )
    assert sample.value == -1.0


def test_ut08_32_audit_and_redact_call_sites(
    reset_process_state: ProcessState, herness_cfg: object
) -> None:
    """UT08-32 call sites: each audit line counts herness_audit_lines_total{event}; a failed
    redact_batch item counts herness_redact_records_total{result="failed"}."""
    del herness_cfg
    audit.audit("admin_action", "system", action="backup", target="data")
    audit.audit("admin_action", "system", action="purge", target="data")
    directory = NameDirectory.from_files(None, (), None)
    redactor = redact.Redactor(RedactionConfig(directory_file=None), bytes(32), directory)
    assert redactor.redact_batch(["plain", "a" * 4_000_001]) == ["plain", None]
    assert reset_process_state.metric_buffer.counters == {
        ("herness_audit_lines_total", (("event", "admin_action"),), "audit"): 2.0,
        ("herness_redact_records_total", (("result", "failed"),), "redact"): 1.0,
    }


def test_ut08_32_partial_flush_counts_written_and_discarded(
    ops_db: OpsStoreHandle, mono: _Mono
) -> None:
    """UT08-32 a flush whose second 500-row chunk fails returns the 500 rows written and logs
    flush_failed with the 200 rows discarded; the first chunk stays committed."""
    del ops_db, mono
    backend = _SecondChunkFails()
    bind_ops_backend(backend)
    for i in range(700):
        m.record_histogram("herness_jobs_run_seconds", float(i), component="jobs")
    with structlog.testing.capture_logs() as logs:
        assert m.flush_metrics() == 500
    failed = next(e for e in logs if e["event"] == "resilience.metrics.flush_failed")
    assert (failed["rows"], failed["written"], failed["error_type"]) == (200, 500, "StoreBusy")
    assert backend.calls == 2
    assert len(_rows("histogram")) == 500
    assert process_state().metric_buffer.histograms == []


class _BrokenStoreBackend(SqliteResilienceBackend):
    def write_open(self) -> bool:
        msg = "ops store busy while opening"
        raise StoreBusy(msg)


def test_ut08_32_auto_flush_defers_inside_an_open_write(
    ops_db: OpsStoreHandle, mono: _Mono
) -> None:
    """UT08-32 a due auto-flush inside a `run_write` callback keeps the buffer (no nested-write
    failure, nothing discarded); the next due record call outside the write flushes it
    (T08-11, T08-05 m3)."""
    del ops_db
    m.record_counter("herness_jobs_enqueued_total", component="jobs")
    mono.now += 10.0

    def inside(conn: sqlite3.Connection) -> None:
        del conn
        with structlog.testing.capture_logs() as logs:
            m.record_counter("herness_jobs_enqueued_total", component="jobs")
            m.record_gauge("herness_jobs_gpu_vram_used_mb", 512.0, component="jobs")
            m.record_histogram("herness_jobs_run_seconds", 1.5, component="jobs")
        assert logs == []
        assert SqliteResilienceBackend().write_open()

    run_write(inside, op="test_write")
    assert _rows() == []
    assert not SqliteResilienceBackend().write_open()
    m.record_gauge("herness_jobs_gpu_vram_used_mb", 640.0, component="jobs")  # flushes all
    assert [r["value"] for r in _rows("counter")] == [2.0]
    assert [r["value"] for r in _rows("gauge")] == [640.0]
    assert [r["value"] for r in _rows("histogram")] == [1.5]


def test_ut08_32_auto_flush_flushes_when_the_store_check_fails(
    reset_process_state: ProcessState, ops_store: OpsStoreHandle, mono: _Mono
) -> None:
    """UT08-32 a store error from the open-write check does not defer: the flush runs."""
    del ops_store
    bind_ops_backend(_BrokenStoreBackend())
    m.record_counter("herness_jobs_enqueued_total", component="jobs")
    mono.now += 10.0
    m.record_counter("herness_jobs_enqueued_total", component="jobs")
    assert [r["value"] for r in _rows("counter")] == [2.0]
    assert reset_process_state.metric_buffer.counters == {}
