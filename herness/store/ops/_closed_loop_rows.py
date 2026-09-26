"""Row types, constant SQL and plumbing of the ops area ``closed_loop`` (impl 07 §3.5 C1 … C7).

Private sibling of ``herness.store.ops.closed_loop``, split off to keep that module inside its
impl 07 §2 line budget; only ``closed_loop`` imports it (``ops-areas-acyclic`` ignore entry).
Migration 004 is looser than impl 07 §4.1 (``confidence``, ``reason`` and ``query_id`` are
nullable there), so the "(app)" constraints are checked here before every write.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Final, TypedDict

from pydantic import JsonValue

from herness.core.errors import HernessError, SchemaViolation, StoreBusy, ToolInputError

from . import core

type Conn = sqlite3.Connection | None
type Params = Sequence[object] | Mapping[str, object]
type Check = Callable[[object], bool]

_ULID = "[0-9A-HJKMNP-TV-Z]{26}"
REC_ID_RE: Final = re.compile(rf"rec_{_ULID}")
RUN_ID_RE: Final = re.compile(rf"run_{_ULID}")
TASK_ID_RE: Final = re.compile(rf"task_{_ULID}")
OUT_ID_RE: Final = re.compile(rf"out_{_ULID}")
QUERY_ID_RE: Final = re.compile(r"q_[0-9a-f]{16}")
HEX32_RE: Final = re.compile(r"[0-9a-f]{32}")
TS_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", re.ASCII)
RUN_KINDS: Final = frozenset({"funding_review", "org_review", "chat", "eval"})
MEMORY_KINDS: Final = frozenset({"outcome_summary", "decision_note"})
IDS_MAX: Final = 500
ROWS_MAX: Final = 50
SIMILARITY_MAX: Final = 5_000

REC_COLUMNS: Final = (
    "rec_id", "run_id", "kind", "target_type", "target_id", "summary", "numbers",
    "expected_metric", "expected_delta", "expected_usd", "confidence", "confidence_basis",
    "finding_ids", "created_at",
)  # fmt: skip
DECISION_COLUMNS: Final = (
    "rec_id", "decision", "reason", "decided_by", "decided_at", "effective_at",
)  # fmt: skip
OUTCOME_COLUMNS: Final = (
    "outcome_id", "rec_id", "measurement", "measured_at", "metric", "baseline", "actual",
    "delta", "query_id", "verdict", "details",
)  # fmt: skip
_REC: Final = ", ".join(f"r.{c}" for c in REC_COLUMNS)
_DEC: Final = ", ".join(DECISION_COLUMNS)
_OUT: Final = ", ".join(OUTCOME_COLUMNS)

INSERT_REC: Final = (
    f"INSERT INTO recommendation ({', '.join(REC_COLUMNS)})"  # noqa: S608 - constant columns
    f" VALUES ({', '.join('?' * 14)})"
)
INSERT_DECISION: Final = f"INSERT INTO decision_log ({_DEC}) VALUES ({', '.join('?' * 6)})"  # noqa: S608
INSERT_OUTCOME: Final = f"INSERT OR IGNORE INTO outcome ({_OUT}) VALUES ({', '.join('?' * 11)})"  # noqa: S608
RUN_RECS: Final = f"SELECT {_REC} FROM recommendation r WHERE r.run_id = ? ORDER BY r.rec_id"  # noqa: S608
# Latest decision / outcome per rec (U07-32, U07-33): one ROW_NUMBER window, rn = 1 kept.
_LATEST_DEC: Final = (
    f"(SELECT {_DEC}, ROW_NUMBER() OVER (PARTITION BY rec_id"  # noqa: S608 - constant columns
    " ORDER BY decided_at DESC, rowid DESC) AS rn FROM decision_log{where})"
)
_LATEST_OUT: Final = (
    f"(SELECT {_OUT}, ROW_NUMBER() OVER (PARTITION BY rec_id"  # noqa: S608 - constant columns
    " ORDER BY measurement DESC, measured_at DESC) AS rn FROM outcome{where})"
)
_IN: Final = " WHERE rec_id IN ({marks})"
LATEST_DECISIONS: Final = f"SELECT {_DEC} FROM {_LATEST_DEC.format(where=_IN)} WHERE rn = 1"  # noqa: S608
LATEST_OUTCOMES: Final = f"SELECT {_OUT} FROM {_LATEST_OUT.format(where=_IN)} WHERE rn = 1"  # noqa: S608
_ACCEPTED: Final = (
    f" JOIN {_LATEST_DEC.format(where='')} d ON d.rec_id = r.rec_id AND d.rn = 1"
    " AND d.decision = 'accepted'"
)
_HAS_OUT: Final = (
    "EXISTS (SELECT 1 FROM outcome o WHERE o.rec_id = r.rec_id AND o.measurement = {m})"
)
DUE: Final = (
    "SELECT r.rec_id, r.expected_metric, d.effective_at, r.target_type, r.target_id,"  # noqa: S608
    f" r.expected_delta, r.kind, {_HAS_OUT.format(m=1)}, {_HAS_OUT.format(m=2)}"
    f" FROM recommendation r{_ACCEPTED} WHERE r.expected_metric IS NOT NULL"
)
TREATED: Final = (
    f"SELECT DISTINCT r.target_type, r.target_id FROM recommendation r{_ACCEPTED}"  # noqa: S608
    " WHERE r.expected_metric = ? AND d.effective_at >= ? AND d.effective_at < ?"
)
ACCEPTED_SINCE: Final = (
    f"SELECT {_REC} FROM recommendation r{_ACCEPTED}"  # noqa: S608
    " WHERE d.decided_at >= ? ORDER BY r.created_at DESC, r.rec_id DESC"
)
SIMILARITY: Final = (
    "SELECT r.rec_id, r.kind, r.target_type, r.target_id, r.expected_metric, r.summary,"  # noqa: S608
    f" o.verdict, o.measured_at, o.query_id FROM recommendation r"
    f" JOIN {_LATEST_OUT.format(where='')} o ON o.rec_id = r.rec_id AND o.rn = 1"
    f" ORDER BY o.measured_at DESC, r.rec_id LIMIT {SIMILARITY_MAX}"
)
RECENT_RUNS: Final = (  # R-68 carve-out: own SQL on `run`, mirroring runs.select_runs
    "SELECT run_id FROM run WHERE kind = ? AND run_id <> ?"
    " AND run_id IN (SELECT DISTINCT run_id FROM recommendation)"
    " ORDER BY started_at DESC, run_id DESC LIMIT ?"
)
# Constant partial-index predicate first (ix_memory_rec), the caller's kinds second.
REC_MEMORY: Final = (
    "SELECT memory_id FROM memory_item WHERE json_extract(data,'$.rec_id') IN ({ids})"
    " AND kind IN ('outcome_summary','decision_note') AND kind IN ({kinds})"
    " ORDER BY created_at, memory_id"
)
DEAD_TASKS: Final = "SELECT COUNT(*) FROM task WHERE run_id = ? AND status = 'dead'"
PROMOTION: Final = (
    "SELECT finding_id, status, query_ids, task_id, verification FROM finding WHERE run_id = ?"
    " AND (status = 'verified' OR (status = 'rejected'"
    " AND json_extract(verification,'$.passed') = 0)) ORDER BY finding_id"
)
TASK_SPEC: Final = "SELECT spec FROM task WHERE task_id = ?"
DONE_RUNS: Final = (
    "SELECT run_id FROM run WHERE status = 'done' AND finished_at >= ? ORDER BY finished_at, run_id"
)


class RecommendationRow(TypedDict):
    """One ``recommendation`` row, ``numbers``, ``confidence_basis``, ``finding_ids`` parsed."""

    rec_id: str
    run_id: str
    kind: str
    target_type: str
    target_id: str
    summary: str
    numbers: list[JsonValue]
    expected_metric: str | None
    expected_delta: float | None
    expected_usd: str | None
    confidence: float
    confidence_basis: dict[str, JsonValue]
    finding_ids: list[str]
    created_at: str


class DecisionRow(TypedDict):
    """One ``decision_log`` row (U07-32)."""

    rec_id: str
    decision: str
    reason: str
    decided_by: str
    decided_at: str
    effective_at: str


class OutcomeRow(TypedDict):
    """One ``outcome`` row with ``details`` parsed (U07-33)."""

    outcome_id: str
    rec_id: str
    measurement: int
    measured_at: str
    metric: str
    baseline: float | None
    actual: float | None
    delta: float | None
    query_id: str
    verdict: str
    details: dict[str, JsonValue]


class DueMeasurement(TypedDict):
    """One due, unmeasured (``rec_id``, ``measurement``) pair (U07-33)."""

    rec_id: str
    measurement: int
    metric: str
    effective_at: str
    target_type: str
    target_id: str
    expected_delta: float | None
    kind: str


class SimilarityRow(TypedDict):
    """A recommendation with its latest outcome (U07-34)."""

    rec_id: str
    kind: str
    target_type: str
    target_id: str
    expected_metric: str | None
    summary: str
    verdict: str
    measured_at: str
    query_id: str | None


class PromotionSource(TypedDict):
    """A finding eligible as a procedural promotion source (U07-36)."""

    finding_id: str
    status: str
    query_ids: list[str]
    task_id: str
    verification: dict[str, JsonValue] | None


def _str(pattern: re.Pattern[str]) -> Check:
    return lambda v: isinstance(v, str) and pattern.fullmatch(v) is not None


def _text(most: int, *, least: int = 1) -> Check:
    return lambda v: isinstance(v, str) and least <= len(v) <= most


def _one_of(*allowed: str) -> Check:
    return lambda v: v in allowed


def _number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _opt(check: Check) -> Check:
    return lambda v: v is None or check(v)


def _str_list(v: object) -> bool:
    return isinstance(v, list) and all(isinstance(i, str) for i in v)


def is_int(v: object, least: int) -> bool:
    """An ``int`` (not ``bool``) ≥ ``least``."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= least


_TS: Final = _str(TS_RE)
REC_CHECKS: Final[Mapping[str, Check]] = {
    "rec_id": _str(REC_ID_RE),
    "run_id": _str(RUN_ID_RE),
    "kind": _one_of("fund", "org_action"),
    "target_type": _one_of("service", "team", "org", "work_item"),
    "target_id": _text(200),
    "summary": _text(400),  # R-30
    "numbers": lambda v: isinstance(v, list),
    "expected_metric": _opt(_text(200)),
    "expected_delta": _opt(_number),
    "expected_usd": _opt(_text(64)),
    "confidence": lambda v: _number(v) and 0 <= float(v) <= 1,  # type: ignore[arg-type]
    "confidence_basis": lambda v: isinstance(v, dict),
    "finding_ids": _str_list,
    "created_at": _TS,
}
DECISION_CHECKS: Final[Mapping[str, Check]] = {
    "rec_id": _str(REC_ID_RE),
    "decision": _one_of("accepted", "rejected", "deferred"),
    "reason": _text(1_000),
    "decided_by": _str(HEX32_RE),
    "decided_at": _TS,
    "effective_at": _TS,
}
OUTCOME_CHECKS: Final[Mapping[str, Check]] = {
    "outcome_id": _str(OUT_ID_RE),
    "rec_id": _str(REC_ID_RE),
    "measurement": lambda v: is_int(v, 1),
    "measured_at": _TS,
    "metric": _text(200),
    "baseline": _opt(_number),
    "actual": _opt(_number),
    "delta": _opt(_number),
    "query_id": _str(QUERY_ID_RE),
    "verdict": _one_of("paid_off", "no_effect", "worse", "inconclusive"),
    "details": lambda v: isinstance(v, dict),
}


def check_row(row: Mapping[str, object], checks: Mapping[str, Check], op: str) -> None:
    """SchemaViolation("<op>: invalid <column>") for the first missing or failing column."""
    for column, check in checks.items():
        if column not in row or not check(row[column]):
            msg = f"{op}: invalid {column}"
            raise SchemaViolation(msg)


def bad_arg(op: str) -> ToolInputError:
    """U07-34: ``ToolInputError("<function>: invalid argument")``."""
    return ToolInputError(f"{op}: invalid argument")


def bad_id(op: str) -> ToolInputError:
    """U07-36: ``ToolInputError("invalid id: <function>")``."""
    return ToolInputError(f"invalid id: {op}")


def valid_ids(values: Sequence[str], pattern: re.Pattern[str], op: str) -> list[str]:
    """C2: at most 500 ids, each matching ``pattern``; deduplicated in input order."""
    if len(values) > IDS_MAX:
        msg = f"too many ids: {op}"
        raise ToolInputError(msg)
    if not all(map(_str(pattern), values)):  # non-str values fail too
        raise bad_id(op)
    return list(dict.fromkeys(values))


def marks(n: int) -> str:
    """``?, ?, …``: a constant fragment repeated ``n`` times, never built from values (C2)."""
    return ", ".join("?" * n)


def _read_error(exc: sqlite3.Error, op: str) -> HernessError:
    text = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text):
        return StoreBusy(f"ops store busy in {op}", op=op)
    return SchemaViolation(f"{op}: {type(exc).__name__}", op=op)


def query(sql: str, params: Params, conn: Conn, op: str) -> list[sqlite3.Row]:
    """C1/C5: run a read on ``conn`` or this thread's connection, errors mapped per C5."""
    try:
        rows: list[sqlite3.Row] = (conn or core.connection()).execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise _read_error(exc, op) from exc
    return rows


def write[T](fn: Callable[[sqlite3.Connection], T], conn: Conn, op: str) -> T:
    """C1: run ``fn`` on the caller's connection (errors propagate), else in ``run_write``."""
    return fn(conn) if conn is not None else core.run_write(fn, op=op)


def load_typed[T](text: str | None, kind: type[T], field: str, ref: str) -> T | None:
    """C3: a JSON column that must hold ``kind`` or NULL; else SchemaViolation naming the row."""
    try:
        value = core.load_json(text, field=field)
    except SchemaViolation:
        value = ...
    if value is None or isinstance(value, kind):
        return value
    msg = f"{field} invalid JSON for {ref}"
    raise SchemaViolation(msg)
