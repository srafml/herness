"""Tests for herness.harness.pipelines.funding_review (T06-11): U06-71, U06-72 (UT06-50, UT06-51).

Spec 11's `tiny_build` (T11-17) does not exist yet: `_make_build` writes a local tiny
`wh-<build_id>.duckdb` with only the tables the funding reads use (`score.funding`,
`score.funding_attribution`, `core.incident`, `enrich.incident_change_link`). The reader is
spec 05 `execute_recorded` bound to a real `ToolContext` over that build (tools stand-in of
T05-15), so query ids and evidence rows come from the real recording path.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import JsonValue
from tests.support import tools_standin as sd
from tests.support.harness_fakes import FakeOps

from herness.core.errors import QueryError
from herness.core.resilience import ProcessState
from herness.core.types import EntityScope, Finding, ReportDraft
from herness.harness import tools
from herness.harness.findings import compute_dedup_key
from herness.harness.llm.settings import SqlSettings
from herness.harness.pipelines import _review_common as common
from herness.harness.pipelines.base import Pipeline, PlanContext, get_pipeline
from herness.harness.pipelines.funding_review import FundingReviewPipeline
from herness.harness.pipelines.settings import PipelinesConfig, resolve_knobs
from herness.harness.tools import RecordedResult
from herness.harness.warehouse import open_warehouse

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
_TYPES = ("epic", "feature", "initiative")
# e01..e12 rank 1..12 (epic/feature/initiative), c1..c7 cluster fixes rank 13..19, one other type
_CANDIDATES = [(f"e{i:02d}", _TYPES[i % 3], i, [f"q_{i:016x}"]) for i in range(1, 13)]
_CLUSTERS = [(f"c{i}", "cluster_fix", 12 + i, [f"q_{100 + i:016x}"]) for i in range(1, 8)]
_OTHER = [("x1", "team_fix", 0, ["q_00000000000000ff"])]
# incidents: in window (i1..i3), before window (i0); the change links cover i4, i5, i9
_INCIDENTS = [("i0", "2025-01-01 00:00:00"), ("i1", "2026-09-01 00:00:00"),
              ("i2", "2026-08-01 00:00:00"), ("i3", "2026-07-01 00:00:00")]  # fmt: skip
_ATTRIBUTION = [
    ("e01", "i1", "incident"), ("e01", "i2", "incident"),  # e01: 2 in window -> ops task
    ("e02", "i0", "incident"),  # e02: only an incident before the window -> no ops task
    ("e03", "w1", "work_item"),  # e03: no incident record -> no ops task
    # c1: 4 incidents, 2 linked (share 0.5 > 0.2) -> change task in deep
    ("c1", "i4", "incident"), ("c1", "i5", "incident"), ("c1", "i6", "incident"),
    ("c1", "i7", "incident"),
    # c2: 5 incidents, 1 linked (share 0.2, not > 0.2) -> no change task
    ("c2", "i9", "incident"), *[("c2", f"j{i}", "incident") for i in range(4)],
]  # fmt: skip
_LINKS = [("i4", "chg1"), ("i4", "chg2"), ("i5", "chg1"), ("i9", "chg3")]


def _make_build(warehouse_dir: Path, *, links: bool = True) -> None:
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(warehouse_dir / f"wh-{sd.BUILD_ID}.duckdb"))
    for schema in ("core", "enrich", "score"):
        con.execute(f"CREATE SCHEMA {schema}")
    con.execute(
        "CREATE TABLE score.funding (candidate_id VARCHAR, candidate_type VARCHAR,"
        " rank INTEGER, query_ids VARCHAR[])"
    )
    con.executemany(
        "INSERT INTO score.funding VALUES (?, ?, ?, ?)", [*_CANDIDATES, *_CLUSTERS, *_OTHER]
    )
    con.execute(
        "CREATE TABLE score.funding_attribution (candidate_id VARCHAR, record_id VARCHAR,"
        " record_kind VARCHAR)"
    )
    con.executemany("INSERT INTO score.funding_attribution VALUES (?, ?, ?)", _ATTRIBUTION)
    con.execute("CREATE TABLE core.incident (record_id VARCHAR, opened_at TIMESTAMP)")
    con.executemany("INSERT INTO core.incident VALUES (?, CAST(? AS TIMESTAMP))", _INCIDENTS)
    if links:
        con.execute(
            "CREATE TABLE enrich.incident_change_link (incident_id VARCHAR, change_id VARCHAR)"
        )
        con.executemany("INSERT INTO enrich.incident_change_link VALUES (?, ?)", _LINKS)
    con.close()


class _Recorded:
    """`RecordedReader`: `execute_recorded` bound to a real `ToolContext`; logs each SQL."""

    def __init__(self, ctx: Any, ops: FakeOps) -> None:
        self.ctx, self.ops = ctx, ops
        self.calls: list[tuple[str, dict[str, JsonValue]]] = []
        self.qids: list[str] = []

    def __call__(self, sql: str, params: dict[str, JsonValue]) -> RecordedResult:
        self.calls.append((sql, params))
        result = tools.execute_recorded(self.ctx, sql, params)
        self.qids.append(result.query_id)
        return result


def _open(tmp_path: Path, *, links: bool = True) -> Iterator[_Recorded]:
    _make_build(tmp_path, links=links)
    handle = open_warehouse(sd.BUILD_ID, warehouse_dir=tmp_path, sql=SqlSettings())
    try:
        ops = FakeOps()
        yield _Recorded(sd.make_ctx(handle, ops), ops)
    finally:
        handle.close()


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState) -> None:
    del reset_process_state
    sd.patch_config(monkeypatch)


@pytest.fixture
def reader(tmp_path: Path, patched: None) -> Iterator[_Recorded]:
    del patched
    yield from _open(tmp_path)


def _ctx(depth: str = "fast", override: dict[str, int] | None = None, **fields: Any) -> PlanContext:
    knobs = resolve_knobs(PipelinesConfig(), "funding_review", depth, override=override)  # type: ignore[arg-type]
    kwargs: dict[str, Any] = {
        "run_id": _RUN, "kind": "funding_review", "depth": depth, "profile": "local",
        "build_id": sd.BUILD_ID, "question": "What should we fund?", "focus": None,
        "knobs": knobs, "dq_warnings": [], "unconfirmed_weights": ["w1"],
        "prior_context": "ctx", "prior_recs": [],
        "portfolio": {"scenario": "base", "rows": [], "selected": [], "query_ids": [],
                      "custom": []},
    }  # fmt: skip
    kwargs.update(fields)
    return PlanContext.model_validate(kwargs)


def _prior(rec: str, target: str, *, outcome: dict[str, Any] | None, run_id: str = _PRIOR_RUN,
           decision: str | None = "accepted") -> dict[str, Any]:  # fmt: skip
    return {"rec_id": rec, "run_id": run_id, "target_id": target, "decision": decision,
            "outcome": outcome}  # fmt: skip


def _plan(reader: _Recorded, ctx: PlanContext) -> list[Any]:
    return FundingReviewPipeline(reader, window_end=_END).deterministic_tasks(ctx)


def _pairs(tasks: list[Any]) -> list[tuple[str, str]]:
    return [(t.scope.entity_ids[0], t.specialty) for t in tasks]


def _by(tasks: list[Any], entity_id: str, specialty: str) -> Any:
    (task,) = [t for t in tasks if t.scope.entity_ids == [entity_id] and t.specialty == specialty]
    return task


_EPICS = [c[0] for c in _CANDIDATES]
_CLUSTER_IDS = [c[0] for c in _CLUSTERS]

_DELIVERY = [(e, "delivery") for e in _EPICS]
_CLUSTER_OPS = [(c, "ops") for c in _CLUSTER_IDS]
_E01_OPS = [("e01", "delivery"), ("e01", "ops")]

# --- UT06-50 deterministic_tasks --------------------------------------------------------------


@pytest.mark.parametrize(
    ("depth", "expected", "n_reads"),
    [
        ("fast", [*_DELIVERY[:10], *_CLUSTER_OPS[:5]], 2),
        ("standard", [*_E01_OPS, *_DELIVERY[1:], *_CLUSTER_OPS], 3),
        (
            "deep",
            [*_E01_OPS, *_DELIVERY[1:], _CLUSTER_OPS[0], ("c1", "change"), *_CLUSTER_OPS[1:]],
            4,
        ),
    ],
)
def test_ut06_50_counts_and_specialties_per_depth(
    reader: _Recorded, depth: str, expected: list[tuple[str, str]], n_reads: int
) -> None:
    """UT06-50 fast/standard/deep: tasks, order and specialties per the U06-72 step 5 rules."""
    ctx = _ctx(depth)
    tasks = _plan(reader, ctx)
    assert _pairs(tasks) == expected
    assert len(reader.calls) == n_reads
    knobs = ctx.knobs
    assert len(tasks) <= 2 * (knobs.K_candidates + knobs.K_clusters) + 1
    assert {t.scope.entity_type for t in tasks} == {"candidate"}
    assert len({t.task_id for t in tasks}) == len(tasks)
    first = tasks[0]
    assert (first.role, first.model_role, first.run_id) == ("analyst", "analyst", _RUN)
    assert first.budget == knobs.analyst_budget
    assert first.dedup_key == compute_dedup_key(
        "analyst", first.specialty, first.scope, first.objective
    )
    assert first.tools == [n for n in _ANALYST_TOOLS if depth != "fast" or n != "request_subtask"]


def test_ut06_50_query_ids_come_from_the_recording_path(reader: _Recorded) -> None:
    """UT06-50 inputs.query_ids = the score row's query_ids plus the selection query id."""
    tasks = _plan(reader, _ctx("deep"))
    select_qid, cluster_qid = reader.qids[0], reader.qids[1]
    assert _by(tasks, "e01", "delivery").inputs.query_ids == [f"q_{1:016x}", select_qid]
    assert _by(tasks, "e01", "ops").inputs.query_ids == [f"q_{1:016x}", select_qid]
    assert _by(tasks, "c1", "change").inputs.query_ids == [f"q_{101:016x}", cluster_qid]
    assert _by(tasks, "e05", "delivery").inputs.candidate_ids == ["e05"]
    # every read has an evidence row and a use by the planner's task
    assert set(reader.qids) <= set(reader.ops.evidence)
    assert {u[0] for u in reader.ops.uses} == set(reader.qids)
    assert all(qid.startswith("q_") for qid in reader.qids)


def test_ut06_50_objectives_are_exact_templates(reader: _Recorded) -> None:
    """UT06-50 delivery, candidate ops, cluster ops and change objectives use the templates."""
    tasks = _plan(reader, _ctx("deep"))
    assert _by(tasks, "e01", "delivery").objective == (
        "Build the funding case for candidate e01 (feature): check addressable pain, effort and"
        " confidence drivers in score.funding and score.funding_attribution."
    )
    assert _by(tasks, "e01", "ops").objective == (
        "Assess the operational pain behind candidate e01: incidents, MTTR and toil in"
        " metrics.incident_fact for its attributed records."
    )
    assert _by(tasks, "c2", "ops").objective == (
        "Assess recurring incident cluster fix c2: volume, trend and cost in"
        " metrics.incident_fact and enrich.cluster_member."
    )
    assert _by(tasks, "c1", "change").objective == (
        "Check change-caused incidents for cluster fix c1 using enrich.incident_change_link and"
        " metrics.change_fact."
    )


def test_ut06_50_must_cover_and_priorities(reader: _Recorded) -> None:
    """UT06-50 must-cover = top M_must + portfolio selected + top 3 clusters; U06-91 priority."""
    portfolio = {"scenario": "base", "rows": [], "selected": ["e11", "c7"], "query_ids": [],
                 "custom": []}  # fmt: skip
    ctx = _ctx("fast", portfolio=portfolio)
    pipe = FundingReviewPipeline(reader, window_end=_END)
    must = pipe.must_cover(ctx)
    assert must == {f"candidate:{c}" for c in ["e01", "e02", "e03", "e04", "e05", "e11", "c7",
                                               "c1", "c2", "c3"]}  # fmt: skip
    tasks = pipe.deterministic_tasks(ctx)
    assert len(reader.calls) == 2  # selections read once, cached across must_cover and tasks
    assert (_by(tasks, "e01", "delivery").must_cover, _by(tasks, "e01", "delivery").priority) == (
        True, 149.0
    )  # fmt: skip
    assert (_by(tasks, "e06", "delivery").must_cover, _by(tasks, "e06", "delivery").priority) == (
        False, 94.0
    )  # fmt: skip
    assert (_by(tasks, "c3", "ops").must_cover, _by(tasks, "c3", "ops").priority) == (True, 135.0)
    assert (_by(tasks, "c4", "ops").must_cover, _by(tasks, "c4", "ops").priority) == (False, 84.0)
    assert all(t.scope.entity_ids != ["e11"] for t in tasks)  # outside K_candidates in fast


def test_ut06_50_focus_ignores_k(reader: _Recorded) -> None:
    """UT06-50 with `focus` the K limits are dropped and ids filtered (candidates and clusters)."""
    focus = EntityScope(entity_type="candidate", entity_ids=["e12", "c6", "e01", "x1"])
    ctx = _ctx("fast", override={"K_candidates": 1, "K_clusters": 0}, focus=focus)
    tasks = _plan(reader, ctx)
    assert _pairs(tasks) == [("e01", "delivery"), ("e12", "delivery"), ("c6", "ops")]
    for sql, params in reader.calls:
        assert "LIMIT" not in sql
        assert params == {"focus_ids": ["e12", "c6", "e01", "x1"]}


def test_ut06_50_focus_of_other_type_selects_nothing(reader: _Recorded) -> None:
    """UT06-50 a focus without candidate ids filters to no candidate: no task, no more reads."""
    focus = EntityScope(entity_type="team", entity_ids=["e01"])
    assert _plan(reader, _ctx("deep", focus=focus)) == []
    assert [p for _, p in reader.calls] == [{"focus_ids": []}, {"focus_ids": []}]


def test_ut06_50_exact_reads_through_the_reader(reader: _Recorded) -> None:
    """UT06-50 deep plans with exactly the 4 U06-72 reads (named params), all via the reader."""
    _plan(reader, _ctx("deep"))
    sqls = [s for s, _ in reader.calls]
    assert sqls[0] == (
        "SELECT candidate_id, candidate_type, rank, query_ids FROM score.funding WHERE"
        " candidate_type IN ('epic','feature','initiative') ORDER BY rank, candidate_id LIMIT $k"
    )
    assert sqls[1] == (
        "SELECT candidate_id, candidate_type, rank, query_ids FROM score.funding WHERE"
        " candidate_type = 'cluster_fix' ORDER BY rank, candidate_id LIMIT $k"
    )
    assert "FROM score.funding_attribution a JOIN core.incident i" in sqls[2]
    assert "LEFT JOIN (SELECT DISTINCT incident_id FROM enrich.incident_change_link) l" in sqls[3]
    params = [p for _, p in reader.calls]
    assert params[0] == {"k": 60}
    assert params[1] == {"k": 25}
    assert params[2] == {"ids": _EPICS, "since": "2025-09-26"}
    assert params[3] == {"ids": _CLUSTER_IDS}


def test_ut06_50_modules_do_no_other_io() -> None:
    """UT06-50 the pipeline modules import no DuckDB, store, LLM or network module."""
    root = Path(__file__).resolve().parents[4] / "herness" / "harness" / "pipelines"
    banned = ("duckdb", "herness.store", "herness.harness.llm", "herness.core.egress", "httpx",
              "sqlite3", "open")  # fmt: skip
    for name in ("funding_review.py", "_review_common.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imported = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)] + [
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        ]
        assert not [m for m in imported if m.startswith(banned)], name
        calls = [n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)]  # fmt: skip
        assert "open" not in calls, name


def test_ut06_50_query_error_propagates(tmp_path: Path, patched: None) -> None:
    """UT06-50 a `QueryError` from the reader (missing change-link table) propagates."""
    del patched
    for recorded in _open(tmp_path, links=False):
        with pytest.raises(QueryError):
            _plan(recorded, _ctx("deep"))


def test_ut06_50_retrospective_notes_and_dq(reader: _Recorded) -> None:
    """UT06-50 retrospective task, prior-rec notes per target and DQ matches per group."""
    other_run = f"run_{_ULID}C"
    prior = [
        _prior("rec_1", "e02", outcome={"verdict": "improved"}),
        _prior("rec_2", "e02", outcome=None, decision=None, run_id=other_run),
        _prior("rec_3", "c1", outcome={"verdict": "no_change"}, run_id=other_run),
    ]

    def dq(name: str, details: object) -> dict[str, Any]:
        return {"check_name": name, "severity": "warn", "value": 1, "threshold": 0,
                "details": details, "query_id": "q_0000000000000900"}  # fmt: skip

    warnings = [
        dq("e02_gap", {"entity_id": "e02"}),
        dq("work_item_nulls", {"table": "core.work_item"}),
        dq("cluster_gap", {"table": "enrich.cluster_member"}),
        dq("incident_nulls", "core.incident sparse"),
    ]
    ctx = _ctx("fast", prior_recs=prior, dq_warnings=warnings)
    pipe = FundingReviewPipeline(reader, window_end=_END)
    assert {f"run:{_PRIOR_RUN}", f"run:{other_run}"} <= pipe.must_cover(ctx)
    tasks = pipe.deterministic_tasks(ctx)
    retro = tasks[-1]
    assert (retro.specialty, retro.scope.entity_type) == ("retrospective", "run")
    assert retro.scope.entity_ids == [_PRIOR_RUN, other_run]
    assert (retro.must_cover, retro.priority) == (True, 50.0)
    assert retro.objective == (
        f"Review prior recommendations and their measured outcomes for runs {_PRIOR_RUN},"
        f" {other_run}: compare expected and actual values in the outcome rows."
    )
    assert _by(tasks, "e02", "delivery").inputs.notes == (
        "prior rec rec_1: decision accepted, outcome improved\n"
        "prior rec rec_2: decision none, outcome none"
    )
    assert _by(tasks, "c1", "ops").inputs.notes == (
        "prior rec rec_3: decision accepted, outcome no_change"
    )
    assert _by(tasks, "e03", "delivery").inputs.notes is None
    assert _by(tasks, "e02", "delivery").inputs.dq_warnings == [
        "e02_gap", "work_item_nulls", "incident_nulls"
    ]  # fmt: skip
    assert _by(tasks, "c1", "ops").inputs.dq_warnings == ["cluster_gap", "incident_nulls"]


def test_ut06_50_no_retrospective_without_outcomes(reader: _Recorded) -> None:
    """UT06-50 prior recs without any outcome add no retrospective task."""
    tasks = _plan(reader, _ctx(prior_recs=[_prior("rec_1", "e01", outcome=None)]))
    assert all(t.specialty != "retrospective" for t in tasks)


# --- UT06-51 FundingReviewPipeline ------------------------------------------------------------


def _finding(n: int, *, challenge: list[dict[str, Any]] | None = None) -> Finding:
    number = {"id": "n1", "value": "1200.00", "unit": "usd", "query_id": "q_0000000000000001",
              "column": "c", "row_key": None}  # fmt: skip
    return Finding.model_validate({
        "finding_id": f"fnd_{_ULID}{n}", "run_id": _RUN, "task_id": _TASK,
        "author_role": "analyst", "claim": "Pain is high", "entity_type": "candidate",
        "entity_id": "e01", "numbers": [number], "query_ids": ["q_0000000000000001"],
        "confidence": 0.8, "status": "verified", "challenge": challenge or [], "created_at": _NOW,
    })  # fmt: skip


def _challenge(verdict: str, concern: str) -> dict[str, Any]:
    checks = [
        {"check": c, "result": "pass", "note": ""}
        for c in ("seasonality", "mis_mapping", "small_sample", "double_counting", "survivorship")
    ]
    checks.insert(0, {"check": "confounding", "result": "fail", "note": concern,
                      "query_ids": ["q_0000000000000001"]})  # fmt: skip
    return {"finding_id": f"fnd_{_ULID}1", "checks": checks, "verdict": verdict}


def test_ut06_51_planner_input_keys(reader: _Recorded) -> None:
    """UT06-51 planner_input keys, deterministic rows with score rows, H_wildcards."""
    ctx = _ctx("standard", prior_recs=[_prior("rec_1", "e01", outcome={"verdict": "x"})])
    pipe = FundingReviewPipeline(reader, window_end=_END)
    tasks = pipe.deterministic_tasks(ctx)
    out = pipe.planner_input(ctx, tasks)
    assert set(out) == {"kind", "question", "deterministic", "dq_warnings",
                        "unconfirmed_weights", "prior_context", "H_wildcards"}  # fmt: skip
    assert (out["kind"], out["question"], out["H_wildcards"]) == (
        "funding_review", "What should we fund?", 3
    )  # fmt: skip
    rows = out["deterministic"]
    assert isinstance(rows, list)
    assert rows[0] == {
        "dedup_key": tasks[0].dedup_key, "specialty": "delivery", "entity_type": "candidate",
        "entity_ids": ["e01"], "objective": tasks[0].objective,
        "score_row": {"candidate_id": "e01", "candidate_type": "feature", "rank": 1,
                      "query_ids": [f"q_{1:016x}"]},
    }  # fmt: skip
    assert rows[-1]["score_row"] is None  # retrospective


def test_ut06_51_writer_input_keys_and_outline(reader: _Recorded) -> None:
    """UT06-51 writer_input keys, the funding outline and U06-71 finding rows."""
    challenged = _finding(1, challenge=[_challenge("reject", "old"), _challenge("uphold", "new")])
    ctx = _ctx()
    pipe = FundingReviewPipeline(reader, window_end=_END)
    out = pipe.writer_input(ctx, [challenged, _finding(2)])
    assert set(out) == {"kind", "question", "outline", "findings", "portfolio", "dq_warnings",
                        "unconfirmed_weights", "prior_context", "prior_recs"}  # fmt: skip
    assert out["outline"] == ["executive_summary", "recommendations", "portfolio",
                              "retrospective", "risks_and_caveats", "method"]  # fmt: skip
    assert out["portfolio"] == ctx.portfolio
    first, second = out["findings"]  # type: ignore[misc]
    assert set(first) == {"finding_id", "entity_type", "entity_id", "claim", "numbers",
                          "confidence", "query_ids", "challenge_summary"}  # fmt: skip
    assert first["challenge_summary"] == {"verdict": "uphold", "notes": ["new"]}
    assert second["challenge_summary"] is None
    assert reader.calls == []  # the writer input reads nothing


def test_ut06_51_must_cover_ranked_entities_and_defaults(reader: _Recorded) -> None:
    """UT06-51 ranked_entities uses the cached must-cover ranks; U06-68 and U06-70 defaults."""
    portfolio = {"scenario": "base", "rows": [], "selected": ["e11", "zz"], "query_ids": [],
                 "custom": []}  # fmt: skip
    ctx = _ctx("standard", override={"M_must": 2}, portfolio=portfolio)
    pipe = get_pipeline("funding_review", reader, window_end=_END)
    assert isinstance(pipe, FundingReviewPipeline)
    assert isinstance(pipe, Pipeline)
    assert (pipe.kind, pipe.window_end) == ("funding_review", _END)
    assert pipe.must_cover(ctx) == {f"candidate:{c}" for c in
                                    ["e01", "e02", "e11", "zz", "c1", "c2", "c3"]}  # fmt: skip
    draft = ReportDraft.model_validate(_draft_payload())
    ranked = pipe.ranked_entities(draft)
    assert [(r.rank, r.entity_id) for r in ranked] == [
        (1, "e02"), (2, "e01"), (3, "e11"), (4, "c1"), (5, "c2"), (6, "c3")
    ]  # fmt: skip
    assert {r.entity_type for r in ranked} == {"candidate"}
    f = _finding(1)
    assert pipe.challenge_priority(f, ctx) == pytest.approx(0.8 * 3.0795, rel=1e-3)
    assert [d.target_id for d in pipe.recommendation_drafts(draft, {f.finding_id: f})] == ["e02"]


def test_ut06_51_default_tools_stand_in_scope() -> None:
    """UT06-51 the T06-13 tool stand-in covers the non-crosscheck analyst only."""
    knobs = _ctx("deep").knobs
    assert common.default_tools("analyst", "ops", "deep", child_depth=2, knobs=knobs) == [
        n for n in _ANALYST_TOOLS if n != "request_subtask"
    ]
    with pytest.raises(ValueError, match="analyst only"):
        common.default_tools("analyst", "crosscheck", "deep", child_depth=0, knobs=knobs)
    with pytest.raises(ValueError, match="analyst only"):
        common.default_tools("skeptic", "general", "deep", child_depth=0, knobs=knobs)


def _draft_payload() -> dict[str, Any]:
    num = {"id": "n1", "value": "1200.00", "unit": "usd", "query_id": "q_0000000000000001",
           "column": "c", "row_key": None}  # fmt: skip
    return {
        "run_id": _RUN, "kind": "funding_review", "depth": "standard", "profile": "local",
        "build_id": "b", "title": "Funding review",
        "sections": [], "caveats": [], "prior_outcomes_commentary": None, "banners": [],
        "flags": {}, "contested": [], "removed": [], "dead_tasks": [],
        "query_ids": ["q_0000000000000001"], "ranked_entities": [],
        "recommendations": [{
            "rank": 1, "kind": "fund", "target_type": "work_item", "target_id": "e02",
            "headline": "Fund e02", "summary": "Saves [[n1]]", "numbers": [num],
            "finding_ids": [f"fnd_{_ULID}1"], "query_ids": ["q_0000000000000001"],
        }],
        "coverage": {"planned_tasks": 1, "done_tasks": 1, "dead_tasks": 0, "must_cover_total": 2,
                     "must_cover_done": 2, "verified_findings": 1, "rejected_findings": 0,
                     "publishable": True},
        "verification": {"build_id": "b", "passed": True, "items": [], "n_numbers": 0,
                         "n_failed": 0, "verified_at": _NOW, "duration_ms": 0},
    }  # fmt: skip
