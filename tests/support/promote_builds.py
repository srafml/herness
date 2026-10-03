"""Synthetic warehouse builds and ops rows for the promotion and cleanup tests (impl 02 T02-21).

A synthetic build file holds only the ``meta.build`` columns that ``list_builds`` reads and
``update_build_row`` writes, so retention rules can be checked without running the SQL
build. Runs and ``build_pipeline`` jobs are written straight into a migrated ops store.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from decimal import Decimal
from typing import Final

import duckdb
from pydantic import JsonValue

from herness.core import time as clock
from herness.core.ids import IdKind, new_id
from herness.store import ops, warehouse
from herness.store.layout import DataLayout

CFG_HASH: Final = "cfg_" + "0" * 16
_JOB_SQL: Final = (
    "INSERT INTO job (job_id, kind, gpu_class, status, priority, payload, result,"
    " max_attempts, scheduled_for, created_at)"
    " VALUES (?, 'build_pipeline', 'none', ?, 50, ?, ?, 3, ?, ?)"
)


def build_id(day: int, suffix: str = "AAAAAA") -> str:
    """A valid build ID on 2026-09-``day`` (higher day = newer build)."""
    return f"202609{day:02d}-000000-{suffix}"


def make_build(layout: DataLayout, build: str, status: str, *, finished: bool = True) -> None:
    """Create a minimal build file whose one ``meta.build`` row has ``status``."""
    path = warehouse.build_path(build, layout=layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE SCHEMA meta")
        con.execute(
            "CREATE TABLE meta.build (build_id VARCHAR, started_at TIMESTAMPTZ,"
            " finished_at TIMESTAMPTZ, status VARCHAR)"
        )
        con.execute(
            "INSERT INTO meta.build VALUES (?, now(), CASE WHEN ? THEN now() END, ?)",
            [build, finished, status],
        )
    finally:
        con.close()


def statuses(layout: DataLayout) -> dict[str, str]:
    """``build_id -> status`` of every build file."""
    return {b.build_id: b.status for b in warehouse.list_builds(layout=layout)}


def add_run(status: str, build: str | None) -> str:
    """Insert a ``run`` row in ``status`` on ``build``; its run ID."""
    run_id = new_id(IdKind.RUN)
    row = ops.RunRow(
        run_id=run_id,
        kind="funding_review",
        depth="standard",
        profile="local",
        build_id=build,
        status=status,
        started_at=clock.now() - datetime.timedelta(hours=1),
        finished_at=None,
        token_usage={},
        cost_usd=Decimal(0),
        config_hash=CFG_HASH,
        meta={},
    )
    assert ops.run_write(lambda conn: ops.insert_run(conn, row), op="test_run")
    return run_id


def add_job(
    status: str,
    payload: dict[str, JsonValue],
    state: dict[str, JsonValue] | None = None,
) -> str:
    """Insert a ``build_pipeline`` job in ``status`` with an optional saved state."""
    job_id = new_id(IdKind.JOB)
    now = clock.format_utc(clock.now())
    result = None if state is None else json.dumps({"state": state})
    params = (job_id, status, json.dumps(payload), result, now, now)

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(_JOB_SQL, params)

    ops.run_write(insert, op="test_job")
    return job_id
