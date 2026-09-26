"""Tests for the Jev-shape wire mapping and parser (U03-49, U03-50, T03-11).

The recorded OpenJev fixtures (`tests/fixtures/openjev/`, impl 11) do not exist yet;
per the program ruling these tests use hand-built payloads in the documented shapes
(design 03 §3.3), covering both `probabilities` shapes until V-10 freezes one.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

from herness.core.errors import ConfigError, OutputValidationError
from herness.core.types import Question
from herness.enrich.deciders.jev_wire import parse_wire_answers, to_wire_questions

pytestmark = pytest.mark.unit

_LEVELS = ("No impact at all.", "Minor impact.", "Major impact.", "Outage.")
_BOOL = Question(
    id="is_outage", type="bool", instructions="Is this ticket an outage?", threshold=0.9
)
_CHOICE = Question(
    id="root_cause",
    type="choice",
    instructions="Classify the root cause of this ticket.",
    options={"defect": "A software defect.", "config": "A configuration error.", "other": "Other."},
    threshold=0.9,
)
_SCORE = Question(
    id="impact",
    type="score",
    instructions="Rate the impact of this ticket.",
    levels=_LEVELS,
    threshold=0.8,
)
_ASKED = (_BOOL, _CHOICE, _SCORE)


def test_ut03_46_wire_shapes() -> None:
    """UT03-46 bool, choice and score questions map to the design 03 §3.2 wire shapes."""
    wire = to_wire_questions(_ASKED)
    assert list(wire) == ["is_outage", "root_cause", "impact"]
    assert wire["is_outage"] == {"type": "noul", "instructions": _BOOL.instructions}
    criteria = {
        "defect": "A software defect.",
        "config": "A configuration error.",
        "other": "Other.",
    }
    assert wire["root_cause"] == {
        "type": "choice",
        "instructions": _CHOICE.instructions,
        "criteria": criteria,
    }
    assert list(criteria) == list(wire["root_cause"]["criteria"])  # type: ignore[call-overload]
    assert wire["impact"] == {
        "type": "score",
        "instructions": _SCORE.instructions,
        "criteria": list(_LEVELS),
    }
    assert list(to_wire_questions((_SCORE, _BOOL))) == ["impact", "is_outage"]
    json.dumps(wire)  # the mapping is JSON-serialisable as sent


def test_ut03_46_unresolved_or_oversized_choice_is_config_error() -> None:
    """UT03-46 choice without resolved options or with > 255 options -> ConfigError."""
    dynamic = Question(
        id="owning_team",
        type="choice",
        instructions="Which team owns this ticket?",
        options_source="core.team",
        threshold=0.9,
    )
    with pytest.raises(ConfigError):
        to_wire_questions((dynamic,))
    big = Question(
        id="owning_team",
        type="choice",
        instructions="Which team owns this ticket?",
        options={f"team_{i}": f"Team {i}." for i in range(256)},
        options_source="core.team",
        threshold=0.9,
    )
    with pytest.raises(ConfigError):
        to_wire_questions((big,))


def _fixture() -> dict[str, Any]:
    """A hand-built OpenJev 0.4.0-shaped `answers` object (dict `probabilities`)."""
    return {
        "is_outage": {"noul": 0.8},
        "root_cause": {
            "choice": "config",
            "probabilities": {"defect": 0.2, "config": 0.7, "other": 0.1},
            "confidence": 0.4,
        },
        "impact": {
            "score": 1.9,
            "legend": "0=none 3=outage",
            "probabilities": {"0": 0.1, "1": 0.15, "2": 0.3, "3": 0.45},
            "confidence": 0.2,
        },
    }


def test_ut03_47_parse_dict_shapes() -> None:
    """UT03-47 noul, choice dict and score dict parse; score = argmax, not `score`."""
    answers = parse_wire_answers(json.loads(json.dumps(_fixture())), _ASKED)
    assert list(answers) == ["is_outage", "root_cause", "impact"]
    noul = answers["is_outage"]
    assert (noul.answer, noul.probability, noul.backend_confidence) == ("true", 0.8, None)
    assert noul.distribution == pytest.approx({"true": 0.8, "false": 0.2})
    choice = answers["root_cause"]
    assert (choice.answer, choice.probability, choice.backend_confidence) == ("config", 0.7, 0.4)
    assert choice.distribution == {"defect": 0.2, "config": 0.7, "other": 0.1}
    score = answers["impact"]
    assert (score.answer, score.probability, score.backend_confidence) == ("3", 0.45, 0.2)
    assert score.distribution == {"0": 0.1, "1": 0.15, "2": 0.3, "3": 0.45}


def test_ut03_47_parse_list_shapes() -> None:
    """UT03-47 choice list and score list/description-keyed shapes parse the same way."""
    raw = {
        "is_outage": {"noul": 0.3},
        "root_cause": {"choice": "defect", "probabilities": [0.5, 0.3, 0.2]},
        "impact": {"score": 1.0, "probabilities": [0.1, 0.7, 0.1, 0.1]},
    }
    answers = parse_wire_answers(raw, _ASKED)
    assert answers["is_outage"].answer == "false"
    assert answers["is_outage"].probability == pytest.approx(0.7)
    assert answers["root_cause"].answer == "defect"
    assert answers["root_cause"].backend_confidence is None
    assert answers["root_cause"].distribution == {"defect": 0.5, "config": 0.3, "other": 0.2}
    assert answers["impact"].answer == "1"
    probs = dict(zip(_LEVELS, (0.05, 0.05, 0.8, 0.1), strict=True))
    by_description = {"impact": {"probabilities": probs}}
    assert parse_wire_answers(by_description, _ASKED)["impact"].answer == "2"


def test_ut03_47_ties_absent_and_mismatch() -> None:
    """UT03-47 ties pick the first option; absent questions are absent; mismatch logs."""
    probs = {"other": 0.4, "config": 0.4, "defect": 0.2}
    raw = {"root_cause": {"choice": "other", "probabilities": probs}}
    with capture_logs() as logs:
        answers = parse_wire_answers(raw, _ASKED)
    assert list(answers) == ["root_cause"]
    assert answers["root_cause"].answer == "config"
    assert answers["root_cause"].distribution == {"defect": 0.2, "config": 0.4, "other": 0.4}
    mismatch = [e for e in logs if e["event"] == "enrich.decider.choice_mismatch"]
    assert mismatch
    assert mismatch[0]["log_level"] == "debug"
    assert mismatch[0]["question"] == "root_cause"
    assert parse_wire_answers({"is_outage": {"noul": 0.5}}, _ASKED)["is_outage"].answer == "true"
    assert parse_wire_answers({"is_outage": {"noul": 1}}, _ASKED)["is_outage"].answer == "true"
    assert parse_wire_answers({}, _ASKED) == {}


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(
            {"root_cause": {"probabilities": {"defect": 0.2, "config": 0.7, "bogus": 0.1}}},
            id="unknown_option",
        ),
        pytest.param(
            {"root_cause": {"probabilities": {"defect": 0.3, "config": 0.7}}},
            id="missing_option",
        ),
        pytest.param(
            {"root_cause": {"probabilities": {"defect": 0.2, "config": 0.6, "other": 0.1}}},
            id="sum_0_9",
        ),
        pytest.param({"not_asked": {"noul": 0.5}}, id="unknown_qid"),
        pytest.param({"is_outage": {"noul": 1.2}}, id="noul_1_2"),
    ],
)
def test_ut03_48_invalid_answers_rejected(raw: dict[str, Any]) -> None:
    """UT03-48 unknown/missing option, sum 0.9, unknown qid, noul 1.2 -> OutputValidationError."""
    with pytest.raises(OutputValidationError):
        parse_wire_answers(raw, _ASKED)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param([], id="not_object"),
        pytest.param({"is_outage": 0.5}, id="answer_not_object"),
        pytest.param({"is_outage": {}}, id="noul_missing"),
        pytest.param({"is_outage": {"noul": True}}, id="noul_bool"),
        pytest.param({"is_outage": {"noul": "0.5"}}, id="noul_string"),
        pytest.param({"is_outage": {"noul": 0.5, "extra": 1}}, id="extra_field"),
        pytest.param({"root_cause": {}}, id="choice_no_probabilities"),
        pytest.param({"root_cause": {"probabilities": "x"}}, id="probabilities_string"),
        pytest.param({"root_cause": {"probabilities": [0.5, 0.5]}}, id="choice_list_short"),
        pytest.param(
            {"root_cause": {"probabilities": [0.5, 0.5, 0.0], "confidence": "x"}},
            id="bad_confidence",
        ),
        pytest.param(
            {"root_cause": {"probabilities": [0.5, 0.5, 0.0], "confidence": 2.0}},
            id="confidence_range",
        ),
        pytest.param({"root_cause": {"probabilities": [1.5, -0.5, 0.0]}}, id="value_range"),
        pytest.param({"impact": {"probabilities": [0.25, 0.25, 0.5]}}, id="score_list_short"),
        pytest.param(
            {"impact": {"probabilities": {"0": 0.5, "1": 0.5, "2": 0.0, "4": 0.0}}},
            id="score_bad_keys",
        ),
        pytest.param({"impact": {"probabilities": {"0": 1.0}}}, id="score_missing_level"),
    ],
)
def test_ut03_48_malformed_structures_rejected(raw: Any) -> None:
    """UT03-48 structural violations -> OutputValidationError."""
    with pytest.raises(OutputValidationError):
        parse_wire_answers(raw, _ASKED)


def test_ut03_48_answer_validator_failure_is_output_validation_error() -> None:
    """UT03-48 an answer the `Answer` validator rejects (> 255 labels) -> OutputValidationError."""
    many = Question(
        id="owning_team",
        type="choice",
        instructions="Which team owns this ticket?",
        options={f"team_{i}": f"Team {i}." for i in range(300)},
        options_source="core.team",
        threshold=0.9,
    )
    raw = {"owning_team": {"probabilities": [1.0] + [0.0] * 299}}
    with pytest.raises(OutputValidationError):
        parse_wire_answers(raw, (many,))


def test_ut03_48_error_messages_do_not_echo_values() -> None:
    """UT03-48 errors name the question and rule, never the offending value."""
    marker = "sentinel_value_9f3"
    for raw in ({marker: {"noul": 0.5}}, {"root_cause": {"probabilities": {marker: 1.0}}}):
        with pytest.raises(OutputValidationError) as info:
            parse_wire_answers(raw, _ASKED)
        assert marker not in str(info.value)


_KEYS = st.sampled_from(
    ["is_outage", "root_cause", "impact", "noul", "probabilities", "choice", "confidence", "0"]
)
_JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=True, allow_infinity=True)
    | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=5)
        | st.dictionaries(_KEYS | st.text(max_size=6), children, max_size=5)
    ),
    max_leaves=25,
)
_ANSWERS = st.dictionaries(
    st.sampled_from(["is_outage", "root_cause", "impact", "x"]), _JSON, max_size=3
)
_NUMBER = st.floats(allow_nan=True, allow_infinity=True) | st.integers() | st.booleans()
_PROB_KEYS = st.sampled_from(["defect", "config", "other", "0", "1", "2", "3", *_LEVELS])
_PROBS = st.lists(_NUMBER | _JSON, max_size=5) | st.dictionaries(
    _PROB_KEYS, _NUMBER | _JSON, max_size=5
)
_FIELD = st.fixed_dictionaries(
    {},
    optional={
        "noul": _NUMBER | _JSON,
        "choice": _JSON,
        "score": _JSON,
        "legend": _JSON,
        "confidence": _NUMBER | _JSON,
        "probabilities": _PROBS,
    },
)
_STRUCTURED = st.dictionaries(st.sampled_from(["is_outage", "root_cause", "impact"]), _FIELD)


@given(st.one_of(_JSON, _ANSWERS, _STRUCTURED))
def test_pt03_05_parse_raises_only_output_validation_error(raw: Any) -> None:
    """PT03-05 parse never raises anything but OutputValidationError on arbitrary JSON."""
    with contextlib.suppress(OutputValidationError):
        parse_wire_answers(raw, _ASKED)
