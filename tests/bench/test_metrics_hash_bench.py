"""Parallel hashing benchmark (BT04-09). Run: pytest -m "integration and slow" tests/bench.

Stand-in for `metrics.incident_fact` on `lake_small` (stage 400 and the synthetic lake are not
built yet): a 5,000,000-row fact-shaped SELECT materialized with ``IntoSpec(replace)``, so the
read-back count is above ``HASH_PARALLEL_MIN_ROWS`` and hashing runs in ``HASH_WORKERS``
processes. The timed span is the whole ``run_recorded`` call (store, read back, hash, evidence).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from herness.metrics import evidence
from herness.metrics.evidence import IntoSpec, run_recorded

pytestmark = [pytest.mark.integration, pytest.mark.slow]

ROWS = 5_000_000
LIMIT_S = 25.0
BUILD = "20260924-211403-ABCDEF"
SETTINGS_SQL = (
    Path(__file__).resolve().parents[2] / "herness" / "model" / "sql" / "000_settings.sql"
)
FACT_SQL = (
    "SELECT 'inc_' || range AS record_id, 'svc_' || (range % 997) AS service_id,"
    " 'team_' || (range % 61) AS team_id, CAST(range % 4 + 1 AS SMALLINT) AS priority,"
    " TIMESTAMPTZ '2026-01-01 00:00:00+00' + to_seconds(range) AS opened_at,"
    " (range % 7200) / 60.0 AS resolve_hours, range % 3 = 0 AS is_repeat"
    " FROM range($n)"
)


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    c = duckdb.connect()
    c.execute(SETTINGS_SQL.read_text(encoding="utf-8"))
    c.execute("INSERT INTO meta.build (build_id) VALUES (?)", [BUILD])
    yield c
    c.close()


def test_bt04_09_parallel_hash_5m_rows(con: duckdb.DuckDBPyConnection) -> None:
    """BT04-09 parallel hashing of a 5M-row incident fact table finishes in under 25 s."""
    assert evidence.HASH_PARALLEL_MIN_ROWS <= ROWS
    into = IntoSpec("metrics.incident_fact", "replace", "query_id")
    params = {"bind": {"n": ROWS}, "template": {"name": "bt04_09"}}
    start = time.perf_counter()
    rq = run_recorded(con, FACT_SQL, params, "facts", into=into)
    elapsed = time.perf_counter() - start
    assert rq.row_count == ROWS
    assert elapsed < LIMIT_S, f"{elapsed:.1f}s with {evidence.HASH_WORKERS} workers"
