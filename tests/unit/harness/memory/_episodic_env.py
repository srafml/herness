"""Shared helpers for the episodic tests (T07-16): deps factory and closed-loop seeds.

Not a test module. Rows go through the real `herness.store.ops.closed_loop` writers on the
migrated `ops_store` of the test; runs are inserted with plain SQL (the run area has no seed).
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tests.unit.harness.memory._write_env import NOW, Env, make_writer

from herness.core import time as clock
from herness.core.ids import new_ulid
from herness.harness.memory.episodic import EpisodicDeps
from herness.harness.memory.policy import InjectionScanner
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder
from herness.harness.memory.write import MemoryWriter
from herness.store import ops
from herness.store.ops import core
from herness.store.ops import memory as mem_ops

QID = "q_" + "a" * 16
USER = "d" * 32
DATE_PATTERN = r"\d{4}-\d{2}-\d{2}"


def make_deps(
    env: Env, cfg: MemoryConfig | None = None, allowed: tuple[str, ...] = ()
) -> EpisodicDeps:
    """EpisodicDeps around the writer of `env`; `allowed` numeral patterns (default none)."""
    cfg = cfg or MemoryConfig()
    return EpisodicDeps(
        conn_factory=core.connection,
        writer=env.writer,
        redactor=env.redactor,
        allowed=tuple(re.compile(p) for p in allowed),
        episodic=cfg.episodic,
        outcome=cfg.outcome,
    )


def make_writer_allowing(tmp_path: Path, *patterns: str) -> Env:
    """`make_writer` whose MemoryWriter allows the given numeral patterns."""
    base = make_writer(tmp_path)
    cfg = MemoryConfig()
    writer = MemoryWriter(
        cfg, redactor=base.redactor, scanner=InjectionScanner(cfg.injection_patterns),
        allowed_patterns=tuple(re.compile(p) for p in patterns), conn_factory=core.connection,
        vectors=base.vectors, embedder=Embedder(base.embed, model_name="bge-m3"),
    )  # fmt: skip
    return replace(base, writer=writer)


def with_cfg(deps: EpisodicDeps, **episodic: int) -> EpisodicDeps:
    """`deps` with episodic settings replaced."""
    return replace(deps, episodic=deps.episodic.model_copy(update=episodic))


def seed_run(kind: str = "org_review", started: datetime = NOW) -> str:
    """One `run` row of `kind` started at `started`; returns its id."""
    run_id = "run_" + new_ulid()
    core.run_write(
        lambda c: c.execute(
            "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at,"
            " meta) VALUES (?, ?, 'standard', 'p', 'h', 'done', ?, '{}')",
            (run_id, kind, clock.format_utc(started)),
        ),
        op="test_seed",
    )
    return run_id


def rec_row(run_id: str, created: datetime, **fields: Any) -> ops.RecommendationRow:
    """A valid recommendation row of `run_id` created at `created`."""
    base: dict[str, Any] = {
        "rec_id": "rec_" + new_ulid(), "run_id": run_id, "kind": "fund",
        "target_type": "service", "target_id": "svc_alpha",
        "summary": "Fund the platform team to lift [[n1]].",
        "numbers": [{"id": "n1", "value": 1.5, "unit": "pct", "query_id": QID,
                     "column": "c", "row_key": None}],
        "expected_metric": "mttr", "expected_delta": None, "expected_usd": None,
        "confidence": 0.7, "confidence_basis": {}, "finding_ids": [],
        "created_at": clock.format_utc(created),
    }  # fmt: skip
    return ops.RecommendationRow(**(base | fields))  # type: ignore[typeddict-item]


def seed_recs(rows: list[ops.RecommendationRow]) -> list[str]:
    """Insert the rows (≤ 50 per call) in one transaction; returns their ids."""
    for i in range(0, len(rows), 50):
        chunk = rows[i : i + 50]
        core.run_write(lambda c, ch=chunk: ops.insert_recommendations(ch, conn=c), op="test_seed")
    return [r["rec_id"] for r in rows]


def seed_decision(
    rec_id: str, decision: str, at: datetime, effective: datetime | None = None
) -> None:
    """One `decision_log` row by USER."""
    ops.insert_decision(
        ops.DecisionRow(
            rec_id=rec_id,
            decision=decision,
            reason="seeded",
            decided_by=USER,
            decided_at=clock.format_utc(at),
            effective_at=clock.format_utc(effective or at),
        )
    )


def seed_outcome(rec_id: str, verdict: str, measurement: int = 1, rel: Any = 0.1234) -> str:
    """One `outcome` row; `details.rel` = `rel`; returns the outcome id."""
    outcome_id = "out_" + new_ulid()
    ops.insert_outcome(
        ops.OutcomeRow(
            outcome_id=outcome_id,
            rec_id=rec_id,
            measurement=measurement,
            measured_at=clock.format_utc(NOW - timedelta(days=1)),
            metric="mttr",
            baseline=10.0,
            actual=9.0,
            delta=-1.0,
            query_id=QID,
            verdict=verdict,
            details={"rel": rel},
        )
    )
    return outcome_id


def seed_note(rec_id: str, kind: str = "decision_note") -> str:
    """A memory item of `kind` citing `rec_id` in `data.rec_id`; returns its id."""
    memory_id = "mem_" + new_ulid()
    mem_ops.insert_memory_item({
        "memory_id": memory_id, "layer": "episodic", "kind": kind, "content": "seeded note",
        "data": {"rec_id": rec_id}, "provenance": {"author_type": "system", "via": "pipeline"},
        "confidence": 1.0, "status": "active", "created_at": clock.format_utc(NOW),
        "expires_at": None, "last_used_at": None, "use_count": 0,
    })  # fmt: skip
    return memory_id
