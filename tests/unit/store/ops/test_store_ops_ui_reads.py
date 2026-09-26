"""Unit tests for the ops area ``ui_reads`` (impl 09 U09-52, T09-04; UT09-44, TH09-13).

Rows are inserted with raw SQL through ``core.run_write`` on the real migrated ``ops_store``
fixture (T11-40); ``core.read_all`` is spied on to count statements and placeholders.
"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herness.core import time as clock
from herness.store import ops
from herness.store.ops import core, ui_reads
from herness.store.ops.ui_reads import (
    ui_evidence_ids_present,
    ui_failed_jobs_since,
    ui_finding_ids_with_status,
    ui_finding_query_ids,
    ui_finding_status_counts,
    ui_get_evidence_rows,
    ui_get_recommendation,
    ui_job_exists,
    ui_job_status_counts,
    ui_jobs_for_run,
    ui_latest_decisions,
    ui_list_findings_for_entity,
    ui_list_memory_items,
    ui_list_outcomes,
    ui_list_recommendations,
    ui_list_resilience_events,
    ui_list_run_findings,
    ui_list_runs,
    ui_list_source_health,
    ui_list_tasks,
    ui_oldest_queued_age_s,
    ui_run_rec_ids,
    ui_task_status_counts,
)

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
_INJECT = "x' OR '1'='1'; DROP TABLE finding; --"

type Calls = list[tuple[str, list[object], int]]


def _ts(minutes: int) -> str:
    return clock.format_utc(_T0 + timedelta(minutes=minutes))


def _qid(n: int) -> str:
    return f"q_{n:016x}"


def _write(sql: str, rows: Sequence[Sequence[object]]) -> None:
    def fn(conn: sqlite3.Connection) -> None:
        conn.executemany(sql, rows)

    core.run_write(fn, op="test_seed")


@pytest.fixture
def store(ops_store: object) -> object:
    """The real migrated ops store (T11-40)."""
    return ops_store


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch, store: object) -> Calls:
    """Spy on ``core.read_all``: (sql, params, max_rows) per statement."""
    seen: Calls = []
    real: Callable[..., list[sqlite3.Row]] = core.read_all

    def spy(
        sql: str, params: Sequence[object] = (), *, max_rows: int = 100_000
    ) -> list[sqlite3.Row]:
        seen.append((sql, list(params), max_rows))
        return real(sql, params, max_rows=max_rows)

    monkeypatch.setattr(core, "read_all", spy)
    return seen


def _rec_ids(rows: Sequence[Mapping[str, object]]) -> list[object]:
    return [r["rec_id"] for r in rows]


def _last_bound(calls: Calls) -> object:
    """The last bound parameter (the LIMIT) of the latest statement."""
    return calls[-1][1][-1]


def _evidence(n: int) -> tuple[object, ...]:
    return (
        _qid(n),
        "run_1",
        "b1",
        "SELECT 1",
        "h",
        json.dumps({"a": n}),
        '[{"x": 1}]',
        1,
        2,
        _ts(n),
    )


def _seed_evidence(ns: Sequence[int]) -> None:
    _write(
        "INSERT INTO evidence (query_id, run_id, build_id, sql, result_hash, params,"
        " result_sample, row_count, duration_ms, executed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [_evidence(n) for n in ns],
    )


def _seed_run(
    run_id: str, *, kind: str = "funding_review", status: str = "done", at: int = 0
) -> None:
    _write(
        "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at,"
        " token_usage, cost_usd) VALUES (?,?,?,?,?,?,?,?,?)",
        [(run_id, kind, "fast", "p", "c", status, _ts(at), '{"input": 3}', "1.25")],
    )


def _seed_task(task_id: str, run_id: str, role: str, status: str, at: int) -> None:
    spec = json.dumps({"dedup_key": task_id, "objective": "obj " + task_id})
    _write(
        "INSERT INTO task (task_id, run_id, role, spec, status, attempts, last_error,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [(task_id, run_id, role, spec, status, 1, None, _ts(at), _ts(at))],
    )


def _finding(fid: str, run_id: str, status: str, at: int, **kw: object) -> tuple[object, ...]:
    return (
        fid,
        run_id,
        "t1",
        "analyst",
        "claim [[n1]] " + fid,
        kw.get("entity_type", "candidate"),
        kw.get("entity_id", "c1"),
        '[{"id": "n1"}]',
        kw.get("query_ids", json.dumps([_qid(1)])),
        0.5,
        status,
        _ts(at),
    )


def _seed_findings(rows: Sequence[tuple[object, ...]]) -> None:
    _write(
        "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, entity_type,"
        " entity_id, numbers, query_ids, confidence, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _seed_recs(rows: Sequence[tuple[str, str, str, str, int]]) -> None:
    """(rec_id, run_id, kind, target_id, created minute)."""
    _write(
        "INSERT INTO recommendation (rec_id, run_id, target_type, target_id, summary, kind,"
        " numbers, confidence_basis, finding_ids, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (r, run, "candidate", tgt, "sum", kind, "[]", '{"b": 1}', '["f1"]', _ts(at))
            for r, run, kind, tgt, at in rows
        ],
    )


def _seed_job(job_id: str, status: str, payload: object, at: int, **kw: object) -> None:
    _write(
        "INSERT INTO job (job_id, kind, gpu_class, status, priority, payload, last_error,"
        " attempts, max_attempts, scheduled_for, created_at, finished_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                job_id,
                "review",
                "none",
                status,
                50,
                json.dumps(payload),
                kw.get("last_error"),
                0,
                3,
                _ts(at),
                _ts(at),
                kw.get("finished_at"),
            )
        ],
    )


# --- chunking (UT09-44) -------------------------------------------------------------------


def test_ut09_44_evidence_presence_is_chunked(calls: Calls) -> None:
    """UT09-44 1,200 ids -> three statements of 500, 500, 200 placeholders; present ids found."""
    _seed_evidence([0, 600, 1199])
    ids = [_qid(n) for n in range(1200)]
    assert ui_evidence_ids_present(ids + ids[:10]) == {_qid(0), _qid(600), _qid(1199)}
    assert [sql.count("?") for sql, _, _ in calls] == [500, 500, 200]
    assert all(len(params) <= 500 for _, params, _ in calls)
    assert all("'" not in sql.split("IN")[1] for sql, _, _ in calls)


def test_ut09_44_empty_id_collections_run_no_statement(calls: Calls) -> None:
    """UT09-44 an empty collection runs no query and returns an empty result."""
    assert ui_evidence_ids_present([]) == set()
    assert ui_get_evidence_rows(()) == {}
    assert ui_finding_ids_with_status(set(), "verified") == set()
    assert ui_finding_query_ids([]) == {}
    assert ui_latest_decisions([]) == {}
    assert ui_list_outcomes([]) == {}
    assert calls == []


def test_ut09_44_get_evidence_rows_parses_json(calls: Calls) -> None:
    """UT09-44 evidence rows keyed by query_id, params and result_sample parsed, chunked."""
    _seed_evidence([3, 700])
    rows = ui_get_evidence_rows([_qid(n) for n in range(1001)])
    assert set(rows) == {_qid(3), _qid(700)}
    assert rows[_qid(3)]["params"] == {"a": 3}
    assert rows[_qid(3)]["result_sample"] == [{"x": 1}]
    assert rows[_qid(700)]["build_id"] == "b1"
    assert [sql.count("?") for sql, _, _ in calls] == [500, 500, 1]


def test_ut09_44_finding_ids_with_status_chunked(calls: Calls) -> None:
    """UT09-44 status bound after each chunk's ids; only matching ids come back."""
    ids = [f"fnd_{n:04d}" for n in range(1200)]
    _seed_findings([_finding(ids[1], "r1", "verified", 0), _finding(ids[900], "r1", "rejected", 1)])
    assert ui_finding_ids_with_status(ids, "verified") == {ids[1]}
    assert [len(p) for _, p, _ in calls] == [501, 501, 201]
    assert all(p[-1] == "verified" for _, p, _ in calls)


def test_ut09_44_finding_query_ids_parsed(store: object) -> None:
    """UT09-44 finding.query_ids parsed to a list; a non-list JSON value reads as []."""
    _seed_findings(
        [
            _finding("f1", "r1", "verified", 0, query_ids=json.dumps([_qid(1), _qid(2)])),
            _finding("f2", "r1", "verified", 1, query_ids="{}"),
        ]
    )
    assert ui_finding_query_ids(["f1", "f2", "f9"]) == {"f1": [_qid(1), _qid(2)], "f2": []}


# --- recommendations, decisions, outcomes ---------------------------------------------------


def test_ut09_44_run_rec_ids_and_get_recommendation(store: object) -> None:
    """UT09-44 rec ids of a run; PK read parses numbers, confidence_basis and finding_ids."""
    _seed_recs([("r1", "run_a", "fund", "c1", 0), ("r2", "run_a", "fund", "c2", 1)])
    _seed_recs([("r3", "run_b", "org_action", "c1", 2)])
    assert ui_run_rec_ids("run_a") == {"r1", "r2"}
    rec = ui_get_recommendation("r3")
    assert rec is not None
    assert rec["confidence_basis"] == {"b": 1}
    assert rec["finding_ids"] == ["f1"]
    assert rec["numbers"] == []
    assert rec["kind"] == "org_action"
    assert ui_get_recommendation("missing") is None


def test_ut09_44_list_recommendations_null_tolerant_filters(store: object) -> None:
    """UT09-44 each optional filter narrows; unset filters match all; window is inclusive."""
    _seed_recs(
        [
            ("r1", "run_a", "fund", "c1", 0),
            ("r2", "run_a", "org_action", "c2", 10),
            ("r3", "run_b", "fund", "c1", 20),
        ]
    )
    assert _rec_ids(ui_list_recommendations()) == ["r3", "r2", "r1"]
    assert _rec_ids(ui_list_recommendations(run_id="run_a")) == ["r2", "r1"]
    assert _rec_ids(ui_list_recommendations(kind="fund")) == ["r3", "r1"]
    assert _rec_ids(ui_list_recommendations(target_id="c1", kind="fund", run_id="run_b")) == ["r3"]
    window = ui_list_recommendations(
        created_from=_T0 + timedelta(minutes=10), created_to=_T0 + timedelta(minutes=20)
    )
    assert _rec_ids(window) == ["r3", "r2"]
    assert _rec_ids(ui_list_recommendations(created_to=_T0)) == ["r1"]
    assert _rec_ids(ui_list_recommendations(limit=1)) == ["r3"]


def test_ut09_44_latest_decision_per_rec_via_window_function(calls: Calls) -> None:
    """UT09-44 latest decided_at per rec_id; a tie goes to the later insert."""
    _seed_recs([("r1", "run", "fund", "c", 0), ("r2", "run", "fund", "c", 0)])
    _seed_recs([("r3", "run", "fund", "c", 0)])
    _write(
        "INSERT INTO decision_log (rec_id, decision, reason, decided_by, decided_at,"
        " effective_at) VALUES (?,?,?,?,?,?)",
        [
            ("r1", "deferred", "wait", "u1", _ts(1), _ts(1)),
            ("r1", "accepted", None, "u2", _ts(5), _ts(5)),
            ("r1", "rejected", "old", "u3", _ts(3), _ts(3)),
            ("r2", "accepted", None, "u1", _ts(2), _ts(2)),
            ("r2", "rejected", "tie", "u2", _ts(2), _ts(2)),
        ],
    )
    got = ui_latest_decisions(["r1", "r2", "r3"])
    assert set(got) == {"r1", "r2"}
    assert got["r1"]["decision"] == "accepted"
    assert got["r1"]["decided_by"] == "u2"
    assert got["r2"]["reason"] == "tie"
    assert "OVER (PARTITION BY rec_id" in calls[0][0]


def test_ut09_44_list_outcomes_ordered_by_measurement(store: object) -> None:
    """UT09-44 outcomes grouped per rec_id, ordered by measurement, details parsed."""
    _seed_recs([("r1", "run", "fund", "c", 0), ("r2", "run", "fund", "c", 0)])
    _write(
        "INSERT INTO outcome (outcome_id, rec_id, measurement, measured_at, metric, verdict,"
        " details) VALUES (?,?,?,?,?,?,?)",
        [
            ("o2", "r1", 2, _ts(2), "m", "paid_off", '{"k": 2}'),
            ("o1", "r1", 1, _ts(1), "m", "inconclusive", "{}"),
            ("o3", "r2", 1, _ts(1), "m", "worse", "{}"),
        ],
    )
    got = ui_list_outcomes(["r1", "r2", "r9"])
    assert [o["outcome_id"] for o in got["r1"]] == ["o1", "o2"]
    assert got["r1"][1]["details"] == {"k": 2}
    assert [o["verdict"] for o in got["r2"]] == ["worse"]
    assert "r9" not in got


# --- findings, tasks, runs ------------------------------------------------------------------


def test_ut09_44_finding_reads(store: object) -> None:
    """UT09-44 status counts, entity findings newest first, run findings oldest first."""
    _seed_findings(
        [
            _finding("f1", "r1", "verified", 0),
            _finding("f2", "r1", "verified", 5),
            _finding("f3", "r1", "rejected", 2),
            _finding("f4", "r2", "verified", 1, entity_id="c2"),
        ]
    )
    assert ui_finding_status_counts("r1") == {"verified": 2, "rejected": 1}
    ent = ui_list_findings_for_entity("candidate", "c1")
    assert [f["finding_id"] for f in ent] == ["f2", "f1"]
    assert ent[0]["numbers"] == [{"id": "n1"}]
    assert ent[0]["query_ids"] == [_qid(1)]
    assert ent[0]["claim"].startswith("claim [[n1]]")
    assert [f["finding_id"] for f in ui_list_run_findings("r1")] == ["f1", "f2"]
    assert [f["finding_id"] for f in ui_list_run_findings("r1", status="rejected")] == ["f3"]
    assert [f["finding_id"] for f in ui_list_findings_for_entity("candidate", "c1", limit=1)] == [
        "f2"
    ]


def test_ut09_44_task_reads(store: object) -> None:
    """UT09-44 task counts by role and status; task projection with objective and filter."""
    _seed_run("run_1")
    _seed_task("t2", "run_1", "analyst", "done", 2)
    _seed_task("t1", "run_1", "analyst", "pending", 1)
    _seed_task("t3", "run_1", "judge", "done", 3)
    assert ui_task_status_counts("run_1") == [
        ("analyst", "done", 1),
        ("analyst", "pending", 1),
        ("judge", "done", 1),
    ]
    tasks = ui_list_tasks("run_1")
    assert [t["task_id"] for t in tasks] == ["t1", "t2", "t3"]
    assert tasks[0]["objective"] == "obj t1"
    assert set(tasks[0]) == {"task_id", "role", "status", "attempts", "objective", "last_error"}
    assert [t["task_id"] for t in ui_list_tasks("run_1", status="done")] == ["t2", "t3"]


def test_ut09_44_list_runs_filters_and_order(store: object) -> None:
    """UT09-44 runs newest first; kind and status filters; token_usage parsed."""
    _seed_run("a", at=0)
    _seed_run("b", kind="org_review", at=5)
    _seed_run("c", status="failed", at=9)
    assert [r["run_id"] for r in ui_list_runs()] == ["c", "b", "a"]
    assert [r["run_id"] for r in ui_list_runs(kind="org_review")] == ["b"]
    assert [r["run_id"] for r in ui_list_runs(status="done", kind="funding_review")] == ["a"]
    row = ui_list_runs(limit=1)[0]
    assert row["token_usage"] == {"input": 3}
    assert row["cost_usd"] == "1.25"


def test_ut09_44_limits_clamped_to_caps(calls: Calls) -> None:
    """UT09-44 a limit above 500 binds 500 (max_rows 500); a limit below 1 binds 1."""
    _write(
        "INSERT INTO memory_item (memory_id, layer, kind, content, status, created_at)"
        " VALUES (?,?,?,?,?,?)",
        [(f"m{n:04d}", "semantic", "fact", "c", "active", _ts(n)) for n in range(510)],
    )
    assert len(ui_list_memory_items(limit=10_000)) == 500
    assert _last_bound(calls) == 500
    assert calls[-1][2] == 500
    assert len(ui_list_memory_items(limit=0)) == 1
    assert _last_bound(calls) == 1
    for fn in (ui_list_runs, ui_list_recommendations, ui_list_resilience_events):
        fn(limit=999)
        assert _last_bound(calls) == 500
    ui_list_tasks("r", limit=-5)
    assert _last_bound(calls) == 1
    ui_jobs_for_run("r", limit=501)
    assert calls[-1][2] == 500


def test_ut09_44_memory_items_filters(store: object) -> None:
    """UT09-44 memory items by layer and status, newest first, data and provenance parsed."""
    _write(
        "INSERT INTO memory_item (memory_id, layer, kind, content, data, provenance, status,"
        " created_at) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("m1", "semantic", "fact", "c1", '{"d": 1}', '{"p": 1}', "active", _ts(0)),
            ("m2", "episodic", "run", "c2", "{}", "{}", "active", _ts(1)),
            ("m3", "semantic", "fact", "c3", "{}", "{}", "expired", _ts(2)),
        ],
    )
    assert [m["memory_id"] for m in ui_list_memory_items()] == ["m3", "m2", "m1"]
    got = ui_list_memory_items(layer="semantic", status="active")
    assert [m["memory_id"] for m in got] == ["m1"]
    assert got[0]["data"] == {"d": 1}
    assert got[0]["provenance"] == {"p": 1}


# --- jobs, resilience, source health -----------------------------------------------------------


def test_ut09_44_jobs_for_run_matches_both_payload_paths(store: object) -> None:
    """UT09-44 `$.run_id` and `$.request.run_id` both match, newest first; others excluded."""
    _seed_job("j1", "done", {"run_id": "run_x"}, 0)
    _seed_job("j2", "failed", {"request": {"run_id": "run_x"}}, 5, last_error='{"class": "E"}')
    _seed_job("j3", "done", {"run_id": "run_y"}, 9)
    _seed_job("j4", "done", {"note": "run_x"}, 7)
    got = ui_jobs_for_run("run_x")
    assert [j["job_id"] for j in got] == ["j2", "j1"]
    assert got[0]["last_error"] == {"class": "E"}
    assert got[1]["last_error"] is None
    assert "payload" not in got[0]
    assert [j["job_id"] for j in ui_jobs_for_run("run_x", limit=1)] == ["j2"]


def test_ut09_44_job_existence_counts_and_queue_age(store: object) -> None:
    """UT09-44 job_exists by PK; counts by status; oldest queued age from min created_at."""
    now = _T0 + timedelta(minutes=30)
    assert ui_oldest_queued_age_s(now) is None
    assert ui_job_status_counts() == {}
    _seed_job("j1", "queued", {}, 10)
    _seed_job("j2", "queued", {}, 5)
    _seed_job("j3", "running", {}, 1)
    assert ui_job_exists("j1") is True
    assert ui_job_exists("nope") is False
    assert ui_job_status_counts() == {"queued": 2, "running": 1}
    assert ui_oldest_queued_age_s(now) == 25 * 60.0
    assert ui_oldest_queued_age_s(_T0) == 0.0


def test_ut09_44_failed_jobs_since(store: object) -> None:
    """UT09-44 failed jobs with finished_at >= since, newest first; last_error parsed."""
    _seed_job("j1", "failed", {}, 0, finished_at=_ts(10), last_error='{"class": "A"}')
    _seed_job("j2", "failed", {}, 0, finished_at=_ts(20), last_error='{"class": "B"}')
    _seed_job("j3", "failed", {}, 0, finished_at=_ts(5), last_error='{"class": "C"}')
    _seed_job("j4", "done", {}, 0, finished_at=_ts(30))
    got = ui_failed_jobs_since(_T0 + timedelta(minutes=10))
    assert [j["job_id"] for j in got] == ["j2", "j1"]
    assert got[0]["last_error"] == {"class": "B"}
    assert [j["job_id"] for j in ui_failed_jobs_since(_T0, limit=1)] == ["j2"]


def test_ut09_44_resilience_events_filters(store: object) -> None:
    """UT09-44 resilience events by run_id and job_id, newest ts first, detail parsed."""
    _write(
        "INSERT INTO resilience_event (event_id, ts, kind, component, run_id, job_id, detail)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            ("e1", _ts(0), "retry", "jobs", "r1", "j1", '{"n": 1}'),
            ("e2", _ts(1), "retry", "jobs", "r1", "j2", "{}"),
            ("e3", _ts(2), "breaker", "sync", None, None, "{}"),
        ],
    )
    assert [e["event_id"] for e in ui_list_resilience_events()] == ["e3", "e2", "e1"]
    assert [e["event_id"] for e in ui_list_resilience_events(run_id="r1")] == ["e2", "e1"]
    got = ui_list_resilience_events(run_id="r1", job_id="j1")
    assert [e["event_id"] for e in got] == ["e1"]
    assert got[0]["detail"] == {"n": 1}


def test_ut09_44_source_health_capped_at_200(calls: Calls) -> None:
    """UT09-44 all source_health rows ordered by source, at most 200."""
    _write(
        "INSERT INTO source_health (source, state, updated_at) VALUES (?,?,?)",
        [(f"s{n:03d}", "open" if n == 0 else "closed", _ts(0)) for n in range(205)],
    )
    rows = ui_list_source_health()
    assert len(rows) == 200
    assert rows[0]["source"] == "s000"
    assert rows[0]["state"] == "open"
    assert calls[-1][2] == 200


# --- TH09-13 and module checks -----------------------------------------------------------------


def test_ut09_44_th09_13_injection_looking_filters_are_bound(store: object) -> None:
    """UT09-44 TH09-13 injection-looking input matches nothing and changes nothing."""
    _seed_findings([_finding("f1", "r1", "verified", 0)])
    _seed_recs([("r1", "run", "fund", "c", 0)])
    before = core.connection().total_changes
    assert ui_list_recommendations(run_id=_INJECT, kind=_INJECT, target_id=_INJECT) == []
    assert ui_list_run_findings(_INJECT, status=_INJECT) == []
    assert ui_list_findings_for_entity(_INJECT, _INJECT) == []
    assert ui_finding_ids_with_status([_INJECT, "f1"], _INJECT) == set()
    assert ui_evidence_ids_present([_INJECT]) == set()
    assert ui_list_memory_items(layer=_INJECT) == []
    assert ui_list_resilience_events(job_id=_INJECT) == []
    assert ui_jobs_for_run(_INJECT) == []
    assert ui_job_exists(_INJECT) is False
    assert ui_list_tasks(_INJECT, status=_INJECT) == []
    assert ui_list_runs(kind=_INJECT) == []
    assert ui_latest_decisions([_INJECT]) == {}
    assert ui_list_outcomes([_INJECT]) == {}
    assert core.connection().total_changes == before
    assert ui_finding_ids_with_status(["f1"], "verified") == {"f1"}


def test_ut09_44_sql_has_no_string_formatting() -> None:
    """UT09-44 the module has no f-string and no `%` formatting (review check, TH09-13)."""
    source = Path(inspect.getfile(ui_reads)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)]
    mods = [n for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod)]
    assert mods == []
    assert ".format(" not in source


def test_ut09_44_public_names_are_prefixed() -> None:
    """UT09-44 every public name defined in the module (and re-exported) starts with ui_/Ui."""
    own = [
        name
        for name, obj in vars(ui_reads).items()
        if not name.startswith("_") and getattr(obj, "__module__", None) == ui_reads.__name__
    ]
    assert len(own) == 34
    assert all(name.startswith(("ui_", "Ui")) for name in own)
    for name in own:
        assert getattr(ops, name) is getattr(ui_reads, name)
        assert name in ops.__all__
