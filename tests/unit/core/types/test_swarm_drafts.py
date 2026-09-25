"""Tests for herness.core.types.swarm report draft and chat types (T06-02)."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

import herness.core.types as shared
from herness.core.types import swarm
from herness.core.types.harness import VerificationResult
from herness.core.types.swarm import (
    CHAT_EVENT_ADAPTER,
    ChatAnswer,
    CorrectionCapturedEvent,
    Coverage,
    ErrorEvent,
    EscalatedEvent,
    EvidenceEvent,
    FinalEvent,
    ModeEvent,
    Paragraph,
    RankedEntity,
    RecommendationItem,
    ReportDraft,
    Section,
    TokenEvent,
    ToolEvent,
    VerificationEvent,
)

pytestmark = pytest.mark.unit

_ULID = "01J9ZQ4Y8M6V3K2N1P0R5T7W9X"
_RUN = f"run_{_ULID}"
_FND = f"fnd_{_ULID}"
_Q1 = "q_0123456789abcdef"
_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _verification() -> VerificationResult:
    return VerificationResult(
        build_id="b1", passed=True, items=[], n_numbers=0, n_failed=0, verified_at=_NOW,
        duration_ms=0,
    )  # fmt: skip


def _ref(ref_id: str = "n1", unit: str = "count", value: object = 3) -> dict[str, Any]:
    return {
        "id": ref_id,
        "value": value,
        "unit": unit,
        "query_id": _Q1,
        "column": "n",
        "row_key": None,
    }


def _paragraph(**overrides: Any) -> dict[str, Any]:
    return {
        "text": "Incidents rose to [[n1]]",
        "numbers": [_ref()],
        "finding_ids": [_FND],
    } | overrides


def _rec(rank: int = 1, **overrides: Any) -> dict[str, Any]:
    return {
        "rank": rank,
        "kind": "fund",
        "target_type": "epic",
        "target_id": f"epic-{rank}",
        "headline": "Fund the cache rewrite",
        "summary": "It saves [[n2]] a year",
        "numbers": [_ref(), _ref("n2", "usd", "1200.50")],
        "expected_usd_ref": "n2",
        "confidence_ref": "n1",
        "action_levers": [
            {"entity_type": "team", "entity_id": "t1", "metric": "mttr", "delta_usd_ref": "n2"}
        ],
        "finding_ids": [_FND],
        "query_ids": [_Q1],
    } | overrides


def _coverage() -> dict[str, Any]:
    return {
        "planned_tasks": 4, "done_tasks": 3, "dead_tasks": 1, "must_cover_total": 2,
        "must_cover_done": 2, "verified_findings": 5, "rejected_findings": 1, "publishable": True,
    }  # fmt: skip


def _draft(**overrides: Any) -> dict[str, Any]:
    return {
        "run_id": _RUN,
        "kind": "funding_review",
        "depth": "standard",
        "profile": "local",
        "build_id": "20260925-120000-ABCDEF",
        "title": "Funding review",
        "sections": [
            {"id": "executive_summary", "title": "Summary", "paragraphs": [_paragraph()]},
            {"id": "recommendations", "title": "Recommendations", "paragraphs": []},
        ],
        "recommendations": [_rec(1), _rec(2)],
        "ranked_entities": [
            {"rank": 1, "entity_type": "candidate", "entity_id": "epic-1"},
            {"rank": 2, "entity_type": "candidate", "entity_id": "epic-2"},
        ],
        "caveats": ["Data covers 90 days"],
        "prior_outcomes_commentary": None,
        "banners": ["dq_warnings"],
        "flags": {"partial_coverage": ["team:t9"], "notes": []},
        "contested": [],
        "removed": [{"where": "sections[0]", "reason": "unverified"}],
        "coverage": _coverage(),
        "dead_tasks": [
            {"task_id": "task_x", "role": "analyst", "objective": "o", "last_error": None}
        ],
        "query_ids": [_Q1],
        "verification": _verification(),
    } | overrides


def test_ut06_08_valid_draft_defaults() -> None:
    """UT06-08 a valid draft validates; schema_version 1, mode full and empty custom list."""
    draft = ReportDraft.model_validate(_draft())
    assert draft.schema_version == "1"
    assert draft.mode == "full"
    assert draft.portfolio_custom == []
    assert [r.rank for r in draft.recommendations] == [1, 2]
    assert draft.recommendations[0].action_levers[0]["delta_usd_ref"] == "n2"
    assert ReportDraft.model_validate_json(draft.model_dump_json()) == draft


def test_ut06_08_numbers_without_finding_ids_rejected() -> None:
    """UT06-08 a paragraph with numbers and no finding ids is rejected."""
    with pytest.raises(ValidationError, match="finding_ids"):
        Paragraph.model_validate(_paragraph(finding_ids=[]))
    assert Paragraph.model_validate(_paragraph(numbers=[], finding_ids=[])).numbers == []


@pytest.mark.parametrize(
    "paragraph",
    [
        _paragraph(numbers=[_ref(), _ref()]),
        _paragraph(text=""),
        _paragraph(text="x" * 4_001),
        _paragraph(numbers=[_ref(f"n{i}") for i in range(41)]),
        _paragraph(finding_ids=["fnd_bad"]),
    ],
    ids=["dup_number_id", "empty_text", "long_text", "too_many_numbers", "bad_finding_id"],
)
def test_ut06_08_paragraph_bounds(paragraph: dict[str, Any]) -> None:
    """UT06-08 paragraph field bounds and unique number ids."""
    with pytest.raises(ValidationError):
        Paragraph.model_validate(paragraph)


def test_ut06_08_rank_gap_rejected() -> None:
    """UT06-08 recommendation ranks must be 1..n in order."""
    with pytest.raises(ValidationError, match="rank"):
        ReportDraft.model_validate(_draft(recommendations=[_rec(1), _rec(3)]))
    with pytest.raises(ValidationError, match="rank"):
        ReportDraft.model_validate(_draft(recommendations=[_rec(2), _rec(1)]))


def test_ut06_08_unknown_flags_key_rejected() -> None:
    """UT06-08 flags keys are only partial_coverage and notes."""
    with pytest.raises(ValidationError, match="flags"):
        ReportDraft.model_validate(_draft(flags={"partial_coverage": [], "other": []}))
    assert ReportDraft.model_validate(_draft(flags={})).flags == {}


def test_ut06_08_summary_401_chars_rejected() -> None:
    """UT06-08 a recommendation summary of 401 characters is rejected (R-30)."""
    assert RecommendationItem.model_validate(_rec(summary="x" * 400)).summary == "x" * 400
    with pytest.raises(ValidationError, match="summary"):
        RecommendationItem.model_validate(_rec(summary="x" * 401))


def test_ut06_08_findings_only_with_recommendation_rejected() -> None:
    """UT06-08 findings_only holds no recommendations and only the executive summary (R-49)."""
    only_summary = [{"id": "executive_summary", "title": "Findings", "paragraphs": []}]
    draft = ReportDraft.model_validate(
        _draft(mode="findings_only", sections=only_summary, recommendations=[])
    )
    assert draft.mode == "findings_only"
    with pytest.raises(ValidationError, match="findings_only"):
        ReportDraft.model_validate(_draft(mode="findings_only", sections=only_summary))
    with pytest.raises(ValidationError, match="findings_only"):
        ReportDraft.model_validate(_draft(mode="findings_only", recommendations=[]))


@pytest.mark.parametrize(
    "overrides",
    [
        {"sections": [
            {"id": "method", "title": "M", "paragraphs": []},
            {"id": "method", "title": "M2", "paragraphs": []},
        ]},
        {"banners": ["dq_warnings", "dq_warnings"]},
        {"banners": ["unknown_banner"]},
        {"caveats": ["x" * 601]},
        {"title": ""},
        {"run_id": "run_bad"},
        {"removed": [{"where": "x"}]},
        {"removed": [{"where": "x", "reason": "y", "extra": "z"}]},
        {"dead_tasks": [{"task_id": "t", "role": "analyst", "objective": "o"}]},
        {"contested": ["fnd_bad"]},
        {"schema_version": "2"},
        {"mode": "partial"},
        {"unknown": 1},
    ],
    ids=[
        "dup_section", "dup_banner", "bad_banner", "long_caveat", "empty_title", "bad_run_id",
        "removed_missing_key", "removed_extra_key", "dead_task_missing_key", "bad_contested",
        "bad_schema_version", "bad_mode", "extra_field",
    ],
)  # fmt: skip
def test_ut06_08_draft_invariants(overrides: dict[str, Any]) -> None:
    """UT06-08 draft field bounds and invariants."""
    with pytest.raises(ValidationError):
        ReportDraft.model_validate(_draft(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_usd_ref": "n9"},
        {"effort_usd_ref": "n9"},
        {"action_levers": [
            {"entity_type": "team", "entity_id": "t1", "metric": "m", "delta_usd_ref": "n9"}
        ]},
        {"action_levers": [{"entity_type": "team", "entity_id": "t1", "metric": "m"}]},
        {"action_levers": [
            {"entity_type": "t", "entity_id": "t", "metric": "m", "delta_usd_ref": "n2", "x": "y"}
        ]},
        {"numbers": []},
        {"finding_ids": []},
        {"rank": 0},
        {"kind": "defund"},
        {"headline": "x" * 121},
        {"target_type": "x" * 41},
        {"summary": ""},
    ],
    ids=[
        "dangling_usd_ref", "dangling_effort_ref", "dangling_lever_ref", "lever_missing_key",
        "lever_extra_key", "no_numbers", "no_findings", "rank_zero", "bad_kind", "long_headline",
        "long_target_type", "empty_summary",
    ],
)  # fmt: skip
def test_ut06_08_recommendation_invariants(overrides: dict[str, Any]) -> None:
    """UT06-08 every ref names a number; field bounds hold."""
    with pytest.raises(ValidationError):
        RecommendationItem.model_validate(_rec(**overrides))


def test_ut06_08_sub_types() -> None:
    """UT06-08 section, ranked entity and coverage bounds."""
    with pytest.raises(ValidationError):
        Section.model_validate({"id": "intro", "title": "T", "paragraphs": []})
    with pytest.raises(ValidationError):
        Section.model_validate({"id": "method", "title": "T", "paragraphs": [_paragraph()] * 51})
    with pytest.raises(ValidationError):
        RankedEntity.model_validate({"rank": 1, "entity_type": "epic", "entity_id": "e"})
    with pytest.raises(ValidationError):
        RankedEntity.model_validate({"rank": 0, "entity_type": "team", "entity_id": "e"})
    with pytest.raises(ValidationError):
        Coverage.model_validate(_coverage() | {"dead_tasks": -1})


def _objects(node: object) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found.extend(_objects(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_objects(value))
    return found


def test_ut06_09_writer_schema() -> None:
    """UT06-09 writer schema has no rank, rec_id or coverage; objects closed; same class."""
    model = ReportDraft.writer_output_model()
    assert model is ReportDraft.writer_output_model()
    assert issubclass(model, BaseModel)
    assert set(model.model_fields) == {
        "title", "sections", "recommendations", "caveats", "prior_outcomes_commentary",
    }  # fmt: skip
    schema = ReportDraft.writer_schema()
    assert schema == model.model_json_schema()
    records = [o for o in _objects(schema) if "properties" in o]
    # "rank" survives only as a NumberRef unit enum value (spec 05), never as a field
    properties = {name for o in records for name in o["properties"]}
    assert not properties & {"rank", "rec_id", "coverage", "flags", "verification", "mode"}
    assert '"rank"' not in json.dumps(schema).replace('"score", "rank", "other"', "")
    assert len(records) >= 4  # output, section, paragraph, recommendation, lever, NumberRef
    assert all(o.get("additionalProperties") is False for o in records)
    # the only open object is the spec 05 NumberRef.row_key map (keys are column names)
    open_maps = [o for o in _objects(schema) if "properties" not in o]
    assert open_maps == [schema["$defs"]["_RowKey"]]
    rec_fields = set(RecommendationItem.model_fields) - {"rank", "rec_id"}
    assert set(schema["$defs"]["_WriterRecommendation"]["properties"]) == rec_fields


def test_ut06_09_writer_recommendation_same_validators() -> None:
    """UT06-09 the Writer recommendation keeps the RecommendationItem validators."""
    model = ReportDraft.writer_output_model()
    body = {k: v for k, v in _rec().items() if k != "rank"}
    output = {"title": "T", "sections": [], "recommendations": [body], "caveats": [],
              "prior_outcomes_commentary": None}  # fmt: skip
    assert model.model_validate(output).model_dump()["recommendations"][0]["target_id"] == "epic-1"
    with pytest.raises(ValidationError):
        model.model_validate(output | {"recommendations": [body | {"summary": "x" * 401}]})
    with pytest.raises(ValidationError):
        model.model_validate(output | {"recommendations": [body | {"expected_usd_ref": "n9"}]})
    with pytest.raises(ValidationError):
        model.model_validate(output | {"recommendations": [body | {"rank": 1}]})


def _answer() -> ChatAnswer:
    return ChatAnswer(text="There were [[n1]]", numbers=[_ref()], query_ids=[_Q1])  # type: ignore[list-item]


_EVENTS = [
    ModeEvent(mode="live", message="Answering now"),
    TokenEvent(text="There were"),
    ToolEvent(name="run_sql", query_id=_Q1, ok=True),
    ToolEvent(name="run_sql", query_id=None, ok=False),
    EvidenceEvent(query_id=_Q1),
    VerificationEvent(result=_verification(), status="partial", removed_claims=["x"]),
    EscalatedEvent(run_id=_RUN, job_id=f"job_{_ULID}"),
    FinalEvent(answer=_answer(), run_id=_RUN),
    ErrorEvent(error_type="NotFound", message="message not found", hint=None),
    CorrectionCapturedEvent(memory_id=f"mem_{_ULID}"),
]


def test_ut06_10_every_event_round_trips() -> None:
    """UT06-10 dump and validate returns an equal object for every member."""
    kinds = [event.type for event in _EVENTS]
    assert set(kinds) == {
        "mode", "token", "tool", "evidence", "verification", "escalated", "final", "error",
        "correction_captured",
    }  # fmt: skip
    for event in _EVENTS:
        dumped = event.model_dump(mode="json")
        assert CHAT_EVENT_ADAPTER.validate_python(dumped) == event
        assert CHAT_EVENT_ADAPTER.validate_json(json.dumps(dumped)) == event


def test_ut06_10_unknown_type_rejected() -> None:
    """UT06-10 an unknown or missing type is rejected."""
    with pytest.raises(ValidationError):
        CHAT_EVENT_ADAPTER.validate_python({"type": "thinking", "text": "x"})
    with pytest.raises(ValidationError):
        CHAT_EVENT_ADAPTER.validate_python({"text": "x"})
    with pytest.raises(ValidationError):
        CHAT_EVENT_ADAPTER.validate_python({"type": "mode", "mode": "turbo", "message": "x"})


@pytest.mark.parametrize(
    "answer",
    [
        {"text": ""},
        {"text": "x" * 8_001},
        {"numbers": [_ref(f"n{i}") for i in range(41)]},
        {"unknowns": ["u"] * 21},
        {"followups": ["f"] * 11},
        {"query_ids": ["q_bad"]},
    ],
    ids=["empty_text", "long_text", "many_numbers", "many_unknowns", "many_followups", "bad_q"],
)
def test_ut06_10_chat_answer_bounds(answer: dict[str, Any]) -> None:
    """UT06-10 ChatAnswer field bounds; defaults are empty lists."""
    base = {"text": "t", "numbers": [], "query_ids": []}
    assert ChatAnswer.model_validate(base).unknowns == []
    with pytest.raises(ValidationError):
        ChatAnswer.model_validate(base | answer)


def test_ut06_10_reexported() -> None:
    """UT06-10 the report and chat types are re-exported from herness.core.types."""
    assert shared.ReportDraft is swarm.ReportDraft
    assert shared.ChatEvent is swarm.ChatEvent
    assert shared.CHAT_EVENT_ADAPTER is swarm.CHAT_EVENT_ADAPTER
    assert shared.CorrectionCapturedEvent is swarm.CorrectionCapturedEvent
