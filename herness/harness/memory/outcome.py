"""The `outcome_measure` job handler (impl 07 U07-86, U07-87, T07-18; design 07 §5.9).

Every number comes from recorded spec 04 queries (`compute_metric`, `peer_group`) whose evidence
is persisted before the outcome; the statistics are the pure functions of `outcome_stats`.
TH07-18: peers with their own accepted recommendation on the metric in the measured span never
serve as control, too few peers fall back to the prior year, and thin data stays inconclusive.
Collaborators come from the one module seam `configure_outcome` (wired by the T07-23
composition root). Logs carry ids, verdicts and counts only.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Final, Literal, cast

import duckdb
from pydantic import JsonValue

from herness.core import time as clock
from herness.core.config import config_hash, get_config
from herness.core.errors import ConfigError, ToolInputError
from herness.core.ids import IdKind, new_id
from herness.core.jobs import JobContext
from herness.core.logging import get_logger
from herness.core.resilience import fault_point
from herness.core.types import Evidence, JobOutcome, MemoryProposal, Provenance
from herness.harness.memory import _write_steps as st
from herness.harness.memory import outcome_stats as stats
from herness.harness.memory.policy import find_uncited_numerals, keyed_hash
from herness.harness.memory.settings import OutcomeConfig
from herness.harness.memory.types import REC_ID_RE
from herness.harness.memory.write import MemoryWriter
from herness.metrics.catalog import MetricCatalog, catalog_from_config
from herness.metrics.compute import MetricResult, compute_metric, peer_group
from herness.metrics.evidence import RecordedQuery
from herness.metrics.settings import EntityType
from herness.store import ops, warehouse

__all__ = [
    "OutcomeDeps", "configure_outcome", "measure_recommendation", "outcome_deps",
    "outcome_measure_handler",
]  # fmt: skip

type _PeerType = Literal["team", "org", "service", "work_item"]
_PY: Final = timedelta(days=364)  # prior-year shift (whole weeks, same weekday)
_PEER_IDS_MAX: Final = 200
_OWNER_SQL: Final = "SELECT service_id FROM core.work_item WHERE record_id = ?"
_PHRASE: Final[Mapping[str, str]] = {
    "paid_off": "a measurable improvement", "no_effect": "no measurable effect",
    "worse": "a measurable deterioration", "inconclusive": "an inconclusive result",
}  # fmt: skip
_ORDINAL: Final = {1: "first", 2: "second"}
_log = get_logger("memory")


def _current_config_hash() -> str:
    return config_hash(get_config())


@dataclass(frozen=True, slots=True)
class OutcomeDeps:
    """Collaborators of the one-argument handler (R-42, T07-18 spec note); T07-23 builds them."""

    writer: MemoryWriter  # outcome_summary through the system write path (U07-50)
    outcome: OutcomeConfig  # cfg.memory.outcome
    allowed: Sequence[re.Pattern[str]] = ()  # the writer's allowed numeral patterns
    config_hash: Callable[[], str] = _current_config_hash  # details.config_hash (T10-03)
    open_current: Callable[[], duckdb.DuckDBPyConnection] = warehouse.open_readonly
    load_catalog: Callable[[], MetricCatalog] = catalog_from_config
    record_evidence: Callable[[Evidence], object] = ops.record_evidence  # step 8


_SEAM: dict[str, OutcomeDeps] = {}  # the module's only state


def configure_outcome(deps: OutcomeDeps | None) -> None:
    """Set the handler's collaborators (composition root, T07-23); `None` clears them."""
    if deps is None:
        _SEAM.clear()
    else:
        _SEAM["deps"] = deps


def outcome_deps() -> OutcomeDeps:
    """The configured collaborators; ConfigError when the composition root set none."""
    if (deps := _SEAM.get("deps")) is None:
        msg = "outcome measurement is not configured: call configure_outcome first"
        raise ConfigError(msg)
    return deps


# ---------------------------------------------------------------- U07-87


@dataclass(frozen=True, slots=True)
class _Pair:
    due: ops.DueMeasurement
    con: duckdb.DuckDBPyConnection
    deps: OutcomeDeps
    now: datetime
    w: stats.Windows


@dataclass(slots=True)
class _Measured:
    """What steps 4-8 produced: series, control, method and the recorded query ids."""

    query_id: str
    build_id: str
    peer_query_id: str | None
    target: dict[date, float] = field(default_factory=dict)
    control: dict[date, float] = field(default_factory=dict)
    method: Literal["did_peer_median", "prior_year", "none"] = "none"
    has_control: bool = False
    peer_ids: list[str] = field(default_factory=list)


def _day(d: date) -> str:
    return clock.format_utc(datetime.combine(d, datetime.min.time(), UTC))


def _owner(p: _Pair) -> tuple[EntityType, str | None]:
    """Step 4: the series entity (the owning service of a work item, else the target)."""
    if p.due["target_type"] != "work_item":
        return cast("EntityType", p.due["target_type"]), p.due["target_id"]
    row = p.con.execute(_OWNER_SQL, [p.due["target_id"]]).fetchone()
    return "service", (str(row[0]) if row is not None and row[0] is not None else None)


def _series(p: _Pair, kind: EntityType, ids: list[str], shift: timedelta) -> MetricResult:
    """One recorded weekly series over the windows shifted back by `shift`, with evidence."""
    fault_point("sql.query", kind="outcome_measure")  # FT07-04 injection point (T08-08)
    start = time.perf_counter()
    window = (p.w.pre[0] - shift, p.w.post[1] - timedelta(days=1) - shift)
    res = compute_metric(p.due["metric"], kind, ids, "week", window=window, con=p.con)
    p.deps.record_evidence(Evidence(
        query_id=res.query_id, run_id=None, build_id=res.build_id, sql=res.sql,
        params=res.params, result_hash=res.result_hash, row_count=res.row_count,
        result_sample=res.result_sample, executed_at=p.now,
        duration_ms=int((time.perf_counter() - start) * 1000),
    ))  # fmt: skip
    return res


def _values(res: MetricResult, entities: Sequence[str], shift: timedelta) -> dict[date, float]:
    """Weekly value of one entity, or the weekly median over several (peer control)."""
    by_week: dict[date, list[float]] = {}
    for r in res.rows:
        if r.entity_id in entities and r.value is not None:
            by_week.setdefault(r.period_start + shift, []).append(r.value)
    return {week: median(values) for week, values in by_week.items()}


def _measure(p: _Pair) -> tuple[_Measured, str]:
    """Steps 4-8: entity, peer group, TH07-18 exclusion, series and evidence."""
    recorded: list[RecordedQuery] = []
    pg = peer_group(cast("_PeerType", p.due["target_type"]), p.due["target_id"],
                    metric=p.due["metric"], con=p.con, on_evidence=recorded.append)  # fmt: skip
    for rq in recorded:  # the peer group query is the evidence of peer_query_id
        p.deps.record_evidence(rq.to_evidence(None))
    out = _Measured(pg.query_id, recorded[0].build_id, pg.query_id)
    kind, entity = _owner(p)
    if entity is None:  # unresolved owner: no series and no control, so inconclusive
        return out, pg.key
    treated = ops.treated_targets(metric=p.due["metric"], start=_day(p.w.pre[0]),
                                  end=_day(p.w.post[1]))  # fmt: skip
    excluded = {target_id for _, target_id in treated}  # TH07-18; any target type (conservative)
    peers = sorted(set(pg.member_ids) - {entity} - excluded)
    zero = timedelta(0)
    if pg.fallback != "prior_year" and len(peers) >= p.deps.outcome.min_peers:
        res = _series(p, kind, [entity, *peers], zero)
        out.method, out.has_control = "did_peer_median", True
        out.control, out.peer_ids = _values(res, peers, zero), peers[:_PEER_IDS_MAX]
    else:
        res, res_py = _series(p, kind, [entity], zero), _series(p, kind, [entity], _PY)
        out.method, out.has_control = "prior_year", len(res_py.rows) > 0
        out.control, out.peer_query_id = _values(res_py, [entity], _PY), res_py.query_id
    out.target = _values(res, [entity], zero)
    out.query_id, out.build_id = res.query_id, res.build_id
    return out, pg.key


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _summary_text(due: ops.DueMeasurement, verdict: str, allowed: Sequence[re.Pattern[str]]) -> str:
    """The outcome_summary sentence; a target id holding an uncited numeral is named generically
    so the system numeral rule cannot reject the summary (catalog metric names hold none)."""
    target = due["target_id"]
    if find_uncited_numerals(target, allowed):
        target = f"a {due['target_type']} target"
    return (f"Accepted {due['kind']} {due['rec_id']} for {target} on {due['metric']} showed "
            f"{_PHRASE[verdict]} ({_ORDINAL[due['measurement']]} measurement).")  # fmt: skip


def _skip(due: ops.DueMeasurement, reason: str) -> None:
    _log.info("memory.outcome.skipped", rec_id=due["rec_id"], measurement=due["measurement"],
              reason=reason)  # fmt: skip


def _write(p: _Pair, row: ops.OutcomeRow, rel: JsonValue) -> str | None:
    """Step 10: one `run_write` for the outcome and its summary; None when the row existed."""
    due, m, qid = p.due, row["measurement"], row["query_id"]
    summary = MemoryProposal(
        layer="episodic", kind="outcome_summary", confidence=1.0,
        content=_summary_text(due, row["verdict"], p.deps.allowed),
        data={"rec_id": row["rec_id"], "outcome_id": row["outcome_id"], "measurement": m,
              "verdict": row["verdict"], "metric": row["metric"], "baseline": row["baseline"],
              "actual": row["actual"], "delta": row["delta"], "rel": rel, "query_id": qid},
        provenance=Provenance(author_type="system", author_role=None, author_ref=None,
                              run_id=None, task_id=None, query_ids=[qid], via="outcome_job"),
    )  # fmt: skip
    key = keyed_hash("outcome_summary:" + row["rec_id"] + ":" + str(m))

    def tx(conn: sqlite3.Connection) -> str | None:
        if not ops.insert_outcome(row, conn=conn):
            return None  # a concurrent measurement won: no second summary
        return p.deps.writer.insert_system_item(
            summary, key_hash=key, conn=conn, now=p.now
        ).memory_id

    return ops.run_write(tx, op="outcome_measure")


def measure_recommendation(
    due: ops.DueMeasurement, *, con: duckdb.DuckDBPyConnection, catalog: MetricCatalog,
    deps: OutcomeDeps, now: datetime,
) -> ops.OutcomeRow | None:  # fmt: skip
    """Measure one pair and write `outcome` plus `outcome_summary` (U07-87): the inserted row,
    or None when it existed, the metric is unknown or the target not measurable."""
    rec_id, m, metric, cfg = due["rec_id"], due["measurement"], due["metric"], deps.outcome
    if ops.outcome_exists(rec_id, m):
        return None
    try:
        better = catalog.get(metric).better
    except ToolInputError:
        _skip(due, "unknown_metric")
        return None
    eff = clock.parse_utc(due["effective_at"]).date()
    w = stats.measurement_windows(eff, cast("Literal[1, 2]", m), stats.metric_weeks(cfg, metric))
    p = _Pair(due, con, deps, now, w)
    try:
        got, pg_key = _measure(p)
    except ToolInputError:  # unknown target, or a request the catalog refuses
        _skip(due, "invalid_request")
        return None
    d = stats.did_statistics(got.target, got.control, w, better=better,
                       expected_delta=due["expected_delta"], min_rel=cfg.min_rel)  # fmt: skip
    verdict = stats.classify_verdict(d, has_control=got.has_control, cfg=cfg)
    details: dict[str, JsonValue] = {
        "method": got.method, "pre": [w.pre[0].isoformat(), w.pre[1].isoformat()],
        "post": [w.post[0].isoformat(), w.post[1].isoformat()], "peer_group_key": pg_key,
        "peer_ids": list(got.peer_ids), "peer_query_id": got.peer_query_id,
        "n_pre": d.n_pre, "n_post": d.n_post, "coverage": _finite(d.coverage),
        "did": _finite(d.did), "se": _finite(d.se), "t": _finite(d.t), "rel": _finite(d.rel),
        "expected_rel": _finite(d.expected_rel), "build_id": got.build_id,
        "config_hash": deps.config_hash(),
    }  # fmt: skip
    row = ops.OutcomeRow(
        outcome_id=new_id(IdKind.OUTCOME), rec_id=rec_id, measurement=m,
        measured_at=clock.format_utc(now), metric=metric, baseline=_finite(d.mean_pre),
        actual=_finite(d.mean_post), delta=_finite(d.did), query_id=got.query_id,
        verdict=verdict, details=details,
    )  # fmt: skip
    memory_id = _write(p, row, details["rel"])
    if memory_id is None:
        return None
    deps.writer.embed_after_commit(memory_id)  # step 11
    _log.info("memory.outcome.measured", rec_id=rec_id, measurement=m, verdict=verdict,
              method=got.method)  # fmt: skip
    st.count("herness_memory_outcomes_total", verdict=verdict)
    return row


# ---------------------------------------------------------------- U07-86


def _payload(payload: Mapping[str, JsonValue]) -> tuple[str, int] | None:
    """None for a sweep, (rec_id, measurement) for one pair; ConfigError otherwise (R-42)."""
    if set(payload) == {"sweep"} and payload["sweep"] is True:
        return None
    rec_id, m = payload.get("rec_id"), payload.get("measurement")
    if (set(payload) == {"rec_id", "measurement"} and isinstance(rec_id, str)
            and REC_ID_RE.fullmatch(rec_id) and type(m) is int and m in (1, 2)):  # fmt: skip
        return rec_id, m
    msg = "outcome_measure payload invalid"
    raise ConfigError(msg)


def outcome_measure_handler(ctx: JobContext) -> JobOutcome:
    """Weekly sweep or one-off measurement (U07-86). QueryError and RetryableError propagate:
    spec 08 retries the job and the next weekly sweep measures the same pair."""
    single = _payload(ctx.job.payload)
    deps, now = outcome_deps(), clock.now()
    per, default = stats.due_weeks(deps.outcome)
    pairs = ops.due_measurements(now=clock.format_utc(now), due_weeks=per,
                                 default_due_weeks=default)  # fmt: skip
    if single is not None:
        pairs = [p for p in pairs if (p["rec_id"], p["measurement"]) == single]
        if not pairs:
            return JobOutcome(status="done", result={"skipped": "not_due"})
    measured, skipped, verdicts = 0, 0, dict[str, JsonValue]()
    if not pairs:  # no warehouse needed for an empty sweep
        return JobOutcome(status="done", result={"measured": 0, "skipped": 0, "verdicts": {}})
    catalog, con = deps.load_catalog(), deps.open_current()
    try:
        for due in pairs:
            if ctx.should_yield():
                return JobOutcome(status="yield", result={"measured": measured})
            row = measure_recommendation(due, con=con, catalog=catalog, deps=deps, now=now)
            if row is None:
                skipped += 1
            else:
                measured += 1
                verdicts[row["verdict"]] = cast("int", verdicts.get(row["verdict"], 0)) + 1
            ctx.heartbeat(f"measured {due['rec_id']} m{due['measurement']}")
    finally:
        con.close()
    result: dict[str, JsonValue] = {"measured": measured, "skipped": skipped, "verdicts": verdicts}
    return JobOutcome(status="done", result=result)
