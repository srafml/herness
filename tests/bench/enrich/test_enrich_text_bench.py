"""Incremental text stage benchmark BT03-01 (impl 03 §10, T03-36).

Run (idle host only): `HERNESS_BENCH_FULL=1 HERNESS_SYNTH_FULL_ROOT=<seed 42 full root>
pytest -m "integration and slow" tests/bench/enrich/test_enrich_text_bench.py`.

BT03-01 measures `herness.enrich.text.build_text_redacted` (the real stage, real
`redact_table`) on the `full` build with `prev_warehouse` set and exactly 10,000 changed
records (8,000 incidents, 1,500 changes, 500 problems; chosen by `sha256(record_id)` order).
The previous warehouse is a slim file made from the build itself: `core.<entity>` reduced to
`(record_id, source_updated_at)` with the changed records one day older, and
`enrich.text_redacted` holding the composed text of every record (the unredacted stand-in
`fill_text_sql`; the stage copies these rows without reading their text, so they cost the
same as redacted ones). The timed call copies every unchanged row and redacts the 10,000
changed ones; the number redacted and copied is asserted against the SQL expectation.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

import duckdb
import pytest
from tests.bench.enrich._common import BT01_LIMIT_S, NIGHTLY_RECORDS, TIMEOUT_S, record
from tests.bench.enrich._lake import LakeBuild, Report, activate_enrich, fill_text_sql
from tests.support._build_clock import pinned_clock
from tests.support.bench import BenchRecorder

from herness.enrich.text import build_text_redacted

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_SPLIT: Final = (("incident", 8_000), ("change", 1_500), ("problem", 500))
assert sum(n for _, n in _SPLIT) == NIGHTLY_RECORDS
_OLD_SQL: Final = "SELECT count(*) FROM mk.enrich.text_redacted AS p JOIN core.{t} AS c ON c.record_id = p.record_id JOIN mk.core.{t} AS o ON o.record_id = p.record_id WHERE p.entity = '{t}' AND c.source_updated_at IS NOT DISTINCT FROM o.source_updated_at AND c.source_updated_at IS NOT NULL"  # noqa: E501


def _prepare_prev(wh: duckdb.DuckDBPyConnection, prev: Path) -> None:
    """Write the slim previous warehouse `prev` and age the chosen records in it."""
    wh.execute("DELETE FROM enrich.text_redacted")
    wh.execute(f"ATTACH '{prev.as_posix()}' AS mk")
    wh.execute("CREATE SCHEMA mk.core")
    wh.execute("CREATE SCHEMA mk.enrich")
    for table, count in _SPLIT:
        wh.execute(
            f"CREATE TABLE mk.core.{table} AS SELECT record_id, source_updated_at"  # noqa: S608
            f" FROM core.{table}"
        )
        wh.execute(
            f"CREATE TEMP TABLE chosen AS SELECT record_id FROM core.{table}"  # noqa: S608
            f" WHERE record_id IS NOT NULL AND source_updated_at IS NOT NULL"
            f" ORDER BY sha256(record_id) LIMIT {count}"
        )
        wh.execute(
            f"UPDATE mk.core.{table} SET source_updated_at = source_updated_at - INTERVAL 1 DAY"  # noqa: S608
            f" WHERE record_id IN (SELECT record_id FROM chosen)"
        )
        wh.execute("DROP TABLE chosen")
    wh.execute(
        "CREATE TABLE mk.enrich.text_redacted (record_id VARCHAR, entity VARCHAR,"
        " text VARCHAR, content_hash VARCHAR)"
    )
    for statement in fill_text_sql("mk."):
        wh.execute(statement)


def _expected_copied(wh: duckdb.DuckDBPyConnection) -> int:
    """Records with a previous text row and an unchanged `source_updated_at`."""
    total = 0
    for table, _ in _SPLIT:
        row = wh.execute(_OLD_SQL.format(t=table)).fetchone()
        total += int(row[0]) if row else 0
    return total


@pytest.mark.timeout(TIMEOUT_S)
@pytest.mark.usefixtures("redaction_key")
def test_bt03_01_incremental_text_stage_10k_changed(
    lake: LakeBuild, wh: duckdb.DuckDBPyConnection, tmp_path: Path, bench_recorder: BenchRecorder
) -> None:
    """BT03-01 incremental text stage with 10k changed records on the `full` build takes
    under 1 min."""
    with pinned_clock():
        activate_enrich(lake, tmp_path / "data", "bt01")
        prev = tmp_path / "prev.duckdb"
        _prepare_prev(wh, prev)
        copied_expected = _expected_copied(wh)
        wh.execute("DETACH mk")
        report = Report()
        start = time.perf_counter()
        build_text_redacted(wh, prev_warehouse=prev, report=report)
        elapsed = time.perf_counter() - start
    redacted = report.rows - report.cache_hits
    wh.execute("DELETE FROM enrich.text_redacted")
    assert report.failed == 0
    assert report.cache_hits == copied_expected
    assert 0 < redacted <= NIGHTLY_RECORDS  # the changed records that have a non-empty text
    assert redacted >= NIGHTLY_RECORDS * 0.95  # nearly all changed records have a text
    assert record(bench_recorder, "BT03-01", "text_incremental_10k_s", elapsed, "s",
                  BT01_LIMIT_S, lake.scale).passed  # fmt: skip
