"""Tests for herness._cli.payloads: job payload builders and budget parsing (T09-21, U09-92)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from herness._cli import payloads as p
from herness.harness.swarm import RunRequest
from herness.metrics.portfolio import Scenario
from herness.reports.rules import UserInputError

pytestmark = pytest.mark.unit


def test_ut09_70_budgets_and_stage_suffix() -> None:
    """UT09-70 budgets `2,000,000` and `1_500_000.00`, stage `dq` -> scenarios and stages."""
    budgets = [p.parse_budget_usd("2,000,000"), p.parse_budget_usd("1_500_000.00")]
    assert budgets == [Decimal(2_000_000), Decimal(1_500_000)]
    payload = p.review_request("funding_review", "standard", budgets)
    request = payload["request"]
    assert isinstance(request, dict)
    assert [s["name"] for s in request["scenarios"]] == ["custom_2000000", "custom_1500000"]
    assert [s["budget_usd"] for s in request["scenarios"]] == ["2000000", "1500000"]
    assert (request["kind"], request["depth"], request["question"]) == (
        "funding_review",
        "standard",
        None,
    )
    assert json.loads(json.dumps(payload)) == payload  # JSON-serialisable as is
    stages = p.stages_from("dq")
    assert stages == ["dq", "promote"]
    assert p.pipeline_payload(stages, None) == {"stages": ["dq", "promote"], "build_id": None}


def test_ut09_70_review_request_round_trips_to_scenarios() -> None:
    """UT09-70 the `request` payload validates back into a RunRequest with Scenario entries."""
    payload = p.review_request("org_review", "deep", [Decimal(5)], question="Why?")
    req = RunRequest.model_validate(payload["request"])
    assert req.scenarios == [Scenario(name="custom_5", budget_usd=Decimal(5))]
    assert (req.kind, req.depth, req.question) == ("org_review", "deep", "Why?")
    assert p.review_request("org_review", "fast", [])["request"]["scenarios"] == []  # type: ignore[index]


def test_ut09_70_review_request_invalid_is_user_error() -> None:
    """UT09-70 more than five budgets or an unknown depth -> UserInputError."""
    with pytest.raises(UserInputError, match="invalid review request"):
        p.review_request("funding_review", "standard", [Decimal(n) for n in range(1, 7)])
    with pytest.raises(UserInputError):
        p.review_request("funding_review", "huge", [])


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1", Decimal(1)),
        ("1.00", Decimal(1)),
        ("2.00", Decimal(2)),
        ("2,000,000.00", Decimal(2_000_000)),
        ("1_500_000", Decimal(1_500_000)),
        ("2000000", Decimal(2_000_000)),
        ("1,000,000,000,000", Decimal(10**12)),
        ("1_000_000_000_000.00", Decimal(10**12)),
    ],
)
def test_ut09_70_parse_budget_accepts(text: str, value: Decimal) -> None:
    """UT09-70 digits with `_`/`,` separators and an optional `.00`; 1 to 10^12."""
    parsed = p.parse_budget_usd(text)
    assert parsed == value
    assert parsed == parsed.to_integral_value()
    assert str(parsed) == str(int(value))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "0",
        "0.00",
        "1.5",
        "1.50",
        "2.5",
        "2.01",
        "1.000",
        "-5",
        " 5",
        "5 ",
        "abc",
        "1,,000",
        ",100",
        "100_",
        "1e6",
        "1_000_000_000_001",
        "9" * 40,
    ],
)
def test_ut09_70_parse_budget_rejects(text: str) -> None:
    """UT09-70 anything else (zero, fractions, signs, spaces, > 10^12) -> UserInputError."""
    with pytest.raises(UserInputError, match="whole number of US dollars"):
        p.parse_budget_usd(text)


def test_ut09_70_pipeline_payload_keys_and_stages() -> None:
    """UT09-70 DD-10 keys: stages, build_id plus enrich_stage / depth / score_steps."""
    build = "20260925-101500-ABCDEF"
    payload = p.pipeline_payload(("enrich",), build, enrich_stage="classify", depth="fast")
    assert payload == {
        "stages": ["enrich"],
        "build_id": build,
        "enrich_stage": "classify",
        "depth": "fast",
    }
    scored = p.pipeline_payload(p.stages_from("score"), build, score_steps=["funding"])
    assert scored["stages"] == ["score", "dq", "promote"]
    assert scored["score_steps"] == ["funding"]
    assert p.stages_from("build") == list(p.STAGE_ORDER)
    assert p.STAGE_ORDER == ("build", "enrich", "score", "dq", "promote")


def test_ut09_70_unknown_stage_is_user_error() -> None:
    """UT09-70 an unknown stage name -> UserInputError, in either builder."""
    with pytest.raises(UserInputError, match="unknown stage"):
        p.stages_from("deploy")
    with pytest.raises(UserInputError, match="unknown stage"):
        p.pipeline_payload(["build", "deploy"], None)
