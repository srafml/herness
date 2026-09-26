"""Tests for herness.harness.pipelines.org_review (T06-12): U06-73, U06-74 (UT06-52, UT06-53).

UT06-52 builds a local tiny DuckDB in `tmp_path` with only the tables the org queries read
(`score.org`, `score.action_lever`, `core.team`); it switches to the spec 11 `tiny_build` fixture
when T11-17 (tests/support/builds.py) lands. `_Reader` implements the `RecordedReader` contract
over that DuckDB and hands out a distinct `query_id` per call (precedent: T06-07 UT06-34).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from herness.core.types import EntityScope, Finding, ReportDraft
from herness.harness.findings import compute_dedup_key
from herness.harness.pipelines.base import Pipeline, PlanContext, get_pipeline
from herness.harness.pipelines.org_review import OrgReviewPipeline
from herness.harness.pipelines.settings import PipelinesConfig, resolve_knobs

pytestmark = pytest.mark.unit

_ULID = "01J9ZQ4Y8M6V3K2N1P0R5T7W9"
_RUN = f"run_{_ULID}A"
_PRIOR_RUN = f"run_{_ULID}B"
_TASK = f"task_{_ULID}A"
_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
_END = date(2026, 9, 26)
_ANALYST_TOOLS = [
    "describe_table", "get_cluster", "get_metric", "get_record", "get_scores", "list_findings",
    "list_tables", "post_finding", "propose_memory", "recall_memory", "request_subtask", "run_sql",
    "semantic_search",
]  # fmt: skip


def _q(n: int) -> str:
    return f"q_{n:016x}"


# team -> (min rank rows), levers (metric, delta_usd, query id n), org
_SCORE_ORG = [
    ("team", "t1", "mttr", 3, [_q(101)]),
    ("team", "t1", "reopen", 1, [_q(102)]),
    ("team", "t2", "mttr", 2, [_q(103)]),
    ("team", "t3", "mttr", 4, [_q(104)]),
    ("team", "t3", "sla", 5, [_q(105)]),
    ("team", "t4", "mttr", 6, [_q(106)]),
    ("team", "t5", "mttr", 7, [_q(107)]),
    ("service", "s1", "mttr", 0, [_q(108)]),
]
_LEVERS = [
    ("team", "t1", "mttr", "peer_median", 10.0, 6.0, "5000.00", [_q(201)]),
    ("team", "t1", "reopen_rate", "peer_median", 0.2, 0.1, "9000.00", [_q(202)]),
    ("team", "t1", "cfr", "peer_median", 0.3, 0.2, "5000.00", [_q(203)]),
    ("team", "t1", "sla_breach", "peer_median", 0.1, 0.05, "100.00", [_q(204)]),
    ("team", "t2", "mttr", "peer_median", 9.0, 7.0, "700.00", [_q(205)]),
    ("service", "s1", "mttr", "peer_median", 9.0, 7.0, "99999.00", [_q(206)]),
]
_TEAMS = [("t1", "o1"), ("t2", "o1"), ("t3", "o2"), ("t4", "o2"), ("t5", "o3")]


def _make_build(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA score")
    con.execute("CREATE SCHEMA core")
    con.execute(
        "CREATE TABLE score.org (entity_type VARCHAR, entity_id VARCHAR, metric VARCHAR,"
        " rank INTEGER, query_ids VARCHAR[])"
    )
    con.executemany("INSERT INTO score.org VALUES (?, ?, ?, ?, ?)", _SCORE_ORG)
    con.execute(
        "CREATE TABLE score.action_lever (entity_type VARCHAR, entity_id VARCHAR, metric VARCHAR,"
        " target_kind VARCHAR, current_value DOUBLE, target_value DOUBLE,"
        " delta_usd DECIMAL(18,2), query_ids VARCHAR[])"
    )
    con.executemany("INSERT INTO score.action_lever VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _LEVERS)
    con.execute("CREATE TABLE core.team (team_id VARCHAR, org_id VARCHAR)")
    con.executemany("INSERT INTO core.team VALUES (?, ?)", _TEAMS)
    con.close()


@dataclass(frozen=True)
class _Result:
    query_id: str
    columns: list[str]
    rows: list[tuple[object, ...]]
    row_count: int


class _Reader:
    """`RecordedReader` over the tiny DuckDB: one distinct `query_id` per call."""

    def __init__(self, path: Path) -> None:
        self._con = duckdb.connect(str(path), read_only=True)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, sql: str, params: dict[str, object]) -> _Result:
        self.calls.append((sql, params))
        cur = self._con.execute(sql, params)
        columns = [d[0] for d in cur.description or []]
        rows = [tuple(r) for r in cur.fetchall()]
        return _Result(_q(len(self.calls)), columns, rows, len(rows))


@pytest.fixture
def reader(tmp_path: Path) -> _Reader:
    path = tmp_path / "wh-tiny.duckdb"
    _make_build(path)
    return _Reader(path)


def _ctx(depth: str = "fast", override: dict[str, int] | None = None, **fields: Any) -> PlanContext:
    knobs = resolve_knobs(PipelinesConfig(), "org_review", depth, override=override)  # type: ignore[arg-type]
    kwargs: dict[str, Any] = {
        "run_id": _RUN,
        "kind": "org_review",
        "depth": depth,
        "profile": "local",
        "build_id": "20260926-120000-ABCDEF",
        "question": "Which teams need help?",
        "focus": None,
        "knobs": knobs,
        "dq_warnings": [],
        "unconfirmed_weights": ["w1"],
        "prior_context": "ctx",
        "prior_recs": [],
        "portfolio": {
            "scenario": "base",
            "rows": [],
            "selected": [],
            "query_ids": [],
            "custom": [],
        },
    }
    kwargs.update(fields)
    return PlanContext.model_validate(kwargs)


def _prior(rec: str, target: str, *, outcome: dict[str, Any] | None, run_id: str = _PRIOR_RUN,
           decision: str | None = "accepted") -> dict[str, Any]:  # fmt: skip
    return {"rec_id": rec, "run_id": run_id, "target_id": target, "decision": decision,
            "outcome": outcome}  # fmt: skip


def _by(tasks: list[Any], entity_id: str, specialty: str) -> Any:
    (task,) = [
        t for t in tasks if t.scope.entity_ids == [entity_id] and t.specialty == specialty
    ]  # fmt: skip
    return task


# --- UT06-52 deterministic_tasks ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("depth", "specialties"),
    [("fast", ["ops"]), ("standard", ["ops", "change"]), ("deep", ["ops", "change", "delivery"])],
)
def test_ut06_52_one_task_per_specialty_per_team(
    reader: _Reader, depth: str, specialties: list[str]
) -> None:
    """UT06-52 each selected team gets one task per `org_specialties` entry of the depth."""
    ctx = _ctx(depth)
    tasks = OrgReviewPipeline(reader, window_end=_END).deterministic_tasks(ctx)
    team_tasks = [t for t in tasks if t.scope.entity_type == "team"]
    assert [(t.scope.entity_ids[0], t.specialty) for t in team_tasks] == [
        (team, s) for team in ["t1", "t2", "t3", "t4", "t5"] for s in specialties
    ]
    t1 = _by(tasks, "t1", specialties[-1])
    assert t1.role == "analyst"
    assert t1.run_id == _RUN
    assert t1.model_role == "analyst"
    assert t1.budget == ctx.knobs.analyst_budget
    assert t1.dedup_key == compute_dedup_key("analyst", t1.specialty, t1.scope, t1.objective)
    expected_tools = [n for n in _ANALYST_TOOLS if depth != "fast" or n != "request_subtask"]
    assert t1.tools == expected_tools
    assert len({t.task_id for t in tasks}) == len(tasks)


def test_ut06_52_objectives_are_exact_templates(reader: _Reader) -> None:
    """UT06-52 ops, change and delivery objectives use the exact U06-74 templates."""
    tasks = OrgReviewPipeline(reader, window_end=_END).deterministic_tasks(_ctx("deep"))
    assert _by(tasks, "t2", "ops").objective == (
        "Review team t2 operations: incident volume, MTTR, reopen and SLA metrics against peers"
        " in score.org."
    )
    assert _by(tasks, "t2", "change").objective == (
        "Review team t2 change health: failure rate and change-caused incidents in"
        " metrics.change_fact."
    )
    assert _by(tasks, "t2", "delivery").objective == (
        "Review team t2 delivery flow: cycle time and unplanned work in metrics.work_item_fact."
    )
    assert _by(tasks, "o1", "org").objective == (
        "Roll up team findings for org o1: compare its teams in score.org and metrics.metric_value."
    )


def test_ut06_52_lever_query_ids_and_notes(reader: _Reader) -> None:
    """UT06-52 the top 3 levers per team feed `inputs.query_ids` and the notes' first line."""
    tasks = OrgReviewPipeline(reader, window_end=_END).deterministic_tasks(_ctx())
    teams_qid, levers_qid = _q(1), _q(2)
    t1 = _by(tasks, "t1", "ops")
    assert t1.inputs.query_ids == [teams_qid, levers_qid, _q(202), _q(203), _q(201)]
    assert _q(204) not in t1.inputs.query_ids  # 4th lever by delta_usd is cut
    assert t1.inputs.notes == "Top action levers: reopen_rate, cfr, mttr."
    t2 = _by(tasks, "t2", "ops")
    assert t2.inputs.query_ids == [teams_qid, levers_qid, _q(205)]
    assert t2.inputs.notes == "Top action levers: mttr."
    t3 = _by(tasks, "t3", "ops")
    assert t3.inputs.query_ids == [teams_qid]
    assert t3.inputs.notes is None
    assert t3.inputs.candidate_ids == []


def test_ut06_52_rollup_for_orgs_with_two_or_more_teams(reader: _Reader) -> None:
    """UT06-52 an `org` task per org owning >= 2 selected teams, priority from min team rank."""
    tasks = OrgReviewPipeline(reader, window_end=_END).deterministic_tasks(_ctx())
    rollups = [t for t in tasks if t.scope.entity_type == "org"]
    assert [t.scope.entity_ids for t in rollups] == [["o1"], ["o2"]]  # o3 owns only t5
    o1, o2 = rollups
    assert (o1.specialty, o1.must_cover, o1.priority) == ("org", False, 99.0)
    assert o2.priority == 96.0  # min rank of t3 (4) and t4 (6)
    assert o1.inputs.query_ids == [_q(3), _q(1)]

    limited = OrgReviewPipeline(reader, window_end=_END).deterministic_tasks(
        _ctx(override={"K_teams": 3})
    )
    assert [t.scope.entity_ids for t in limited if t.scope.entity_type == "org"] == [["o1"]]
    assert {t.scope.entity_ids[0] for t in limited if t.scope.entity_type == "team"} == {
        "t1", "t2", "t3"
    }  # fmt: skip


def test_ut06_52_must_cover_and_priority(reader: _Reader) -> None:
    """UT06-52 the top `M_must` teams are must-cover; priority is U06-91 of rank and flag."""
    ctx = _ctx(override={"M_must": 2})
    pipe = OrgReviewPipeline(reader, window_end=_END)
    assert pipe.must_cover(ctx) == {"team:t1", "team:t2"}
    tasks = pipe.deterministic_tasks(ctx)
    assert (_by(tasks, "t1", "ops").must_cover, _by(tasks, "t1", "ops").priority) == (True, 149.0)
    assert (_by(tasks, "t2", "ops").must_cover, _by(tasks, "t2", "ops").priority) == (True, 148.0)
    assert (_by(tasks, "t3", "ops").must_cover, _by(tasks, "t3", "ops").priority) == (False, 96.0)
    assert len(reader.calls) == 3  # teams read once and cached across must_cover and tasks


def test_ut06_52_focus_drops_limit_and_filters(reader: _Reader) -> None:
    """UT06-52 with `focus` the `K_teams` limit is dropped and teams are filtered by id."""
    focus = EntityScope(entity_type="team", entity_ids=["t5", "t3", "t4"])
    ctx = _ctx(override={"K_teams": 1}, focus=focus)
    tasks = OrgReviewPipeline(reader, window_end=_END).deterministic_tasks(ctx)
    assert [t.scope.entity_ids[0] for t in tasks if t.scope.entity_type == "team"] == [
        "t3", "t4", "t5"
    ]  # fmt: skip
    sql, params = reader.calls[0]
    assert "LIMIT" not in sql
    assert params == {"focus_ids": ["t5", "t3", "t4"]}


def test_ut06_52_no_teams_reads_nothing_more(reader: _Reader) -> None:
    """UT06-52 an empty selection plans no team, lever or rollup task."""
    focus = EntityScope(entity_type="team", entity_ids=["zz"])
    pipe = OrgReviewPipeline(reader, window_end=_END)
    assert pipe.deterministic_tasks(_ctx(focus=focus)) == []
    assert len(reader.calls) == 1


def test_ut06_52_retrospective_and_prior_rec_notes(reader: _Reader) -> None:
    """UT06-52 a retrospective task for prior recs with outcomes; prior-rec lines in notes."""
    other_run = f"run_{_ULID}C"
    prior = [
        _prior("rec_1", "t1", outcome={"verdict": "improved"}),
        _prior("rec_2", "t1", outcome=None, decision=None, run_id=other_run),
        _prior("rec_3", "o1", outcome={"verdict": "no_change"}, run_id=other_run),
    ]
    ctx = _ctx(prior_recs=prior)
    pipe = OrgReviewPipeline(reader, window_end=_END)
    assert {f"run:{_PRIOR_RUN}", f"run:{other_run}"} <= pipe.must_cover(ctx)
    tasks = pipe.deterministic_tasks(ctx)
    retro = tasks[-1]
    assert retro.specialty == "retrospective"
    assert retro.scope.entity_type == "run"
    assert retro.scope.entity_ids == [_PRIOR_RUN, other_run]
    assert (retro.must_cover, retro.priority) == (True, 50.0)
    assert retro.objective == (
        f"Review prior recommendations and their measured outcomes for runs {_PRIOR_RUN},"
        f" {other_run}: compare expected and actual values in the outcome rows."
    )
    assert retro.inputs.notes is not None
    assert "prior rec rec_1: decision accepted, outcome improved" in retro.inputs.notes
    assert _by(tasks, "t1", "ops").inputs.notes == (
        "Top action levers: reopen_rate, cfr, mttr.\n"
        "prior rec rec_1: decision accepted, outcome improved\n"
        "prior rec rec_2: decision none, outcome none"
    )
    assert (
        _by(tasks, "o1", "org").inputs.notes
        == "prior rec rec_3: decision accepted, outcome no_change"
    )


def test_ut06_52_no_retrospective_without_outcomes(reader: _Reader) -> None:
    """UT06-52 prior recs without any outcome add no retrospective task."""
    ctx = _ctx(prior_recs=[_prior("rec_1", "t1", outcome=None)])
    tasks = OrgReviewPipeline(reader, window_end=_END).deterministic_tasks(ctx)
    assert all(t.specialty != "retrospective" for t in tasks)


def test_ut06_52_notes_cut_to_1500_chars(reader: _Reader) -> None:
    """UT06-52 `inputs.notes` is cut to 1,500 characters."""
    prior = [_prior(f"rec_{i:03d}", "t3", outcome=None) for i in range(60)]
    tasks = OrgReviewPipeline(reader, window_end=_END).deterministic_tasks(_ctx(prior_recs=prior))
    notes = _by(tasks, "t3", "ops").inputs.notes
    assert notes is not None
    assert len(notes) == 1500


def test_ut06_52_dq_warnings_match_entity_or_table(reader: _Reader) -> None:
    """UT06-52 DQ check names whose details mention the entity or an org DQ table."""

    def dq(name: str, details: object) -> dict[str, Any]:
        return {"check_name": name, "severity": "warn", "value": 1, "threshold": 0,
                "details": details, "query_id": _q(900)}  # fmt: skip

    warnings = [
        dq("team_t2_gap", {"entity_id": "t2"}),
        dq("incident_nulls", {"table": "core.incident"}),
        dq("unrelated", {"table": "core.service"}),
        dq("o2_note", "org o2 is sparse"),
    ]
    tasks = OrgReviewPipeline(reader, window_end=_END).deterministic_tasks(
        _ctx(dq_warnings=warnings)
    )
    assert _by(tasks, "t2", "ops").inputs.dq_warnings == ["team_t2_gap", "incident_nulls"]
    assert _by(tasks, "t1", "ops").inputs.dq_warnings == ["incident_nulls"]
    assert _by(tasks, "o2", "org").inputs.dq_warnings == ["incident_nulls", "o2_note"]


def test_ut06_52_dq_warnings_match_non_ascii_team_id() -> None:
    """UT06-52 a non-ASCII team id in the DQ details JSON still matches (no ASCII escaping)."""
    results = iter([
        _Result(_q(1), ["entity_id", "rank"], [("équipe-ü", 1)], 1),
        _Result(_q(2), ["entity_id", "metric", "delta_usd", "query_ids"], [], 0),
        _Result(_q(3), ["org_id", "n", "team_ids"], [], 0),
    ])  # fmt: skip
    warning = {"check_name": "accent_gap", "severity": "warn", "value": 1, "threshold": 0,
               "details": {"entity_id": "équipe-ü"}, "query_id": _q(900)}  # fmt: skip
    pipe = OrgReviewPipeline(lambda sql, params: next(results), window_end=_END)
    tasks = pipe.deterministic_tasks(_ctx(dq_warnings=[warning]))
    assert _by(tasks, "équipe-ü", "ops").inputs.dq_warnings == ["accent_gap"]


# --- UT06-53 OrgReviewPipeline, get_pipeline -----------------------------------------------------


def _finding(n: int, *, challenge: list[dict[str, Any]] | None = None) -> Finding:
    return Finding.model_validate(
        {
            "finding_id": f"fnd_{_ULID}{n}",
            "run_id": _RUN,
            "task_id": _TASK,
            "author_role": "analyst",
            "claim": "MTTR is high",
            "entity_type": "team",
            "entity_id": "t1",
            "numbers": [
                {
                    "id": "n1",
                    "value": "1200.00",
                    "unit": "usd",
                    "query_id": _q(101),
                    "column": "c",
                    "row_key": None,
                }
            ],
            "query_ids": [_q(101)],
            "confidence": 0.8,
            "status": "verified",
            "challenge": challenge or [],
            "created_at": _NOW,
        }
    )


def _challenge(verdict: str, concern: str) -> dict[str, Any]:
    checks = [
        {"check": c, "result": "pass", "note": ""}
        for c in ("seasonality", "mis_mapping", "small_sample", "double_counting", "survivorship")
    ]
    checks.insert(0, {"check": "confounding", "result": "concern", "note": concern,
                      "query_ids": [_q(101)]})  # fmt: skip
    return {"finding_id": f"fnd_{_ULID}1", "checks": checks, "verdict": verdict}


def test_ut06_53_writer_input_levers_keep_query_id(reader: _Reader) -> None:
    """UT06-53 writer_input levers are the selected teams' rows, each with its read query_id."""
    ctx = _ctx(override={"K_teams": 2})
    pipe = OrgReviewPipeline(reader, window_end=_END)
    pipe.deterministic_tasks(ctx)
    out = pipe.writer_input(ctx, [_finding(1)])
    levers_qid = _q(len(reader.calls))
    assert out["outline"] == [
        "executive_summary", "recommendations", "org_scorecards", "actions", "retrospective",
        "risks_and_caveats", "method",
    ]  # fmt: skip
    levers = out["levers"]
    assert isinstance(levers, list)
    assert {(r["entity_id"], r["metric"]) for r in levers} == {
        ("t1", "mttr"), ("t1", "reopen_rate"), ("t1", "cfr"), ("t1", "sla_breach"), ("t2", "mttr")
    }  # fmt: skip
    assert all(r["query_id"] == levers_qid for r in levers)
    row = next(r for r in levers if r["metric"] == "reopen_rate")
    assert set(row) == {
        "entity_id", "metric", "target_kind", "current_value", "target_value", "delta_usd",
        "query_ids", "query_id",
    }  # fmt: skip
    assert row["query_ids"] == [_q(202)]
    assert out["kind"] == "org_review"
    assert out["question"] == "Which teams need help?"
    assert out["portfolio"] == ctx.portfolio
    assert (out["unconfirmed_weights"], out["prior_context"]) == (["w1"], "ctx")
    assert out["dq_warnings"] == []
    assert out["prior_recs"] == []


def test_ut06_53_writer_input_findings_and_challenge_summary(reader: _Reader) -> None:
    """UT06-53 findings carry the U06-71 fields; challenge_summary is last verdict + notes."""
    challenged = _finding(1, challenge=[_challenge("reject", "old"), _challenge("uphold", "new")])
    ctx = _ctx()
    out = OrgReviewPipeline(reader, window_end=_END).writer_input(ctx, [challenged, _finding(2)])
    first, second = out["findings"]  # type: ignore[misc]
    assert first["finding_id"] == f"fnd_{_ULID}1"
    assert (first["entity_type"], first["entity_id"], first["claim"]) == (
        "team",
        "t1",
        "MTTR is high",
    )
    assert first["numbers"][0]["id"] == "n1"
    assert (first["confidence"], first["query_ids"]) == (0.8, [_q(101)])
    assert first["challenge_summary"] == {"verdict": "uphold", "notes": ["new"]}
    assert second["challenge_summary"] is None


def test_ut06_53_writer_input_without_teams_reads_no_levers(reader: _Reader) -> None:
    """UT06-53 no selected team: `levers` is empty and no lever read happens."""
    focus = EntityScope(entity_type="team", entity_ids=["zz"])
    out = OrgReviewPipeline(reader, window_end=_END).writer_input(_ctx(focus=focus), [])
    assert out["levers"] == []
    assert len(reader.calls) == 1


def test_ut06_53_get_pipeline_org_review(reader: _Reader) -> None:
    """UT06-53 get_pipeline("org_review") returns an OrgReviewPipeline satisfying Pipeline."""
    pipe = get_pipeline("org_review", reader, window_end=_END)
    assert isinstance(pipe, OrgReviewPipeline)
    assert isinstance(pipe, Pipeline)
    assert pipe.kind == "org_review"
    assert pipe.window_end == _END


def test_ut06_53_planner_input_shape(reader: _Reader) -> None:
    """UT06-53 planner_input carries deterministic tasks with score rows, as for funding."""
    ctx = _ctx(
        override={"K_teams": 2}, prior_recs=[_prior("rec_1", "t1", outcome={"verdict": "x"})]
    )
    pipe = OrgReviewPipeline(reader, window_end=_END)
    tasks = pipe.deterministic_tasks(ctx)
    out = pipe.planner_input(ctx, tasks)
    assert set(out) == {
        "kind", "question", "deterministic", "dq_warnings", "unconfirmed_weights",
        "prior_context", "H_wildcards",
    }  # fmt: skip
    assert out["H_wildcards"] == ctx.knobs.H_wildcards
    rows = out["deterministic"]
    assert isinstance(rows, list)
    assert rows[0] == {
        "dedup_key": tasks[0].dedup_key, "specialty": "ops", "entity_type": "team",
        "entity_ids": ["t1"], "objective": tasks[0].objective,
        "score_row": {"entity_id": "t1", "rank": 1},
    }  # fmt: skip
    org_row = next(r for r in rows if r["entity_type"] == "org")
    assert org_row["score_row"] == {"org_id": "o1", "n": 2, "team_ids": ["t1", "t2"]}
    assert rows[-1]["score_row"] is None  # retrospective


def test_ut06_53_ranked_entities_priority_and_drafts(reader: _Reader) -> None:
    """UT06-53 ranked_entities uses cached must-cover ranks; defaults for the other methods."""
    ctx = _ctx(override={"M_must": 2})
    pipe = OrgReviewPipeline(reader, window_end=_END)
    pipe.must_cover(ctx)
    draft = ReportDraft.model_validate(_draft_payload())
    ranked = pipe.ranked_entities(draft)
    assert [(r.rank, r.entity_type, r.entity_id) for r in ranked] == [
        (1, "team", "t2"), (2, "team", "t1")
    ]  # fmt: skip
    f = _finding(1)
    assert pipe.challenge_priority(f, ctx) == pytest.approx(0.8 * 3.0795, rel=1e-3)
    drafts = pipe.recommendation_drafts(draft, {f.finding_id: f})
    assert [d.target_id for d in drafts] == ["t2"]


def _draft_payload() -> dict[str, Any]:
    num = {"id": "n1", "value": "1200.00", "unit": "usd", "query_id": _q(101), "column": "c",
           "row_key": None}  # fmt: skip
    return {
        "run_id": _RUN, "kind": "org_review", "depth": "fast", "profile": "local",
        "build_id": "b", "title": "Org review",
        "sections": [], "caveats": [], "prior_outcomes_commentary": None, "banners": [],
        "flags": {}, "contested": [], "removed": [], "dead_tasks": [], "query_ids": [_q(101)],
        "ranked_entities": [],
        "recommendations": [{
            "rank": 1, "kind": "org_action", "target_type": "team", "target_id": "t2",
            "headline": "Fix MTTR", "summary": "Saves [[n1]]", "numbers": [num],
            "finding_ids": [f"fnd_{_ULID}1"], "query_ids": [_q(101)],
        }],
        "coverage": {"planned_tasks": 1, "done_tasks": 1, "dead_tasks": 0, "must_cover_total": 2,
                     "must_cover_done": 2, "verified_findings": 1, "rejected_findings": 0,
                     "publishable": True},
        "verification": {"build_id": "b", "passed": True, "items": [], "n_numbers": 0,
                         "n_failed": 0, "verified_at": _NOW, "duration_ms": 0},
    }  # fmt: skip
