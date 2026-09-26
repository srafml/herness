"""Worker ops-store area (impl 08 U08-96, R-08): SQL for `worker` and the `run` read of
`enqueue_resume`.

`WorkerSqlMixin` is a mixin of `herness.store.ops.jobs.SqliteJobsBackend` (U08-95). The table
is impl 02 migration 002 (§4.3.2); `current_jobs` is JSON TEXT and every timestamp is ts text.
Reads use `read_one` / `read_all`; every write runs in `run_write` (`BEGIN IMMEDIATE`, R-10).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Final

from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.jobs.ports import WorkerRow
from herness.core.types import GpuClass

from .core import dump_json, read_all, read_one, run_write

# U08-96: the only columns `update_worker` may set; names are never taken from input.
WORKER_UPDATE_COLUMNS: Final = frozenset(
    (
        "status",
        "gpu_class_loaded",
        "requested_class",
        "current_jobs",
        "heartbeat_at",
        "faults_enabled",
    )
)

_UPSERT: Final = (
    "INSERT INTO worker (worker_id, host, pid, gpu_slot, cpu_slots, gpu_class_loaded,"
    " requested_class, status, current_jobs, started_at, heartbeat_at, version, faults_enabled)"
    " VALUES (:worker_id, :host, :pid, :gpu_slot, :cpu_slots, :gpu_class_loaded,"
    " :requested_class, :status, :current_jobs, :started_at, :heartbeat_at, :version,"
    " :faults_enabled)"
    " ON CONFLICT(worker_id) DO UPDATE SET host = excluded.host, pid = excluded.pid,"
    " gpu_slot = excluded.gpu_slot, cpu_slots = excluded.cpu_slots,"
    " gpu_class_loaded = excluded.gpu_class_loaded, requested_class = excluded.requested_class,"
    " status = excluded.status, current_jobs = excluded.current_jobs,"
    " started_at = excluded.started_at, heartbeat_at = excluded.heartbeat_at,"
    " version = excluded.version, faults_enabled = excluded.faults_enabled"
)


def _column_value(column: str, value: object) -> object:
    """Bind form of one `update_worker` value: ts text, JSON TEXT or 0/1."""
    if column == "current_jobs":
        return dump_json(value, field="worker.current_jobs")
    if column == "faults_enabled":
        return int(bool(value))
    if isinstance(value, datetime):
        return clock.format_utc(value)
    return value


def _worker_params(row: WorkerRow) -> Mapping[str, object]:
    values = row.model_dump()
    values["current_jobs"] = dump_json(row.current_jobs, field="worker.current_jobs")
    values["started_at"] = clock.format_utc(row.started_at)
    values["heartbeat_at"] = clock.format_utc(row.heartbeat_at)
    values["faults_enabled"] = int(row.faults_enabled)
    return values


class WorkerSqlMixin:
    """`worker` rows and the `run` read of the jobs port (U08-96); stateless, thread-safe."""

    def upsert_worker(self, row: WorkerRow) -> None:
        """Insert the worker row or replace every column of the existing one."""
        params = _worker_params(row)

        def upsert(conn: sqlite3.Connection) -> None:
            conn.execute(_UPSERT, params)

        run_write(upsert, op="worker_upsert")

    def update_worker(self, worker_id: str, **fields: object) -> None:
        """Set the named columns of one worker row; no fields → nothing is written.

        Raises ConfigError for a column outside `WORKER_UPDATE_COLUMNS` (TH: no SQL from
        input); a value the table rejects raises SchemaViolation from `run_write`.
        """
        unknown = sorted(set(fields) - WORKER_UPDATE_COLUMNS)
        if unknown:
            msg = f"update_worker: column not allowed: {', '.join(unknown)}"
            raise ConfigError(msg)
        if not fields:
            return
        columns = sorted(fields)
        assignments = ", ".join(f"{column} = :{column}" for column in columns)
        sql = f"UPDATE worker SET {assignments} WHERE worker_id = :worker_id"  # noqa: S608 - allowlisted names
        params = {column: _column_value(column, fields[column]) for column in columns}
        params["worker_id"] = worker_id

        def update(conn: sqlite3.Connection) -> None:
            conn.execute(sql, params)

        run_write(update, op="worker_update")

    def list_workers(self) -> list[WorkerRow]:
        """Every worker row, ordered by `worker_id`."""
        rows = read_all("SELECT * FROM worker ORDER BY worker_id", max_rows=10_000)
        return [WorkerRow.model_validate(dict(row)) for row in rows]

    def set_requested_class(self, worker_id: str, cls: GpuClass | None) -> None:
        """Set (or clear with None) the class requested of one worker (U08-102)."""

        def update(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE worker SET requested_class = ? WHERE worker_id = ?", (cls, worker_id)
            )

        run_write(update, op="worker_requested_class")

    def run_row(self, run_id: str) -> tuple[str, str] | None:
        """`(kind, status)` of the `run` row, or None (U08-93)."""
        row = read_one("SELECT kind, status FROM run WHERE run_id = ?", (run_id,))
        return None if row is None else (str(row["kind"]), str(row["status"]))
