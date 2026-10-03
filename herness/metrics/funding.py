"""Funding step: attribution, then scores (impl 04 U04-66; design 04 §5.4-§5.7).

`run_funding_step` renders the named score templates with the step binds and stores each one
through `run_recorded`, so every stored number comes from a recorded SELECT and its evidence row.
"""

from typing import Final

import duckdb

from herness.metrics.context import StepContext, StepResult
from herness.metrics.evidence import IntoSpec, run_recorded
from herness.metrics.render import render_named
from herness.metrics.settings import WEIGHT_USES, unconfirmed_blocks

__all__ = ["ATTRIBUTION_TABLE", "run_funding_step"]

ATTRIBUTION_TABLE: Final = "score.funding_attribution"


def run_funding_step(con: duckdb.DuckDBPyConnection, sc: StepContext, /) -> StepResult:
    """Rewrite `score.funding_attribution` (U04-66 steps 1-2 and 4).

    Runs inside the caller's transaction; DuckDB and query errors propagate for the runner to
    convert into SchemaViolation.
    """
    unconfirmed = bool(unconfirmed_blocks(sc.weights, WEIGHT_USES["funding"]))
    binds = {**sc.binds(), "unconfirmed": unconfirmed}
    rendered = render_named("funding_attribution", {}, binds)
    params = {"bind": rendered.bind, "template": rendered.template}
    into = IntoSpec(ATTRIBUTION_TABLE, "replace", "query_id")
    attribution = run_recorded(con, rendered.sql, params, "score", build_id=sc.build_id, into=into)
    # T04-15: U04-66 step 3 renders "funding_score" with the same binds and stores it with
    # IntoSpec("score.funding", "replace", "query_ids", (attribution.query_id,)); step 4 adds
    # its row count and the warning "no funding candidates" when score.funding is empty.
    return StepResult(
        row_counts={ATTRIBUTION_TABLE: attribution.row_count},
        warnings=[],
        flags=[],
        failed_checks=[],
    )
