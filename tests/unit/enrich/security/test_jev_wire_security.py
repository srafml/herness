"""Security tests for the Jev-shape response parser (TH03-06, T03-11)."""

from __future__ import annotations

import json

import pytest

from herness.core.errors import OutputValidationError
from herness.core.types import Question
from herness.enrich.deciders.jev_wire import MAX_BODY_BYTES, load_wire_body, parse_wire_answers

pytestmark = pytest.mark.unit

_CHOICE = Question(
    id="root_cause",
    type="choice",
    instructions="Classify the root cause of this ticket.",
    options={"defect": "A software defect.", "other": "Anything else."},
    threshold=0.9,
)
_BOOL = Question(
    id="is_outage", type="bool", instructions="Is this ticket an outage?", threshold=0.9
)


def _body(answers: dict[str, object], pad: int = 0) -> bytes:
    return json.dumps({"model": "openjev-latest", "answers": answers, "pad": "x" * pad}).encode()


def test_st03_08_oversized_response_rejected() -> None:
    """ST03-08 a 2 MB response body -> OutputValidationError before JSON parsing."""
    body = _body({"root_cause": {"probabilities": {"defect": 0.5, "other": 0.5}}}, 2 * 1024 * 1024)
    assert len(body) > MAX_BODY_BYTES
    with pytest.raises(OutputValidationError):
        load_wire_body(body)
    small = load_wire_body(_body({"is_outage": {"noul": 0.9}}))
    answers = small["answers"]
    assert isinstance(answers, dict)
    assert parse_wire_answers(answers, (_BOOL,))["is_outage"].answer == "true"


@pytest.mark.parametrize(
    "body",
    [b"\xff\xfe", b"{not json", b"[1, 2]", b"[" * 100_000, b'{"answers": {"a": NaN}}'],
    ids=["bad_utf8", "bad_json", "not_object", "deep_nesting", "nan_constant"],
)
def test_st03_08_malformed_body_rejected(body: bytes) -> None:
    """ST03-08 undecodable, non-object, deeply nested or NaN-bearing bodies are rejected."""
    with pytest.raises(OutputValidationError):
        load_wire_body(body)


@pytest.mark.parametrize(
    "answers",
    [
        {"root_cause": {"probabilities": {"defect": float("nan"), "other": 1.0}}},
        {"root_cause": {"probabilities": [float("nan"), 1.0]}},
        {"root_cause": {"probabilities": [0.5, float("inf")]}},
        {"is_outage": {"noul": float("nan")}},
        {"root_cause": {"probabilities": [0.5, 0.5], "confidence": float("nan")}},
    ],
    ids=["nan_dict", "nan_list", "inf_list", "nan_noul", "nan_confidence"],
)
def test_st03_08_nan_probabilities_rejected(answers: dict[str, object]) -> None:
    """ST03-08 NaN or infinite probabilities -> OutputValidationError."""
    with pytest.raises(OutputValidationError):
        parse_wire_answers(answers, (_CHOICE, _BOOL))


@pytest.mark.parametrize(
    "answers",
    [
        {"root_cause": {"probabilities": {"defect": 0.5, "other": 0.3, "extra": 0.2}}},
        {"root_cause": {"probabilities": [0.5, 0.3, 0.2]}},
    ],
    ids=["extra_key", "extra_list_entry"],
)
def test_st03_08_extra_option_rejected(answers: dict[str, object]) -> None:
    """ST03-08 an option outside the question's label set -> OutputValidationError."""
    with pytest.raises(OutputValidationError):
        parse_wire_answers(answers, (_CHOICE,))
