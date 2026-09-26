"""Ops area ``ui_reads`` (impl 09 U09-52, R-08, R-68): reads for the renderer, dashboard and CLI.

Public names carry ``ui_`` / ``Ui`` (flat ``herness.store.ops`` namespace). Rows are UI
projections with spec 02 column names, JSON columns parsed by ``core.load_json``, timestamps and
decimal TEXT kept as stored. Read-only; SQL is constant text plus at most an ``IN (?, ...)`` list
sized from the chunk (TH09-13); optional filters bind as ``(? IS NULL OR col = ?)``. Ids are
de-duplicated and chunked by 500; limits clamp to ``1..500`` (``source_health``: 200 rows).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Final, TypedDict, cast

from herness.core import time as clock

from . import core

_CHUNK: Final = 500
_MAX_LIMIT: Final = 500
_HEALTH_CAP: Final = 200

_Opt = str | None
_OptF = float | None

# Functional TypedDicts keep the eleven row shapes inside the 350-line budget (module map).
# fmt: off
UiRunRow = TypedDict("UiRunRow", {  # noqa: UP013 - compact form, see above
    "run_id": str, "kind": str, "depth": str, "status": str, "started_at": str,
    "finished_at": _Opt, "token_usage": dict[str, object], "cost_usd": _Opt,
})
UiTaskRow = TypedDict("UiTaskRow", {  # noqa: UP013 - objective = json_extract(spec, '$.objective')
    "task_id": str, "role": str, "status": str, "attempts": int, "objective": _Opt,
    "last_error": _Opt,
})
UiEvidenceRow = TypedDict("UiEvidenceRow", {  # noqa: UP013 - compact form
    "query_id": str, "run_id": _Opt, "build_id": str, "sql": str, "result_hash": str,
    "params": object, "result_sample": object, "row_count": int, "duration_ms": int,
    "executed_at": str,
})
UiRecommendationRow = TypedDict("UiRecommendationRow", {  # noqa: UP013 - compact form
    "rec_id": str, "run_id": str, "target_type": str, "target_id": str, "summary": str,
    "kind": str, "numbers": object, "expected_metric": _Opt, "expected_delta": _OptF,
    "confidence": _OptF, "expected_usd": _Opt, "confidence_basis": object,
    "finding_ids": object, "created_at": str,
})
UiDecisionRow = TypedDict("UiDecisionRow", {  # noqa: UP013 - one decision_log row
    "rec_id": str, "decision": str, "reason": _Opt, "decided_by": str, "decided_at": str,
    "effective_at": str,
})
UiOutcomeRow = TypedDict("UiOutcomeRow", {  # noqa: UP013 - compact form
    "outcome_id": str, "rec_id": str, "measurement": int, "measured_at": str, "metric": str,
    "baseline": _OptF, "actual": _OptF, "delta": _OptF, "query_id": _Opt, "verdict": str,
    "details": object,
})
UiFindingRow = TypedDict("UiFindingRow", {  # noqa: UP013 - claim stays text (not JSON)
    "finding_id": str, "run_id": str, "task_id": str, "author_role": str, "claim": str,
    "entity_type": _Opt, "entity_id": _Opt, "numbers": object, "query_ids": object,
    "confidence": _OptF, "status": str, "created_at": str,
})
UiMemoryItemRow = TypedDict("UiMemoryItemRow", {  # noqa: UP013 - compact form
    "memory_id": str, "layer": str, "kind": str, "content": str, "data": object,
    "provenance": object, "confidence": _OptF, "status": str, "created_at": str,
    "expires_at": _Opt, "last_used_at": _Opt, "use_count": int,
})
UiResilienceEventRow = TypedDict("UiResilienceEventRow", {  # noqa: UP013 - compact form
    "event_id": str, "ts": str, "kind": str, "component": str, "target": _Opt,
    "run_id": _Opt, "job_id": _Opt, "task_id": _Opt, "detail": object,
})
UiSourceHealthRow = TypedDict("UiSourceHealthRow", {  # noqa: UP013 - compact form
    "source": str, "state": str, "failures": int, "trips": int, "opened_at": _Opt,
    "last_error": _Opt, "updated_at": str,
})
UiJobLiteRow = TypedDict("UiJobLiteRow", {  # noqa: UP013 - no payload or result columns
    "job_id": str, "kind": str, "status": str, "attempts": int, "max_attempts": int,
    "last_error": object, "created_at": str, "started_at": _Opt, "finished_at": _Opt,
})
# fmt: on

_REC: Final = (
    "SELECT rec_id, run_id, target_type, target_id, summary, kind, numbers, expected_metric,"
    " expected_delta, confidence, expected_usd, confidence_basis, finding_ids, created_at"
    " FROM recommendation"
)
_REC_JSON: Final = (
    "recommendation.numbers",
    "recommendation.confidence_basis",
    "recommendation.finding_ids",
)
_FINDING: Final = (
    "SELECT finding_id, run_id, task_id, author_role, claim, entity_type, entity_id, numbers,"
    " query_ids, confidence, status, created_at FROM finding"
)
_FINDING_JSON: Final = ("finding.numbers", "finding.query_ids")
_JOB: Final = (
    "SELECT job_id, kind, status, attempts, max_attempts, last_error, created_at, started_at,"
    " finished_at FROM job"
)


def _parsed(row: sqlite3.Row, *json_cols: str) -> dict[str, object]:
    """The row as a dict; each ``table.column`` of ``json_cols`` parsed by ``core.load_json``."""
    out: dict[str, object] = {key: row[key] for key in row.keys()}  # noqa: SIM118 - sqlite3.Row
    for field in json_cols:
        col = field.partition(".")[2]
        out[col] = core.load_json(row[col], field=field)
    return out


def _select[R](rt: type[R], sql: str, params: Sequence[object], limit: int, *js: str) -> list[R]:
    """``sql LIMIT ?`` with ``limit`` clamped to ``1..500``; rows parsed, typed as ``rt``."""
    n = max(1, min(int(limit), _MAX_LIMIT))
    rows = core.read_all(sql + " LIMIT ?", [*params, n], max_rows=n)
    return cast(list[R], [_parsed(r, *js) for r in rows])


def _chunked(
    head: str, ids: Collection[str], tail: str = "", extra: Sequence[object] = ()
) -> list[sqlite3.Row]:
    """Run ``head (?, ...) tail`` per chunk of at most 500 distinct ids; ``extra`` binds last."""
    unique = sorted(set(ids))
    rows: list[sqlite3.Row] = []
    for start in range(0, len(unique), _CHUNK):
        chunk = unique[start : start + _CHUNK]
        marks = "(" + ",".join("?" * len(chunk)) + ")"
        rows.extend(core.read_all(head + marks + tail, [*chunk, *extra]))
    return rows


def ui_evidence_ids_present(ids: Collection[str]) -> set[str]:
    """The ``ids`` that have an ops ``evidence`` row."""
    rows = _chunked("SELECT query_id FROM evidence WHERE query_id IN ", ids)
    return {str(r["query_id"]) for r in rows}


def ui_get_evidence_rows(ids: Collection[str]) -> dict[str, UiEvidenceRow]:
    """``{query_id: row}`` for the ``ids`` present; ``params`` and ``result_sample`` parsed."""
    rows = _chunked(
        "SELECT query_id, run_id, build_id, sql, result_hash, params, result_sample, row_count,"
        " duration_ms, executed_at FROM evidence WHERE query_id IN ",
        ids,
    )
    parsed = (_parsed(r, "evidence.params", "evidence.result_sample") for r in rows)
    return {str(p["query_id"]): cast(UiEvidenceRow, p) for p in parsed}


def ui_run_rec_ids(run_id: str) -> set[str]:
    """Ids of the recommendations recorded by ``run_id``."""
    rows = core.read_all("SELECT rec_id FROM recommendation WHERE run_id = ?", (run_id,))
    return {str(r["rec_id"]) for r in rows}


def ui_get_recommendation(rec_id: str) -> UiRecommendationRow | None:
    """One recommendation by primary key, or None."""
    row = core.read_one(_REC + " WHERE rec_id = ?", (rec_id,))
    return None if row is None else cast(UiRecommendationRow, _parsed(row, *_REC_JSON))


def ui_finding_ids_with_status(ids: Collection[str], status: str) -> set[str]:
    """The ``ids`` whose finding has ``status``."""
    head = "SELECT finding_id FROM finding WHERE finding_id IN "
    rows = _chunked(head, ids, " AND status = ?", (status,))
    return {str(r["finding_id"]) for r in rows}


def ui_finding_query_ids(ids: Collection[str]) -> dict[str, list[str]]:
    """``{finding_id: query_ids}`` for the ``ids`` present (``finding.query_ids`` parsed)."""
    rows = _chunked("SELECT finding_id, query_ids FROM finding WHERE finding_id IN ", ids)
    out: dict[str, list[str]] = {}
    for row in rows:
        value = core.load_json(row["query_ids"], field="finding.query_ids")
        out[str(row["finding_id"])] = [str(q) for q in value] if isinstance(value, list) else []
    return out


def ui_finding_status_counts(run_id: str) -> dict[str, int]:
    """``{status: count}`` of the run's findings."""
    sql = "SELECT status, count(*) AS n FROM finding WHERE run_id = ? GROUP BY status"
    return {str(r["status"]): int(r["n"]) for r in core.read_all(sql, (run_id,))}


def ui_task_status_counts(run_id: str) -> list[tuple[str, str, int]]:
    """``(role, status, count)`` of the run's tasks, ordered by role then status."""
    sql = "SELECT role, status, count(*) AS n FROM task WHERE run_id = ? GROUP BY role, status"
    rows = core.read_all(sql + " ORDER BY role, status", (run_id,))
    return [(str(r["role"]), str(r["status"]), int(r["n"])) for r in rows]


def ui_list_tasks(run_id: str, *, status: str | None = None, limit: int = 500) -> list[UiTaskRow]:
    """Task projection of ``run_id`` ordered by ``created_at``; ``limit`` ≤ 500."""
    sql = (
        "SELECT task_id, role, status, attempts, json_extract(spec, '$.objective') AS objective,"
        " last_error FROM task WHERE run_id = ? AND (? IS NULL OR status = ?)"
        " ORDER BY created_at, task_id"
    )
    return _select(UiTaskRow, sql, (run_id, status, status), limit)


def ui_list_runs(
    *, kind: str | None = None, status: str | None = None, limit: int = 50
) -> list[UiRunRow]:
    """Run projection, newest ``started_at`` first; ``token_usage`` parsed; ``limit`` ≤ 500."""
    sql = (
        "SELECT run_id, kind, depth, status, started_at, finished_at, token_usage, cost_usd"
        " FROM run WHERE (? IS NULL OR kind = ?) AND (? IS NULL OR status = ?)"
        " ORDER BY started_at DESC, run_id DESC"
    )
    return _select(UiRunRow, sql, (kind, kind, status, status), limit, "run.token_usage")


def ui_list_recommendations(
    *,
    run_id: str | None = None,
    kind: str | None = None,
    target_id: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = 500,
) -> list[UiRecommendationRow]:
    """Recommendations matching every given filter (``created_from`` ≤ ``created_at`` ≤
    ``created_to``, both inclusive), newest first; ``limit`` ≤ 500."""
    lo = None if created_from is None else clock.format_utc(created_from)
    hi = None if created_to is None else clock.format_utc(created_to)
    sql = (
        _REC + " WHERE (? IS NULL OR run_id = ?) AND (? IS NULL OR kind = ?)"
        " AND (? IS NULL OR target_id = ?) AND (? IS NULL OR created_at >= ?)"
        " AND (? IS NULL OR created_at <= ?) ORDER BY created_at DESC, rec_id"
    )
    params = (run_id, run_id, kind, kind, target_id, target_id, lo, lo, hi, hi)
    return _select(UiRecommendationRow, sql, params, limit, *_REC_JSON)


def ui_list_findings_for_entity(
    entity_type: str, entity_id: str, *, status: str = "verified", limit: int = 20
) -> list[UiFindingRow]:
    """Findings about one entity with ``status``, newest first; ``limit`` ≤ 500."""
    sql = (
        _FINDING + " WHERE entity_type = ? AND entity_id = ? AND status = ?"
        " ORDER BY created_at DESC, finding_id DESC"
    )
    return _select(UiFindingRow, sql, (entity_type, entity_id, status), limit, *_FINDING_JSON)


def ui_list_run_findings(
    run_id: str, *, status: str = "verified", limit: int = 200
) -> list[UiFindingRow]:
    """The run's findings with ``status`` by ``created_at``, ``finding_id`` (R-49); ≤ 500."""
    sql = _FINDING + " WHERE run_id = ? AND status = ? ORDER BY created_at, finding_id"
    return _select(UiFindingRow, sql, (run_id, status), limit, *_FINDING_JSON)


def ui_jobs_for_run(run_id: str, *, limit: int = 20) -> list[UiJobLiteRow]:
    """Jobs whose payload names ``run_id`` at ``$.run_id`` or ``$.request.run_id``, newest first."""
    sql = (
        _JOB + " WHERE json_extract(payload, '$.run_id') = ?"
        " OR json_extract(payload, '$.request.run_id') = ?"
        " ORDER BY created_at DESC, job_id DESC"
    )
    return _select(UiJobLiteRow, sql, (run_id, run_id), limit, "job.last_error")


def ui_latest_decisions(rec_ids: Collection[str]) -> dict[str, UiDecisionRow]:
    """``{rec_id: latest decision_log row}`` by ``decided_at`` (later insert wins a tie)."""
    rows = _chunked(
        "SELECT rec_id, decision, reason, decided_by, decided_at, effective_at FROM"
        " (SELECT *, row_number() OVER (PARTITION BY rec_id ORDER BY decided_at DESC,"
        " rowid DESC) AS rn FROM decision_log WHERE rec_id IN ",
        rec_ids,
        ") WHERE rn = 1",
    )
    return {str(r["rec_id"]): cast(UiDecisionRow, _parsed(r)) for r in rows}


def ui_list_outcomes(rec_ids: Collection[str]) -> dict[str, list[UiOutcomeRow]]:
    """``{rec_id: outcome rows by measurement}`` for the ids that have outcomes."""
    rows = _chunked(
        "SELECT outcome_id, rec_id, measurement, measured_at, metric, baseline, actual, delta,"
        " query_id, verdict, details FROM outcome WHERE rec_id IN ",
        rec_ids,
        " ORDER BY rec_id, measurement",
    )
    out: dict[str, list[UiOutcomeRow]] = {}
    for row in rows:
        parsed = cast(UiOutcomeRow, _parsed(row, "outcome.details"))
        out.setdefault(parsed["rec_id"], []).append(parsed)
    return out


def ui_list_memory_items(
    *, layer: str | None = None, status: str | None = None, limit: int = 50
) -> list[UiMemoryItemRow]:
    """Memory items matching the filters, newest first; ``limit`` ≤ 500."""
    sql = (
        "SELECT memory_id, layer, kind, content, data, provenance, confidence, status,"
        " created_at, expires_at, last_used_at, use_count FROM memory_item"
        " WHERE (? IS NULL OR layer = ?) AND (? IS NULL OR status = ?)"
        " ORDER BY created_at DESC, memory_id DESC"
    )
    args = (layer, layer, status, status)
    return _select(UiMemoryItemRow, sql, args, limit, "memory_item.data", "memory_item.provenance")


def ui_list_resilience_events(
    *, run_id: str | None = None, job_id: str | None = None, limit: int = 200
) -> list[UiResilienceEventRow]:
    """Resilience events matching the filters, newest ``ts`` first; ``limit`` ≤ 500."""
    sql = (
        "SELECT event_id, ts, kind, component, target, run_id, job_id, task_id, detail"
        " FROM resilience_event WHERE (? IS NULL OR run_id = ?) AND (? IS NULL OR job_id = ?)"
        " ORDER BY ts DESC, event_id DESC"
    )
    params = (run_id, run_id, job_id, job_id)
    return _select(UiResilienceEventRow, sql, params, limit, "resilience_event.detail")


def ui_list_source_health() -> list[UiSourceHealthRow]:
    """All ``source_health`` rows by source (at most 200)."""
    sql = "SELECT source, state, failures, trips, opened_at, last_error, updated_at"
    return _select(UiSourceHealthRow, sql + " FROM source_health ORDER BY source", (), _HEALTH_CAP)


def ui_job_exists(job_id: str) -> bool:
    """True when a ``job`` row with ``job_id`` exists."""
    return core.read_one("SELECT 1 FROM job WHERE job_id = ?", (job_id,)) is not None


def ui_job_status_counts() -> dict[str, int]:
    """``{status: count}`` over all jobs."""
    rows = core.read_all("SELECT status, count(*) AS n FROM job GROUP BY status")
    return {str(r["status"]): int(r["n"]) for r in rows}


def ui_oldest_queued_age_s(now: datetime) -> float | None:
    """Seconds since the oldest ``queued`` job's ``created_at`` (never negative), or None."""
    row = core.read_one("SELECT min(created_at) AS t FROM job WHERE status = 'queued'")
    if row is None or row["t"] is None:
        return None
    return max(0.0, (now - clock.parse_utc(row["t"])).total_seconds())


def ui_failed_jobs_since(since: datetime, *, limit: int = 50) -> list[UiJobLiteRow]:
    """Failed jobs with ``finished_at >= since``, newest first; ``limit`` ≤ 500."""
    sql = (
        _JOB + " WHERE status = 'failed' AND finished_at >= ?"
        " ORDER BY finished_at DESC, job_id DESC"
    )
    return _select(UiJobLiteRow, sql, (clock.format_utc(since),), limit, "job.last_error")
