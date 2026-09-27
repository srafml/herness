"""Setup DDL of the build: the U02-107 (`000_settings.sql`) checks of IT02-21.

IT02-21 itself, the `lake_small` pipeline against the golden counts and `core.*` snapshot,
is `test_model_build_pipeline.py::test_it02_21_lake_small_build` (T02-18). This file pins
the DDL details that snapshot does not show: every schema, the fixed `meta.*`, `enrich.*`
and `stg.*` tables with their column counts, and idempotent re-runs.
"""

import duckdb
import pytest
from tests.support.build_harness import BuildHarness

pytestmark = pytest.mark.integration


@pytest.fixture
def harness(build_harness: BuildHarness) -> BuildHarness:
    assert build_harness.run(0, 99) == ["000_settings.sql", "010_macros.sql"]
    return build_harness


def test_it02_21_setup_ddl_schemas_and_tables(harness: BuildHarness) -> None:
    """IT02-21 (U02-107 DDL details) 000_settings.sql creates the six schemas and the fixed
    tables; a re-run is a no-op."""
    schemas = {r[0] for r in harness.query("SELECT schema_name FROM information_schema.schemata")}
    assert {"stg", "core", "enrich", "metrics", "score", "meta"} <= schemas
    harness.con.execute("INSERT INTO stg.build_counts VALUES ('n', 1)")
    harness.run(0, 99)
    assert harness.query("SELECT * FROM stg.build_counts") == [("n", 1)]
    tables = {
        f"{r[0]}.{r[1]}": r[2]
        for r in harness.query(
            "SELECT table_schema, table_name, count(*) FROM information_schema.columns"
            " WHERE table_schema IN ('meta', 'enrich', 'stg') GROUP BY ALL"
        )
    }
    assert tables == {
        "meta.build": 9,
        "meta.evidence": 8,
        "meta.dq_result": 6,
        "enrich.text_redacted": 4,
        "enrich.decision": 12,
        "enrich.cluster": 9,
        "enrich.cluster_member": 3,
        "enrich.incident_change_link": 4,
        "stg.cast_stats": 4,
        "stg.build_counts": 2,
    }
    harness.con.execute("INSERT INTO meta.evidence (query_id) VALUES ('q_1')")
    with pytest.raises(duckdb.ConstraintException):
        harness.con.execute("INSERT INTO meta.evidence (query_id) VALUES ('q_1')")
    cluster = harness.query(
        "SELECT data_type FROM information_schema.columns"
        " WHERE table_schema = 'enrich' AND table_name = 'cluster' AND column_name = 'top_terms'"
    )
    assert cluster == [("VARCHAR[]",)]
