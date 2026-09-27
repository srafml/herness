"""Shared setup for the Blackboard tests (impl 06 T06-08: UT06-35 … UT06-42, IT06-16, ST06-*).

`bb_env` gives a migrated ops store with the SQLite jobs backend bound, one `running` review
run, a claimed analyst task, one ops `evidence` row of the run's build, a fake warehouse
holding `core.team` and one `meta.evidence` id, a test redactor with one planted name, and a
`Blackboard` over all of it. Helpers build valid `post_finding` arguments, challenges and
verification records.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from tests.support.harness_fakes import FakeLedger, FakeOps, FakeVectors, RecordingTracer
from tests.support.ops_store import OpsStoreHandle

from herness.core import redact as r
from herness.core import time as clock
from herness.core.ids import IdKind, new_id
from herness.core.ids import query_id as compute_query_id
from herness.core.jobs.ports import bind_jobs_backend
from herness.core.jobs.tasks import claim_task
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.core.types import (
    SKEPTIC_CHECKS,
    Budgets,
    Challenge,
    EntityScope,
    Evidence,
    SqlLimits,
    TaskSpec,
    ToolContext,
    VerificationRecord,
)
from herness.harness.blackboard import Blackboard
from herness.harness.findings import EntityCatalog, compute_dedup_key
from herness.store.ops import get_task, insert_tasks, record_evidence, run_write
from herness.store.ops.jobs import SqliteJobsBackend

BUILD_ID = "20260925-101500-ABCDEF"
PLANTED_NAME = "Priya Raman"
T0 = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
_OPS_SQL = "SELECT count(*) AS incidents FROM core.incident WHERE team_id = $team"
META_QID = "q_" + "c" * 16
FAKE_QID = "q_" + "f" * 16
_SCOPE = EntityScope(entity_type="team", entity_ids=["t1"])


class FakeWarehouse:
    """`WarehouseHandle` over an in-memory DuckDB with `core.team` and `meta.evidence`."""

    def __init__(self, build_id: str = BUILD_ID) -> None:
        self._con = duckdb.connect(":memory:")
        for schema in ("core", "meta"):
            self._con.execute(f"CREATE SCHEMA {schema}")
        self._con.execute("CREATE TABLE core.team (team_id VARCHAR, name VARCHAR)")
        self._con.execute("INSERT INTO core.team VALUES ('t1', 'Team One'), ('t2', 'Team Two')")
        self._con.execute("CREATE TABLE meta.evidence (query_id VARCHAR)")
        self._con.execute("INSERT INTO meta.evidence VALUES (?)", [META_QID])
        self._build_id = build_id

    @property
    def build_id(self) -> str:
        return self._build_id

    @property
    def path(self) -> Path:
        return Path(f"wh-{self._build_id}.duckdb")

    def cursor(self) -> object:
        return self._con.cursor()

    def schema(self) -> dict[str, dict[str, dict[str, str]]]:
        return {}

    def table_comment(self, qualified: str) -> str:
        del qualified
        return ""


@dataclass
class BbEnv:
    """Everything one Blackboard test needs."""

    run_id: str
    task_id: str
    ops_qid: str
    warehouse: FakeWarehouse
    bb: Blackboard
    extra: list[Blackboard] = field(default_factory=list)

    def ctx(self, *, task_id: str | None = None, role: str = "analyst") -> ToolContext:
        tid = self.task_id if task_id is None else task_id
        return ToolContext(
            run_id=self.run_id,
            task_id=tid,
            build_id=BUILD_ID,
            role=role,
            specialty="general",
            depth="standard",
            profile="local",
            tool_names=["post_finding"],
            warehouse=self.warehouse,
            ops=FakeOps(),
            vectors=FakeVectors(),
            budgets=Budgets(
                max_steps=10, max_tokens=10_000, max_cost_usd=Decimal(1), wall_clock_s=600
            ),
            ledger=FakeLedger(),
            sql_limits=SqlLimits(timeout_s=30.0),
            tracer=RecordingTracer(self.run_id, tid),
        )

    def blackboard(self) -> Blackboard:
        """A second Blackboard over the same run with its own writer (closed at teardown)."""
        bb = Blackboard(
            self.run_id,
            build_id=BUILD_ID,
            catalog=EntityCatalog(self.warehouse),
            allowed_numerals=(),
        )
        self.extra.append(bb)
        return bb


def args(qid: str, **overrides: object) -> dict[str, object]:
    """Valid `post_finding` arguments citing query `qid`."""
    values: dict[str, object] = {
        "claim": "Team t1 had [[n1]] incidents",
        "entity_type": "team",
        "entity_id": "t1",
        "numbers": [
            {
                "id": "n1",
                "value": 7,
                "unit": "count",
                "query_id": qid,
                "column": "incidents",
                "row_key": None,
            }
        ],
        "query_ids": [],
        "confidence": 0.8,
    }
    values.update(overrides)
    return values


def task_spec(run_id: str, *, n: int = 0, revision_of: str | None = None) -> TaskSpec:
    """An analyst task spec (a revision task when `revision_of` is given)."""
    values: dict[str, Any] = {
        "task_id": new_id(IdKind.TASK),
        "run_id": run_id,
        "role": "analyst",
        "objective": f"objective {n}",
        "scope": {"entity_type": "team", "entity_ids": ["t1"]},
        "tools": ["post_finding"],
        "budget": {"max_steps": 5, "max_tokens": 5_000, "wall_clock_s": 60},
        "model_role": "analyst",
        "dedup_key": compute_dedup_key(
            "analyst", "general", _SCOPE, f"objective {n} {revision_of}"
        ),
    }
    if revision_of is not None:
        values.update(revision_of=revision_of, round=1)
    return TaskSpec.model_validate(values)


def add_running_task(run_id: str, *, n: int, revision_of: str | None = None) -> str:
    """Insert and claim one analyst task; return its id."""
    spec = task_spec(run_id, n=n, revision_of=revision_of)
    run_write(lambda conn: insert_tasks(conn, [spec], now=clock.now()), op="test_task")
    assert claim_task(spec.task_id)
    return spec.task_id


def challenge(finding_id: str, verdict: str = "uphold") -> Challenge:
    return Challenge.model_validate(
        {
            "finding_id": finding_id,
            "round": 1,
            "checks": [{"check": c, "result": "pass", "note": ""} for c in SKEPTIC_CHECKS],
            "verdict": verdict,
            "required_actions": ["recheck"] if verdict == "revise" else [],
        }
    )


def verification() -> VerificationRecord:
    result = {
        "build_id": BUILD_ID,
        "passed": True,
        "items": [],
        "n_numbers": 0,
        "n_failed": 0,
        "verified_at": T0,
        "duration_ms": 3,
    }
    return VerificationRecord.model_validate({"gate": 1, "result": result})


def checkpoint_of(task_id: str) -> dict[str, object] | None:
    row = get_task(task_id)
    assert row is not None
    return row.checkpoint


@pytest.fixture
def test_redactor(monkeypatch: pytest.MonkeyPatch) -> r.Redactor:
    """A process redactor with a fixed key and one planted directory name (no config)."""
    directory = NameDirectory.from_files(None, (PLANTED_NAME,), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    return redactor


@pytest.fixture
def bb_env(ops_store: OpsStoreHandle, test_redactor: r.Redactor) -> Iterator[BbEnv]:
    del ops_store, test_redactor
    bind_jobs_backend(SqliteJobsBackend())
    run_id = new_id(IdKind.RUN)

    def insert_run(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO run (run_id, kind, depth, profile, config_hash, build_id, status,"
            " started_at) VALUES (?, 'org_review', 'standard', 'default', 'h', ?, 'running', ?)",
            (run_id, BUILD_ID, clock.format_utc(T0)),
        )

    run_write(insert_run, op="test_run")
    params: dict[str, object] = {"team": "t1"}
    qid = compute_query_id(_OPS_SQL, params, BUILD_ID)
    ev = Evidence(
        query_id=qid,
        run_id=run_id,
        build_id=BUILD_ID,
        sql=_OPS_SQL,
        params={"team": "t1"},
        result_hash="0" * 64,
        row_count=1,
        result_sample=[{"incidents": 7}],
        executed_at=T0,
        duration_ms=1,
    )
    assert record_evidence(ev)
    warehouse = FakeWarehouse()
    bb = Blackboard(
        run_id, build_id=BUILD_ID, catalog=EntityCatalog(warehouse), allowed_numerals=()
    )
    env = BbEnv(run_id, add_running_task(run_id, n=0), qid, warehouse, bb)
    try:
        yield env
    finally:
        for other in env.extra:
            other.close()
        bb.close()
