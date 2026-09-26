"""Tests for herness.harness.verifier (U05-66): compare_value, canonical_cell_text, row_matches."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.harness import verifier as v

pytestmark = pytest.mark.unit

REL_TOL = 0.005


# --- compare_value: int exact -----------------------------------------------------------


@pytest.mark.parametrize(
    ("claimed", "actual", "duckdb_type", "expected"),
    [
        (5, 5, "INTEGER", True),
        (5, 6, "INTEGER", False),
        ("5", 5, "BIGINT", True),
        (0, 0, "TINYINT", True),
        (-3, -3, "HUGEINT", True),
    ],
    ids=["match", "mismatch", "str_claim", "zero", "negative"],
)
def test_ut05_116_compare_value_int_exact(
    claimed: float | int | str, actual: object, duckdb_type: str, expected: bool
) -> None:
    """UT05-116 integer duckdb types compare with exact Decimal equality."""
    assert (
        v.compare_value(claimed, actual, unit="", duckdb_type=duckdb_type, rel_tol=REL_TOL)
        is expected
    )


@pytest.mark.parametrize(
    ("claimed", "actual", "unit", "expected"),
    [
        (5, 5, "count", True),
        (2, 3, "count", False),
        (1, 1, "rank", True),
    ],
    ids=["count_match", "count_mismatch", "rank_match"],
)
def test_ut05_116_compare_value_count_like_units(
    claimed: float | int | str, actual: object, unit: str, expected: bool
) -> None:
    """UT05-116 count/rank units use exact Decimal equality regardless of duckdb_type."""
    assert (
        v.compare_value(claimed, actual, unit=unit, duckdb_type="DOUBLE", rel_tol=REL_TOL)
        is expected
    )


# --- compare_value: usd / DECIMAL half-even ---------------------------------------------


@pytest.mark.parametrize(
    ("claimed", "actual", "expected"),
    [
        ("0.12", 0.125, True),  # tie rounds to even digit 2
        ("0.13", 0.125, False),
        ("0.14", 0.135, True),  # tie rounds to even digit 4
        ("0.13", 0.135, False),
        (100, 100.0, True),
        (100, Decimal("100.00"), True),
    ],
    ids=[
        "tie_down_to_even",
        "tie_down_wrong",
        "tie_up_to_even",
        "tie_up_wrong",
        "whole_dollar_float",
        "whole_dollar_decimal",
    ],
)
def test_ut05_116_compare_value_usd_half_even(
    claimed: float | int | str, actual: object, expected: bool
) -> None:
    """UT05-116 usd unit / DECIMAL duckdb_type quantizes actual with ROUND_HALF_EVEN."""
    assert (
        v.compare_value(claimed, actual, unit="usd", duckdb_type="DECIMAL(18,2)", rel_tol=REL_TOL)
        is expected
    )


def test_ut05_116_compare_value_decimal_type_without_usd_unit() -> None:
    """UT05-116 a DECIMAL duckdb_type takes the half-even path even for a non-usd unit."""
    assert (
        v.compare_value("1.5", 1.5, unit="ratio", duckdb_type="DECIMAL(10,1)", rel_tol=REL_TOL)
        is True
    )


# --- compare_value: DOUBLE rounding and 0.5% tolerance ----------------------------------


@pytest.mark.parametrize(
    ("claimed", "actual", "expected"),
    [
        (3.14, 3.14159, True),  # quantize match at claimed's precision
        (100.0, 100.4, True),  # within 0.5% relative tolerance
        (100.0, 101.0, False),  # outside quantize and tolerance
        (0.0, 1e-10, True),  # both below the 1e-9 floor
        (1e-10, 0.0, True),
    ],
    ids=[
        "quantize_match",
        "within_tolerance",
        "outside_both",
        "tiny_both",
        "tiny_claim_zero_actual",
    ],
)
def test_ut05_116_compare_value_double_rounding_and_tolerance(
    claimed: float | int | str, actual: object, expected: bool
) -> None:
    """UT05-116 DOUBLE/FLOAT/REAL compares by quantize-match, relative tolerance, or tiny floor."""
    assert (
        v.compare_value(claimed, actual, unit="ratio", duckdb_type="DOUBLE", rel_tol=REL_TOL)
        is expected
    )


def test_ut05_116_compare_value_int_claim_on_double() -> None:
    """UT05-116 an int claimed value on a DOUBLE column still compares correctly."""
    assert v.compare_value(3, 3.0, unit="ratio", duckdb_type="DOUBLE", rel_tol=REL_TOL) is True


# --- compare_value: NULL / non-numeric actual mismatch ----------------------------------


@pytest.mark.parametrize(
    "actual",
    [None, True, False, "5", date(2024, 1, 1), datetime(2024, 1, 1, tzinfo=UTC), object()],
    ids=["none", "true", "false", "str", "date", "datetime", "other_object"],
)
def test_ut05_116_compare_value_null_and_non_numeric_mismatch(actual: object) -> None:
    """UT05-116 NULL and non-numeric actual values never match (rule 1-2)."""
    assert v.compare_value(5, actual, unit="count", duckdb_type="INTEGER", rel_tol=REL_TOL) is False


def test_ut05_116_compare_value_invalid_claim_text_is_false() -> None:
    """UT05-116 unparseable claimed text compares as False rather than raising."""
    assert (
        v.compare_value("not-a-number", 5, unit="count", duckdb_type="INTEGER", rel_tol=REL_TOL)
        is False
    )


# --- canonical_cell_text -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (False, False),
        (5, 5),
        (5.5, 5.5),
        (Decimal("1.50"), Decimal("1.50")),
        (date(2024, 3, 4), "2024-03-04"),
        ("hello", "hello"),
    ],
    ids=["none", "true", "false", "int", "float", "decimal", "date", "other_str"],
)
def test_ut05_116_canonical_cell_text_scalars(value: object, expected: object) -> None:
    """UT05-116 canonical_cell_text renders scalars per U05-66's algorithm table."""
    assert v.canonical_cell_text(value) == expected


def test_ut05_116_canonical_cell_text_datetime_naive_treated_as_utc() -> None:
    """UT05-116 a naive datetime is treated as UTC and rendered to the second."""
    naive = datetime(2024, 3, 4, 10, 30, 15)  # noqa: DTZ001 -- exercises the naive-datetime path
    assert v.canonical_cell_text(naive) == "2024-03-04T10:30:15Z"


def test_ut05_116_canonical_cell_text_datetime_aware_converts_to_utc() -> None:
    """UT05-116 an aware datetime is converted to UTC before rendering."""
    tz = timezone(timedelta(hours=-5))
    value = datetime(2024, 3, 4, 5, 30, 15, tzinfo=tz)
    assert v.canonical_cell_text(value) == "2024-03-04T10:30:15Z"


def test_ut05_116_canonical_cell_text_datetime_drops_microseconds_form() -> None:
    """UT05-116 canonical_cell_text always renders to the second, dropping microseconds."""
    value = datetime(2024, 3, 4, 10, 30, 15, 123456, tzinfo=UTC)
    assert v.canonical_cell_text(value) == "2024-03-04T10:30:15Z"


# --- row_matches: date keys and general key/cell comparisons ----------------------------


def test_ut05_116_row_matches_all_keys_present_and_equal() -> None:
    """UT05-116 row_matches is True only when every row_key column matches."""
    row = {"id": 5, "name": "bob", "active": True}
    assert v.row_matches(row, {"id": 5, "name": "bob"}) is True
    assert v.row_matches(row, {"id": 5, "name": "carol"}) is False


def test_ut05_116_row_matches_missing_column_is_false() -> None:
    """UT05-116 a row_key column absent from the row never matches."""
    assert v.row_matches({"id": 5}, {"missing": 1}) is False


def test_ut05_116_row_matches_none_key() -> None:
    """UT05-116 a None key matches only a None cell."""
    assert v.row_matches({"x": None}, {"x": None}) is True
    assert v.row_matches({"x": 0}, {"x": None}) is False


def test_ut05_116_row_matches_bool_key() -> None:
    """UT05-116 a bool key matches only an equal bool cell, never a numeric one."""
    assert v.row_matches({"x": True}, {"x": True}) is True
    assert v.row_matches({"x": False}, {"x": True}) is False
    assert v.row_matches({"x": 1}, {"x": True}) is False


@pytest.mark.parametrize(
    ("cell", "expected"),
    [(5, True), (5.0, True), (6, False), ("5", True), (True, False), (None, False)],
    ids=["int_eq", "float_eq", "int_ne", "str_eq", "bool_cell", "none_cell"],
)
def test_ut05_116_row_matches_numeric_key(cell: object, expected: bool) -> None:
    """UT05-116 a numeric key compares by Decimal value against numeric or string cells."""
    assert v.row_matches({"x": cell}, {"x": 5}) is expected


@pytest.mark.parametrize(
    ("key", "cell", "expected"),
    [
        ("5", 5, True),
        ("5", 5.0, True),
        ("5.5", 5.5, True),
        ("abc", 5, False),
        ("5", "5", True),
        ("foo", "foo", True),
        ("foo", "bar", False),
    ],
    ids=[
        "str_int_eq",
        "str_float_eq",
        "str_decimal_eq",
        "unparseable_vs_numeric",
        "str_str_eq",
        "eq",
        "ne",
    ],
)
def test_ut05_116_row_matches_string_key(key: str, cell: object, expected: bool) -> None:
    """UT05-116 a string key parses as a number against a numeric cell, else compares as text."""
    assert v.row_matches({"x": cell}, {"x": key}) is expected


def test_ut05_116_row_matches_numeric_key_signaling_nan_cell_is_false() -> None:
    """UT05-116 a signaling-NaN Decimal cell raises InvalidOperation, caught as no match."""
    assert v.row_matches({"x": Decimal("sNaN")}, {"x": 5}) is False


def test_ut05_116_row_matches_date_key() -> None:
    """UT05-116 a string key in ISO date form matches a date cell via canonical_cell_text."""
    assert v.row_matches({"created": date(2024, 1, 15)}, {"created": "2024-01-15"}) is True
    assert v.row_matches({"created": date(2024, 1, 15)}, {"created": "2024-01-16"}) is False


def test_ut05_116_row_matches_datetime_key_seconds_form() -> None:
    """UT05-116 a string key in the seconds form matches a datetime cell."""
    cell = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
    assert v.row_matches({"ts": cell}, {"ts": "2024-01-15T10:30:00Z"}) is True


def test_ut05_116_row_matches_datetime_key_isoformat_z_form() -> None:
    """UT05-116 a string key equal to isoformat with +00:00 replaced by Z also matches."""
    cell = datetime(2024, 1, 15, 10, 30, 0, 123456, tzinfo=UTC)
    assert v.row_matches({"ts": cell}, {"ts": "2024-01-15T10:30:00.123456Z"}) is True
    # the seconds form drops microseconds, so it still matches this same-second key
    assert v.row_matches({"ts": cell}, {"ts": "2024-01-15T10:30:00Z"}) is True
    assert v.row_matches({"ts": cell}, {"ts": "2024-01-15T10:31:00Z"}) is False


# --- PT05-04: for any finite actual and d in 0..6, compare_value(round(actual, d), actual) ---


def _round_half_even(value: float, places: int) -> Decimal:
    quantum = Decimal(1).scaleb(-places)
    return Decimal(repr(value)).quantize(quantum, rounding=ROUND_HALF_EVEN)


_finite_actuals = st.floats(
    min_value=-1_000_000.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False
)
_places = st.integers(min_value=0, max_value=6)


@given(actual=_finite_actuals, places=_places)
def test_pt05_04_double_matches_its_own_half_even_rounding(actual: float, places: int) -> None:
    """PT05-04 compare_value(round_half_even(actual, d), actual) is True for DOUBLE."""
    claimed = str(_round_half_even(actual, places))
    assert (
        v.compare_value(claimed, actual, unit="ratio", duckdb_type="DOUBLE", rel_tol=REL_TOL)
        is True
    )


@given(actual=_finite_actuals, places=_places)
def test_pt05_04_decimal_matches_its_own_half_even_rounding(actual: float, places: int) -> None:
    """PT05-04 compare_value(round_half_even(actual, d), actual) is True for DECIMAL."""
    claimed = str(_round_half_even(actual, places))
    assert (
        v.compare_value(
            claimed, actual, unit="ratio", duckdb_type="DECIMAL(38,10)", rel_tol=REL_TOL
        )
        is True
    )
