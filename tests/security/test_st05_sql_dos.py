"""Security test ST05-09 (TH05-09): expensive SQL is cut off in time (T05-27).

Agent SQL through the real `run_sql` tool (guard, interrupt timer, `scan_rows` cap) on the
stand-in build of `tests.support.warehouse_tools_build`, scaled to 20,000 incidents: a
cartesian join of large tables and an unbounded recursive CTE end in a `QueryError` timeout
or size error within `timeout_s + 1 s` (wall time measured with `time.monotonic`).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support import warehouse_tools_build as wb
from tests.support.dispatch_standin import use_test_config
from tests.support.harness_fakes import FakeOps
from tests.support.tools_standin import make_ctx

from herness.core import config as c
from herness.core.errors import QueryError
from herness.core.types import SqlLimits
from herness.harness import warehouse_tools as wt
from herness.harness.llm.settings import SqlSettings
from herness.harness.warehouse import DuckWarehouse, open_warehouse

pytestmark = pytest.mark.unit

TIMEOUT_S = 1.0
SLACK_S = 1.0  # the row's bound: timeout_s + 1 s
SCAN_ROWS = 100_000
INCIDENTS = 20_000  # 4e8 pairs, 8e12 triples

CARTESIAN_AGGREGATE = (
    "SELECT count(*) AS n FROM core.incident a, core.incident b, core.incident c"
    " WHERE a.priority + b.priority + c.priority > 0"
)
CARTESIAN_ROWS = (
    "SELECT a.record_id AS a_id, b.record_id AS b_id"
    " FROM core.incident a CROSS JOIN core.incident b"
)
RECURSIVE_UNBOUNDED = (
    "WITH RECURSIVE r(n) AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r) SELECT max(n) AS m FROM r"
)
RECURSIVE_ROWS = (
    "WITH RECURSIVE r(n) AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r) SELECT n FROM r"
)


@pytest.fixture(scope="module")
def big_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The stand-in build with 20,000 incidents (built once for the module)."""
    root = tmp_path_factory.mktemp("st05_09")
    wb.make_build(root / "wh", incidents=INCIDENTS)
    return root / "wh"


@pytest.fixture
def wh(big_build: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DuckWarehouse]:
    """The test config (blocked columns), stub redaction, the opened 20,000-incident build."""
    use_test_config(tmp_path / "cfg")
    wb.patch_redaction(monkeypatch)
    handle = open_warehouse(wb.BUILD_ID, warehouse_dir=big_build, sql=SqlSettings())
    try:
        yield handle
    finally:
        handle.close()
        c.reset_config()


def _run_sql(handle: DuckWarehouse, sql: str) -> tuple[QueryError, float]:
    """Run `sql` through the real `run_sql` tool; the error and the elapsed wall time."""
    ctx = make_ctx(handle, FakeOps(), limits=SqlLimits(timeout_s=TIMEOUT_S, scan_rows=SCAN_ROWS))
    started = time.monotonic()
    with pytest.raises(QueryError) as info:
        wt.RunSql()(ctx, sql=sql, purpose="st05-09")
    return info.value, time.monotonic() - started


@pytest.mark.parametrize(
    "sql",
    [CARTESIAN_AGGREGATE, RECURSIVE_UNBOUNDED],
    ids=["cartesian_join_aggregate", "recursive_cte_unbounded"],
)
def test_st05_09_expensive_query_times_out_within_bound(wh: DuckWarehouse, sql: str) -> None:
    """ST05-09 a cartesian join of large tables and an unbounded recursive CTE (aggregated, so
    no row reaches the scan cap): `QueryError` timeout with its hint within `timeout_s + 1 s`."""
    err, elapsed = _run_sql(wh, sql)
    assert err.message == f"timeout after {TIMEOUT_S}s"
    assert err.hint == "filter by period or use get_metric"
    assert elapsed < TIMEOUT_S + SLACK_S


@pytest.mark.parametrize(
    "sql", [CARTESIAN_ROWS, RECURSIVE_ROWS], ids=["cartesian_join_rows", "recursive_cte_rows"]
)
def test_st05_09_huge_result_is_timeout_or_size_error_within_bound(
    wh: DuckWarehouse, sql: str
) -> None:
    """ST05-09 a cartesian join and an unbounded recursive CTE returning their rows: a
    `QueryError` size error (`scan_rows`) or timeout within `timeout_s + 1 s`."""
    err, elapsed = _run_sql(wh, sql)
    assert (err.message, err.hint) in {
        ("result too large", "aggregate first or add filters"),
        (f"timeout after {TIMEOUT_S}s", "filter by period or use get_metric"),
    }
    assert elapsed < TIMEOUT_S + SLACK_S
