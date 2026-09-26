"""Files connector reader throughput (impl 01 BT01-04, design 01 §8).

Run: pytest -m "integration and slow" tests/bench/connectors. Files are generated with
DuckDB ``COPY`` in ``tmp_path``; throughput counts rows yielded by ``read_file``.
"""

from __future__ import annotations

import datetime
import os
import time
from pathlib import Path

import duckdb
import pytest
from tests.unit.connectors import _files_data as d

from herness.connectors.files import FilesConnector
from herness.connectors.settings import FilesSettings

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_NOW = datetime.datetime(2026, 9, 1, 12, tzinfo=datetime.UTC)
_SELECT = (
    "SELECT i AS id, 'team_' || (i % 97) AS name, i % 1000 AS size, "
    "TIMESTAMP '2026-01-01' + to_seconds(i) AS updated FROM range({rows}) t(i)"
)


def _generate(path: Path, rows: int, fmt: str) -> None:
    con = duckdb.connect(":memory:")
    try:
        if fmt == "xlsx":
            con.execute("LOAD excel")
        target = str(path).replace("'", "''")
        con.execute(f"COPY ({_SELECT.format(rows=rows)}) TO '{target}' (FORMAT {fmt})")
    finally:
        con.close()
    old = int((_NOW - datetime.timedelta(hours=1)).timestamp()) * 10**9
    os.utime(path, ns=(old, old))


def _throughput(root: Path, suffix: str, rows: int) -> float:
    entity = {"pattern": f"*.{suffix}", "key_field": ["id"], "updated_field": "updated"}
    cfg = FilesSettings.model_validate(
        {"enabled": True, "batch_rows": 100_000, "entities": {"bench": entity}}
    )
    conn = FilesConnector(cfg, inbox_root=root, clock=lambda: _NOW)
    (f,) = conn.candidates("bench")
    start = time.perf_counter()
    seen = sum(batch.num_rows for batch in conn.read_file("bench", f))
    elapsed = time.perf_counter() - start
    assert seen == rows
    return rows / elapsed


@pytest.mark.parametrize(
    ("suffix", "rows", "floor"),
    [("csv", 2_000_000, 200_000), ("xlsx", 200_000, 20_000), ("parquet", 10_000_000, 1_000_000)],
)
def test_bt01_04_files_reader_throughput(
    tmp_path: Path, suffix: str, rows: int, floor: int
) -> None:
    """BT01-04 CSV 2M, XLSX 200k and Parquet 10M rows at >= 200k / 20k / 1M rows/s."""
    if suffix == "xlsx" and not d.excel_available():
        pytest.skip(d.EXCEL_SKIP)
    (tmp_path / "bench").mkdir()
    _generate(tmp_path / "bench" / f"data.{suffix}", rows, suffix)
    rate = _throughput(tmp_path, suffix, rows)
    assert rate >= floor, f"{suffix}: {rate:,.0f} rows/s < {floor:,} rows/s"
