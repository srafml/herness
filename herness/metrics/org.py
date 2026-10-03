"""Scoring step `org`: materialize `score.org` (impl 04 U04-68, design 04 §5.8).

Every stored number comes from the one recorded SELECT of `sql/org_score.sql.j2` (U04-67),
materialized by `run_recorded` into `score.org` with `query_ids = [own]`; Python only renders
the static template with the catalog scorecard binds, runs it and reports the row count.
"""

from typing import Final

import duckdb

from herness.core.errors import QueryError, SchemaViolation
from herness.metrics._scoring_checks import existing_tables
from herness.metrics.context import StepContext, StepResult
from herness.metrics.evidence import IntoSpec, run_recorded
from herness.metrics.render import render_named

__all__ = ["ORG_TABLE", "run_org_step"]

ORG_TABLE: Final = "score.org"
_METRIC_VALUE: Final = "metrics.metric_value"


def run_org_step(con: duckdb.DuckDBPyConnection, sc: StepContext, /) -> StepResult:
    """Rewrite `score.org` from `metrics.metric_value` (U04-68).

    Raises SchemaViolation when `metrics.metric_value` is missing or the org query fails.
    """
    if _METRIC_VALUE not in existing_tables(con):
        msg = "metric_value missing; run step metrics first"
        raise SchemaViolation(msg)
    rendered = render_named("org_score", {}, {**sc.binds(), "unconfirmed": False})
    params = {"bind": rendered.bind, "template": rendered.template}
    into = IntoSpec(ORG_TABLE, "replace", "query_ids")
    try:
        rq = run_recorded(con, rendered.sql, params, "score", build_id=sc.build_id, into=into)
    except QueryError as err:
        msg = f"org score failed: {err.message}"
        raise SchemaViolation(msg) from err
    return StepResult(row_counts={ORG_TABLE: rq.row_count}, warnings=[], flags=[], failed_checks=[])
