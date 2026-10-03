"""Shared helpers for the procedural memory tests (T07-19): deps, guard, seeds, row reads.

Not a test module. `make_deps` builds `ProceduralDeps` on the real collaborators of
`_write_env.make_writer` (MemoryWriter, Redactor, LanceDB VectorIndex in tmp) and a real
`SqlGuard` over a two-table schema. Seeds write `run`, `task`, `evidence` and `finding`
rows with raw SQL (those tables belong to impl 05/06).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.unit.harness.memory._write_env import NOW, PATTERNS, Env, make_writer, memory_rows

from herness.core import time as clock
from herness.core.ids import new_ulid
from herness.harness.memory.policy import InjectionScanner
from herness.harness.memory.procedural import ProceduralDeps
from herness.harness.memory.settings import MemoryConfig, ProceduralConfig, PromoteConfig
from herness.harness.sql_guard import SqlGuard
from herness.store.ops import core

BUILD = "20260901-120000-ABCDEF"
SCHEMA = {
    "metrics": {"daily_incidents": {"service_id": "VARCHAR", "day": "DATE", "incidents": "BIGINT"}},
    "core": {"service": {"service_id": "VARCHAR", "team_id": "VARCHAR", "name": "VARCHAR"}},
}
OBJECTIVE = "How many incidents did the checkout service have last month?"
LOW = ProceduralConfig(
    promote=PromoteConfig(min_passes=1, min_runs=1, min_pass_lb=0.1), demote_pass_lb=0.05
)


def incidents_sql(start: str = "2026-01-01", end: str = "2026-02-01", svc: str = "svc_a") -> str:
    """A verified-looking warehouse query; only the literal values vary."""
    return (  # test data: the promoted SQL is built from literals on purpose
        "SELECT service_id, SUM(incidents) AS n FROM metrics.daily_incidents"  # noqa: S608
        f" WHERE day >= '{start}' AND day < '{end}' AND service_id = '{svc}'"
        " GROUP BY service_id"
    )


@dataclass
class ProcEnv:
    """Deps plus the writer env behind them."""

    deps: ProceduralDeps
    env: Env


def guard() -> SqlGuard:
    """The real SQL guard over SCHEMA."""
    return SqlGuard(SCHEMA, blocked_columns=())


def make_deps(tmp_path: Path, cfg: ProceduralConfig | None = None) -> ProcEnv:
    """ProceduralDeps on the migrated `ops_store` of the test."""
    env = make_writer(tmp_path)
    deps = ProceduralDeps(
        conn_factory=core.connection,
        writer=env.writer,
        vectors=env.vectors,
        redactor=env.redactor,
        config=cfg or ProceduralConfig(),
        guard=guard(),
        scanner=InjectionScanner(MemoryConfig(injection_patterns=PATTERNS).injection_patterns),
    )
    return ProcEnv(deps, env)


def _at() -> str:
    return clock.format_utc(NOW)


def _write(sql: str, params: tuple[object, ...]) -> None:
    core.run_write(lambda c: c.execute(sql, params), op="test_seed")


def seed_run(build_id: str | None = BUILD) -> str:
    """A done `run` row."""
    run_id = "run_" + new_ulid()
    _write(
        "INSERT INTO run (run_id, kind, depth, profile, config_hash, build_id, status,"
        " started_at) VALUES (?, 'org_review', 'fast', 'p', 'h', ?, 'done', ?)",
        (run_id, build_id, _at()),
    )
    return run_id


def seed_task(run_id: str, objective: str | None = OBJECTIVE) -> str:
    """A `task` row whose spec carries `objective` (omitted when None)."""
    task_id = "task_" + new_ulid()
    spec = "{}" if objective is None else core.dump_json({"objective": objective}, field="spec")
    _write(
        "INSERT INTO task (task_id, run_id, role, spec, status, created_at, updated_at)"
        " VALUES (?, ?, 'analyst', ?, 'done', ?, ?)",
        (task_id, run_id, spec, _at(), _at()),
    )
    return task_id


def seed_query(sql: str, build_id: str = BUILD) -> str:
    """An `evidence` row holding `sql`."""
    query_id = "q_" + secrets.token_hex(8)
    _write(
        "INSERT INTO evidence (query_id, build_id, sql, result_hash, params, row_count,"
        " duration_ms, executed_at) VALUES (?, ?, ?, 'h', '{}', 1, 1, ?)",
        (query_id, build_id, sql, _at()),
    )
    return query_id


def seed_finding(run_id: str, task_id: str, query_ids: list[str], *, passed: bool) -> str:
    """A verified finding (passed) or a rejected one whose verification failed."""
    finding_id = "fnd_" + new_ulid()
    status = "verified" if passed else "rejected"
    verification = core.dump_json({"passed": passed}, field="verification")
    _write(
        "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, query_ids,"
        " status, verification, created_at) VALUES (?, ?, ?, 'analyst', 'c', ?, ?, ?, ?)",
        (finding_id, run_id, task_id, core.dump_json(query_ids, field="q"), status,
         verification, _at()),
    )  # fmt: skip
    return finding_id


def run_with(
    queries: list[tuple[str, bool]], objective: str | None = OBJECTIVE, *, run_id: str = ""
) -> str:
    """A run with one task and one finding per (sql, passed) query."""
    run_id = run_id or seed_run()
    task_id = seed_task(run_id, objective)
    for sql, passed in queries:
        seed_finding(run_id, task_id, [seed_query(sql)], passed=passed)
    return run_id


def rows(kind: str) -> list[dict[str, Any]]:
    """Every memory_item row of `kind`, oldest first."""
    return [r for r in memory_rows() if r["kind"] == kind]
