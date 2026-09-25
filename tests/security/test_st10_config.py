"""Security tests for the herness.yaml section models (impl 10 ST10-37, TH10-10)."""

import time
from typing import Any

import pytest
from pydantic import ValidationError

from herness.core.settings import RedactionConfig

pytestmark = pytest.mark.unit

REDOS = "(a+)+$"
LONG = "a" * 600


@pytest.mark.parametrize(
    ("data", "loc"),
    [
        ({"custom_patterns": {"bad": REDOS}}, ("custom_patterns", "bad")),
        ({"custom_patterns": {"bad": LONG}}, ("custom_patterns", "bad")),
        ({"custom_patterns": {"bad": r"(\w*)*x"}}, ("custom_patterns", "bad")),
        ({"custom_patterns": {"bad": r"(a|b+){3,}"}}, ("custom_patterns", "bad")),
        ({"id_patterns": {"USER_ID": [REDOS]}}, ("id_patterns", "USER_ID", 0)),
        ({"national_id_patterns": [LONG]}, ("national_id_patterns", 0)),
    ],
)
def test_st10_37_redos_and_long_patterns_rejected(
    data: dict[str, Any], loc: tuple[Any, ...]
) -> None:
    """ST10-37 nested-quantifier and over-long redaction patterns fail validation by key."""
    started = time.perf_counter()
    with pytest.raises(ValidationError) as info:
        RedactionConfig.model_validate(data)
    # Generous bound: catastrophic backtracking would take far longer; CI jitter will not.
    assert time.perf_counter() - started < 10.0
    errors = info.value.errors(include_input=False)
    assert [tuple(e["loc"]) for e in errors] == [loc]
    assert REDOS not in str(errors)
    assert LONG not in str(errors)
