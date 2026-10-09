"""IT11-04 (U11-04, U11-07): the distributions of a seed 42 `small` lake (design 11 §5.1.3).

The lake is generated into a pytest tmp dir (never the repository's `data/`) and queried
with plain DuckDB SQL over the current version of every incident (latest `_source_updated_at`
per `_record_id`, tombstones and the planted dirty rows dropped).

Spec note (T11-16): the spec asks for the priority mix within 1 pp of the base mix
{1: .01, 2: .06, 3: .38, 4: .50, 5: .05}. The generator doubles the P1/P2 shares of
criticality-1 services by design (U11-07 step 2), and those services carry 1.5x incident
weight, so the all-services mix sits near 7.6 % P2. The base mix is therefore asserted over
the criticality 2-4 services (within 1 pp), and the criticality-1 services are asserted to
carry a higher P1+P2 share than that base mix.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from tools import synth_data

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_BASE_MIX = {"1": 0.01, "2": 0.06, "3": 0.38, "4": 0.50, "5": 0.05}
_VERSION = (
    "qualify row_number() over (partition by _record_id"
    " order by _source_updated_at desc, _fetched_at desc) = 1"
)


def _glob(root: Path, entity: str) -> str:
    return (root / "data" / "raw" / "servicenow" / entity / "*" / "*.parquet").as_posix()


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Iterator[duckdb.DuckDBPyConnection]:
    root = tmp_path_factory.mktemp("small42") / "root"
    synth_data.generate(42, "small", root)
    con = duckdb.connect()
    con.execute(
        f"create view cur as select * from read_parquet('{_glob(root, 'incident')}') {_VERSION}"  # noqa: S608
    )
    con.execute(
        "create view inc as select *, try_cast(opened_at as timestamp) as o,"
        " try_cast(resolved_at as timestamp) as r from cur where not _deleted"
        " and priority in ('1','2','3','4','5') and try_cast(opened_at as timestamp) is not null"
    )
    con.execute(
        f"create view svc as select sys_id, left(busines_criticality, 1) as crit from"  # noqa: S608
        f" read_parquet('{_glob(root, 'cmdb_ci_service')}') {_VERSION}"
    )
    yield con
    con.close()


def _mix(con: duckdb.DuckDBPyConnection, crits: str) -> dict[str, float]:
    rows = con.execute(
        "select priority, count(*) from inc join svc on inc.business_service = svc.sys_id"  # noqa: S608
        f" where svc.crit in ({crits}) group by 1"
    ).fetchall()
    total = sum(n for _, n in rows)
    return {p: n / total for p, n in rows}


def test_it11_04_distributions_of_seed_42_small(lake: duckdb.DuckDBPyConnection) -> None:
    """IT11-04 priority mix, weekend ratio, P2 median MTTR and top-5 % service share."""
    base = _mix(lake, "'2','3','4'")
    for priority, share in _BASE_MIX.items():
        assert abs(base[priority] - share) <= 0.01, (priority, base[priority])
    top = _mix(lake, "'1'")
    assert top["1"] + top["2"] > 1.5 * (_BASE_MIX["1"] + _BASE_MIX["2"])

    (weekend, weekday) = lake.execute(
        "with d as (select cast(o as date) as dy, count(*) as c from inc group by 1)"
        " select avg(c) filter (where dayofweek(dy) in (0, 6)),"
        " avg(c) filter (where dayofweek(dy) not in (0, 6)) from d"
    ).fetchone() or (0.0, 1.0)
    assert abs(weekend / weekday - 0.3) <= 0.05, weekend / weekday

    (median,) = lake.execute(
        "select median(epoch(r - o) / 3600.0) from inc where priority = '2' and r > o"
    ).fetchone() or (0.0,)
    assert abs(median - 8.0) <= 0.8, median

    (share,) = lake.execute(
        "with s as (select business_service, count(*) as c from inc"
        " where business_service is not null group by 1),"
        " t as (select c, row_number() over (order by c desc) as rn, count(*) over () as n from s)"
        " select sum(c) filter (where rn <= ceil(n * 0.05)) * 1.0 / sum(c) from t"
    ).fetchone() or (0.0,)
    assert share >= 0.35, share
