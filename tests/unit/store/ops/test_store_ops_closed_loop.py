"""Unit tests for herness.store.ops.closed_loop (impl 07 U07-31 … U07-34, U07-36).

UT07-10 is the card's test; the store-level rows backing UT07-63, UT07-64, UT07-66, UT07-67,
UT07-68, UT07-74, UT07-78 and UT07-84 (whose full cases need the L4 callers) carry those IDs.
Every test runs on a fresh ops store migrated through 070; `run`, `task`, `finding` and
`memory_item` rows are seeded with raw SQL (they belong to other areas or cards).
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation, StoreBusy, ToolInputError
from herness.core.ids import new_ulid
from herness.store import ops
from herness.store.ops import _closed_loop_rows, closed_loop, core
from herness.store.ops.closed_loop import DecisionRow, OutcomeRow, RecommendationRow

pytestmark = pytest.mark.unit

_T0 = datetime.datetime(2026, 1, 5, 10, 30, tzinfo=datetime.UTC)
_USER = "ab" * 16


@pytest.fixture(autouse=True)
def migrated(ops_store: Path) -> Path:
    return ops_store


def _at(days: float = 0.0) -> str:
    return clock.format_utc(_T0 + datetime.timedelta(days=days))


def _id(prefix: str) -> str:
    return f"{prefix}_{new_ulid()}"


def _write(sql: str, params: tuple[object, ...] = ()) -> None:
    core.run_write(lambda c: c.execute(sql, params), op="test_write")


def _run(
    run_id: str | None = None,
    *,
    kind: str = "funding_review",
    status: str = "done",
    started: float = 0.0,
    finished: float | None = None,
) -> str:
    rid = run_id or _id("run")
    _write(
        "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at,"
        " finished_at) VALUES (?, ?, 'fast', 'p', 'h', ?, ?, ?)",
        (rid, kind, status, _at(started), None if finished is None else _at(finished)),
    )
    return rid


def _task(run_id: str, *, status: str = "pending", spec: str = '{"goal": "x"}') -> str:
    tid = _id("task")
    _write(
        "INSERT INTO task (task_id, run_id, role, spec, status, created_at, updated_at)"
        " VALUES (?, ?, 'analyst', ?, ?, ?, ?)",
        (tid, run_id, spec, status, _at(), _at()),
    )
    return tid


def _rec(run_id: str | None = None, **over: Any) -> RecommendationRow:
    row: dict[str, Any] = {
        "rec_id": _id("rec"),
        "run_id": run_id or _id("run"),
        "kind": "fund",
        "target_type": "service",
        "target_id": "svc-a",
        "summary": "Fund [[n1]] more engineers",
        "numbers": [{"ref": "n1", "value": "2"}],
        "expected_metric": "lead_time",
        "expected_delta": -1.5,
        "expected_usd": "1000.00",
        "confidence": 0.7,
        "confidence_basis": {"prior": 0.6},
        "finding_ids": ["fnd_" + new_ulid()],
        "created_at": _at(),
    }
    row.update(over)
    return row  # type: ignore[return-value]


def _put(*rows: RecommendationRow) -> None:
    core.run_write(lambda c: closed_loop.insert_recommendations(list(rows), conn=c), op="test_recs")


def _decide(rec_id: str, decision: str = "accepted", *, at: float = 0.0, **over: Any) -> None:
    row: dict[str, Any] = {
        "rec_id": rec_id,
        "decision": decision,
        "reason": "because",
        "decided_by": _USER,
        "decided_at": _at(at),
        "effective_at": _at(at),
    }
    row.update(over)
    closed_loop.insert_decision(row)  # type: ignore[arg-type]


def _outcome(rec_id: str, measurement: int = 1, **over: Any) -> OutcomeRow:
    row: dict[str, Any] = {
        "outcome_id": _id("out"),
        "rec_id": rec_id,
        "measurement": measurement,
        "measured_at": _at(90),
        "metric": "lead_time",
        "baseline": 10.0,
        "actual": 8.0,
        "delta": -2.0,
        "query_id": "q_" + "0" * 16,
        "verdict": "paid_off",
        "details": {"peers": 3},
    }
    row.update(over)
    return row  # type: ignore[return-value]


# --- UT07-10 due_measurements ---------------------------------------------------------------


def test_ut07_10_due_measurements_only_due_unmeasured_pairs() -> None:
    """UT07-10 accepted recs with per-metric weeks: only due, unmeasured pairs, oldest first."""
    a = _rec(expected_metric="lead_time")  # per-metric weeks (2, 4)
    b = _rec(expected_metric="cost")  # default weeks (12, 26)
    rejected = _rec(expected_metric="lead_time")
    no_metric = _rec(expected_metric=None)
    undecided = _rec(expected_metric="lead_time")
    _put(a, b, rejected, no_metric, undecided)
    _decide(a["rec_id"], at=0)  # effective 2026-01-05 → due 01-19 (m1), 02-02 (m2)
    _decide(b["rec_id"], at=-100)  # effective 2025-09-27 → due 12-20 (m1), 2026-03-28 (m2)
    _decide(rejected["rec_id"], at=-200)
    _decide(rejected["rec_id"], "rejected", at=-199)  # latest decision wins
    _decide(no_metric["rec_id"], at=-200)
    closed_loop.insert_outcome(_outcome(a["rec_id"], 1))  # m1 of a already measured
    now = clock.format_utc(datetime.datetime(2026, 2, 2, tzinfo=datetime.UTC))
    due = closed_loop.due_measurements(
        now=now, due_weeks={"lead_time": (2, 4)}, default_due_weeks=(12, 26)
    )
    got = [(d["rec_id"], d["measurement"]) for d in due]
    assert got == [(b["rec_id"], 1), (a["rec_id"], 2)]  # a m2 due exactly at now (≤)
    first = due[0]
    assert first == {
        "rec_id": b["rec_id"],
        "measurement": 1,
        "metric": "cost",
        "effective_at": _at(-100),
        "target_type": "service",
        "target_id": "svc-a",
        "expected_delta": -1.5,
        "kind": "fund",
    }


def test_ut07_10_due_measurements_orders_and_skips_future() -> None:
    """UT07-10 ties on due date order by rec_id then measurement; future pairs are skipped."""
    recs = [_rec(), _rec()]
    _put(*recs)
    for rec in recs:
        _decide(rec["rec_id"], at=0)
    now = _at(7)
    due = closed_loop.due_measurements(now=now, due_weeks={}, default_due_weeks=(1, 1))
    ids = sorted(r["rec_id"] for r in recs)
    assert [(d["rec_id"], d["measurement"]) for d in due] == [
        (ids[0], 1),
        (ids[0], 2),
        (ids[1], 1),
        (ids[1], 2),
    ]
    assert closed_loop.due_measurements(now=_at(6), due_weeks={}, default_due_weeks=(1, 2)) == []


@pytest.mark.parametrize(
    ("now", "weeks", "default"),
    [
        ("2026-01-01", {}, (1, 2)),
        (_at(), {}, (0, 2)),
        (_at(), {"m": (1, True)}, (1, 2)),
        (_at(), {"m": (1, 2, 3)}, (1, 2)),
        (_at(), {}, [1, 2]),
    ],
)
def test_ut07_10_due_measurements_rejects_bad_arguments(
    now: str, weeks: dict[str, Any], default: Any
) -> None:
    """UT07-10 a non-fixed-width `now` or a non-positive week pair → ToolInputError."""
    with pytest.raises(ToolInputError, match="due_measurements: invalid argument"):
        closed_loop.due_measurements(now=now, due_weeks=weeks, default_due_weeks=default)


# --- U07-31 recommendations (UT07-63, UT07-64) ----------------------------------------------


def test_ut07_64_insert_and_read_back_run_recommendations() -> None:
    """UT07-64 recommendations round-trip in rec_id order with the JSON columns parsed."""
    run_id = _id("run")
    rows = [_rec(run_id), _rec(run_id), _rec()]
    _put(*rows)
    got = closed_loop.run_recommendations(run_id)
    assert got == sorted(rows[:2], key=lambda r: r["rec_id"])
    assert isinstance(got[0]["numbers"], list)
    with core.connection() as conn:
        assert closed_loop.run_recommendations(run_id, conn=conn) == got
    assert closed_loop.run_recommendations(_id("run")) == []


def test_ut07_64_insert_recommendations_requires_the_callers_transaction() -> None:
    """UT07-64 `conn` is required (C1): ConfigError without it, nothing written."""
    with pytest.raises(ConfigError, match="requires the caller's transaction"):
        closed_loop.insert_recommendations([_rec()])


def test_ut07_64_one_bad_row_writes_nothing() -> None:
    """UT07-64 every row is validated before the first INSERT; the transaction rolls back."""
    run_id = _id("run")
    with pytest.raises(SchemaViolation, match="insert_recommendations: invalid summary"):
        _put(_rec(run_id), _rec(run_id, summary="x" * 401))
    assert closed_loop.run_recommendations(run_id) == []


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("rec_id", "rec_bad"),
        ("run_id", "run_1"),
        ("kind", "other"),
        ("target_type", "planet"),
        ("summary", ""),
        ("confidence", 1.5),
        ("confidence", None),
        ("confidence", True),
        ("numbers", {}),
        ("confidence_basis", []),
        ("finding_ids", [1]),
        ("expected_delta", "1"),
        ("created_at", "2026-01-01"),
    ],
)
def test_ut07_63_invalid_recommendation_column(column: str, value: object) -> None:
    """UT07-63 a violated (app) constraint → SchemaViolation naming the column."""
    with pytest.raises(SchemaViolation, match=f"insert_recommendations: invalid {column}$"):
        _put(_rec(**{column: value}))


def test_ut07_63_missing_column_and_row_count() -> None:
    """UT07-63 a missing column, 0 rows or 51 rows → SchemaViolation."""
    row = dict(_rec())
    del row["summary"]
    with pytest.raises(SchemaViolation, match="invalid summary"):
        _put(row)  # type: ignore[arg-type]
    with pytest.raises(SchemaViolation, match="invalid rows"):
        _put()
    with pytest.raises(SchemaViolation, match="invalid rows"):
        _put(*[_rec() for _ in range(51)])


def test_ut07_64_duplicate_rec_id_rolls_back() -> None:
    """UT07-64 a duplicate primary key maps to SchemaViolation through run_write."""
    row = _rec()
    _put(row)
    with pytest.raises(SchemaViolation):
        _put(_rec(row["run_id"]), row)
    assert len(closed_loop.run_recommendations(row["run_id"])) == 1


def test_ut07_64_run_recommendations_rejects_bad_id() -> None:
    """UT07-64 a malformed run id → ToolInputError before any SQL."""
    with pytest.raises(ToolInputError, match="invalid id: run_recommendations"):
        closed_loop.run_recommendations("run_x")


# --- U07-32 decisions (UT07-68) -------------------------------------------------------------


def test_ut07_68_latest_decision_by_decided_at_then_rowid() -> None:
    """UT07-68 the latest row per rec wins by (decided_at, rowid); undecided recs are absent."""
    a, b, c = _rec(), _rec(), _rec()
    _put(a, b, c)
    _decide(a["rec_id"], "deferred", at=1)
    _decide(a["rec_id"], "accepted", at=2)
    _decide(a["rec_id"], "rejected", at=0)  # older decided_at, newer rowid
    _decide(b["rec_id"], "accepted", at=3)
    _decide(b["rec_id"], "rejected", at=3)  # same decided_at: higher rowid wins
    got = closed_loop.latest_decisions([a["rec_id"], b["rec_id"], c["rec_id"], a["rec_id"]])
    assert set(got) == {a["rec_id"], b["rec_id"]}
    assert got[a["rec_id"]]["decision"] == "accepted"
    assert got[b["rec_id"]] == {
        "rec_id": b["rec_id"],
        "decision": "rejected",
        "reason": "because",
        "decided_by": _USER,
        "decided_at": _at(3),
        "effective_at": _at(3),
    }
    assert closed_loop.latest_decisions([]) == {}


def test_ut07_68_insert_decision_in_callers_transaction_and_unknown_rec() -> None:
    """UT07-68 with `conn` the insert joins the caller's transaction; an unknown rec fails."""
    rec = _rec()
    _put(rec)
    row: DecisionRow = {
        "rec_id": rec["rec_id"],
        "decision": "accepted",
        "reason": "r",
        "decided_by": _USER,
        "decided_at": _at(),
        "effective_at": _at(1),
    }
    core.run_write(lambda c: closed_loop.insert_decision(row, conn=c), op="test_decide")
    assert closed_loop.latest_decisions([rec["rec_id"]])[rec["rec_id"]] == row
    with pytest.raises(SchemaViolation, match="FOREIGN KEY"):
        closed_loop.insert_decision({**row, "rec_id": _id("rec")})


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("decision", "maybe"),
        ("reason", None),
        ("reason", ""),
        ("reason", "x" * 1001),
        ("decided_by", "ABC"),
        ("decided_at", "yesterday"),
        ("effective_at", None),
        ("rec_id", "rec_1"),
    ],
)
def test_ut07_68_invalid_decision_column(column: str, value: object) -> None:
    """UT07-68 a violated decision constraint → SchemaViolation naming the column."""
    rec = _rec()
    _put(rec)
    rec_id, over = (value, {}) if column == "rec_id" else (rec["rec_id"], {column: value})
    with pytest.raises(SchemaViolation, match=f"insert_decision: invalid {column}$"):
        _decide(rec_id, **over)  # type: ignore[arg-type]


def test_ut07_68_latest_decisions_id_bounds() -> None:
    """UT07-68 more than 500 ids or a malformed id → ToolInputError (C2)."""
    with pytest.raises(ToolInputError, match="too many ids: latest_decisions"):
        closed_loop.latest_decisions([_id("rec") for _ in range(501)])
    with pytest.raises(ToolInputError, match="invalid id: latest_decisions"):
        closed_loop.latest_decisions(["rec_1' OR 1=1 --"])


# --- U07-33 outcomes (UT07-74) --------------------------------------------------------------


def test_ut07_74_insert_outcome_is_idempotent_per_measurement() -> None:
    """UT07-74 the unique (rec_id, measurement) index makes a rerun a no-op returning False."""
    rec = _rec()
    _put(rec)
    first = _outcome(rec["rec_id"], 1)
    assert closed_loop.insert_outcome(first) is True
    assert closed_loop.insert_outcome(_outcome(rec["rec_id"], 1, verdict="worse")) is False
    assert closed_loop.outcome_exists(rec["rec_id"], 1) is True
    assert closed_loop.outcome_exists(rec["rec_id"], 2) is False
    with core.connection() as conn:
        assert closed_loop.outcome_exists(rec["rec_id"], 1, conn=conn) is True
    ok = core.run_write(
        lambda c: closed_loop.insert_outcome(_outcome(rec["rec_id"], 2), conn=c), op="t"
    )
    assert ok is True


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("outcome_id", "out_1"),
        ("verdict", "great"),
        ("measurement", 0),
        ("measurement", True),
        ("query_id", None),
        ("baseline", "1"),
        ("details", None),
        ("metric", ""),
    ],
)
def test_ut07_74_invalid_outcome_column(column: str, value: object) -> None:
    """UT07-74 a violated outcome constraint → SchemaViolation naming the column."""
    with pytest.raises(SchemaViolation, match=f"insert_outcome: invalid {column}$"):
        closed_loop.insert_outcome(_outcome(_id("rec"), **{column: value}))


def test_ut07_74_outcome_exists_bounds() -> None:
    """UT07-74 measurement outside 1 … 2 or a bad rec id → ToolInputError."""
    for bad in (0, 3, True):
        with pytest.raises(ToolInputError, match="outcome_exists: invalid argument"):
            closed_loop.outcome_exists(_id("rec"), bad)
    with pytest.raises(ToolInputError, match="invalid id: outcome_exists"):
        closed_loop.outcome_exists("rec_x", 1)


def test_ut07_74_latest_outcomes_by_measurement_then_measured_at() -> None:
    """UT07-74 the latest outcome per rec is the highest measurement; details parsed."""
    a, b = _rec(), _rec()
    _put(a, b)
    closed_loop.insert_outcome(_outcome(a["rec_id"], 1, measured_at=_at(200)))
    second = _outcome(a["rec_id"], 2, measured_at=_at(180), verdict="no_effect")
    closed_loop.insert_outcome(second)
    got = closed_loop.latest_outcomes([a["rec_id"], b["rec_id"]])
    assert got == {a["rec_id"]: second}
    assert closed_loop.latest_outcomes([]) == {}


def test_ut07_74_treated_targets_accepted_in_window() -> None:
    """UT07-74 treated peers: accepted recs on the metric effective in [start, end) (TH07-18)."""
    inside = _rec(target_id="svc-in")
    other_metric = _rec(target_id="svc-metric", expected_metric="cost")
    late = _rec(target_id="svc-late")
    reverted = _rec(target_id="svc-reverted")
    team = _rec(target_type="team", target_id="t1")
    _put(inside, other_metric, late, reverted, team)
    _decide(inside["rec_id"], at=1)
    _decide(other_metric["rec_id"], at=1)
    _decide(late["rec_id"], at=10)  # effective_at == end: excluded
    _decide(reverted["rec_id"], at=1)
    _decide(reverted["rec_id"], "rejected", at=2)
    _decide(team["rec_id"], at=0)  # effective_at == start: included
    got = closed_loop.treated_targets(metric="lead_time", start=_at(0), end=_at(10))
    assert got == {("service", "svc-in"), ("team", "t1")}
    with pytest.raises(ToolInputError, match="treated_targets: invalid argument"):
        closed_loop.treated_targets(metric="", start=_at(), end=_at())


# --- U07-34 prior context reads (UT07-67, UT07-66) ------------------------------------------


def test_ut07_67_recent_runs_with_recommendations() -> None:
    """UT07-67 runs of the kind with recs, newest started_at first, excluded run and limit."""
    old = _run(started=0)
    new = _run(started=5)
    current = _run(started=9)
    _run(started=7)  # no recommendations
    chat = _run(kind="org_review", started=8)
    _put(_rec(old), _rec(new), _rec(new), _rec(current), _rec(chat))
    got = closed_loop.recent_runs_with_recommendations(
        "funding_review", limit=10, exclude_run_id=current
    )
    assert got == [new, old]
    assert closed_loop.recent_runs_with_recommendations(
        "funding_review", limit=1, exclude_run_id=current
    ) == [new]
    assert (
        closed_loop.recent_runs_with_recommendations("chat", limit=0, exclude_run_id=current) == []
    )


@pytest.mark.parametrize(
    ("kind", "limit", "exclude"),
    [("nope", 1, None), ("chat", 11, None), ("chat", -1, None), ("chat", True, None),
     ("chat", 1, "run_bad")],
)  # fmt: skip
def test_ut07_67_recent_runs_bounds(kind: str, limit: int, exclude: str | None) -> None:
    """UT07-67 an unknown run kind, limit outside 0 … 10 or a bad run id → ToolInputError."""
    with pytest.raises(ToolInputError, match="recent_runs_with_recommendations: invalid"):
        closed_loop.recent_runs_with_recommendations(
            kind, limit=limit, exclude_run_id=exclude or _id("run")
        )


def test_ut07_67_accepted_since_latest_decision_accepted() -> None:
    """UT07-67 recs whose latest decision is accepted at or after `since`, newest first."""
    old = _rec(created_at=_at(0))
    new = _rec(created_at=_at(3))
    before = _rec()
    flipped = _rec()
    _put(old, new, before, flipped)
    _decide(old["rec_id"], at=5)
    _decide(new["rec_id"], at=5)
    _decide(before["rec_id"], at=1)
    _decide(flipped["rec_id"], at=5)
    _decide(flipped["rec_id"], "deferred", at=6)
    assert closed_loop.accepted_since(_at(5)) == [new, old]
    with pytest.raises(ToolInputError, match="accepted_since: invalid argument"):
        closed_loop.accepted_since("2026")


def test_ut07_66_outcomes_for_similarity_latest_outcome_newest_first() -> None:
    """UT07-66 each rec with its latest outcome, newest measured_at first; unmeasured absent."""
    a, b, c = _rec(), _rec(), _rec()
    _put(a, b, c)
    closed_loop.insert_outcome(_outcome(a["rec_id"], 1, measured_at=_at(10)))
    closed_loop.insert_outcome(_outcome(a["rec_id"], 2, measured_at=_at(30), verdict="worse"))
    closed_loop.insert_outcome(_outcome(b["rec_id"], 1, measured_at=_at(20)))
    got = closed_loop.outcomes_for_similarity()
    assert [(r["rec_id"], r["verdict"]) for r in got] == [
        (a["rec_id"], "worse"),
        (b["rec_id"], "paid_off"),
    ]
    assert got[0] == {
        "rec_id": a["rec_id"],
        "kind": "fund",
        "target_type": "service",
        "target_id": "svc-a",
        "expected_metric": "lead_time",
        "summary": a["summary"],
        "verdict": "worse",
        "measured_at": _at(30),
        "query_id": "q_" + "0" * 16,
    }


def _memory(rec_id: str, kind: str, created: float) -> str:
    mid = _id("mem")
    _write(
        "INSERT INTO memory_item (memory_id, layer, kind, content, data, provenance,"
        " confidence, status, created_at) VALUES (?, 'episodic', ?, 'c', ?, '{}', 0.5,"
        " 'active', ?)",
        (mid, kind, core.dump_json({"rec_id": rec_id}, field="data"), _at(created)),
    )
    return mid


def test_ut07_67_rec_memory_ids_by_kind_oldest_first() -> None:
    """UT07-67 memory ids citing the recs with the given kinds, oldest first."""
    rec, other = _id("rec"), _id("rec")
    note = _memory(rec, "decision_note", 2)
    summary = _memory(rec, "outcome_summary", 1)
    _memory(rec, "run_summary", 0)  # kind outside the allowed set
    _memory(other, "decision_note", 0)
    both = ["outcome_summary", "decision_note", "decision_note"]
    assert closed_loop.rec_memory_ids([rec], kinds=both) == [summary, note]
    assert closed_loop.rec_memory_ids([rec], kinds=["decision_note"]) == [note]
    assert closed_loop.rec_memory_ids([], kinds=["decision_note"]) == []
    for kinds in ([], ["run_summary"]):
        with pytest.raises(ToolInputError, match="rec_memory_ids: invalid argument"):
            closed_loop.rec_memory_ids([rec], kinds=kinds)


def test_ut07_67_rec_memory_ids_uses_the_partial_index() -> None:
    """UT07-67 the constant kind predicate lets the planner use ix_memory_rec (070)."""
    sql = _closed_loop_rows.REC_MEMORY.format(ids="?", kinds="?")
    plan = core.read_all(f"EXPLAIN QUERY PLAN {sql}", (_id("rec"), "decision_note"))
    assert "ix_memory_rec" in " ".join(str(r["detail"]) for r in plan)


# --- U07-36 run-end, promotion, maintenance lookups (UT07-63, UT07-78, UT07-84) -------------


def test_ut07_63_dead_task_count_and_task_spec() -> None:
    """UT07-63 dead tasks of the run are counted; task_spec parses spec or returns None."""
    run_id = _run()
    _task(run_id, status="dead")
    _task(run_id, status="dead")
    tid = _task(run_id, status="done", spec='{"goal": "g", "n": [1]}')
    _task(_run(), status="dead")
    assert closed_loop.dead_task_count(run_id) == 2
    assert closed_loop.dead_task_count(_id("run")) == 0
    assert closed_loop.task_spec(tid) == {"goal": "g", "n": [1]}
    assert closed_loop.task_spec(_id("task")) is None
    with pytest.raises(ToolInputError, match="invalid id: task_spec"):
        closed_loop.task_spec("task_1")
    with pytest.raises(ToolInputError, match="invalid id: dead_task_count"):
        closed_loop.dead_task_count("run_1")


def test_ut07_63_task_spec_not_an_object_is_schema_violation() -> None:
    """UT07-63 a stored spec that is not a JSON object → SchemaViolation naming the task (C3)."""
    tid = _task(_run(), spec="[1]")
    with pytest.raises(SchemaViolation, match=f"task.spec invalid JSON for {tid}"):
        closed_loop.task_spec(tid)


def _finding(run_id: str, status: str, verification: str | None, qids: str = "[]") -> str:
    fid = _id("fnd")
    _write(
        "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, query_ids,"
        " status, verification, created_at) VALUES (?, ?, ?, 'analyst', 'c', ?, ?, ?, ?)",
        (fid, run_id, _id("task"), qids, status, verification, _at()),
    )
    return fid


def test_ut07_78_run_findings_for_promotion() -> None:
    """UT07-78 verified findings plus rejected ones with failed verification, by finding_id."""
    run_id = _run()
    verified = _finding(run_id, "verified", '{"passed": true}', '["q_' + "1" * 16 + '"]')
    failed = _finding(run_id, "rejected", '{"passed": false}')
    _finding(run_id, "rejected", '{"passed": true}')
    _finding(run_id, "rejected", None)
    _finding(run_id, "proposed", None)
    _finding(_run(), "verified", None)
    got = closed_loop.run_findings_for_promotion(run_id)
    assert [f["finding_id"] for f in got] == sorted([verified, failed])
    by_id = {f["finding_id"]: f for f in got}
    assert by_id[verified]["query_ids"] == ["q_" + "1" * 16]
    assert by_id[failed]["verification"] == {"passed": False}
    assert by_id[failed]["status"] == "rejected"
    with pytest.raises(ToolInputError, match="invalid id: run_findings_for_promotion"):
        closed_loop.run_findings_for_promotion("fnd_1")


def test_ut07_84_recent_done_runs_since_by_finished_at() -> None:
    """UT07-84 done runs finished at or after `since`, ordered by finished_at."""
    early = _run(finished=1)
    late = _run(finished=3)
    _run(finished=0)  # before since
    _run(status="failed", finished=4)
    _run(status="running")
    assert closed_loop.recent_done_runs(_at(1)) == [early, late]
    with pytest.raises(ToolInputError, match="recent_done_runs: invalid argument"):
        closed_loop.recent_done_runs("x")


# --- C3 / C5 plumbing ------------------------------------------------------------------------


def test_ut07_67_corrupt_json_column_is_schema_violation() -> None:
    """UT07-67 a stored JSON column of the wrong shape → SchemaViolation naming the rec (C3)."""
    rec = _rec()
    _put(rec)
    _write("UPDATE recommendation SET numbers = '{}' WHERE rec_id = ?", (rec["rec_id"],))
    with pytest.raises(SchemaViolation, match=r"recommendation\.numbers invalid JSON for rec_"):
        closed_loop.run_recommendations(rec["run_id"])


class _Failing:
    def __init__(self, exc: sqlite3.Error) -> None:
        self.exc = exc

    def execute(self, *_: object) -> Iterator[object]:
        raise self.exc


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (sqlite3.OperationalError("database is locked"), StoreBusy),
        (sqlite3.DatabaseError("disk image is malformed"), SchemaViolation),
    ],
)
def test_ut07_67_read_errors_are_mapped(exc: sqlite3.Error, expected: type[Exception]) -> None:
    """UT07-67 busy/locked → StoreBusy, any other sqlite3.Error → SchemaViolation (C5)."""
    conn: Any = _Failing(exc)
    with pytest.raises(expected) as info:
        closed_loop.outcomes_for_similarity(conn=conn)
    assert "disk image" not in str(info.value)


def test_ut07_68_package_reexports_the_07_closed_loop_block() -> None:
    """UT07-68 `herness.store.ops` re-exports every closed_loop name; no `get_run` (R-09)."""
    for name in closed_loop.__all__:
        assert getattr(ops, name) is getattr(closed_loop, name)
    assert not hasattr(closed_loop, "get_run")


def test_ut07_67_unparsable_json_text_is_schema_violation() -> None:
    """UT07-67 JSON text that does not parse → SchemaViolation naming the row, not the text."""
    with pytest.raises(SchemaViolation, match=r"outcome\.details invalid JSON for out_1$"):
        _closed_loop_rows.load_typed("{secret", dict, "outcome.details", "out_1")
