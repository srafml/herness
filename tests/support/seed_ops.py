"""T6 prior-run seeding in the ops store and memory (impl 11 U11-49; design 11 §5.1.5 T6).

`seed_prior_run` plants the prior `funding_review` run of the T6 outcome plant: one `done`
run 30 days before `effective_at`, per side (paid, unpaid) one recorded incident-count query
on the build's warehouse and one verified finding citing it, then the two `fund`
recommendations through `MemoryStore.write_recommendations` and their `accepted` decisions
through `MemoryStore.decide` (the only review-decision path, R-33). Nothing is written to the
lake. The ops store is reached through `herness.store.ops` functions (R-10).

Spec notes (T11-33, recorded in docs/impl/11 under U11-49):
* `record_evidence` opens its own `run_write` (op `record_evidence`), so step 2 calls it
  directly; a nested `run_write` raises `ConfigError`. Steps 1 and 3 run in
  `run_write(fn, op="seed_prior_run")`, one call per step.
* `RecommendationDraft.target_type` has no `epic` (closed literal, R-30): the drafts target
  `work_item` (the epic's `core.work_item` record id), which the outcome measurement maps
  to the owning service (U07-83 step 4).
* `MemoryStore.decide` requires a 32-hex `user_ref` (U07-82): the `"eval"` decider is passed
  as `SEED_USER_REF`, the first 32 hex of sha256(`eval`).

A plain module (no fixture, no import side effects); every call plants a new run, so a test
that seeds owns a fresh ops store and memory.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from herness.core import time as clock
from herness.core.config import config_hash, get_config
from herness.core.ids import IdKind, new_id, new_ulid
from herness.core.ids import query_id as compute_query_id
from herness.core.types import Evidence, Finding, NumberRef, RecommendationDraft
from herness.metrics.evidence import result_hash
from herness.store.ops import record_evidence, run_write
from herness.store.ops.findings import insert_finding
from herness.store.ops.runs import RunRow, insert_run

if TYPE_CHECKING:
    import sqlite3

    import duckdb

    from herness.eval.truth import T6Side, TruthManifest
    from herness.harness.memory import MemoryStore

__all__ = ["INCIDENT_SQL", "SEED_USER_REF", "SIDES", "SeededPriorRun", "seed_prior_run"]

INCIDENT_SQL: Final = (
    "SELECT count(*) AS incident_count FROM core.incident"
    " WHERE service_id = $service_id AND opened_at < $effective_at"
)
SIDES: Final = ("paid", "unpaid")
SEED_USER_REF: Final = hashlib.sha256(b"eval").hexdigest()[:32]
PRIOR_RUN_AGE: Final = timedelta(days=30)
DECISION_REASON: Final = "seeded T6 outcome"
_OP: Final = "seed_prior_run"


@dataclass(frozen=True)
class SeededPriorRun:
    """The planted prior run and, per side (`paid`, `unpaid`), its rec and finding ids."""

    run_id: str
    rec_ids: dict[str, str]
    finding_ids: dict[str, str]


def _insert_run(run_id: str, build_id: str, at: datetime) -> None:
    """Step 1: the `done` prior funding run (`insert_run`, R-08)."""
    row = RunRow(
        run_id=run_id, kind="funding_review", depth="standard", profile="synth",
        build_id=build_id, status="done", started_at=at, finished_at=at, token_usage={},
        cost_usd=Decimal(0), config_hash=config_hash(get_config()), meta={"seed": _OP},
    )  # fmt: skip
    run_write(lambda conn: insert_run(conn, row), op=_OP)


def _record_count(
    side: T6Side, effective_at: datetime, *, run_id: str, warehouse: duckdb.DuckDBPyConnection,
    build_id: str,
) -> tuple[Evidence, int]:  # fmt: skip
    """Step 2: the side's incident count before `effective_at`, recorded as evidence."""
    params: dict[str, str] = {
        "service_id": side.service_id, "effective_at": clock.format_utc(effective_at),
    }  # fmt: skip
    started = time.perf_counter()
    cur = warehouse.cursor()
    try:
        cur.execute(INCIDENT_SQL, params)
        types = [(str(d[0]), str(d[1])) for d in cur.description or ()]
        rows = cur.fetchall()
    finally:
        cur.close()
    count = int(rows[0][0])
    ev = Evidence(
        query_id=compute_query_id(INCIDENT_SQL, params, build_id), run_id=run_id,
        build_id=build_id, sql=INCIDENT_SQL, params=dict(params),
        result_hash=result_hash(types, rows), row_count=len(rows),
        result_sample=[{"incident_count": count}], executed_at=clock.now(),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )  # fmt: skip
    record_evidence(ev)
    return ev, count


def _finding(side: T6Side, counted: tuple[Evidence, int], run_id: str, at: datetime) -> Finding:
    """Step 3: the verified analyst finding on the side's epic citing its count."""
    ev, count = counted
    number = NumberRef(id="n1", value=count, unit="count", query_id=ev.query_id,
                       column="incident_count", row_key=None)  # fmt: skip
    claim = f"Epic {side.epic_key} targets [[n1]] incidents on its service"
    return Finding(
        finding_id=new_id(IdKind.FINDING), run_id=run_id, task_id=new_id(IdKind.TASK),
        author_role="analyst", claim=claim,
        entity_type="candidate", entity_id=side.epic_record_id, numbers=[number],
        query_ids=[ev.query_id], confidence=0.7, status="verified", created_at=at,
    )  # fmt: skip


def _draft(side: T6Side, finding_id: str, rank: int) -> RecommendationDraft:
    """Step 4: one `fund` draft on the side's epic (`work_item`, see the module notes)."""
    return RecommendationDraft(
        rank=rank, kind="fund", target_type="work_item", target_id=side.epic_record_id,
        summary=f"Fund {side.epic_key} to reduce incidents", numbers=[],
        expected_metric="incident_count", expected_delta_ref=None, expected_usd_ref=None,
        finding_ids=[finding_id],
    )  # fmt: skip


def seed_prior_run(
    memory: MemoryStore, truth: TruthManifest, *, warehouse: duckdb.DuckDBPyConnection,
    build_id: str,
) -> SeededPriorRun:  # fmt: skip
    """Plant T6 in the ops store and memory (U11-49); `HernessError` propagates."""
    plant = truth.plants.T6_outcomes
    effective_at = plant.effective_at
    started = effective_at - PRIOR_RUN_AGE
    run_id = "run_" + new_ulid()
    _insert_run(run_id, build_id, started)
    sides = {"paid": plant.paid, "unpaid": plant.unpaid}
    evidence = {
        name: _record_count(side, effective_at, run_id=run_id, warehouse=warehouse,
                            build_id=build_id)
        for name, side in sides.items()
    }  # fmt: skip
    findings = {name: _finding(sides[name], evidence[name], run_id, started) for name in SIDES}

    def insert_findings(conn: sqlite3.Connection) -> None:
        for f in findings.values():
            insert_finding(conn, f)

    run_write(insert_findings, op=_OP)
    drafts = [_draft(sides[name], findings[name].finding_id, rank)
              for rank, name in enumerate(SIDES, start=1)]  # fmt: skip
    rec_ids = dict(zip(SIDES, memory.write_recommendations(run_id, drafts), strict=True))
    for rec_id in rec_ids.values():
        memory.decide(rec_id, "accepted", DECISION_REASON, SEED_USER_REF, effective_at=effective_at)
    return SeededPriorRun(
        run_id=run_id, rec_ids=rec_ids,
        finding_ids={name: f.finding_id for name, f in findings.items()},
    )  # fmt: skip
