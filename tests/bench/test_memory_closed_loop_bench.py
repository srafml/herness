"""Closed-loop benchmarks: `write_recommendations` and the outcome job (BT07-07, BT07-08).

Spec targets (impl 07 §10, §11): "BT07-07 `write_recommendations` | 50 recs | < 2 s";
"BT07-08 outcome job per recommendation | tiny build | < 30 s". Run:
pytest -m "integration and slow" tests/bench -k memory.

BT07-07: the `MemoryStore` facade on the real collaborators (migrated ops store, LanceDB in
tmp, hash-seeded `FakeEmbed`) writes the maximum of 50 drafts (each with two cited numbers, a
verified finding, an expected metric) for a fresh run, `REPEATS` times; the gate is the
median of the repeat times and every repeat must write 50 recommendations.

BT07-08: spec 11's `tiny_build` is not in the tree, so the warehouse is planted the way the
outcome tests do (`_outcome_env.plant`: the spec 02 DDL with a weekly incident per service
for about 80 weeks, 3 flat peers, facts materialized by the real spec 04 code, the shipped
catalog with MTTR minimum sample size 1). The real `outcome_measure_handler` runs once per
recommendation (job payload `{rec_id, measurement: 1}`, the "per recommendation" unit) for
`RECS` accepted recommendations; each must measure and write an `outcome` row, the gate is
that every one stays under 30 s (the median is reported).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.support.bench_stats import REPEATS, report
from tests.support.memory_bench_corpus import open_store
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._outcome_env import (
    accepted_rec,
    flat,
    improves,
    make_env,
    outcomes,
    plant,
    run,
)
from tests.unit.harness.memory._write_env import FakeEmbed, execute, seed_evidence

from herness.core import time as clock
from herness.core.ids import new_ulid
from herness.core.types import NumberRef, RecommendationDraft
from herness.store.ops import core

pytestmark = [pytest.mark.integration, pytest.mark.slow]

RECS = 50
WRITE_LIMIT_S = 2.0
OUTCOME_RECS = 6
OUTCOME_LIMIT_S = 30.0
QID = "q_" + "a" * 16
PEERS = ("svc_p1", "svc_p2", "svc_p3")
_NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _run_row() -> str:
    run_id = "run_" + new_ulid()
    execute(
        "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at, meta)"
        " VALUES (?, 'org_review', 'standard', 'p', 'h', 'recording', ?, '{}')",
        (run_id, clock.format_utc(_NOW)),
    )
    return run_id


def _num(ref: str, value: Any, unit: str) -> NumberRef:
    return NumberRef(id=ref, value=value, unit=unit, query_id=QID, column="c", row_key=None)  # type: ignore[arg-type]


def _drafts(finding_id: str) -> list[RecommendationDraft]:
    return [
        RecommendationDraft(
            rank=rank,
            kind="fund",
            target_type="service",
            target_id=f"svc_{rank}",
            summary="Fund the platform team to lift [[n1]] and save [[n2]].",
            numbers=[_num("n1", 1.5, "pct"), _num("n2", "1234.567", "usd")],
            expected_metric="mttr",
            expected_delta_ref="n1",
            expected_usd_ref="n2",
            finding_ids=[finding_id],
        )
        for rank in range(1, RECS + 1)
    ]


def test_bt07_07_write_recommendations_50_under_2s(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """BT07-07 `write_recommendations` of 50 recommendations: < 2 s (median of 3 runs)."""
    del ops_store
    seed_evidence(QID)
    store = open_store(tmp_path, FakeEmbed())
    seconds: list[float] = []
    for _ in range(REPEATS):
        run_id = _run_row()
        finding = seed_finding_for(run_id)
        drafts = _drafts(finding)
        start = time.perf_counter()
        ids = store.write_recommendations(run_id, drafts)
        seconds.append(time.perf_counter() - start)
        assert len(ids) == RECS
    median = report(
        "BT07-07", "median_write_recommendations", seconds, "s", f"< {WRITE_LIMIT_S:g} s"
    )
    assert median < WRITE_LIMIT_S, f"write_recommendations {median:.2f} s"
    assert core.read_all("SELECT count(*) AS n FROM recommendation")[0]["n"] == RECS * REPEATS


def seed_finding_for(run_id: str) -> str:
    """A verified finding of `run_id` (the recommendation writer checks run ownership)."""
    finding_id = "fnd_" + new_ulid()
    task_id = "task_" + new_ulid()
    stamp = clock.format_utc(_NOW)
    execute(
        "INSERT INTO task (task_id, run_id, role, spec, status, created_at, updated_at)"
        " VALUES (?, ?, 'analyst', '{}', 'done', ?, ?)",
        (task_id, run_id, stamp, stamp),
    )
    execute(
        "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, query_ids,"
        " confidence, status, created_at) VALUES (?, ?, ?, 'analyst', 'c', '[]', 0.7,"
        " 'verified', ?)",
        (finding_id, run_id, task_id, stamp),
    )
    return finding_id


def test_bt07_08_outcome_job_per_recommendation_under_30s(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BT07-08 one outcome job per recommendation on a planted tiny warehouse: < 30 s each."""
    del ops_store
    targets = [f"svc_t{i}" for i in range(OUTCOME_RECS)]
    con = plant(dict.fromkeys(targets, improves()) | dict.fromkeys(PEERS, flat))
    make_env(tmp_path, monkeypatch, con)
    rec_ids = [accepted_rec(target) for target in targets]
    seconds: list[float] = []
    for rec_id in rec_ids:
        start = time.perf_counter()
        result, _ = run({"rec_id": rec_id, "measurement": 1})
        seconds.append(time.perf_counter() - start)
        assert result.result["measured"] == 1
    assert len(outcomes()) == OUTCOME_RECS
    median = report("BT07-08", "median_outcome_job", seconds, "s", f"< {OUTCOME_LIMIT_S:g} s")
    assert max(seconds) < OUTCOME_LIMIT_S, f"outcome job {max(seconds):.2f} s (median {median:.2f})"
