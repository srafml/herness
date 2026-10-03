"""Ops store benchmarks (impl 02 BT02-06, BT02-07; impl 05 BT05-08).

Run: pytest -m "integration and slow" tests/bench.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.support.bench_stats import REPEATS, report
from tests.support.ops_store import OpsStoreHandle

from herness.core.ids import query_id as compute_query_id
from herness.core.types.harness.evidence import Evidence
from herness.store.ops import core, migrate
from herness.store.ops.evidence import record_evidence, record_evidence_use

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_INSERT = (
    "INSERT INTO review_item (item_id, kind, payload, created_at) "
    "VALUES (?, 'mapping_suggestion', ?, '2026-09-25T10:00:00.000000Z')"
)


@pytest.fixture
def fresh_ops_store(tmp_path: Path) -> Iterator[Path]:
    """BT02-07 needs an un-migrated store; the plugin `ops_store` is already migrated."""
    db_path = tmp_path / "ops.sqlite"
    core.reset_connections(path=db_path)
    yield db_path
    core.reset_connections()


def test_bt02_06_single_row_write_p95(ops_store: OpsStoreHandle) -> None:
    """BT02-06 1,000 run_write inserts into review_item (migration 005): p95 under 10 ms."""
    payload = core.dump_json({"source": "jira", "value": "team-a"}, field="payload")
    durations: list[float] = []
    for i in range(1_000):

        def insert(conn: sqlite3.Connection, i: int = i) -> None:
            conn.execute(_INSERT, (f"r{i:05d}", payload))

        start = time.perf_counter()
        core.run_write(insert, op="create_review_item")
        durations.append(time.perf_counter() - start)
    durations.sort()
    p95 = durations[int(0.95 * (len(durations) - 1))]
    sys.stderr.write(f"BT02-06 p95={p95 * 1000:.3f} ms\n")
    assert p95 < 0.010


def test_bt02_07_fresh_migration_under_2_s(fresh_ops_store: Path) -> None:
    """BT02-07 `migrate()` on an empty ops file completes in under 2 s."""
    start = time.perf_counter()
    report = migrate()
    elapsed = time.perf_counter() - start
    sys.stderr.write(f"BT02-07 fresh migrate={elapsed * 1000:.1f} ms ({len(report.applied)})\n")
    assert report.applied
    assert elapsed < 2.0


def test_bt05_08_evidence_and_use_pair_write_p95(ops_store: Path) -> None:
    """BT05-08 10,000 evidence + evidence_use insert pairs on a WAL ops store: p95 < 10 ms.

    Spec target (impl 05 §10.1): "BT05-08 | Evidence + evidence_use insert | 10,000 inserts on
    a WAL ops store | p95 < 10 ms per pair". Method: 50 warm-up pairs, then 10,000 new pairs
    (distinct query ids) measured `REPEATS` times; the gate is the median of the p95s."""
    migrate()
    build_id = "20260101-000000-ABCDEF"
    used_at = datetime(2026, 9, 26, 10, tzinfo=UTC)

    def pairs(first: int, count: int) -> list[float]:
        durations: list[float] = []
        for i in range(first, first + count):
            sql = f"select {i} as n"
            ev = Evidence(
                query_id=compute_query_id(sql, {}, build_id),
                run_id="run_1",
                build_id=build_id,
                sql=sql,
                params={},
                result_hash="a" * 64,
                row_count=1,
                result_sample=[{"n": i}],
                executed_at=used_at,
                duration_ms=1,
            )
            start = time.perf_counter()
            assert record_evidence(ev)
            assert record_evidence_use(ev.query_id, "run_1", "task_1", used_at)
            durations.append(time.perf_counter() - start)
        durations.sort()
        return durations

    pairs(-50, 50)  # warm-up
    p95s: list[float] = []
    for r in range(REPEATS):
        durations = pairs(r * 10_000, 10_000)
        p95s.append(durations[int(0.95 * (len(durations) - 1))] * 1000)
    p95_ms = report("BT05-08", "p95_evidence_pair", p95s, "ms", "< 10 ms")
    assert p95_ms < 10
