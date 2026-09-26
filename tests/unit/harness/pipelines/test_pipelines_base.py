"""Tests for herness.harness.pipelines.base (T06-10): U06-66 … U06-70, U06-75."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from herness.core.errors import ConfigError, ReportContractError
from herness.core.types import (
    Finding,
    RankedEntity,
    RecommendationDraft,
    ReportDraft,
    RunKind,
    TaskSpec,
)
from herness.harness.pipelines.base import (
    Pipeline,
    PlanContext,
    build_ranked_entities,
    default_challenge_priority,
    get_pipeline,
    to_recommendation_drafts,
)
from herness.harness.pipelines.settings import PipelinesConfig, resolve_knobs

pytestmark = pytest.mark.unit

_ULID = "01J9ZQ4Y8M6V3K2N1P0R5T7W9"
_RUN = f"run_{_ULID}A"
_TASK = f"task_{_ULID}A"
_Q1 = "q_0123456789abcdef"
_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _fid(n: int) -> str:
    return f"fnd_{_ULID}{n}"


def _num(nid: str, value: object = 1, unit: str = "count") -> dict[str, Any]:
    return {
        "id": nid,
        "value": value,
        "unit": unit,
        "query_id": _Q1,
        "column": "c",
        "row_key": None,
    }


def _finding(finding_id: str, *, numbers: list[dict[str, Any]], confidence: float = 0.5,
             status: str = "verified") -> Finding:  # fmt: skip
    return Finding.model_validate(
        {
            "finding_id": finding_id,
            "run_id": _RUN,
            "task_id": _TASK,
            "author_role": "analyst",
            "claim": "x",
            "entity_type": "team",
            "entity_id": "t1",
            "numbers": numbers,
            "query_ids": [_Q1],
            "confidence": confidence,
            "status": status,
            "created_at": _NOW,
        }
    )


def _rec(rank: int, target_id: str, *, finding_ids: list[str], **overrides: Any) -> dict[str, Any]:
    return {
        "rank": rank,
        "kind": "fund",
        "target_type": "team",
        "target_id": target_id,
        "headline": "Fund it",
        "summary": "It saves [[n2]] a year",
        "numbers": [_num("n1"), _num("n2", "1200.50", "usd")],
        "expected_metric": "toil_hours",
        "expected_delta_ref": "n1",
        "expected_usd_ref": "n2",
        "finding_ids": finding_ids,
        "query_ids": [],
    } | overrides


def _draft(recommendations: list[dict[str, Any]]) -> ReportDraft:
    return ReportDraft.model_validate(
        {
            "run_id": _RUN,
            "kind": "funding_review",
            "depth": "standard",
            "profile": "local",
            "build_id": "20260925-120000-ABCDEF",
            "title": "Funding review",
            "sections": [{"id": "executive_summary", "title": "Summary", "paragraphs": []}],
            "recommendations": recommendations,
            "ranked_entities": [],
            "caveats": [],
            "prior_outcomes_commentary": None,
            "banners": [],
            "flags": {},
            "contested": [],
            "removed": [],
            "coverage": {
                "planned_tasks": 1,
                "done_tasks": 1,
                "dead_tasks": 0,
                "must_cover_total": 0,
                "must_cover_done": 0,
                "verified_findings": 1,
                "rejected_findings": 0,
                "publishable": True,
            },
            "dead_tasks": [],
            "query_ids": [_Q1],
            "verification": {
                "build_id": "20260925-120000-ABCDEF",
                "passed": True,
                "items": [],
                "n_numbers": 0,
                "n_failed": 0,
                "verified_at": _NOW,
                "duration_ms": 0,
            },
        }
    )


def _ctx(**overrides: Any) -> PlanContext:
    knobs = resolve_knobs(PipelinesConfig(), "funding_review", "fast")
    kwargs: dict[str, Any] = {
        "run_id": _RUN,
        "kind": "funding_review",
        "depth": "fast",
        "profile": "local",
        "build_id": "20260925-120000-ABCDEF",
        "question": None,
        "focus": None,
        "knobs": knobs,
        "dq_warnings": [],
        "unconfirmed_weights": [],
        "prior_context": "",
        "prior_recs": [],
        "portfolio": {
            "scenario": "base",
            "rows": [],
            "selected": [],
            "query_ids": [],
            "custom": [],
        },
    }
    kwargs.update(overrides)
    return PlanContext.model_validate(kwargs)


class _FakePipeline:
    """Minimal conformer to `Pipeline` for the structural (runtime-checkable) test."""

    kind: RunKind = "funding_review"

    def deterministic_tasks(self, ctx: PlanContext) -> list[TaskSpec]:
        return []

    def must_cover(self, ctx: PlanContext) -> set[str]:
        return set()

    def planner_input(self, ctx: PlanContext, tasks: list[TaskSpec]) -> dict[str, object]:
        return {}

    def challenge_priority(self, f: Finding, ctx: PlanContext) -> float:
        return default_challenge_priority(f)

    def writer_input(self, ctx: PlanContext, verified: list[Finding]) -> dict[str, object]:
        return {}

    def ranked_entities(self, draft: ReportDraft) -> list[RankedEntity]:
        return []

    def recommendation_drafts(
        self, draft: ReportDraft, findings: dict[str, Finding]
    ) -> list[RecommendationDraft]:
        return []


# --- U06-66 Pipeline, U06-67 PlanContext ----------------------------------------------------


def test_ut06_48_fake_pipeline_satisfies_protocol() -> None:
    """UT06-48 a conforming implementation satisfies the runtime-checkable Pipeline protocol."""
    assert isinstance(_FakePipeline(), Pipeline)
    assert not isinstance(object(), Pipeline)


def test_ut06_48_plan_context_forbids_unknown_field_and_is_frozen() -> None:
    """UT06-48 PlanContext is closed (extra=forbid) and immutable (frozen=True)."""
    ctx = _ctx()
    assert ctx.knobs.max_tasks_per_run > 0
    with pytest.raises(ValidationError):
        PlanContext.model_validate({**ctx.model_dump(mode="json"), "bogus": 1})
    with pytest.raises(ValidationError):
        ctx.run_id = f"run_{_ULID}B"  # type: ignore[misc]


# --- U06-68 default_challenge_priority ------------------------------------------------------


def test_ut06_48_default_challenge_priority_log_formula() -> None:
    """UT06-48 default_challenge_priority is log10(1 + impact_usd) x confidence."""
    numbers = [_num("n1", "1250000.00", "usd"), _num("n2", 42, "count")]
    f = _finding(_fid(1), numbers=numbers, confidence=0.6)
    expected = math.log10(1 + float(Decimal("1250000.00"))) * 0.6
    assert default_challenge_priority(f) == pytest.approx(expected)


def test_ut06_48_default_challenge_priority_zero_without_usd() -> None:
    """UT06-48 no usd number gives impact 0, so priority is 0 regardless of confidence."""
    f = _finding(_fid(2), numbers=[_num("n1", 42, "count")], confidence=0.9)
    assert default_challenge_priority(f) == 0.0


# --- U06-69 build_ranked_entities -------------------------------------------------------------


def test_ut06_48_build_ranked_entities_recommendation_order_then_must_cover_by_rank() -> None:
    """UT06-48 recs rank first (first-appearance order); remaining follow by rank then id."""
    draft = _draft(
        [
            _rec(1, "cand-3", finding_ids=[_fid(1)]),
            _rec(2, "cand-1", finding_ids=[_fid(1)]),
            _rec(3, "cand-3", finding_ids=[_fid(1)]),  # duplicate target, already seen
            _rec(4, "cand-5", finding_ids=[_fid(1)]),
        ]
    )
    remaining = [("cand-2", 1), ("cand-4", 3), ("cand-1", 1), ("cand-6", 2)]

    result = build_ranked_entities(draft, "candidate", remaining)

    expected_ids = ["cand-3", "cand-1", "cand-5", "cand-2", "cand-6", "cand-4"]
    assert result == [
        RankedEntity(rank=i, entity_type="candidate", entity_id=eid)
        for i, eid in enumerate(expected_ids, start=1)
    ]


def test_ut06_48_build_ranked_entities_no_recommendations() -> None:
    """UT06-48 with no recommendations, ranks come only from `remaining`, sorted."""
    draft = _draft([])
    result = build_ranked_entities(draft, "team", [("t2", 2), ("t1", 1)])
    assert result == [
        RankedEntity(rank=1, entity_type="team", entity_id="t1"),
        RankedEntity(rank=2, entity_type="team", entity_id="t2"),
    ]


# --- U06-70 to_recommendation_drafts ----------------------------------------------------------


def test_ut06_49_unverified_finding_raises_report_contract_error() -> None:
    """UT06-49 a recommendation citing a finding missing or not verified raises the error."""
    draft = _draft([_rec(1, "team-1", finding_ids=[_fid(1)])])

    with pytest.raises(ReportContractError, match=_fid(1)):
        to_recommendation_drafts(draft, {})

    proposed = _finding(_fid(1), numbers=[_num("n1")], status="proposed")
    with pytest.raises(ReportContractError, match=_fid(1)):
        to_recommendation_drafts(draft, {_fid(1): proposed})


def test_ut06_49_valid_draft_maps_to_spec_07_shape() -> None:
    """UT06-49 a valid draft maps to spec 07 RecommendationDraft, keeping summary markers."""
    verified = _finding(_fid(1), numbers=[_num("n1")], status="verified")
    draft = _draft([_rec(1, "team-1", finding_ids=[_fid(1)])])

    result = to_recommendation_drafts(draft, {_fid(1): verified})

    assert len(result) == 1
    got = result[0]
    assert isinstance(got, RecommendationDraft)
    assert got.rank == 1
    assert got.kind == "fund"
    assert got.target_type == "team"
    assert got.target_id == "team-1"
    assert got.summary == "It saves [[n2]] a year"
    assert [n.id for n in got.numbers] == ["n1", "n2"]
    assert got.expected_metric == "toil_hours"
    assert got.expected_delta_ref == "n1"
    assert got.expected_usd_ref == "n2"
    assert got.finding_ids == [_fid(1)]


# --- U06-75 get_pipeline (only the unknown-kind path; funding/org land in later cards) --------


def test_ut06_53_get_pipeline_unknown_kind_raises_config_error() -> None:
    """UT06-53 (partial) an unknown kind ("chat" or otherwise) raises ConfigError."""

    def _reader(sql: str, params: dict[str, object]) -> Any:
        msg = "reader must not be called"
        raise AssertionError(msg)

    with pytest.raises(ConfigError, match="no review pipeline for kind chat"):
        get_pipeline("chat", _reader, window_end=date(2026, 9, 25))  # type: ignore[arg-type]

    with pytest.raises(ConfigError, match="no review pipeline for kind bogus"):
        get_pipeline("bogus", _reader, window_end=date(2026, 9, 25))  # type: ignore[arg-type]
