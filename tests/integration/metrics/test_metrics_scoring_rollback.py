"""FT04-03 (impl 04 11.4; T04-21, T04-23c): a metric template raising a DuckDB error rolls the
whole metrics step back.

It is in-process and hermetic (a tiny DuckDB build, a few seconds), so it carries the
`integration` marker and is selected by merge-gate suite (a) `(unit or integration) and not
slow`; as a `fault`-only test it was invisible to that gate (T04-23c).
"""

from __future__ import annotations

import pytest
from tests.support.build_harness import FakeJobContext
from tests.support.metrics_scoring import patches, small_catalog, tiny_with_facts
from tests.support.metrics_tiny import BUILD_ID

from herness.core.errors import SchemaViolation
from herness.metrics import _scoring_checks, scoring
from herness.metrics.catalog import MetricCatalog
from herness.metrics.scoring import run_scoring

pytestmark = pytest.mark.integration

_SNAPSHOT_SQL = (
    ("metric_value", "SELECT * FROM metrics.metric_value"),
    ("dq_result", "SELECT * FROM meta.dq_result"),
    (
        "evidence",
        "SELECT query_id, sql, params, result_hash, row_count, producer FROM meta.evidence",
    ),
    ("build", "SELECT build_id, status FROM meta.build"),
)


def _apply(mp: pytest.MonkeyPatch, catalog: MetricCatalog) -> None:
    for obj, name, value in patches(catalog):
        mp.setattr(obj, name, value)


def _broken_catalog() -> MetricCatalog:
    """The small catalog plus `zz_broken_metric` (sorted last, so rows of the other metrics are
    appended first): `mttr_hours` whose value raises at run time. The shipped template calls the
    `davg` macro (U04-35), so the failure is planted in front of that call, not by editing the
    macro name (a bare `avg(` replace also matches inside `davg(` and breaks the Jinja syntax)."""
    base = small_catalog()
    mttr = next(m for m in base.config.metrics if m.name == "mttr_hours")
    broken = mttr.model_copy(
        update={
            "name": "zz_broken_metric",
            "enabled": True,
            "sql": mttr.sql.replace("{{ davg(", "CAST('planted failure' AS INTEGER) + {{ davg(", 1),
        }
    )
    assert broken.sql != mttr.sql
    metrics = [*base.config.metrics, broken]
    return MetricCatalog(base.config.model_copy(update={"metrics": metrics}))


@pytest.mark.parametrize("earlier_run", [False, True])
def test_ft04_03_metric_template_error_rolls_back(
    monkeypatch: pytest.MonkeyPatch, earlier_run: bool
) -> None:
    """FT04-03 a metric template raising a DuckDB error: SchemaViolation, the metrics step is
    rolled back (no partial `metric_value`, no evidence of it, an earlier table unchanged), the
    step is not checkpointed and the build stays `building` (not promoted)."""
    _apply(monkeypatch, small_catalog())
    con = tiny_with_facts()
    try:
        if earlier_run:
            run_scoring(BUILD_ID, steps=["metrics"], con=con)
        before = {
            name: sorted(repr(r) for r in con.execute(sql).fetchall())
            for name, sql in _SNAPSHOT_SQL
            if earlier_run or name != "metric_value"
        }
        monkeypatch.setattr(scoring, "catalog_from_config", lambda _cfg=None: _broken_catalog())
        ctx = FakeJobContext()
        with pytest.raises(
            SchemaViolation, match=r"^metric zz_broken_metric grain \w+ period week failed: "
        ):
            run_scoring(BUILD_ID, con=con, ctx=ctx)
        after = {
            name: sorted(repr(r) for r in con.execute(sql).fetchall())
            for name, sql in _SNAPSHOT_SQL
            if earlier_run or name != "metric_value"
        }
        assert after == before
        tables = _scoring_checks.existing_tables(con)
        assert ("metrics.metric_value" in tables) is earlier_run
        assert ctx.saved_states == []
    finally:
        con.close()
