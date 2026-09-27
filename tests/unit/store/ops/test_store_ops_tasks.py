"""Unit tests for herness.store.ops.tasks (impl 08 U08-97; T08-16).

Backend half of the task helper tests: `TaskSqlMixin` against the real migrated `task` DDL
(impl 02 migration 003). The core halves (argument checks, logging, error shaping) are in
tests/unit/core/jobs/test_jobs_tasks.py; each test names the ID of the spec row it serves.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from tests.support.ops_store import OpsStoreHandle

from herness.core import time as clock
from herness.core.errors import JobStateError, SchemaViolation
from herness.core.ids import IdKind, new_id
from herness.core.jobs.tasks import CHECKPOINT_MAX_BYTES
from herness.store import ops
from herness.store.ops import tasks as tasks_area
from herness.store.ops.core import connection, read_one, run_write
from herness.store.ops.jobs import SqliteJobsBackend
from herness.store.ops.tasks import TaskSqlMixin

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=1)


@pytest.fixture
def backend(ops_store: OpsStoreHandle) -> SqliteJobsBackend:
    del ops_store
    return SqliteJobsBackend()


@pytest.fixture
def run_id(backend: SqliteJobsBackend) -> str:
    del backend
    rid = new_id(IdKind.RUN)

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at)"
            " VALUES (?, 'org_review', 'standard', 'default', 'h', 'running', ?)",
            (rid, clock.format_utc(T0)),
        )

    run_write(insert, op="test_setup")
    return rid


def _task(
    run_id: str, status: str = "running", attempts: int = 1, checkpoint: str | None = None
) -> str:
    task_id = new_id(IdKind.TASK)

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO task (task_id, run_id, role, spec, status, attempts, checkpoint,"
            " created_at, updated_at) VALUES (?, ?, 'judge', ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                run_id,
                json.dumps({"dedup_key": task_id}),
                status,
                attempts,
                checkpoint,
                clock.format_utc(T0),
                clock.format_utc(T0),
            ),
        )

    run_write(insert, op="test_setup")
    return task_id


def _row(task_id: str) -> Mapping[str, object]:
    row = read_one("SELECT * FROM task WHERE task_id = ?", (task_id,))
    assert row is not None
    return dict(row)


def test_ut08_66_backend_is_task_mixin() -> None:
    """UT08-66 SqliteJobsBackend carries TaskSqlMixin; the ops package exports it (U02-62)."""
    assert issubclass(SqliteJobsBackend, TaskSqlMixin)
    assert ops.TaskSqlMixin is TaskSqlMixin
    assert "TaskSqlMixin" in ops.__all__


def test_ut08_66_recover_counts_and_timestamps(backend: SqliteJobsBackend, run_id: str) -> None:
    """UT08-66 recover_tasks returns (running, failed, dead) counts and stamps updated_at."""
    running = _task(run_id, "running", 2)
    failed = _task(run_id, "failed", 1)
    dead = _task(run_id, "dead", 4)
    assert backend.recover_tasks(run_id, max_task_attempts=3, retry_dead=False, now=T1) == (1, 1, 0)
    assert backend.recover_tasks(run_id, max_task_attempts=3, retry_dead=True, now=T1) == (0, 0, 1)
    for task, attempts in ((running, 2), (failed, 1), (dead, 0)):
        row = _row(task)
        assert (row["status"], row["attempts"], row["updated_at"]) == (
            "pending",
            attempts,
            clock.format_utc(T1),
        )


def test_ut08_66_recover_uses_run_status_index(backend: SqliteJobsBackend) -> None:
    """UT08-66 the recovery updates are bounded by the task(run_id, status) index."""
    del backend
    conn = connection()
    params = {"run_id": "r", "now": "t", "max_task_attempts": 3}
    for sql in (tasks_area._RECOVER_RUNNING, tasks_area._RECOVER_FAILED, tasks_area._RECOVER_DEAD):
        plan = " ".join(str(r[3]) for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params))
        assert "task_run_status" in plan


def test_ut08_67_backend_claim(backend: SqliteJobsBackend, run_id: str) -> None:
    """UT08-67 claim_task: True once for a pending task, then False."""
    task = _task(run_id, "pending", 0)
    assert backend.claim_task(task, T1) is True
    assert backend.claim_task(task, T1) is False
    row = _row(task)
    assert (row["status"], row["attempts"], row["updated_at"]) == (
        "running",
        1,
        clock.format_utc(T1),
    )


def test_ut08_109_backend_save_returns_loop_dropped(
    backend: SqliteJobsBackend, run_id: str
) -> None:
    """UT08-109 save_checkpoint returns loop_dropped and keeps the other keys."""
    task = _task(run_id, checkpoint='{"schema_version":1,"state":{"p":1}}')
    assert backend.save_checkpoint(task, "loop", {"t": 1}, None) is False
    big = {"blob": "x" * (CHECKPOINT_MAX_BYTES + 1)}
    assert backend.save_checkpoint(task, "loop", big, None) is True
    stored = json.loads(str(_row(task)["checkpoint"]))
    assert stored == {"schema_version": 1, "state": {"p": 1}}


@pytest.mark.parametrize("stored", ["[1, 2]", '"text"'])
def test_ut08_68_stored_envelope_not_object(
    backend: SqliteJobsBackend, run_id: str, stored: str
) -> None:
    """UT08-68 a stored checkpoint that is not a JSON object → SchemaViolation, unchanged."""
    task = _task(run_id, checkpoint=stored)
    with pytest.raises(SchemaViolation, match="checkpoint envelope invalid"):
        backend.save_checkpoint(task, "state", {"a": 1}, None)
    assert _row(task)["checkpoint"] == stored


def test_ut08_69_backend_guards(backend: SqliteJobsBackend, run_id: str) -> None:
    """UT08-69 not-running tasks raise JobStateError from every guarded backend write."""
    done = _task(run_id, "done")
    with pytest.raises(JobStateError):
        backend.save_checkpoint(done, "loop", {}, None)
    with pytest.raises(JobStateError):
        backend.complete_task(done, {}, writes=None, now=T1)
    with pytest.raises(JobStateError):
        backend.fail_task(done, retryable=True, max_task_attempts=3, last_error={}, now=T1)
    assert _row(done)["updated_at"] == clock.format_utc(T0)


def test_ut08_69_update_guard_rolls_back(backend: SqliteJobsBackend, run_id: str) -> None:
    """UT08-69 writes that move the task out of running make the guarded UPDATE fail → rollback."""
    task = _task(run_id)

    def writes(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE task SET status = 'dead' WHERE task_id = ?", (task,))

    with pytest.raises(JobStateError):
        backend.save_checkpoint(task, "state", {"a": 1}, writes)
    with pytest.raises(JobStateError):
        backend.complete_task(task, {}, writes=writes, now=T1)
    row = _row(task)
    assert (row["status"], row["checkpoint"], row["result"]) == ("running", None, None)


def test_ut08_70_backend_fail(backend: SqliteJobsBackend, run_id: str) -> None:
    """UT08-70 fail_task: retryable below the cap → pending; `attempt` added to last_error."""
    below = _task(run_id, "running", 2)
    at_cap = _task(run_id, "running", 3)
    error = {"class": "ModelUnavailable", "message": "m", "at": clock.format_utc(T1)}
    assert (
        backend.fail_task(below, retryable=True, max_task_attempts=3, last_error=error, now=T1)
        == "pending"
    )
    assert (
        backend.fail_task(at_cap, retryable=True, max_task_attempts=3, last_error=error, now=T1)
        == "dead"
    )
    assert json.loads(str(_row(below)["last_error"])) == {**error, "attempt": 2}
    assert json.loads(str(_row(at_cap)["last_error"])) == {**error, "attempt": 3}
    assert (_row(below)["attempts"], _row(at_cap)["attempts"]) == (2, 3)


def test_ut08_71_backend_release(backend: SqliteJobsBackend, run_id: str) -> None:
    """UT08-71 release_task: True for a running task (attempts - 1), False otherwise."""
    task = _task(run_id, "running", 2)
    assert backend.release_task(task, T1) is True
    assert backend.release_task(task, T1) is False
    row = _row(task)
    assert (row["status"], row["attempts"], row["updated_at"]) == (
        "pending",
        1,
        clock.format_utc(T1),
    )
