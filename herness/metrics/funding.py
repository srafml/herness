"""Funding step: attribution, then scores (impl 04 U04-66; design 04 §5.4-§5.7).

`run_funding_step` renders the named score templates with the step binds and stores each one
through `run_recorded`, so every stored number comes from a recorded SELECT and its evidence row.
"""

from collections.abc import Mapping
from typing import Final

import duckdb

from herness.metrics.context import StepContext, StepResult
from herness.metrics.evidence import IntoSpec, RecordedQuery, run_recorded
from herness.metrics.render import render_named
from herness.metrics.settings import WEIGHT_USES, unconfirmed_blocks

__all__ = ["ATTRIBUTION_TABLE", "FUNDING_TABLE", "NO_CANDIDATES", "run_funding_step"]

ATTRIBUTION_TABLE: Final = "score.funding_attribution"
FUNDING_TABLE: Final = "score.funding"
NO_CANDIDATES: Final = "no funding candidates"


def run_funding_step(con: duckdb.DuckDBPyConnection, sc: StepContext, /) -> StepResult:
    """Rewrite `score.funding_attribution`, then `score.funding` (U04-66).

    Runs inside the caller's transaction; DuckDB and query errors propagate for the runner to
    convert into SchemaViolation. Warns "no funding candidates" when `score.funding` is empty.
    """
    unconfirmed = bool(unconfirmed_blocks(sc.weights, WEIGHT_USES["funding"]))
    binds = {**sc.binds(), "unconfirmed": unconfirmed}
    attribution = _store(
        con, sc, "funding_attribution", binds, IntoSpec(ATTRIBUTION_TABLE, "replace", "query_id")
    )
    into = IntoSpec(FUNDING_TABLE, "replace", "query_ids", (attribution.query_id,))
    score = _store(con, sc, "funding_score", binds, into)
    return StepResult(
        row_counts={ATTRIBUTION_TABLE: attribution.row_count, FUNDING_TABLE: score.row_count},
        warnings=[] if score.row_count else [NO_CANDIDATES],
        flags=[],
        failed_checks=[],
    )


def _store(
    con: duckdb.DuckDBPyConnection,
    sc: StepContext,
    name: str,
    binds: Mapping[str, object],
    into: IntoSpec,
) -> RecordedQuery:
    """Render the named template with `binds` and store its result through `run_recorded`."""
    rendered = render_named(name, {}, binds)
    params = {"bind": rendered.bind, "template": rendered.template}
    return run_recorded(con, rendered.sql, params, "score", build_id=sc.build_id, into=into)
