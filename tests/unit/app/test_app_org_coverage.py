"""UT09-114: the ``org.coverage`` named query (T09-15b; design 04 §5.8 step 7 coverage).

The row was a second UT09-111 in impl 09 §11.1, renumbered by T09-26.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from tests.support.app_env import QUERIES_FILE, plant_scored_build, wh_services

from app.common import bootstrap, wh
from herness.store.layout import DataLayout
from herness.store.warehouse import open_readonly

pytestmark = pytest.mark.unit

COVERED_WEIGHT, OTHER_WEIGHT = 0.6, 0.2


@pytest.fixture(scope="module")
def planted(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scored build whose recorded binds hold two metrics and whose T1 has a z_score."""
    data = tmp_path_factory.mktemp("coverage") / "data"
    build = plant_scored_build(data / "warehouse")
    with duckdb.connect(str(data / "warehouse" / f"wh-{build}.duckdb")) as con:
        query_id = con.execute("SELECT query_ids[1] FROM score.org LIMIT 1").fetchone()
        assert query_id is not None
        row = con.execute(
            "SELECT CAST(params AS VARCHAR) FROM meta.evidence WHERE query_id = ?", [query_id[0]]
        ).fetchone()
        assert row is not None
        params = json.loads(row[0])
        params["bind"]["s_org_metrics"] = ["change_failure_rate", "mtta_minutes"]
        params["bind"]["s_org_weights"] = [COVERED_WEIGHT, OTHER_WEIGHT]
        con.execute(
            "UPDATE meta.evidence SET params = ? WHERE query_id = ?",
            [json.dumps(params), query_id[0]],
        )
        con.execute(
            "UPDATE score.org SET z_score = 1.5"
            " WHERE entity_type = 'team' AND entity_id = 'T1' AND metric = 'change_failure_rate'"
        )
    return data


def test_ut09_114_org_coverage_is_covered_weight_fraction(
    monkeypatch: pytest.MonkeyPatch, planted: Path
) -> None:
    """UT09-114 coverage = sum of w over non-NULL z_score rows / sum of recorded weights."""
    queries = wh.load_named_queries(QUERIES_FILE, page_row_limit=5000)
    services = wh_services(planted, queries=queries)
    monkeypatch.setattr(bootstrap, "get_services", lambda: services)
    current = wh.current_build_id(planted)
    conn = open_readonly(current, layout=DataLayout.from_root(planted))
    monkeypatch.setattr(wh, "get_conn", lambda: (current, conn))
    wh._pool.clear()
    rows = wh.query("org.coverage", entity_type="team").table.to_pylist()
    got = {r["entity_id"]: r["coverage"] for r in rows}
    assert got["T1"] == pytest.approx(COVERED_WEIGHT / (COVERED_WEIGHT + OTHER_WEIGHT))
    assert got["T2"] == 0.0
    assert {r["entity_type"] for r in rows} == {"team"}
