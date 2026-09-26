"""Fault tests for herness.metrics.facts (FT04-05): stage 400 with a missing enrich input."""

import pytest
from tests.support.metrics_tiny import BUILD_ID, build_metrics_tiny, patch_facts_config

from herness.core.errors import SchemaViolation
from herness.metrics.facts import materialize_facts

pytestmark = pytest.mark.fault


def test_ft04_05_missing_cluster_member_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """FT04-05 `enrich.cluster_member` missing: `materialize_facts` raises SchemaViolation and
    writes no fact table and no evidence row."""
    patch_facts_config(monkeypatch)
    con = build_metrics_tiny()
    try:
        con.execute("DROP TABLE enrich.cluster_member")
        with pytest.raises(SchemaViolation, match=r"^stage 400 input enrich\.cluster_member"):
            materialize_facts(con, BUILD_ID)
        written = con.execute(
            "SELECT (SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema = 'metrics'), (SELECT count(*) FROM meta.evidence)"
        ).fetchone()
        assert written == (0, 0)
    finally:
        con.close()
