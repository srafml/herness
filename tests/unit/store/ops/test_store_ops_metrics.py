"""Unit tests for herness.store.ops.metrics, the single metric_sample writer (impl 08 U08-100,
U08-101; T08-05)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import ValidationError
from tests.support.ops_store import OpsStoreHandle

from herness.core.errors import ConfigError, FatalError
from herness.core.types import MetricSample
from herness.store import ops
from herness.store.ops import core
from herness.store.ops import metrics as area
from herness.store.ops.core import read_all, run_write

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_KINDS: tuple[Literal["counter", "gauge", "histogram"], ...] = ("counter", "gauge", "histogram")


def _sample(i: int, ts: datetime = T0) -> MetricSample:
    return MetricSample(
        ts=ts,
        name="herness_jobs_run_seconds",
        kind=_KINDS[i % 3],
        value=float(i),
        labels={"kind": "sync", "slot": f"s{i % 2}"},
        component="jobs",
    )


def _count() -> int:
    return int(read_all("SELECT COUNT(*) FROM metric_sample")[0][0])


def test_ut08_110_writes_1201_rows_in_3_transactions(
    ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-110 1 201 samples of all three kinds become 1 201 rows in 3 transactions."""
    del ops_store
    ops_seen: list[str] = []

    def spy[T](fn: Callable[[sqlite3.Connection], T], *, op: str) -> T:
        ops_seen.append(op)
        return core.run_write(fn, op=op)

    monkeypatch.setattr(area, "run_write", spy)
    assert ops.record_metric_samples is area.record_metric_samples
    assert area.record_metric_samples([_sample(i) for i in range(1201)]) == 1201
    assert ops_seen == ["metric_samples"] * 3
    assert _count() == 1201
    rows = read_all("SELECT * FROM metric_sample ORDER BY rowid LIMIT 3")
    assert [dict(r) for r in rows][1] == {
        "ts": "2026-09-01T12:00:00.000000Z",
        "name": "herness_jobs_run_seconds",
        "kind": "gauge",
        "value": 1.0,
        "labels": '{"kind":"sync","slot":"s1"}',
        "component": "jobs",
    }
    assert {r["kind"] for r in rows} == set(_KINDS)
    assert area.record_metric_samples([]) == 0


def test_ut08_110_conn_rows_commit_with_the_caller(ops_store: OpsStoreHandle) -> None:
    """UT08-110 with `conn` the rows join the caller's transaction: commit and rollback."""
    del ops_store
    samples = [_sample(i) for i in range(3)]
    assert run_write(lambda c: area.record_metric_samples(samples, conn=c), op="test") == 3
    assert _count() == 3

    def fail(conn: sqlite3.Connection) -> None:
        area.record_metric_samples(samples, conn=conn)
        msg = "caller fails after writing"
        raise RuntimeError(msg)

    with pytest.raises(FatalError):
        run_write(fail, op="test")
    assert _count() == 3


def test_ut08_110_oversized_labels_and_free_text(ops_store: OpsStoreHandle) -> None:
    """UT08-110 1 100-byte labels → ConfigError naming the metric; free text → ValidationError."""
    del ops_store
    big = MetricSample.model_construct(
        ts=T0,
        name="herness_jobs_run_seconds",
        kind="counter",
        value=1.0,
        labels={"k": "v" * 1090},
        component="jobs",
    )
    with pytest.raises(ConfigError, match="herness_jobs_run_seconds"):
        area.record_metric_samples([_sample(0), big])
    assert _count() == 0
    with pytest.raises(ValidationError):
        MetricSample(
            ts=T0,
            name="herness_jobs_run_seconds",
            kind="counter",
            value=1.0,
            labels={"reason": "ticket says the password is hunter2"},
            component="jobs",
        )


def test_ut08_110_too_many_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-110 more than 100 000 samples per call → ConfigError (TH08-10)."""
    monkeypatch.setattr(area, "MAX_SAMPLES_PER_CALL", 2)
    with pytest.raises(ConfigError, match="too many metric samples"):
        area.record_metric_samples([_sample(0)] * 3)


def test_ut08_110_purge_removes_only_older_rows(ops_store: OpsStoreHandle) -> None:
    """UT08-110 purge_metric_samples(now - 90 d) removes only rows older than the cutoff."""
    del ops_store
    ages = (120, 91, 89, 1)
    area.record_metric_samples([_sample(i, T0 - timedelta(days=d)) for i, d in enumerate(ages)])
    assert ops.purge_metric_samples is area.purge_metric_samples
    assert area.purge_metric_samples(T0 - timedelta(days=90)) == 2
    kept = [r["ts"] for r in read_all("SELECT ts FROM metric_sample ORDER BY ts")]
    assert kept == ["2026-06-04T12:00:00.000000Z", "2026-08-31T12:00:00.000000Z"]
