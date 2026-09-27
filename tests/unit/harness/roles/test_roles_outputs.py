"""Tests for the part-2 role output models (impl 05 U05-50, T05-20): UT05-93.

`WriterOutput` must publish the same JSON Schema as `ReportDraft.writer_schema()` once `title`
annotations are dropped and `$defs` names are normalized (D05-18); `SkepticOutput` holds exactly
one `CheckResult` per `SkepticCheck`.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from herness.core.types import (
    SKEPTIC_CHECKS,
    ChatAnswer,
    CheckResult,
    Paragraph,
    RecommendationItem,
    ReportDraft,
    Section,
    SkepticCheck,
)
from herness.harness.roles.chat import CHAT
from herness.harness.roles.skeptic import SkepticOutput
from herness.harness.roles.writer import WriterOutput, WriterRecommendation

pytestmark = pytest.mark.unit

_FND = "fnd_01ARZ3NDEKTSV4RRFFQ69G5FAV"
_QID = "q_0123456789abcdef"
_NUMBER = {"id": "n1", "value": 1, "unit": "count", "query_id": _QID, "column": "c"}
_NUMBER |= {"row_key": None}


def _normalize(schema: dict[str, Any]) -> Any:
    """Drop `title` annotations and inline every `$ref`, so `$defs` names no longer matter."""
    defs: dict[str, Any] = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            rest = {k: v for k, v in node.items() if k != "$ref"}
            return {**walk(defs[node["$ref"].removeprefix("#/$defs/")]), **walk(rest)}
        return {
            k: walk(v)
            for k, v in node.items()
            if k != "$defs" and not (k == "title" and isinstance(v, str))
        }

    return walk(schema)


def test_ut05_93_writer_schema_equals_writer_schema() -> None:
    """UT05-93 WriterOutput schema equals ReportDraft.writer_schema() normalized (D05-18)."""
    ours = WriterOutput.model_json_schema()
    theirs = ReportDraft.writer_schema()
    assert _normalize(ours) == _normalize(theirs)
    assert ours["properties"].keys() == theirs["properties"].keys()


def test_ut05_93_normalizer_keeps_real_differences() -> None:
    """UT05-93 the normalizer drops only title annotations: a changed limit still differs."""
    theirs = ReportDraft.writer_schema()
    changed = WriterOutput.model_json_schema()
    changed["properties"]["title"]["maxLength"] = 201
    assert _normalize(changed) != _normalize(theirs)
    assert "title" in _normalize(theirs)["properties"]


def test_ut05_93_writer_recommendation_fields() -> None:
    """UT05-93 WriterRecommendation = RecommendationItem fields minus rank and rec_id."""
    expected = RecommendationItem.model_fields.keys() - {"rank", "rec_id"}
    assert WriterRecommendation.model_fields.keys() == expected
    assert list(WriterRecommendation.model_fields) == [
        n for n in RecommendationItem.model_fields if n in expected
    ]
    assert WriterRecommendation.model_config.get("extra") == "forbid"
    assert WriterRecommendation.model_config.get("frozen") is True


def test_ut05_93_recommendation_item_has_no_field_validators() -> None:
    """UT05-93 guard: only model validators are carried over, so none may be field validators."""
    assert not RecommendationItem.__pydantic_decorators__.field_validators


def _recommendation(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": "fund",
        "target_type": "service",
        "target_id": "svc-1",
        "headline": "Fund svc-1",
        "summary": "MTTR is high.",
        "numbers": [_NUMBER],
        "expected_delta_ref": "n1",
        "finding_ids": [_FND],
        "query_ids": [_QID],
    }
    return base | over


def test_ut05_93_writer_recommendation_validation() -> None:
    """UT05-93 WriterRecommendation keeps the field limits, the ref check and extra=forbid."""
    rec = WriterRecommendation.model_validate(_recommendation())
    assert rec.model_dump()["expected_delta_ref"] == "n1"
    for bad in (
        _recommendation(rank=1),
        _recommendation(rec_id=None),
        _recommendation(numbers=[]),
        _recommendation(expected_delta_ref="n9"),
        _recommendation(headline="h" * 121),
        _recommendation(finding_ids=[]),
    ):
        with pytest.raises(ValidationError):
            WriterRecommendation.model_validate(bad)


def _writer(**over: object) -> dict[str, object]:
    paragraph = {"text": "MTTR rose.", "numbers": [_NUMBER], "finding_ids": [_FND]}
    base: dict[str, object] = {
        "title": "Funding review",
        "sections": [{"id": "executive_summary", "title": "Summary", "paragraphs": [paragraph]}],
        "recommendations": [_recommendation()],
        "caveats": ["Small sample."],
        "prior_outcomes_commentary": None,
    }
    return base | over


def test_ut05_93_writer_output_validation() -> None:
    """UT05-93 WriterOutput types its parts with spec 06 models and forbids extra keys."""
    out = WriterOutput.model_validate(_writer())
    assert isinstance(out.sections[0], Section)
    assert isinstance(out.recommendations[0], WriterRecommendation)
    para = {"text": "t", "numbers": [], "finding_ids": []}
    assert isinstance(
        WriterOutput.model_validate(
            _writer(prior_outcomes_commentary=para)
        ).prior_outcomes_commentary,
        Paragraph,
    )
    for bad in (
        _writer(title=""),
        _writer(title="t" * 201),
        _writer(caveats=["c" * 601]),
        _writer(extra=1),
        _writer(recommendations=[_recommendation(rank=1)]),
    ):
        with pytest.raises(ValidationError):
            WriterOutput.model_validate(bad)
    with pytest.raises(ValidationError):
        out.__setattr__("title", "other")


def _checks(*names: str) -> list[dict[str, object]]:
    return [{"check": n, "result": "pass", "note": ""} for n in names]


def test_ut05_93_skeptic_check_count_validator() -> None:
    """UT05-93 SkepticOutput needs exactly one CheckResult per SkepticCheck value."""
    assert set(SKEPTIC_CHECKS) == set(get_args(SkepticCheck.__value__))
    ok = {"finding_id": _FND, "checks": _checks(*SKEPTIC_CHECKS), "verdict": "uphold"}
    out = SkepticOutput.model_validate(ok)
    assert all(isinstance(c, CheckResult) for c in out.checks)
    assert out.required_actions == []
    reordered = SkepticOutput.model_validate({**ok, "checks": _checks(*reversed(SKEPTIC_CHECKS))})
    assert len(reordered.checks) == len(SKEPTIC_CHECKS)
    for checks in (
        _checks(*SKEPTIC_CHECKS[:-1]),
        _checks(*SKEPTIC_CHECKS, SKEPTIC_CHECKS[0]),
        _checks(*SKEPTIC_CHECKS[:-1], SKEPTIC_CHECKS[0]),
        [],
    ):
        with pytest.raises(ValidationError, match="exactly one"):
            SkepticOutput.model_validate({**ok, "checks": checks})


def test_ut05_93_skeptic_revise_needs_required_actions() -> None:
    """UT05-93 a revise verdict needs at least one required action (as Challenge)."""
    ok = {"finding_id": _FND, "checks": _checks(*SKEPTIC_CHECKS), "verdict": "revise"}
    for actions in ([], None):
        payload = ok if actions is None else {**ok, "required_actions": actions}
        with pytest.raises(ValidationError, match="revise verdict needs"):
            SkepticOutput.model_validate(payload)
    out = SkepticOutput.model_validate({**ok, "required_actions": ["split by service"]})
    assert out.required_actions == ["split by service"]
    for verdict in ("uphold", "reject"):
        assert SkepticOutput.model_validate({**ok, "verdict": verdict}).required_actions == []


def test_ut05_93_skeptic_output_fields() -> None:
    """UT05-93 SkepticOutput verdicts, required_actions and extra keys (TH05-16)."""
    ok = {"finding_id": _FND, "checks": _checks(*SKEPTIC_CHECKS), "verdict": "revise"}
    out = SkepticOutput.model_validate({**ok, "required_actions": ["re-run q"]})
    assert (out.verdict, out.required_actions) == ("revise", ["re-run q"])
    rest = SKEPTIC_CHECKS[1:]
    for bad in (
        {**ok, "verdict": "maybe"},
        {**ok, "finding_id": "task_01ARZ3NDEKTSV4RRFFQ69G5FAV"},
        {**ok, "required_actions": ["a" * 401]},
        {**ok, "required_actions": ["a"] * 11},
        {**ok, "round": 1},
        {**ok, "checks": [{**_checks(SKEPTIC_CHECKS[0])[0], "result": "fail"}, *_checks(*rest)]},
    ):
        with pytest.raises(ValidationError):
            SkepticOutput.model_validate(bad)


def test_ut05_93_chat_output_is_chat_answer() -> None:
    """UT05-93 the chat role uses ChatAnswer directly."""
    assert CHAT.output_model is ChatAnswer
