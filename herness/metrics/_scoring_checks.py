"""Step `check` of `run_scoring`: the design 04 §10.2 invariants (impl 04 U04-59, U04-60).

Private sibling of `herness.metrics.scoring` (impl 04 §2 module-map note), which re-exports
`run_check_step`. Each check is one static segment of `sql/checks.sql.j2`, run through
`run_recorded(producer="score")`; its single row gives `value` and `n_bad` (and, for
`score_pain_total`, the DD04-11 `threshold`), and DuckDB derives `passed` while inserting the
`meta.dq_result` row, so no stored number is computed in Python. Every input table carries
`query_id` (or `query_ids`); each check's render context holds `inputs`, a digest of the
distinct input query_ids, so its query_id pins its exact inputs (a rerun over other inputs records
new evidence instead of a TH04-06 conflict; U04-59 says context `{}`, see the T04-13 report),
plus `has_<schema>_<table>` flags for the optional score.* inputs present (T04-21).
"""

from typing import Final, Literal

import duckdb

from herness.core.logging import get_logger
from herness.core.resilience.metrics import record_counter
from herness.metrics.context import StepContext, StepResult
from herness.metrics.evidence import run_recorded
from herness.metrics.facts import FACT_TABLES
from herness.metrics.render import render_named

__all__ = ["CHECKS", "OPTIONAL", "existing_tables", "input_digest", "run_check_step"]

_METRIC_VALUE: Final = "metrics.metric_value"
_DURATION_FACTS: Final = ("metrics.incident_fact", "metrics.change_fact", "metrics.work_item_fact")
_ATTRIBUTION, _FUNDING, _ORG = "score.funding_attribution", "score.funding", "score.org"
_LEVER, _PORTFOLIO = "score.action_lever", "score.portfolio"
_PAIN_INPUTS: Final = (_ATTRIBUTION, "metrics.incident_fact", "metrics.change_fact")
# (check name, severity, tables it requires) in U04-60 order; a missing one skips the check.
CHECKS: Final[tuple[tuple[str, Literal["error", "warn"], tuple[str, ...]], ...]] = (
    ("score_fact_duration_negative", "error", _DURATION_FACTS),
    ("score_metric_ratio_range", "error", (_METRIC_VALUE,)),
    ("score_metric_pct_range", "error", (_METRIC_VALUE,)),
    ("score_metric_negative", "error", (_METRIC_VALUE,)),
    ("score_metric_count_integral", "error", (_METRIC_VALUE,)),
    ("score_share_sum", "error", (_ATTRIBUTION,)),
    ("score_share_range", "error", (_ATTRIBUTION,)),
    ("score_pain_total", "error", _PAIN_INPUTS),
    ("score_confidence_range", "error", (_FUNDING,)),
    ("score_funding_rank_unique", "error", (_FUNDING,)),
    ("score_org_z_finite", "error", (_ORG,)),
    ("score_portfolio_budget", "error", (_PORTFOLIO, _FUNDING)),
    ("score_portfolio_parent_child", "error", (_PORTFOLIO, "metrics.work_item_closure")),
    ("score_portfolio_blockers", "error", (_PORTFOLIO, _FUNDING)),
    ("score_evidence_coverage", "error", (*FACT_TABLES, _METRIC_VALUE)),
    ("score_work_item_cycle", "warn", ("metrics.work_item_closure",)),
    ("score_unconfirmed_rows", "warn", ()),
)
# Tables a check reads only when present (render flag `has_<schema>_<table>`); a check with
# no required and no present optional table is skipped.
OPTIONAL: Final[dict[str, tuple[str, ...]]] = {
    "score_evidence_coverage": (_ATTRIBUTION, _FUNDING, _ORG, _LEVER, _PORTFOLIO),
    "score_unconfirmed_rows": (_FUNDING, _LEVER, _ORG),
}
_LIST_IDS: Final = frozenset({_FUNDING, _ORG, _LEVER, _PORTFOLIO})  # `query_ids VARCHAR[]`
_NAMES: Final[list[str]] = [name for name, _, _ in CHECKS]
_TABLES_SQL: Final = (
    "SELECT table_schema || '.' || table_name FROM information_schema.tables"
    " WHERE table_catalog = current_database()"
)
_DELETE_SQL: Final = (
    "DELETE FROM meta.dq_result WHERE list_contains(CAST(? AS VARCHAR[]), check_name)"
)
_COUNT_SQL: Final = (
    "SELECT count(*) FROM meta.dq_result WHERE list_contains(CAST(? AS VARCHAR[]), check_name)"
)
# Passes when value = 0, or value <= threshold for a check returning one (score_pain_total).
_RESULT_SQL: Final = (
    "INSERT INTO meta.dq_result (check_name, severity, value, threshold, passed, details)"
    " SELECT ?, ?, v, coalesce(t, 0), coalesce(CASE WHEN t IS NULL THEN v = 0 ELSE v <= t END,"
    " false), json_object('query_id', ?, 'n_bad', n) FROM (SELECT CAST(? AS DOUBLE) AS v,"
    " CAST(? AS BIGINT) AS n, CAST(? AS DOUBLE) AS t) RETURNING passed, threshold"
)
_SKIPPED_SQL: Final = (
    "INSERT INTO meta.dq_result (check_name, severity, value, threshold, passed, details)"
    " SELECT ?, ?, NULL, 0, true, json_object('skipped', true)"
)
_log: Final = get_logger("metrics")

type _Context = dict[str, str | int | bool]


def existing_tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    """`schema.table` names present in the current database (information_schema pre-check)."""
    return {str(row[0]) for row in con.execute(_TABLES_SQL).fetchall()}


def input_digest(con: duckdb.DuckDBPyConnection, tables: tuple[str, ...]) -> str:
    """`in_` + 16 hex of sha256 over the sorted distinct query_ids of the present `tables`
    (computed in DuckDB); the step modules pin their inputs with it too (T04-21)."""
    # S608: the table names are module constants of the callers, never from input
    present = existing_tables(con)
    ids = {t: "unnest(query_ids) AS query_id" if t in _LIST_IDS else "query_id" for t in tables}
    parts = [f"SELECT {e} FROM {t}" for t, e in ids.items() if t in present]  # noqa: S608
    union = " UNION ".join(parts or ["SELECT CAST(NULL AS VARCHAR) AS query_id WHERE false"])
    digest = "sha256(coalesce(string_agg(query_id, ',' ORDER BY query_id), ''))"
    sql = f"SELECT substr({digest}, 1, 16) FROM (SELECT DISTINCT query_id FROM ({union}))"  # noqa: S608
    row = con.execute(sql).fetchone()
    return f"in_{row[0] if row else ''}"


def _run_one(
    con: duckdb.DuckDBPyConnection, check: tuple[str, str], sc: StepContext, context: _Context
) -> bool:
    """Run one check, insert its row and return DuckDB's `passed`."""
    name, severity = check
    rendered = render_named(f"checks:{name}", context, sc.binds())
    params = {"bind": rendered.bind, "template": rendered.template}
    rq = run_recorded(con, rendered.sql, params, "score", build_id=sc.build_id)
    value, n_bad, *limit = (rq.rows or [(None, None)])[0]
    args = [name, severity, rq.query_id, value, n_bad, limit[0] if limit else None]
    row = con.execute(_RESULT_SQL, args).fetchone()
    passed = bool(row is not None and row[0])
    if not passed:
        log = _log.error if severity == "error" else _log.warning
        log(
            "metrics.scoring.check_failed",
            build_id=sc.build_id,
            check_name=name,
            value=value,
            threshold=row[1] if row else 0,
        )
        # T08-05: the resilience buffer flushes through the ops metric writer (R-12).
        record_counter(
            "herness_metrics_check_failures_total", 1, component="metrics", labels={"check": name}
        )
    return passed


def run_check_step(con: duckdb.DuckDBPyConnection, sc: StepContext, /) -> StepResult:
    """Run the U04-60 checks and rewrite their `meta.dq_result` rows (U04-59).

    A check whose input table was never written is recorded as passed with
    `details.skipped = true`; the presence test is an information_schema pre-check, so no
    failing statement aborts the step's transaction.
    """
    con.execute(_DELETE_SQL, [_NAMES])
    present = existing_tables(con)
    failed: list[str] = []
    warnings: list[str] = []
    for name, severity, required in CHECKS:
        optional = [t for t in OPTIONAL.get(name, ()) if t in present]
        if not set(required) <= present or not (required or optional):
            con.execute(_SKIPPED_SQL, [name, severity])
            continue
        context: _Context = {"inputs": input_digest(con, (*required, *optional))}
        flags = {"has_" + t.replace(".", "_"): t in present for t in OPTIONAL.get(name, ())}
        if _run_one(con, (name, severity), sc, {**context, **flags}):
            continue
        if severity == "error":
            failed.append(name)
        else:
            warnings.append(f"check {name} failed")
    row = con.execute(_COUNT_SQL, [_NAMES]).fetchone()
    rows = int(row[0]) if row else 0
    return StepResult(
        row_counts={"meta.dq_result": rows}, warnings=warnings, flags=[], failed_checks=failed
    )
