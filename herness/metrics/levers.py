"""Scoring step `levers`: materialize `score.action_lever` (impl 04 U04-70, U04-71, design 04 §5.9).

Every stored number comes from the one recorded SELECT of `sql/levers.sql.j2` (U04-69),
materialized by `run_recorded` into `score.action_lever` with `query_ids = [own, org query id]`;
Python only reads the upstream org query ID, renders the static template with the step binds,
runs it and reports the row count.
"""

from typing import Final

import duckdb

from herness.core.errors import QueryError, SchemaViolation
from herness.metrics._catalog_checks import LEVER_PLACEHOLDERS
from herness.metrics._scoring_checks import existing_tables, input_digest
from herness.metrics.context import StepContext, StepResult
from herness.metrics.evidence import IntoSpec, run_recorded
from herness.metrics.render import render_named

__all__ = ["LEVER_PLACEHOLDERS", "LEVER_TABLE", "USD_MODELS", "run_levers_step"]

LEVER_TABLE: Final = "score.action_lever"
_ORG_TABLE: Final = "score.org"
_FACTS: Final = ("metrics.incident_fact", "metrics.change_fact")
# U04-71: the lever models, in design 04 §5.9 order. LEVER_PLACEHOLDERS (the only names a
# rationale template may use) is defined once in `_catalog_checks` and re-exported here.
USD_MODELS: Final[tuple[str, ...]] = ("mttr", "repeat", "reopen", "reassign", "sla", "cfr", "noise")
# Every score.org row of one build carries the same single query ID (U04-68).
_ORG_QID_SQL: Final = "SELECT min(query_ids[1]) FROM score.org"


def _org_query_id(con: duckdb.DuckDBPyConnection) -> tuple[str, ...]:
    """The `score.org` query ID as upstream IDs; none when the table is empty."""
    row = con.execute(_ORG_QID_SQL).fetchone()
    return () if row is None or row[0] is None else (str(row[0]),)


def run_levers_step(con: duckdb.DuckDBPyConnection, sc: StepContext, /) -> StepResult:
    """Rewrite `score.action_lever` from `score.org` and the facts (U04-70).

    Raises SchemaViolation when `score.org` is missing or the lever query fails.
    """
    if _ORG_TABLE not in existing_tables(con):
        msg = "score.org missing; run step org first"
        raise SchemaViolation(msg)
    upstream = _org_query_id(con)
    # `inputs` pins the org and fact rows read (a same-build rerun over new ones is new evidence)
    inputs = input_digest(con, (_ORG_TABLE, *_FACTS))
    rendered = render_named("levers", {"inputs": inputs}, sc.binds())
    params = {"bind": rendered.bind, "template": rendered.template}
    into = IntoSpec(LEVER_TABLE, "replace", "query_ids", upstream)
    try:
        rq = run_recorded(con, rendered.sql, params, "score", build_id=sc.build_id, into=into)
    except QueryError as err:
        msg = f"levers failed: {err.message}"
        raise SchemaViolation(msg) from err
    return StepResult(
        row_counts={LEVER_TABLE: rq.row_count}, warnings=[], flags=[], failed_checks=[]
    )
