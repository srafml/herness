"""Ops area ``closed_loop`` (impl 07 U07-31 … U07-34, U07-36; owner 07, R-08).

The only writer of ``recommendation``, ``decision_log`` (append-only) and ``outcome``; read-only
lookups on ``run``, ``task``, ``finding`` and ``memory_item``. Conventions C1 … C7 of impl 07
§3.5: with ``conn`` a function joins the caller's ``run_write`` transaction, else it reads on this
thread's connection or wraps its write in ``run_write(op=<function>)``; SQL is constant and
bound. The ``run`` and ``task`` reads run this area's own SQL mirroring ``runs.select_runs``,
``count_tasks`` and ``get_task``: an area never imports another area (``ops-areas-acyclic``).
Row types and plumbing live in the private sibling ``_closed_loop_rows``.
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Mapping, Sequence
from typing import cast

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation

from . import core
from ._closed_loop_rows import (
    ACCEPTED_SINCE,
    DEAD_TASKS,
    DECISION_CHECKS,
    DECISION_COLUMNS,
    DONE_RUNS,
    DUE,
    INSERT_DECISION,
    INSERT_OUTCOME,
    INSERT_REC,
    LATEST_DECISIONS,
    LATEST_OUTCOMES,
    MEMORY_KINDS,
    OUTCOME_CHECKS,
    OUTCOME_COLUMNS,
    PROMOTION,
    REC_CHECKS,
    REC_COLUMNS,
    REC_ID_RE,
    REC_MEMORY,
    RECENT_RUNS,
    ROWS_MAX,
    RUN_ID_RE,
    RUN_KINDS,
    RUN_RECS,
    SIMILARITY,
    TASK_ID_RE,
    TASK_SPEC,
    TREATED,
    TS_RE,
    Conn,
    DecisionRow,
    DueMeasurement,
    OutcomeRow,
    PromotionSource,
    RecommendationRow,
    SimilarityRow,
    bad_arg,
    check_row,
    is_int,
    load_typed,
    marks,
    query,
    valid_ids,
    write,
)

__all__ = [
    "DecisionRow",
    "DueMeasurement",
    "OutcomeRow",
    "PromotionSource",
    "RecommendationRow",
    "SimilarityRow",
    "accepted_since",
    "dead_task_count",
    "due_measurements",
    "insert_decision",
    "insert_outcome",
    "insert_recommendations",
    "latest_decisions",
    "latest_outcomes",
    "outcome_exists",
    "outcomes_for_similarity",
    "rec_memory_ids",
    "recent_done_runs",
    "recent_runs_with_recommendations",
    "run_findings_for_promotion",
    "run_recommendations",
    "task_spec",
    "treated_targets",
]


def _rec_from_row(row: Sequence[object]) -> RecommendationRow:
    rec = dict(zip(REC_COLUMNS, row, strict=True))
    rid = cast(str, rec["rec_id"])
    rec["numbers"] = load_typed(cast(str, rec["numbers"]), list, "recommendation.numbers", rid)
    basis = cast(str, rec["confidence_basis"])
    rec["confidence_basis"] = load_typed(basis, dict, "recommendation.confidence_basis", rid)
    ids = cast(str, rec["finding_ids"])
    rec["finding_ids"] = load_typed(ids, list, "recommendation.finding_ids", rid)
    return cast(RecommendationRow, rec)


def _outcome_from_row(row: Sequence[object]) -> OutcomeRow:
    out = dict(zip(OUTCOME_COLUMNS, row, strict=True))
    oid = cast(str, out["outcome_id"])
    out["details"] = load_typed(cast(str, out["details"]), dict, "outcome.details", oid) or {}
    return cast(OutcomeRow, out)


def _in_list(sql: str, rec_ids: Sequence[str], conn: Conn, op: str) -> list[sqlite3.Row]:
    """Rows of ``sql`` (holding ``{marks}``) for the valid, deduplicated rec ids; [] for none."""
    ids = valid_ids(rec_ids, REC_ID_RE, op)
    return query(sql.format(marks=marks(len(ids))), ids, conn, op) if ids else []


def _ts_ok(value: object) -> bool:
    return isinstance(value, str) and TS_RE.fullmatch(value) is not None


# --- U07-31 recommendations -----------------------------------------------------------------


def insert_recommendations(rows: Sequence[RecommendationRow], *, conn: Conn = None) -> None:
    """Insert 1 … 50 recommendations in the caller's transaction (U07-31; conn required)."""
    op = "insert_recommendations"
    if conn is None:
        msg = f"{op} requires the caller's transaction"
        raise ConfigError(msg)
    if not 1 <= len(rows) <= ROWS_MAX:
        msg = f"{op}: invalid rows"
        raise SchemaViolation(msg)
    for row in rows:  # validate every row before the first INSERT
        check_row(cast(Mapping[str, object], row), REC_CHECKS, op)
    for row in rows:
        values: dict[str, object] = dict(row)
        for name in ("numbers", "confidence_basis", "finding_ids"):
            values[name] = core.dump_json(values[name], field=name)
        conn.execute(INSERT_REC, [values[c] for c in REC_COLUMNS])


def run_recommendations(run_id: str, *, conn: Conn = None) -> list[RecommendationRow]:
    """The run's recommendations ordered by ``rec_id`` (U07-31, index ``recommendation_run``)."""
    op = "run_recommendations"
    valid_ids([run_id], RUN_ID_RE, op)
    return [_rec_from_row(r) for r in query(RUN_RECS, [run_id], conn, op)]


# --- U07-32 decisions (append-only) ---------------------------------------------------------


def insert_decision(row: DecisionRow, *, conn: Conn = None) -> None:
    """Append one ``decision_log`` row; an unknown ``rec_id`` fails the foreign key (U07-32)."""
    op = "insert_decision"
    check_row(cast(Mapping[str, object], row), DECISION_CHECKS, op)
    params = [row[c] for c in DECISION_COLUMNS]
    write(lambda c: c.execute(INSERT_DECISION, params), conn, op)


def latest_decisions(rec_ids: Sequence[str], *, conn: Conn = None) -> dict[str, DecisionRow]:
    """Each rec's decision with the highest (``decided_at``, ``rowid``) (U07-32)."""
    rows = _in_list(LATEST_DECISIONS, rec_ids, conn, "latest_decisions")
    return {str(r[0]): cast(DecisionRow, dict(zip(DECISION_COLUMNS, r, strict=True))) for r in rows}


# --- U07-33 outcomes ------------------------------------------------------------------------


def insert_outcome(row: OutcomeRow, *, conn: Conn = None) -> bool:
    """``INSERT OR IGNORE`` one outcome; False when (rec_id, measurement) exists (U07-33)."""
    op = "insert_outcome"
    check_row(cast(Mapping[str, object], row), OUTCOME_CHECKS, op)
    values: dict[str, object] = dict(row)
    values["details"] = core.dump_json(values["details"], field="details")
    params = [values[c] for c in OUTCOME_COLUMNS]
    return bool(write(lambda c: c.execute(INSERT_OUTCOME, params).rowcount == 1, conn, op))


def outcome_exists(rec_id: str, measurement: int, *, conn: Conn = None) -> bool:
    """Whether measurement 1 or 2 of ``rec_id`` has an outcome row (U07-33)."""
    op = "outcome_exists"
    valid_ids([rec_id], REC_ID_RE, op)
    if measurement not in (1, 2) or isinstance(measurement, bool):
        raise bad_arg(op)
    sql = "SELECT 1 FROM outcome WHERE rec_id = ? AND measurement = ?"
    return bool(query(sql, [rec_id, measurement], conn, op))


def _weeks_ok(pair: object) -> bool:
    return isinstance(pair, tuple) and len(pair) == 2 and all(is_int(w, 1) for w in pair)  # noqa: PLR2004 - a pair


def due_measurements(
    *,
    now: str,
    due_weeks: Mapping[str, tuple[int, int]],
    default_due_weeks: tuple[int, int],
    conn: Conn = None,
) -> list[DueMeasurement]:
    """Due, unmeasured pairs of accepted recs with a metric, oldest due first (U07-33).

    Due date of measurement m = the ``effective_at`` date + 7 * weeks[m - 1] days (00:00 UTC);
    the weeks come from the caller (U07-83 ``Windows.due``), so no statistics rule lives here."""
    op = "due_measurements"
    pairs = [default_due_weeks, *due_weeks.values()]
    if not (_ts_ok(now) and all(_weeks_ok(p) for p in pairs)):
        raise bad_arg(op)
    due: list[tuple[str, DueMeasurement]] = []
    for rid, metric, eff, ttype, tid, delta, kind, *measured in query(DUE, (), conn, op):
        start = clock.parse_utc(eff).date()
        weeks = due_weeks.get(metric, default_due_weeks)
        for m in (1, 2):
            day = start + datetime.timedelta(days=7 * weeks[m - 1])
            when = clock.format_utc(datetime.datetime.combine(day, datetime.time(), datetime.UTC))
            if when <= now and not measured[m - 1]:
                pair = DueMeasurement(
                    rec_id=rid, measurement=m, metric=metric, effective_at=eff,
                    target_type=ttype, target_id=tid, expected_delta=delta, kind=kind,
                )  # fmt: skip
                due.append((when, pair))
    due.sort(key=lambda d: (d[0], d[1]["rec_id"], d[1]["measurement"]))
    return [pair for _, pair in due]


def latest_outcomes(rec_ids: Sequence[str], *, conn: Conn = None) -> dict[str, OutcomeRow]:
    """Each rec's latest outcome by (``measurement`` DESC, ``measured_at`` DESC) (U07-33)."""
    rows = _in_list(LATEST_OUTCOMES, rec_ids, conn, "latest_outcomes")
    return {str(r[1]): _outcome_from_row(r) for r in rows}


def treated_targets(
    *, metric: str, start: str, end: str, conn: Conn = None
) -> set[tuple[str, str]]:
    """Targets with an accepted rec on ``metric`` effective in [start, end) (U07-33, TH07-18)."""
    op = "treated_targets"
    if not (metric and _ts_ok(start) and _ts_ok(end)):
        raise bad_arg(op)
    return {(str(r[0]), str(r[1])) for r in query(TREATED, [metric, start, end], conn, op)}


# --- U07-34 prior context and outcome feedback ----------------------------------------------


def recent_runs_with_recommendations(
    run_kind: str, *, limit: int, exclude_run_id: str, conn: Conn = None
) -> list[str]:
    """Up to ``limit`` runs of ``run_kind`` that have recommendations, newest first (U07-34)."""
    op = "recent_runs_with_recommendations"
    ok = run_kind in RUN_KINDS and is_int(limit, 0) and limit <= 10  # noqa: PLR2004 - bound
    if not (ok and RUN_ID_RE.fullmatch(exclude_run_id)):
        raise bad_arg(op)
    return [str(r[0]) for r in query(RECENT_RUNS, [run_kind, exclude_run_id, limit], conn, op)]


def accepted_since(since: str, *, conn: Conn = None) -> list[RecommendationRow]:
    """Recs whose latest decision is accepted at or after ``since``, newest first (U07-34)."""
    op = "accepted_since"
    if not _ts_ok(since):
        raise bad_arg(op)
    return [_rec_from_row(r) for r in query(ACCEPTED_SINCE, [since], conn, op)]


def outcomes_for_similarity(*, conn: Conn = None) -> list[SimilarityRow]:
    """Recs joined to their latest outcome, newest ``measured_at`` first, ≤ 5,000 (U07-34)."""
    keys = SimilarityRow.__annotations__
    rows = query(SIMILARITY, (), conn, "outcomes_for_similarity")
    return [cast(SimilarityRow, dict(zip(keys, r, strict=True))) for r in rows]


def rec_memory_ids(rec_ids: Sequence[str], *, kinds: Sequence[str], conn: Conn = None) -> list[str]:
    """Memory ids of the given kinds citing ``data.rec_id``, oldest first (U07-34)."""
    op = "rec_memory_ids"
    if not (kinds and set(kinds) <= MEMORY_KINDS):  # a subset; repeats collapse
        raise bad_arg(op)
    ids = valid_ids(rec_ids, REC_ID_RE, op)
    if not ids:
        return []
    kinds = list(dict.fromkeys(kinds))
    sql = REC_MEMORY.format(ids=marks(len(ids)), kinds=marks(len(kinds)))
    return [str(r[0]) for r in query(sql, [*ids, *kinds], conn, op)]


# --- U07-36 run-end, promotion and maintenance lookups --------------------------------------


def dead_task_count(run_id: str, *, conn: Conn = None) -> int:
    """Tasks of ``run_id`` with status ``dead`` (U07-36; mirrors ``runs.count_tasks``)."""
    op = "dead_task_count"
    valid_ids([run_id], RUN_ID_RE, op)
    return int(query(DEAD_TASKS, [run_id], conn, op)[0][0])


def run_findings_for_promotion(run_id: str, *, conn: Conn = None) -> list[PromotionSource]:
    """Verified findings plus rejected ones whose verification failed, by id (U07-36)."""
    op = "run_findings_for_promotion"
    valid_ids([run_id], RUN_ID_RE, op)
    return [
        PromotionSource(
            finding_id=fid,
            status=status,
            query_ids=load_typed(qids, list, "finding.query_ids", fid) or [],
            task_id=task_id,
            verification=load_typed(verification, dict, "finding.verification", fid),
        )
        for fid, status, qids, task_id, verification in query(PROMOTION, [run_id], conn, op)
    ]


def task_spec(task_id: str, *, conn: Conn = None) -> dict[str, JsonValue] | None:
    """The parsed ``task.spec`` JSON object, None for an unknown task (U07-36; ``get_task``)."""
    op = "task_spec"
    valid_ids([task_id], TASK_ID_RE, op)
    rows = query(TASK_SPEC, [task_id], conn, op)
    return load_typed(rows[0][0], dict, "task.spec", task_id) if rows else None


def recent_done_runs(since: str, *, conn: Conn = None) -> list[str]:
    """Runs ``done`` with ``finished_at >= since``, by (finished_at, run_id) (U07-36)."""
    op = "recent_done_runs"
    if not _ts_ok(since):
        raise bad_arg(op)
    return [str(r[0]) for r in query(DONE_RUNS, [since], conn, op)]
