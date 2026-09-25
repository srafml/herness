"""Tests for herness.core.types.decisions (U03-01 ... U03-08, T03-01).

Deviations from the verbatim test rows (see the T03-01 report for detail):
- UT03-02 and UT03-03 exercise `load_question_set` (U03-16, T03-03) wrapping a
  pydantic `ValidationError` into `ConfigError`. `load_question_set` does not exist
  yet (T03-01 depends only on T00-03/T00-08), so these tests exercise `Question`
  itself and assert the underlying `ValidationError`.
- UT03-05 exercises `build_inputs`/`pair_inputs` (U03-26/U03-27, T03-05), which do
  not exist yet either; this test instead exercises the `content_hash` invariant
  directly (first 32 hex chars of `herness.core.ids.sha256_hex(text)`, U03-26), as a
  construction site must.
- UT03-06's literal "1.002 accepted" contradicts the unit's own postcondition
  (`abs(sum - 1) <= 1e-3`), TH03-06 ("distributions sum to 1 +/- 1e-3") and PT03-01
  ("any perturbation beyond 1e-3 is rejected"), all consistent with a 1e-3 tolerance.
  The accepted case here uses a sum within 1e-3 instead of the literal 1.002.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from pydantic import ValidationError

from herness.core.errors import ConfigError
from herness.core.ids import sha256_hex
from herness.core.types import decisions as d

pytestmark = pytest.mark.unit


def _question(**overrides: object) -> d.Question:
    fields: dict[str, object] = {
        "id": "software_defect",
        "type": "choice",
        "instructions": "Classify the root cause of this ticket.",
        "options": {"defect": "A software defect.", "other": "Anything else."},
        "threshold": 0.9,
    }
    fields.update(overrides)
    return d.Question(**fields)  # type: ignore[arg-type]


def test_ut03_01_closed_sets_reject_unknown_values() -> None:
    """UT03-01 a Question with type "text" is rejected; QuestionType is a closed set."""
    with pytest.raises(ValidationError):
        _question(type="text")
    with pytest.raises(ValidationError):
        _question(applies_to=("planet",))


@pytest.mark.parametrize(
    "overrides",
    [
        {"options": {"a": "one"}},
        {"options": {f"o{i}": "description" for i in range(256)}},
        {"type": "score", "levels": ("a", "b", "c")},
        {"type": "bool", "levels": ("a", "b", "c", "d"), "options": None},
    ],
    ids=["one-option", "256-options", "three-levels", "levels-on-bool"],
)
def test_ut03_02_question_shape_rules(overrides: dict[str, object]) -> None:
    """UT03-02 (adapted: `load_question_set`/U03-16 not yet in the tree) bad question
    shapes raise `ValidationError` directly from `Question`."""
    with pytest.raises(ValidationError):
        _question(**overrides)


@pytest.mark.parametrize("word", ["true", "No", "yes", "FALSE"])
def test_ut03_03_bool_word_option_labels_rejected(word: str) -> None:
    """UT03-03 (adapted, see module docstring) bool-word option keys raise ValidationError."""
    with pytest.raises(ValidationError):
        _question(options={word: "a description", "other": "another description"})


def test_ut03_04_question_set_get_and_for_entity() -> None:
    """UT03-04 get, unknown get, for_entity("incident") and for_entity("change")."""
    q1 = _question(id="q1", applies_to=("incident",))
    q2 = _question(id="q2", applies_to=("incident", "problem"))
    q3 = _question(id="q3", applies_to=("problem",))
    qs = d.QuestionSet(version="qs-2026-01-01", questions=(q1, q2, q3))

    assert qs.get("q2") is q2
    with pytest.raises(ConfigError):
        qs.get("missing")

    incident_set = qs.for_entity("incident")
    assert incident_set.questions == (q1, q2)
    assert incident_set.version == qs.version

    change_set = qs.for_entity("change")
    assert change_set.questions == ()


def test_ut03_04_question_set_limits() -> None:
    """Supporting U03-03: duplicate ids and more than 64 questions both raise."""
    q1 = _question(id="dup")
    q2 = _question(id="dup")
    with pytest.raises(ValidationError):
        d.QuestionSet(version="qs-2026-01-01", questions=(q1, q2))

    many = tuple(_question(id=f"q{i}") for i in range(65))
    with pytest.raises(ValidationError):
        d.QuestionSet(version="qs-2026-01-01", questions=many)


def test_ut03_05_content_hash_matches_text() -> None:
    """UT03-05 (adapted, see module docstring) content_hash == sha256_hex(text) for
    inputs built the way a construction site (T03-05) must."""
    texts = ["a redacted ticket body", "incident text | change text", "x" * 12_000]
    for index, text in enumerate(texts):
        content_hash = sha256_hex(text)[:32]
        di = d.DecisionInput(
            record_id=f"src:incident:{index}",
            entity="incident",
            content_hash=content_hash,
            text=text,
        )
        assert di.content_hash == sha256_hex(di.text)[:32]


def test_ut03_06_answer_distribution_rules() -> None:
    """UT03-06 (see module docstring for the 1.002 deviation): sum 0.99 and a value of
    1.1 and a missing answer key all raise; a sum within 1e-3 of 1 is accepted."""
    with pytest.raises(ValidationError):
        d.Answer(answer="a", probability=0.5, distribution={"a": 0.49, "b": 0.5})
    with pytest.raises(ValidationError):
        d.Answer(answer="a", probability=1.1, distribution={"a": 1.1, "b": -0.1})
    with pytest.raises(ValidationError):
        d.Answer(answer="c", probability=0.5, distribution={"a": 0.5, "b": 0.5})

    accepted = d.Answer(answer="a", probability=0.5005, distribution={"a": 0.5005, "b": 0.5})
    assert accepted.answer == "a"


def test_ut03_06_answer_probability_mismatch() -> None:
    """Supporting U03-07: probability must match distribution[answer] within 1e-6."""
    with pytest.raises(ValidationError):
        d.Answer(answer="a", probability=0.4, distribution={"a": 0.5, "b": 0.5})


def test_ut03_06_answer_out_of_range_values() -> None:
    """Supporting U03-07: an out-of-range or non-finite distribution value raises,
    independent of the probability field's own bounds."""
    with pytest.raises(ValidationError):
        d.Answer(answer="a", probability=0.5, distribution={"a": 0.5, "b": 1.5})
    with pytest.raises(ValidationError):
        d.Answer(answer="a", probability=0.5, distribution={"a": 0.5, "b": float("nan")})


def test_ut03_07_decision_output_error_rules() -> None:
    """UT03-07 error with non-empty answers, and a malformed error, both raise."""
    answer = d.Answer(answer="a", probability=1.0, distribution={"a": 1.0})
    with pytest.raises(ValidationError):
        d.DecisionOutput(
            record_id="r1",
            content_hash="0" * 32,
            decider="llm",
            decider_version="1",
            answers={"q1": answer},
            error="SomeError",
        )
    with pytest.raises(ValidationError):
        d.DecisionOutput(
            record_id="r1",
            content_hash="0" * 32,
            decider="llm",
            decider_version="1",
            answers={},
            error="bad message!",
        )
    ok = d.DecisionOutput(
        record_id="r1",
        content_hash="0" * 32,
        decider="llm",
        decider_version="1",
        answers={},
        error="ConfigError",
    )
    assert ok.error == "ConfigError"
    no_error = d.DecisionOutput(
        record_id="r1",
        content_hash="0" * 32,
        decider="llm",
        decider_version="1",
        answers={"q1": answer},
    )
    assert no_error.error is None


_SUM_TOL = 1e-3
_VALUE = st.floats(min_value=0.01, max_value=0.99, allow_nan=False, allow_infinity=False)
_DELTA = st.floats(min_value=2e-3, max_value=0.3, allow_nan=False, allow_infinity=False)


@given(_VALUE, _DELTA)
def test_pt03_01_distribution_sum_tolerance(value_a: float, delta: float) -> None:
    """PT03-01 any generated valid distribution constructs; any perturbation of the
    sum beyond 1e-3 is rejected."""
    value_b = 1.0 - value_a
    distribution = {"a": value_a, "b": value_b}
    assert d.Answer(answer="a", probability=value_a, distribution=distribution).answer == "a"

    assume(value_a + delta <= 1.0)
    assume(abs((value_a + delta) + value_b - 1.0) > _SUM_TOL)
    perturbed = {"a": value_a + delta, "b": value_b}
    with pytest.raises(ValidationError):
        d.Answer(answer="a", probability=perturbed["a"], distribution=perturbed)


def test_question_dynamic_options_and_descriptions() -> None:
    """Supporting U03-02: dynamic option sources, description bounds and fingerprint."""
    dynamic_none = _question(options_source="core.team", options=None)
    assert dynamic_none.options is None
    dynamic_ok = _question(
        options_source="core.service", options={"a": "one", "b": "two", "c": "three"}
    )
    assert dynamic_ok.options is not None
    with pytest.raises(ValidationError):
        _question(options_source="core.team", options={"a": "only one"})

    with pytest.raises(ValidationError):
        _question(type="bool", options={"a": "x"}, options_source="static")
    with pytest.raises(ValidationError):
        _question(type="bool", options=None, options_source="core.team")

    with pytest.raises(ValidationError):
        _question(options={"a": ""})
    with pytest.raises(ValidationError):
        _question(options={"a": "y" * 501})

    with pytest.raises(ValidationError):
        _question(type="score", levels=None, options=None, options_source="static")
    with pytest.raises(ValidationError):
        _question(
            type="score",
            levels=("", "b", "c", "d"),
            options=None,
            options_source="static",
        )
    scored = _question(
        type="score",
        levels=("low", "mid", "high", "top"),
        options=None,
        options_source="static",
    )
    assert scored.levels == ("low", "mid", "high", "top")

    with pytest.raises(ValidationError):
        _question(applies_to=())
    with pytest.raises(ValidationError):
        _question(applies_to=("incident", "incident"))

    with pytest.raises(ValidationError):
        _question(fingerprint="not-hex")
    fingerprinted = _question(fingerprint="0123456789abcdef")
    assert fingerprinted.fingerprint == "0123456789abcdef"
