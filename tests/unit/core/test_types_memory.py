"""Tests for herness.core.types.memory (impl 07 U07-01 … U07-10; UT07-01 … UT07-03)."""

from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from herness.core.errors import ToolInputError
from herness.core.types import (
    KIND_LAYER,
    ConfidenceAdjustment,
    Kind,
    MemoryItem,
    MemoryProposal,
    MemoryRunContext,
    NumberRef,
    PriorContext,
    PriorRecommendation,
    Provenance,
    RecallHit,
    RecommendationDraft,
    SimilarOutcome,
    ToolContext,
)

pytestmark = pytest.mark.unit

ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RUN = f"run_{ULID}"
TASK = f"task_{ULID}"
MEM = f"mem_{ULID}"
FND = f"fnd_{ULID}"
QID = "q_0123456789abcdef"
USER = "a" * 32
BUILD = "20260101-000000-ABCDEF"
NOW = datetime(2026, 9, 1, tzinfo=UTC)
NAIVE = datetime(2026, 9, 1)  # noqa: DTZ001 - deliberately naive


def _agent(**overrides: Any) -> Provenance:
    fields: dict[str, Any] = {
        "author_type": "agent",
        "author_role": "analyst",
        "author_ref": None,
        "run_id": RUN,
        "task_id": TASK,
        "via": "tool",
    }
    fields.update(overrides)
    return Provenance(**fields)


def _human(**overrides: Any) -> Provenance:
    fields: dict[str, Any] = {
        "author_type": "human",
        "author_role": None,
        "author_ref": USER,
        "run_id": None,
        "task_id": None,
        "via": "dashboard",
    }
    fields.update(overrides)
    return Provenance(**fields)


def _item(**overrides: Any) -> MemoryItem:
    fields: dict[str, Any] = {
        "memory_id": MEM,
        "layer": "semantic",
        "kind": "insight",
        "content": "backlog grows on Mondays",
        "data": {},
        "provenance": _agent(),
        "confidence": 0.5,
        "status": "active",
        "created_at": NOW,
        "expires_at": None,
        "last_used_at": None,
        "use_count": 0,
    }
    fields.update(overrides)
    return MemoryItem(**fields)


def _number(nid: str = "n1") -> NumberRef:
    return NumberRef(id=nid, value=3, unit="count", query_id=QID, column="c", row_key=None)


def _draft(**overrides: Any) -> RecommendationDraft:
    fields: dict[str, Any] = {
        "rank": 1,
        "kind": "fund",
        "target_type": "service",
        "target_id": "svc-1",
        "summary": "Fund the on-call rotation [n1].",
        "numbers": [_number()],
        "expected_metric": None,
        "expected_delta_ref": None,
        "expected_usd_ref": "n1",
        "finding_ids": [FND],
    }
    fields.update(overrides)
    return RecommendationDraft(**fields)


def test_ut07_01_kind_layer_complete_and_read_only() -> None:
    """UT07-01 KIND_LAYER maps every kind to its design 07 §3.2 layer and is read-only."""
    assert isinstance(KIND_LAYER, MappingProxyType)
    assert set(KIND_LAYER) == set(get_args(Kind))
    by_layer: dict[str, set[str]] = {}
    for kind, layer in KIND_LAYER.items():
        by_layer.setdefault(layer, set()).add(kind)
    assert by_layer == {
        "episodic": {"run_summary", "outcome_summary", "decision_note"},
        "semantic": {"glossary", "business_rule", "mapping", "insight", "user_correction"},
        "procedural": {"sql_template", "qa_pair", "analysis_recipe"},
    }
    with pytest.raises(TypeError):
        KIND_LAYER["insight"] = "episodic"  # type: ignore[index]


def test_ut07_01_provenance_invariants() -> None:
    """UT07-01 agent without role or run, human without ref and extra fields are rejected."""
    assert _agent().author_role == "analyst"
    human = _human()
    assert human.query_ids == []
    assert human.finding_ids == []
    assert human.session_id is None
    with pytest.raises(ValidationError, match="author_role"):
        _agent(author_role=None)
    with pytest.raises(ValidationError, match="run_id"):
        _agent(run_id=None)
    with pytest.raises(ValidationError, match="author_ref"):
        _human(author_ref=None)
    bad_values: list[dict[str, Any]] = [
        {"author_kind": "x"},
        {"run_id": "run_bad"},
        {"task_id": "task_bad"},
        {"author_ref": "A" * 32},
        {"author_role": "r" * 41},
        {"query_ids": [QID] * 21},
        {"finding_ids": ["fnd_x"]},
        {"build_id": "bad"},
        {"via": "email"},
        {"session_id": "s" * 65},
    ]
    for bad in bad_values:
        with pytest.raises(ValidationError):
            _agent(**bad)
    with pytest.raises(ValidationError):
        _agent().author_role = "x"  # type: ignore[misc]


def test_ut07_01_memory_item_validation() -> None:
    """UT07-01 bad confidence, layer mismatch, naive datetimes and long content are rejected."""
    item = _item(created_at="2026-09-01T00:00:00.000000Z", data={"a": [1, "b"]})
    assert item.created_at == NOW
    assert item.provenance.run_id == RUN
    bad_values: list[dict[str, Any]] = [
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"use_count": -1},
        {"layer": "episodic"},
        {"created_at": NAIVE},
        {"expires_at": NAIVE},
        {"last_used_at": NAIVE},
        {"memory_id": "mem_x"},
        {"content": "x" * 2_001},
        {"status": "deleted"},
        {"extra": 1},
    ]
    for bad in bad_values:
        with pytest.raises(ValidationError):
            _item(**bad)
    long_sql = _item(layer="procedural", kind="sql_template", content="x" * 8_000)
    assert len(long_sql.content) == 8_000
    with pytest.raises(ValidationError):
        _item(layer="procedural", kind="qa_pair", content="x" * 8_001)


def test_ut07_01_memory_proposal_validation() -> None:
    """UT07-01 proposal: bad confidence, extra field, duplicate numbers, empty content."""
    fields: dict[str, Any] = {
        "layer": "semantic",
        "kind": "insight",
        "content": "c",
        "confidence": 0.6,
        "provenance": _agent(),
    }
    proposal = MemoryProposal(**fields)
    assert proposal.data == {}
    assert proposal.numbers == []
    assert proposal.expires_at is None
    bad_values: list[dict[str, Any]] = [
        {"confidence": 2.0},
        {"extra": 1},
        {"content": ""},
        {"content": "x" * 8_001},
        {"numbers": [_number(), _number()]},
        {"numbers": [_number(f"n{i}") for i in range(21)]},
        {"confidence": "0.5"},
    ]
    for bad in bad_values:
        with pytest.raises(ValidationError):
            MemoryProposal(**{**fields, **bad})


def test_ut07_01_recall_hit_consistency() -> None:
    """UT07-01 RecallHit needs the six components, final == score, unconfirmed == pending."""
    comps = {"sim": 0.5, "kw": 0.1, "ent": 0.0, "rec": 1.0, "conf": 0.5, "final": 0.4}
    base: dict[str, Any] = {"item": _item(), "score": 0.4, "components": comps}
    hit = RecallHit(**base, unconfirmed=False)
    assert hit.components["final"] == hit.score
    pending = _item(status="pending_approval")
    assert RecallHit(item=pending, score=0.4, components=comps, unconfirmed=True).unconfirmed
    bad_values: list[dict[str, Any]] = [
        {"score": 0.3},
        {"unconfirmed": True},
        {"components": {**comps, "x": 0.1}},
        {"components": {"final": 0.4}},
        {"score": 1.5, "components": {**comps, "final": 1.5}},
    ]
    for bad in bad_values:
        with pytest.raises(ValidationError):
            RecallHit(**{**base, "unconfirmed": False, **bad})


def _ctx(**overrides: Any) -> ToolContext:
    fields: dict[str, Any] = {
        "run_id": RUN,
        "task_id": TASK,
        "build_id": BUILD,
        "role": "analyst",
        "profile": "local",
    }
    fields.update(overrides)
    return ToolContext.model_construct(**fields)


def test_ut07_02_from_tool_ctx_copies_fields() -> None:
    """UT07-02 from_tool_ctx copies ctx fields; session and user come from run_meta."""
    plain = MemoryRunContext.from_tool_ctx(_ctx(), run_meta={"kind": "analysis"})
    assert plain == MemoryRunContext(
        run_id=RUN,
        run_kind="analysis",
        role="analyst",
        task_id=TASK,
        build_id=BUILD,
        profile="local",
    )
    assert plain.session_id is None
    assert plain.user_ref is None
    chat = MemoryRunContext.from_tool_ctx(
        _ctx(), run_meta={"kind": "chat", "session_id": "s1", "message_id": "m1", "user_ref": USER}
    )
    assert (chat.run_kind, chat.session_id, chat.user_ref) == ("chat", "s1", USER)
    odd = MemoryRunContext.from_tool_ctx(_ctx(), run_meta={"kind": "chat", "session_id": 3})
    assert odd.session_id is None


def test_ut07_02_missing_kind_raises_tool_input_error() -> None:
    """UT07-02 run_meta without kind raises ToolInputError naming the run."""
    with pytest.raises(ToolInputError, match=f"run kind unknown for {RUN}"):
        MemoryRunContext.from_tool_ctx(_ctx(), run_meta={"session_id": "s1"})
    with pytest.raises(ToolInputError):
        MemoryRunContext.from_tool_ctx(_ctx(), run_meta={"kind": None})


def test_ut07_03_recommendation_draft_limits() -> None:
    """UT07-03 summary 401 chars, empty finding_ids, bad refs and duplicate numbers fail."""
    assert _draft(summary="x" * 400).summary == "x" * 400
    bad_values: list[dict[str, Any]] = [
        {"summary": "x" * 401},
        {"summary": ""},
        {"finding_ids": []},
        {"finding_ids": [FND] * 51},
        {"finding_ids": ["fnd_x"]},
        {"rank": 0},
        {"rank": "1"},
        {"kind": "hire"},
        {"target_type": "person"},
        {"target_id": "x" * 201},
        {"expected_usd_ref": "1"},
        {"expected_delta_ref": "nx"},
        {"numbers": [_number(), _number()]},
        {"numbers": [_number(f"n{i}") for i in range(21)]},
        {"extra": 1},
    ]
    for bad in bad_values:
        with pytest.raises(ValidationError):
            _draft(**bad)
    fields = _draft().model_dump()
    del fields["expected_metric"]
    with pytest.raises(ValidationError):
        RecommendationDraft(**fields)


def _prior(**overrides: Any) -> PriorRecommendation:
    fields: dict[str, Any] = {
        "rec_id": f"rec_{ULID}",
        "run_id": RUN,
        "kind": "fund",
        "target_type": "service",
        "target_id": "svc-1",
        "summary": "Fund [n1].",
        "numbers": [_number()],
        "expected_metric": None,
        "confidence": 0.5,
        "decision": None,
        "decided_at": None,
        "effective_at": None,
        "outcome": None,
        "next_measurement_due": None,
    }
    fields.update(overrides)
    return PriorRecommendation(**fields)


def test_ut07_03_prior_recommendation_outcome_keys() -> None:
    """UT07-03 an outcome must carry exactly the eight keys."""
    outcome: dict[str, Any] = {
        "outcome_id": "out_1",
        "measurement": "30d",
        "verdict": "paid_off",
        "baseline": 1.0,
        "actual": 2.0,
        "delta": 1.0,
        "rel": 1.0,
        "query_id": QID,
    }
    rec = _prior(
        outcome=outcome, decision="accepted", decided_at=NOW, next_measurement_due=date(2026, 10, 1)
    )
    assert rec.outcome == outcome
    partial = {k: v for k, v in outcome.items() if k != "rel"}
    for bad in ({"outcome": partial}, {"outcome": {**outcome, "extra": 1}}, {"decision": "maybe"}):
        with pytest.raises(ValidationError):
            _prior(**bad)


def test_ut07_03_prior_context_tally_filled() -> None:
    """UT07-03 missing tally keys are filled with 0; unknown keys are rejected."""
    ctx = PriorContext(items=[_prior()], rendered="", memory_ids=[], tally={"accepted": 2})
    assert ctx.tally == {
        "accepted": 2,
        "paid_off": 0,
        "no_effect": 0,
        "worse": 0,
        "inconclusive": 0,
        "pending": 0,
    }
    empty = PriorContext(items=[], rendered="", memory_ids=[], tally={})
    assert set(empty.tally.values()) == {0}
    with pytest.raises(ValidationError):
        PriorContext(items=[], rendered="", memory_ids=[], tally={"won": 1})


def test_ut07_03_confidence_adjustment_bounds() -> None:
    """UT07-03 ConfidenceAdjustment and SimilarOutcome hold their U07-10 bounds."""
    similar = SimilarOutcome(rec_id=f"rec_{ULID}", sim=0.9, verdict="worse", outcome_query_id=QID)
    base: dict[str, Any] = {"confidence": 0.5, "base": 0.6, "delta": 0.0, "similar": []}
    adj = ConfidenceAdjustment(**{**base, "similar": [similar]})
    assert adj.similar[0].verdict == "worse"
    bad_values: list[dict[str, Any]] = [
        {"confidence": 0.96},
        {"confidence": 0.04},
        {"base": 1.1},
        {"delta": -0.26},
        {"delta": 0.16},
        {"similar": [similar] * 21},
    ]
    for bad in bad_values:
        with pytest.raises(ValidationError):
            ConfidenceAdjustment(**{**base, **bad})
    with pytest.raises(ValidationError):
        SimilarOutcome(rec_id="r", sim=0.1, verdict="better", outcome_query_id=QID)
