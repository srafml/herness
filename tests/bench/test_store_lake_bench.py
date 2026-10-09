"""Lake write throughput (impl 02 BT02-05; T02-22).

Run: pytest -m "integration and slow" tests/bench/test_store_lake_bench.py
(the `bench` marker of the spec table is not registered; the existing bench files use
`integration` + `slow`).
"""

from __future__ import annotations

import datetime
import json
import shutil
import time
from pathlib import Path

import pyarrow as pa
import pytest
from tests.support.bench_stats import REPEATS, report

from herness.store.lake import LakeWriter

pytestmark = [pytest.mark.integration, pytest.mark.slow]

ROWS = 1_000_000
BATCH_ROWS = 10_000
LIMIT_S = 60.0
_T0 = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=datetime.UTC)
_FETCHED = datetime.datetime(2026, 9, 25, 10, 0, tzinfo=datetime.UTC)
_STATES = ("1", "2", "3", "6", "7")
_PRIORITIES = ("1 - Critical", "2 - High", "3 - Moderate", "4 - Low")
_TEXT = "Users report intermittent failures when submitting the form on node {n}; see change"


def _batch(first: int) -> pa.RecordBatch:
    """`BATCH_ROWS` incident-shaped rows (metadata plus the 20 ServiceNow incident columns,
    ticket text in `short_description` / `description` / `close_notes`) from row `first`."""
    ids = range(first, first + BATCH_ROWS)
    keys = [f"i{n:08d}" for n in ids]
    opened = [(_T0 + datetime.timedelta(minutes=n)).strftime("%Y-%m-%d %H:%M:%S") for n in ids]
    updated = [_T0 + datetime.timedelta(minutes=n) for n in ids]
    texts = [_TEXT.format(n=n) for n in ids]
    payload = [
        json.dumps({"number": f"INC{n:07d}", "opened_at": o, "short_description": t})
        for n, o, t in zip(ids, opened, texts, strict=True)
    ]
    text = pa.string()
    columns: dict[str, pa.Array] = {
        "_record_id": pa.array([f"servicenow:incident:{k}" for k in keys], text),
        "_source": pa.array(["servicenow"] * BATCH_ROWS, text),
        "_entity": pa.array(["incident"] * BATCH_ROWS, text),
        "_source_key": pa.array(keys, text),
        "_source_updated_at": pa.array(updated, pa.timestamp("us", tz="UTC")),
        "_fetched_at": pa.array([_FETCHED] * BATCH_ROWS, pa.timestamp("us", tz="UTC")),
        "_deleted": pa.array([False] * BATCH_ROWS, pa.bool_()),
        "_payload": pa.array(payload, text),
        "number": pa.array([f"INC{n:07d}" for n in ids], text),
        "opened_at": pa.array(opened, text),
        "priority": pa.array([_PRIORITIES[n % 4] for n in ids], text),
        "state": pa.array([_STATES[n % 5] for n in ids], text),
        "business_service": pa.array([f"s{n % 400}" for n in ids], text),
        "assignment_group": pa.array([f"g{n % 120}" for n in ids], text),
        "short_description": pa.array(texts, text),
        "description": pa.array([t + ". " + t for t in texts], text),
        "resolved_at": pa.array(opened, text),
        "closed_at": pa.array(opened, text),
        "close_code": pa.array(["Solved (Permanently)"] * BATCH_ROWS, text),
        "close_notes": pa.array(texts, text),
        "problem_id": pa.array([None] * BATCH_ROWS, text),
        "caused_by": pa.array([None] * BATCH_ROWS, text),
        "made_sla": pa.array(["true"] * BATCH_ROWS, text),
        "business_duration": pa.array([str(n % 9000) for n in ids], text),
        "reassignment_count": pa.array([str(n % 5) for n in ids], text),
        "reopen_count": pa.array(["0"] * BATCH_ROWS, text),
        "cmdb_ci": pa.array([f"ci{n % 3000}" for n in ids], text),
    }
    return pa.RecordBatch.from_pydict(columns)


def _write_once(root: Path) -> tuple[float, int]:
    """One full write of `ROWS` rows; (seconds spent in `write` + `commit`, rows written).
    Building the batches is not part of the measurement."""
    elapsed = 0.0
    with LakeWriter("servicenow", "incident", root=root) as writer:
        for first in range(0, ROWS, BATCH_ROWS):
            batch = _batch(first)
            start = time.perf_counter()
            writer.write(batch)
            elapsed += time.perf_counter() - start
        start = time.perf_counter()
        done = writer.commit()
        elapsed += time.perf_counter() - start
    return elapsed, done.rows


def test_bt02_05_lake_write_one_million_rows_under_60_s(tmp_path: Path) -> None:
    """BT02-05 `LakeWriter` writes 1,000,000 incident-shaped rows in 10,000-row batches
    (`write` plus `commit`) in under 60 s; the gate is the median of `REPEATS` runs."""
    _write_once(tmp_path / "warmup")  # one warm-up pass: imports, zstd, allocator
    shutil.rmtree(tmp_path / "warmup")
    seconds: list[float] = []
    for n in range(REPEATS):
        root = tmp_path / f"run{n}"
        elapsed, rows = _write_once(root)
        assert rows == ROWS
        seconds.append(elapsed)
        shutil.rmtree(root)
    median = report("BT02-05", "lake_write_1M_rows", seconds, "s", f"< {LIMIT_S:.0f} s")
    assert median < LIMIT_S
