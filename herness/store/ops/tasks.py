"""Tasks ops-store area (impl 08 U08-97, R-08): the SQL of the task lease and checkpoint helpers.

`TaskSqlMixin` is a mixin of `herness.store.ops.jobs.SqliteJobsBackend` (U08-95). The table is
impl 02 migration 003 (`task`, index `task(run_id, status)`). Only this mixin writes
`task.status`, `task.attempts` and `task.checkpoint` (design 08 §3.7). Every method is one
`run_write` (`BEGIN IMMEDIATE`, R-10); reads inside it are single rows by `task_id` and the
recovery updates use the `(run_id, status)` index, so no statement is unbounded.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Final, Literal

from herness.core import time as clock
from herness.core.errors import JobStateError, SchemaViolation
from herness.core.ids import canonical_json
from herness.core.jobs.ports import CheckpointKey, JsonMap, SqlWrites
from herness.core.jobs.tasks import build_checkpoint_envelope

from .core import load_json, run_write

_RECOVER_RUNNING: Final = (
    "UPDATE task SET status = 'pending', updated_at = :now"
    " WHERE run_id = :run_id AND status = 'running'"
)
_RECOVER_FAILED: Final = (
    "UPDATE task SET status = 'pending', updated_at = :now"
    " WHERE run_id = :run_id AND status = 'failed' AND attempts < :max_task_attempts"
)
_RECOVER_DEAD: Final = (
    "UPDATE task SET status = 'pending', attempts = 0, updated_at = :now"
    " WHERE run_id = :run_id AND status = 'dead'"
)
_CLAIM: Final = (
    "UPDATE task SET status = 'running', attempts = attempts + 1, updated_at = :now"
    " WHERE task_id = :id AND status = 'pending'"
)
_READ_CHECKPOINT: Final = "SELECT checkpoint FROM task WHERE task_id = :id AND status = 'running'"
_READ_ATTEMPTS: Final = "SELECT attempts FROM task WHERE task_id = :id AND status = 'running'"
_CHECKPOINT: Final = (
    "UPDATE task SET checkpoint = :env, updated_at = :now"
    " WHERE task_id = :id AND status = 'running'"
)
_COMPLETE: Final = (
    "UPDATE task SET status = 'done', result = :result, updated_at = :now"
    " WHERE task_id = :id AND status = 'running'"
)
_FAIL: Final = (
    "UPDATE task SET status = :status, last_error = :last_error, updated_at = :now"
    " WHERE task_id = :id AND status = 'running'"
)
_RELEASE: Final = (
    "UPDATE task SET status = 'pending', attempts = MAX(attempts - 1, 0), updated_at = :now"
    " WHERE task_id = :id AND status = 'running'"
)


def _not_running(task_id: str) -> JobStateError:
    return JobStateError("task not running", task_id=task_id)


def _running_row(conn: sqlite3.Connection, sql: str, task_id: str) -> sqlite3.Row:
    """The single running row of `task_id` read by `sql`, else JobStateError."""
    row: sqlite3.Row | None = conn.execute(sql, {"id": task_id}).fetchone()
    if row is None:
        raise _not_running(task_id)
    return row


def _update_running(conn: sqlite3.Connection, sql: str, params: Mapping[str, object]) -> None:
    """Run one `status = 'running'`-guarded UPDATE; 0 rows → JobStateError (rolls back)."""
    if conn.execute(sql, params).rowcount != 1:
        raise _not_running(str(params["id"]))


def _stored_envelope(text: str | None) -> Mapping[str, object] | None:
    existing = load_json(text, field="task.checkpoint")
    if existing is None or isinstance(existing, dict):
        return existing
    msg = "checkpoint envelope invalid"
    raise SchemaViolation(msg)


class TaskSqlMixin:
    """`task` lease, recovery and checkpoint SQL of the jobs port (U08-97); stateless."""

    def recover_tasks(
        self, run_id: str, *, max_task_attempts: int, retry_dead: bool, now: datetime
    ) -> tuple[int, int, int]:
        """Design 08 §5.12 recovery: `(running_reset, failed_reset, dead_reset)` row counts."""
        params = {"run_id": run_id, "max_task_attempts": max_task_attempts}
        params["now"] = clock.format_utc(now)

        def recover(conn: sqlite3.Connection) -> tuple[int, int, int]:
            running = conn.execute(_RECOVER_RUNNING, params).rowcount
            failed = conn.execute(_RECOVER_FAILED, params).rowcount
            dead = conn.execute(_RECOVER_DEAD, params).rowcount if retry_dead else 0
            return running, failed, dead

        return run_write(recover, op="task_recover")

    def claim_task(self, task_id: str, now: datetime) -> bool:
        """`pending` → `running` with `attempts + 1`; True when this call claimed it."""
        params = {"id": task_id, "now": clock.format_utc(now)}

        def claim(conn: sqlite3.Connection) -> bool:
            return conn.execute(_CLAIM, params).rowcount == 1

        return run_write(claim, op="task_claim")

    def save_checkpoint(
        self,
        task_id: str,
        key: CheckpointKey,
        value: Mapping[str, object],
        writes: SqlWrites | None,
    ) -> bool:
        """Replace envelope `key` and run `writes` in one transaction; return loop_dropped."""

        def save(conn: sqlite3.Connection) -> bool:
            row = _running_row(conn, _READ_CHECKPOINT, task_id)
            env, dropped = build_checkpoint_envelope(
                key, value, _stored_envelope(row["checkpoint"])
            )
            if writes is not None:
                writes(conn)
            params = {"id": task_id, "env": env.decode("utf-8"), "now": _now()}
            _update_running(conn, _CHECKPOINT, params)
            return dropped

        return run_write(save, op="task_checkpoint")

    def complete_task(
        self, task_id: str, result: Mapping[str, object], *, writes: SqlWrites | None, now: datetime
    ) -> None:
        """`running` → `done` with the canonical `result`, committing `writes` with it."""
        params = {"id": task_id, "result": canonical_json(result), "now": clock.format_utc(now)}

        def complete(conn: sqlite3.Connection) -> None:
            if writes is not None:
                writes(conn)
            _update_running(conn, _COMPLETE, params)

        run_write(complete, op="task_complete")

    def fail_task(
        self,
        task_id: str,
        *,
        retryable: bool,
        max_task_attempts: int,
        last_error: JsonMap,
        now: datetime,
    ) -> Literal["pending", "dead"]:
        """`running` → `pending` (retryable, attempts below the cap) or `dead`, with last_error.

        `last_error` gains `attempt` (the row's attempts), completing the U08-50 shape.
        """

        def fail(conn: sqlite3.Connection) -> Literal["pending", "dead"]:
            attempts = int(_running_row(conn, _READ_ATTEMPTS, task_id)["attempts"])
            status: Literal["pending", "dead"] = (
                "pending" if retryable and attempts < max_task_attempts else "dead"
            )
            error = canonical_json({**last_error, "attempt": attempts})
            params = {"id": task_id, "status": status, "last_error": error}
            params["now"] = clock.format_utc(now)
            _update_running(conn, _FAIL, params)
            return status

        return run_write(fail, op="task_fail")

    def release_task(self, task_id: str, now: datetime) -> bool:
        """`running` → `pending` with `attempts - 1` (floor 0); False when not running."""
        params = {"id": task_id, "now": clock.format_utc(now)}

        def release(conn: sqlite3.Connection) -> bool:
            return conn.execute(_RELEASE, params).rowcount == 1

        return run_write(release, op="task_release")


def _now() -> str:
    return clock.format_utc(clock.now())
