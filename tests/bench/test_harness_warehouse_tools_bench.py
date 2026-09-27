"""Warehouse tool benchmarks (BT05-04 `run_sql`, BT05-05 `list_tables` / `describe_table`).

Spec 11's `full` synthetic build does not exist yet: both run on a local "full-like" build of
`tests.support.warehouse_tools_build` with 500,000 incidents over 200 services and 36 months
(`metrics.incident_monthly` 7,200 rows; about 20 MB on disk). Re-point to `full` when spec 11
lands. Run: pytest -m "integration and slow" tests/bench.
"""

# ruff: noqa: S608 - fixed benchmark SQL built from integer loop indices only

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from tests.support import tools_standin as sd
from tests.support import warehouse_tools_build as wb
from tests.support.dispatch_standin import call, make_state, use_test_config
from tests.support.harness_fakes import FakeOps

from herness.core import config as c
from herness.core.resilience import ProcessState
from herness.core.types import ToolResult
from herness.harness import warehouse_tools as wt
from herness.harness.llm.settings import SqlSettings
from herness.harness.tools import dispatch
from herness.harness.warehouse import DuckWarehouse, open_warehouse

pytestmark = [pytest.mark.integration, pytest.mark.slow]

FULL_LIKE = {"incidents": 500_000, "services": 200, "months": 36}
QUERIES = 200
CALLS = 100


@pytest.fixture(scope="module")
def full_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    warehouse_dir = tmp_path_factory.mktemp("full_like")
    path = wb.make_build(warehouse_dir, **FULL_LIKE)
    size_mb = path.stat().st_size / 1_000_000
    sys.stderr.write(f"full-like build {FULL_LIKE} {size_mb:.1f} MB\n")
    return warehouse_dir


@pytest.fixture
def wh(
    full_dir: Path, tmp_path: Path, reset_process_state: ProcessState
) -> Iterator[DuckWarehouse]:
    del reset_process_state
    use_test_config(tmp_path)
    handle = open_warehouse(wb.BUILD_ID, warehouse_dir=full_dir, sql=SqlSettings())
    try:
        yield handle
    finally:
        handle.close()
        c.reset_config()


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[18]


def _aggregates() -> list[str]:
    """200 distinct typical aggregates (no result-cache hits)."""
    templates: list[Callable[[int], str]] = [
        lambda k: (
            "SELECT service_id, count(*) AS n, avg(mttr_hours) AS mttr FROM core.incident"
            f" WHERE opened_at >= TIMESTAMP '2024-01-01' + INTERVAL {k} DAY"
            " GROUP BY service_id ORDER BY n DESC, service_id LIMIT 20"
        ),
        lambda k: (
            "SELECT date_trunc('month', opened_at) AS month, count(*) AS n FROM core.incident"
            f" WHERE priority = {1 + k % 4} AND service_id = 'svc_{k}' GROUP BY 1 ORDER BY 1"
        ),
        lambda k: (
            "SELECT priority, count(*) AS n, sum(mttr_hours) AS hours FROM core.incident"
            f" WHERE service_id IN ('svc_{k}', 'svc_{k + 50}') GROUP BY priority ORDER BY priority"
        ),
        lambda k: (
            "SELECT service_id, sum(incidents) AS n, avg(mttr_hours) AS mttr"
            f" FROM metrics.incident_monthly WHERE month >= DATE '2024-01-01' + INTERVAL {k % 36}"
            " MONTH GROUP BY service_id ORDER BY n DESC, service_id LIMIT 10"
        ),
    ]
    return [templates[i % 4](i // 4) for i in range(QUERIES)]


def test_bt05_04_run_sql_typical_aggregate_p95(wh: DuckWarehouse) -> None:
    """BT05-04 200 typical aggregates through dispatch and `run_sql` end to end: p95 < 2 s."""
    ctx = sd.make_ctx(wh, FakeOps())
    tools = {"run_sql": wt.RunSql()}

    async def rounds() -> list[float]:
        seconds: list[float] = []
        for i, sql in enumerate(_aggregates()):
            state = make_state()
            start = time.perf_counter()
            (result,) = await dispatch(
                ctx, tools, [call("run_sql", f"c{i}", sql=sql, purpose="bench")], None, state
            )
            seconds.append(time.perf_counter() - start)
            assert result.ok, result.content
        return seconds

    seconds = asyncio.run(rounds())
    p95 = _p95(seconds)
    sys.stderr.write(f"BT05-04 run_sql p95 {p95 * 1000:.1f} ms over {len(seconds)} queries\n")
    assert p95 < 2.0, f"run_sql p95 {p95:.3f} s"


def _timed(fn: Callable[[], ToolResult]) -> list[float]:
    seconds: list[float] = []
    for _ in range(CALLS):
        start = time.perf_counter()
        result = fn()
        seconds.append(time.perf_counter() - start)
        assert result.ok
    return seconds[1:]  # the first call warms the schema and result caches


def test_bt05_05_list_and_describe_p95(wh: DuckWarehouse) -> None:
    """BT05-05 100 calls each of list_tables and describe_table, cache warm after the first:
    p95 < 50 ms each."""
    ctx = sd.make_ctx(wh, FakeOps())
    listed = _timed(lambda: wt.ListTables()(ctx, schema=None))
    described = _timed(lambda: wt.DescribeTable()(ctx, table="core.incident"))
    p95_list, p95_describe = _p95(listed), _p95(described)
    sys.stderr.write(
        f"BT05-05 list_tables p95 {p95_list * 1000:.2f} ms,"
        f" describe_table p95 {p95_describe * 1000:.2f} ms\n"
    )
    assert p95_list < 0.05, f"list_tables p95 {p95_list:.4f} s"
    assert p95_describe < 0.05, f"describe_table p95 {p95_describe:.4f} s"
