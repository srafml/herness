"""Ops store benchmark (impl 02 BT02-06). Run: pytest -m "integration and slow" tests/bench."""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

from herness.store.ops import core

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# Test-local table shaped like migration 001's `review_item` (T02-05 creates the real one;
# re-point this benchmark to it then).
_REVIEW_ITEM = """
CREATE TABLE review_item (
    item_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    subject TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (length(payload) <= 65536),
    note TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT
) STRICT
"""
_INSERT = (
    "INSERT INTO review_item (item_id, kind, subject, payload, created_at) "
    "VALUES (?, 'mapping_suggestion', ?, ?, '2026-09-25T10:00:00.000000Z')"
)


def test_bt02_06_single_row_write_p95(ops_store: Path) -> None:
    """BT02-06 1,000 run_write inserts into review_item: p95 under 10 ms."""
    core.run_write(lambda c: c.execute(_REVIEW_ITEM), op="create_review_item")
    payload = core.dump_json({"source": "jira", "value": "team-a"}, field="payload")
    durations: list[float] = []
    for i in range(1_000):

        def insert(conn: sqlite3.Connection, i: int = i) -> None:
            conn.execute(_INSERT, (f"r{i:05d}", f"subject {i}", payload))

        start = time.perf_counter()
        core.run_write(insert, op="create_review_item")
        durations.append(time.perf_counter() - start)
    durations.sort()
    p95 = durations[int(0.95 * (len(durations) - 1))]
    sys.stderr.write(f"BT02-06 p95={p95 * 1000:.3f} ms\n")
    assert p95 < 0.010
