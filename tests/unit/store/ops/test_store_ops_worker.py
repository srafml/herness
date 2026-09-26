"""Unit tests for herness.store.ops.worker (impl 08 U08-96; T08-11).

Backend half of UT08-65 and UT08-108: rows are checked against the real migrated `worker` and
`run` DDL (impl 02 migrations 002 and 003). `worker_alive` and `request_gpu_class` themselves
are later 08 cards.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from tests.support.ops_store import OpsStoreHandle

from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.jobs.ports import WorkerRow
from herness.store.ops.core import read_one, run_write
from herness.store.ops.jobs import SqliteJobsBackend
from herness.store.ops.worker import WORKER_UPDATE_COLUMNS, WorkerSqlMixin

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
HEARTBEAT_S = 30


@pytest.fixture
def backend(ops_store: OpsStoreHandle) -> SqliteJobsBackend:
    del ops_store
    return SqliteJobsBackend()


def _worker(worker_id: str = "h:1", **changes: object) -> WorkerRow:
    values: dict[str, object] = {
        "worker_id": worker_id,
        "host": "h",
        "pid": 1,
        "gpu_slot": 1,
        "cpu_slots": 2,
        "gpu_class_loaded": "none",
        "status": "running",
        "started_at": T0,
        "heartbeat_at": T0,
        "version": "0.1.0",
    }
    return WorkerRow.model_validate(values | changes)


def test_ut08_65_upsert_and_list_workers(backend: SqliteJobsBackend) -> None:
    """UT08-65 `upsert_worker` inserts then replaces every column; `list_workers` parses rows."""
    assert backend.list_workers() == []
    jobs = [{"job_id": "job_1", "slot": "gpu", "kind": "review", "started_at": "t"}]
    first = _worker(requested_class="large", current_jobs=jobs, faults_enabled=True)
    backend.upsert_worker(first)
    assert backend.list_workers() == [first]
    second = _worker(status="draining", pid=2, heartbeat_at=T0 + timedelta(seconds=30))
    backend.upsert_worker(second)
    backend.upsert_worker(_worker("a:9", host="a"))
    assert [w.worker_id for w in backend.list_workers()] == ["a:9", "h:1"]
    assert backend.list_workers()[1] == second


def test_ut08_65_heartbeat_ages_give_alive_and_dead(backend: SqliteJobsBackend) -> None:
    """UT08-65 heartbeats 89 s and 91 s old with `heartbeat_s` 30: the rows let the caller
    compute alive (89 s) and dead (91 s) under `heartbeat_at > now - 3 * heartbeat_s`."""
    now = T0 + timedelta(minutes=10)
    backend.upsert_worker(_worker("h:1", heartbeat_at=now - timedelta(seconds=89)))
    backend.upsert_worker(_worker("h:2", heartbeat_at=now - timedelta(seconds=91)))
    cutoff = now - timedelta(seconds=3 * HEARTBEAT_S)
    alive = {
        w.worker_id: w.status in ("starting", "running", "draining") and w.heartbeat_at > cutoff
        for w in backend.list_workers()
    }
    assert alive == {"h:1": True, "h:2": False}


def test_ut08_65_update_worker_sets_allowlisted_columns(backend: SqliteJobsBackend) -> None:
    """UT08-65 `update_worker` binds each allowlisted column (ts text, JSON, 0/1)."""
    backend.upsert_worker(_worker())
    beat = T0 + timedelta(seconds=30)
    jobs = [{"job_id": "job_1", "slot": "cpu0", "kind": "sync", "started_at": beat}]
    backend.update_worker(
        "h:1",
        status="draining",
        gpu_class_loaded="swapping",
        requested_class="decider",
        current_jobs=jobs,
        heartbeat_at=beat,
        faults_enabled=True,
    )
    (row,) = backend.list_workers()
    assert (row.status, row.gpu_class_loaded, row.requested_class) == (
        "draining",
        "swapping",
        "decider",
    )
    assert row.heartbeat_at == beat
    assert row.faults_enabled is True
    assert row.current_jobs == [
        {"job_id": "job_1", "slot": "cpu0", "kind": "sync", "started_at": clock.format_utc(beat)}
    ]
    backend.update_worker("h:1")  # no fields: nothing written
    assert backend.list_workers() == [row]


def test_ut08_65_update_worker_rejects_other_columns(backend: SqliteJobsBackend) -> None:
    """UT08-65 a column outside the fixed allowlist → ConfigError and no write; a value the
    table rejects → SchemaViolation."""
    assert (
        frozenset(
            {"status", "gpu_class_loaded", "requested_class", "current_jobs", "heartbeat_at"}
            | {"faults_enabled"}
        )
        == WORKER_UPDATE_COLUMNS
    )
    backend.upsert_worker(_worker())
    with pytest.raises(ConfigError, match="pid"):
        backend.update_worker("h:1", status="stopped", pid=5)
    with pytest.raises(ConfigError, match=r"worker_id = 1; --"):
        backend.update_worker("h:1", **{"worker_id = 1; --": "x"})
    with pytest.raises(SchemaViolation):
        backend.update_worker("h:1", status="exploded")
    assert backend.list_workers() == [_worker()]


def test_ut08_108_set_requested_class(backend: SqliteJobsBackend) -> None:
    """UT08-108 `set_requested_class` sets and clears `requested_class` of one worker."""
    backend.upsert_worker(_worker())
    backend.upsert_worker(_worker("h:2"))
    backend.set_requested_class("h:1", "large")
    assert [w.requested_class for w in backend.list_workers()] == ["large", None]
    backend.set_requested_class("h:1", None)
    assert [w.requested_class for w in backend.list_workers()] == [None, None]


def test_ut08_108_run_row(backend: SqliteJobsBackend) -> None:
    """UT08-108 `run_row` returns `(kind, status)` of a run, or None."""

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at)"
            " VALUES ('run_1', 'org_review', 'standard', 'default', 'h', 'running', ?)",
            (clock.format_utc(T0),),
        )

    run_write(insert, op="test_setup")
    assert backend.run_row("run_1") == ("org_review", "running")
    assert backend.run_row("run_x") is None
    assert read_one("SELECT COUNT(*) FROM run")[0] == 1  # type: ignore[index]


def test_ut08_108_backend_is_a_worker_mixin() -> None:
    """UT08-108 `SqliteJobsBackend` inherits `WorkerSqlMixin` (U08-96)."""
    assert issubclass(SqliteJobsBackend, WorkerSqlMixin)
