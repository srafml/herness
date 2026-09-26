"""Tests for herness.reports._format: table-cell formatting and confidence labels (T09-06)."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core import numbers as nm
from herness.reports import _format as f

pytestmark = pytest.mark.unit

_VALUES = (Decimal("1234.5678"), 0, -42, 0.25, Decimal("-0.005"), 1_000_000, Decimal("7"))


@pytest.mark.parametrize("fmt", sorted(nm.NUMBER_FORMATS))
def test_ut09_19_every_format_matches_core_formatter(fmt: str) -> None:
    """UT09-19 each format prints exactly what herness.core.numbers prints for the Decimal."""
    for value in _VALUES:
        expected = nm.format_value(Decimal(str(value)), "", fmt)
        assert f.format_value(value, fmt) == expected
        assert expected != nm.NOT_AVAILABLE


def test_ut09_19_none_is_not_linked_dash() -> None:
    """UT09-19 format_value(None, "usd") is an em dash."""
    assert f.format_value(None, "usd") == "\u2014"


def test_ut09_19_unknown_format_is_delegated() -> None:
    """UT09-19 an unknown format is passed through to the core formatter unchanged."""
    assert f.format_value(Decimal(5), "bogus") == nm.format_value(Decimal(5), "", "bogus")


def test_ut09_20_non_finite_and_float_conversion() -> None:
    """UT09-20 nan, inf -> n/a; 0.1 float goes through Decimal("0.1"); 1e6 as Decimal("1E+6")."""
    assert f.format_value(float("nan"), "usd") == "n/a"
    assert f.format_value(float("inf"), "int") == "n/a"
    assert f.format_value(float("-inf"), "plain") == "n/a"
    assert f.format_value(Decimal("NaN"), "plain") == "n/a"
    for fmt in sorted(nm.NUMBER_FORMATS):
        assert f.format_value(0.1, fmt) == nm.format_value(Decimal("0.1"), "", fmt)
        assert f.format_value(Decimal("1e6"), fmt) == nm.format_value(Decimal("1E+6"), "", fmt)


def test_ut09_20_unconvertible_value_is_not_available() -> None:
    """UT09-20 a value Decimal(str(value)) cannot convert gives n/a."""
    assert f.format_value(10**5000, "int") == "n/a"
    assert f.format_value(True, "int") == "n/a"


@pytest.mark.parametrize(
    ("confidence", "label"),
    [
        (0.7, "high"),
        (0.6999, "medium"),
        (0.4, "medium"),
        (0.39, "low"),
        (None, "unknown"),
        (Decimal("0.7"), "high"),
        (Decimal("0.4"), "medium"),
        (-1.0, "low"),
        (float("nan"), "low"),
        (float("inf"), "high"),
    ],
)
def test_ut09_21_confidence_label(confidence: float | Decimal | None, label: str) -> None:
    """UT09-21 >= 0.7 high, >= 0.4 medium, other numbers low, None unknown."""
    assert f.confidence_label(confidence) == label


_ANY_VALUE = st.one_of(
    st.none(),
    st.floats(),
    st.decimals(),
    st.integers(),
    st.integers(min_value=10**4300, max_value=10**4400),
)


@given(_ANY_VALUE, st.one_of(st.sampled_from(sorted(nm.NUMBER_FORMATS)), st.text(max_size=12)))
def test_pt09_04_format_value_never_raises_and_is_deterministic(value: object, fmt: str) -> None:
    """PT09-04 any value (non-finite and None included) and any format: no exception, stable."""
    out = f.format_value(value, fmt)  # type: ignore[arg-type]
    assert isinstance(out, str)
    assert out == f.format_value(value, fmt)  # type: ignore[arg-type]
