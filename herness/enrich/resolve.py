"""Resolve stage: resolution frame, escalation queue, `enrich.decision` (impl 03 §3.12).

T03-19 adds `resolve_frame` (U03-79), which registers the inputs of
``sql/resolve_decisions.sql`` (U03-78) and runs it into the temp table ``enrich_resolved``,
and `escalation_queue` (U03-80). The ops database is never attached to DuckDB: pending
``label_check`` items reach it only as a registered Arrow table (impl 02 DD02-02).

Deviation from the literal U03-79 signature (recorded in the T03-19 report): `chain_after`
(U03-71) needs `DecidersSettings` since the R-76 settings split, so `resolve_frame` takes a
keyword-only `deciders: DecidersSettings` as well.

T03-20 adds `select_spot_checks` (U03-81), `decision_wide_sql` (U03-82) and the stage entry
`run_resolve` (U03-83). Logs, metrics and review payloads carry counts, hashes and ids only
(TH03-03).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources
from itertools import groupby
from typing import Final, Protocol

import duckdb
import pyarrow as pa

from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.logging import get_logger
from herness.core.resilience.metrics import record_gauge
from herness.core.types import Question, QuestionSet
from herness.enrich._spot_checks import select_spot_checks
from herness.enrich.cache import DecisionCache
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.decide import chain_after
from herness.enrich.labels import LabelStore, sync_label_checks
from herness.enrich.questions import PAIR_QUESTIONS, question_fingerprint
from herness.enrich.review_items import create_if_absent, iter_review_items, open_label_counts
from herness.enrich.settings import DecidersSettings, DecisionsConfig

__all__ = [
    "QueueItem", "decision_wide_sql", "escalation_queue", "resolve_frame", "run_resolve",
    "select_spot_checks",
]  # fmt: skip

_SQL_FILE: Final = "sql/resolve_decisions.sql"
_SCHEMA_ERRORS: Final = (duckdb.CatalogException, duckdb.BinderException)
_S, _LIST = pa.string(), pa.list_(pa.string())
_QMETA_SCHEMA: Final = pa.schema([
    ("question", _S), ("fingerprint", _S), ("qtype", _S), ("threshold", pa.float64()),
    ("scoring_use", pa.bool_()), ("primary", _S), ("chain", _LIST), ("applies_to", _LIST),
    ("labels", _LIST),
])  # fmt: skip
_VERSIONS_SCHEMA: Final = pa.schema([("decider", _S), ("decider_version", _S)])
_PENDING_SCHEMA: Final = pa.schema([("content_hash", _S), ("question", _S)])
_SCORE_LABELS: Final = ("0", "1", "2", "3")

_QUEUE_SQL: Final = """
WITH queued AS (
    SELECT r.record_id, r.entity, r.content_hash, list_sort(list(r.question)) AS question_ids,
        bool_or(r.scoring_use) AS max_scoring, max(r.opened_at) AS opened_at
    FROM enrich_resolved AS r
    WHERE r.status = 'queue'{exclude}
    GROUP BY r.record_id, r.entity, r.content_hash
    ORDER BY max_scoring DESC, opened_at DESC NULLS LAST, record_id, entity
    LIMIT $max_records
)
SELECT q.record_id, q.entity, q.content_hash, t.text, q.question_ids
FROM queued AS q
JOIN enrich.text_redacted AS t ON t.record_id = q.record_id AND t.entity = q.entity
ORDER BY q.max_scoring DESC, q.opened_at DESC NULLS LAST, q.record_id, q.entity
"""
_EXCLUDE_SQL: Final = """ AND NOT EXISTS (
        SELECT 1 FROM enrich_cand AS c
        WHERE c.content_hash = r.content_hash AND c.question = r.question
            AND list_contains(CAST($exclude AS VARCHAR[]), c.decider))"""

_log = get_logger("enrich.resolve")
_QID_RE: Final = re.compile(r"[a-z][a-z0-9_]{1,40}")  # U03-02 pattern, re-checked (TH03-19)
# U03-83 names ("purpose", "question_set_version", "question", "content_hash") plus scope
# {question_set_version}; `create_if_absent` (U03-148) appends scope keys to match_keys without
# deduplication, which impl 02 rejects, so the version is matched through `scope` only.
_SPOT_KEYS: Final = ("purpose", "question", "content_hash")
_INSERT_SQL: Final = """
INSERT INTO enrich.decision
SELECT record_id, question, answer, probability, agreement, decider, decider_version, $qsv,
    content_hash, decided_at, escalated, review_status
FROM enrich_resolved WHERE status = 'final'
"""
_STATS_SQL: Final = """
SELECT entity, count(*) FILTER (WHERE status = 'final'),
    count(*) FILTER (WHERE status = 'final' AND escalated),
    count(*) FILTER (WHERE status <> 'out_of_scope')
FROM enrich_resolved GROUP BY entity ORDER BY entity
"""


class _Report(Protocol):
    """Counters the stage mutates.

    T03-xx pipeline: retype to StageReport (U03-142, herness.enrich.pipeline).
    """

    decided: int
    escalated: int


@dataclass(frozen=True, slots=True)
class QueueItem:
    """One record that needs a teacher or escalation answer (U03-80)."""

    record_id: str
    entity: str
    content_hash: str
    text: str
    question_ids: tuple[str, ...]


def _duck_error(exc: duckdb.Error, *, where: str) -> SchemaViolation:
    """SchemaViolation naming only a catalog/binder message or the class (never row values)."""
    reason = str(exc).splitlines()[0] if isinstance(exc, _SCHEMA_ERRORS) else type(exc).__name__
    return SchemaViolation(f"{where}: {reason}")


def _labels(q: Question) -> list[str]:
    if q.type == "bool":
        return ["true", "false"]
    if q.type == "score":
        return list(_SCORE_LABELS)
    return sorted(q.options or {})


def _qmeta_table(
    qs: QuestionSet,
    *,
    cfg: DecisionsConfig,
    deciders: DecidersSettings,
    primaries: Mapping[str, str],
) -> pa.Table:
    """The ``qmeta`` relation of U03-78: one row per non-pair question of ``qs``."""
    rows: list[dict[str, object]] = []
    for q in qs.questions:
        if q.id in PAIR_QUESTIONS:
            continue
        primary = primaries.get(q.id)
        if primary is None:
            msg = f"resolve: no primary decider for question {q.id}"
            raise ConfigError(msg, question_set_version=qs.version)
        rows.append({
            "question": q.id, "fingerprint": q.fingerprint or question_fingerprint(q),
            "qtype": q.type, "threshold": q.threshold, "scoring_use": q.scoring_use,
            "primary": primary, "chain": list(chain_after(primary, cfg=cfg, deciders=deciders)),
            "applies_to": list(q.applies_to), "labels": _labels(q),
        })  # fmt: skip
    return pa.Table.from_pylist(rows, schema=_QMETA_SCHEMA)


def _pending_items_table(qsv: str) -> pa.Table:
    """Pending ``label_check`` items of ``qsv`` as (content_hash, question) (U03-79 step 2)."""
    rows = []
    for item in iter_review_items(
        "label_check", "pending", payload_match={"question_set_version": qsv}
    ):
        content_hash, question = item.payload.get("content_hash"), item.payload.get("question")
        if isinstance(content_hash, str) and isinstance(question, str):
            rows.append({"content_hash": content_hash, "question": question})
    return pa.Table.from_pylist(rows, schema=_PENDING_SCHEMA)


def _run_resolve_sql(wh: duckdb.DuckDBPyConnection, *, bootstrap_since: datetime) -> None:
    """Execute ``resolve_decisions.sql`` over the already registered views (U03-78)."""
    sql = resources.files("herness.enrich").joinpath(_SQL_FILE).read_text("utf-8")
    try:
        wh.execute(sql, {"bootstrap_since": bootstrap_since})
    except duckdb.Error as exc:
        raise _duck_error(exc, where="resolve_decisions") from exc


def resolve_frame(  # noqa: PLR0913 - U03-79's signature is binding (plus `deciders`, see module doc)
    wh: duckdb.DuckDBPyConnection,
    *,
    qs: QuestionSet,
    cfg: DecisionsConfig,
    deciders: DecidersSettings,
    cache: DecisionCache,
    labels: LabelStore,
    calibration: CalibrationStore,
    primaries: Mapping[str, str],
    versions: Mapping[str, str],
    now: datetime,
) -> None:
    """Register the inputs and run ``resolve_decisions.sql`` into ``enrich_resolved`` (U03-79).

    ``versions`` maps each decider (and ``ensemble`` when deep) to its current version.
    Raises SchemaViolation from the SQL; ops ``StoreBusy`` propagates.
    """
    pending = _pending_items_table(qs.version)  # ops read first: nothing registered on failure
    entries = sorted(versions.items())
    relations: dict[str, pa.Table] = {
        "human_latest": labels.latest_human(),
        "calib": calibration.as_table(entries, qs.version),
        "qmeta": _qmeta_table(qs, cfg=cfg, deciders=deciders, primaries=primaries),
        "versions": pa.Table.from_pylist(
            [{"decider": d, "decider_version": v} for d, v in entries], schema=_VERSIONS_SCHEMA
        ),
        "pending_items": pending,
    }
    registered: list[str] = []
    try:
        cache.register(wh, "cache_rows")
        registered.append("cache_rows")
        for name, table in relations.items():
            wh.register(name, table)
            registered.append(name)
        since = now - timedelta(days=cfg.escalation.bootstrap_window_days)
        _run_resolve_sql(wh, bootstrap_since=since)
    finally:
        for name in registered:
            wh.unregister(name)


def escalation_queue(
    wh: duckdb.DuckDBPyConnection,
    *,
    max_records: int,
    exclude_deciders: frozenset[str] = frozenset(),
) -> list[QueueItem]:
    """Ordered, capped records with queued questions (U03-80, design 03 §5.7 step 4).

    Order: any ``scoring_use`` question first, then newest ``opened_at`` (NULLs last), then
    ``record_id``. A queued (record, question) is left out when a decider in
    ``exclude_deciders`` already has a current candidate row for it. Raises ConfigError when
    ``max_records`` is negative, SchemaViolation on a DuckDB error.
    """
    if max_records < 0:
        msg = "escalation_queue: max_records must be >= 0"
        raise ConfigError(msg)
    params: dict[str, object] = {"max_records": max_records}
    if exclude_deciders:  # read enrich_cand only when something is excluded
        params["exclude"] = sorted(exclude_deciders)
    sql = _QUEUE_SQL.format(exclude=_EXCLUDE_SQL if exclude_deciders else "")
    try:
        rows = wh.execute(sql, params).fetchall()
    except duckdb.Error as exc:
        raise _duck_error(exc, where="escalation_queue") from exc
    return [QueueItem(rid, entity, ch, text, tuple(qids)) for rid, entity, ch, text, qids in rows]


def decision_wide_sql(qs: QuestionSet) -> str:
    """DDL of the ``enrich.decision_wide`` view over the non-pair questions (U03-82).

    Each id is re-checked against the U03-02 pattern before it is interpolated, as a quoted
    identifier and a quoted literal (TH03-19). Raises ConfigError for an id that fails it.
    """
    cols = ["record_id"]
    for q in qs.questions:
        if q.id in PAIR_QUESTIONS:
            continue
        if _QID_RE.fullmatch(q.id) is None:
            msg = "decision_wide_sql: a question id fails the id pattern"
            raise ConfigError(msg, question_set_version=qs.version)
        cols.append(
            f"MAX(answer) FILTER (WHERE question = '{q.id}') AS \"{q.id}\", "
            f"MAX(probability) FILTER (WHERE question = '{q.id}') AS \"{q.id}_p\""
        )
    return (
        f"CREATE OR REPLACE VIEW enrich.decision_wide AS SELECT {', '.join(cols)} "  # noqa: S608 - ids allowlisted above
        "FROM enrich.decision GROUP BY record_id"
    )


def _create_spot_checks(
    wh: duckdb.DuckDBPyConnection, *, qs: QuestionSet, cfg: DecisionsConfig, build_id: str,
    since: datetime,
) -> None:  # fmt: skip
    """U03-83 step 5: select spot-checks and create the missing ``label_check`` items."""
    open_counts = open_label_counts(qsv=qs.version, purposes=frozenset({"spot_check"}))
    payloads = select_spot_checks(
        wh, qs=qs, cfg=cfg, build_id=build_id, since=since, open_counts=open_counts
    )
    for question, group in groupby(payloads, key=lambda p: p["question"]):
        created, _ = create_if_absent(
            "label_check",
            list(group),
            match_keys=_SPOT_KEYS,
            blocking_statuses=("pending", "approved", "rejected"),
            scope={"question_set_version": qs.version},
            now=clock.now(),
        )
        _log.info("enrich.spot_check.created", question=question, count=created,
                  purpose="spot_check")  # fmt: skip


def _report_resolve(stats: list[tuple[str, int, int, int]], report: _Report) -> None:
    """U03-83 step 6: counters, coverage and escalation-share gauges, completion log."""
    decided = sum(row[1] for row in stats)
    escalated = sum(row[2] for row in stats)
    coverage = {entity: final / in_scope for entity, final, _, in_scope in stats if in_scope}
    for entity, ratio in coverage.items():
        record_gauge("herness_enrich_coverage_ratio", ratio, component="enrich",
                     labels={"entity": entity})  # fmt: skip
    if decided:
        record_gauge("herness_enrich_escalation_share_ratio", escalated / decided,
                     component="enrich")  # fmt: skip
    report.decided += decided
    report.escalated += escalated
    _log.info("enrich.resolve.completed", decided=decided, escalated=escalated,
              coverage={entity: round(ratio, 4) for entity, ratio in coverage.items()})  # fmt: skip


def run_resolve(  # noqa: PLR0913 - U03-83: stage arguments plus resolve_frame's (binding)
    wh: duckdb.DuckDBPyConnection,
    *,
    qs: QuestionSet,
    cfg: DecisionsConfig,
    build_id: str,
    run_started_at: datetime,
    report: _Report,
    deciders: DecidersSettings,
    cache: DecisionCache,
    labels: LabelStore,
    calibration: CalibrationStore,
    primaries: Mapping[str, str],
    versions: Mapping[str, str],
    now: datetime,
) -> None:
    """Stage ``resolve``: ``enrich.decision``, ``decision_wide`` and spot-checks (U03-83).

    Spot-checks sample rows decided at or after ``run_started_at``. Raises ConfigError for a
    bad question id or set, SchemaViolation on a DuckDB error; ops errors propagate.
    """
    wide = decision_wide_sql(qs)  # checked before any write
    sync_label_checks(labels, qs=qs)
    resolve_frame(
        wh, qs=qs, cfg=cfg, deciders=deciders, cache=cache, labels=labels,
        calibration=calibration, primaries=primaries, versions=versions, now=now,
    )  # fmt: skip
    try:
        wh.execute(_INSERT_SQL, {"qsv": qs.version})
        wh.execute(wide)
        stats: list[tuple[str, int, int, int]] = wh.execute(_STATS_SQL).fetchall()
    except duckdb.Error as exc:
        raise _duck_error(exc, where="run_resolve") from exc
    _create_spot_checks(wh, qs=qs, cfg=cfg, build_id=build_id, since=run_started_at)
    _report_resolve(stats, report)
