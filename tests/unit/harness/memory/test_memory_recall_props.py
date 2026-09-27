"""Property tests for herness.harness.memory.recall.score_candidate (impl 07 U07-59): PT07-06."""

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.harness.memory.recall import score_candidate
from herness.harness.memory.settings import RecallWeights

pytestmark = pytest.mark.unit

_real = st.floats(min_value=-10.0, max_value=10.0, allow_nan=False)
_unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
_floor = st.floats(min_value=1e-6, max_value=1.0, allow_nan=False)


@st.composite
def _weights(draw: st.DrawFn) -> RecallWeights:
    a, b = sorted((draw(_unit), draw(_unit)))
    return RecallWeights(sim=a, kw=b - a, ent=1.0 - b)


@st.composite
def _args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "sim": draw(_real),
        "kw_raw": draw(st.none() | _real),
        "max_kw": draw(st.floats(min_value=0.0, max_value=50.0, allow_nan=False)),
        "ent": draw(st.sampled_from([0.0, 0.5, 1.0]) | _real),
        "age_days": draw(st.floats(min_value=-100.0, max_value=1e6, allow_nan=False)),
        "half_life_days": draw(st.floats(min_value=0.01, max_value=3650.0, allow_nan=False)),
        "is_pending": draw(st.booleans()),
        "weights": draw(_weights()),
        "conf_floor": draw(_floor),
        "rec_floor": draw(_floor),
        "degraded": draw(st.booleans()),
    }


@given(_args(), _unit)
def test_pt07_06_components_in_unit_interval(args: dict[str, Any], confidence: float) -> None:
    """PT07-06 every component of score_candidate lies in [0, 1]."""
    out = score_candidate(confidence=confidence, **args)
    assert set(out) == {"sim", "kw", "ent", "rec", "conf", "final"}
    for name, value in out.items():
        assert 0.0 <= value <= 1.0, name


@given(_args(), _unit, _unit)
def test_pt07_06_final_non_decreasing_in_confidence(
    args: dict[str, Any], c1: float, c2: float
) -> None:
    """PT07-06 final is non-decreasing in confidence, all else equal."""
    low, high = sorted((c1, c2))
    assert (
        score_candidate(confidence=low, **args)["final"]
        <= score_candidate(confidence=high, **args)["final"]
    )
