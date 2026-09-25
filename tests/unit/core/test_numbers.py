"""Tests for herness.core.numbers (U00-64 … U00-70)."""

import datetime
from dataclasses import dataclass

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from herness.core import numbers as nm
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

DEFAULT_PATTERNS = [
    r"\b(19|20)\d{2}\b",
    r"\d{4}-\d{2}-\d{2}",
    r"Q[1-4] \d{4}",
    r"(INC|CHG|PRB)\d+",
    r"[A-Z][A-Z0-9]+-\d+",
]
ALLOWED = nm.compile_allowed_patterns(DEFAULT_PATTERNS)
ALPHABET = set("0123456789,.$%- KMBhmin")


def test_ut00_74_parse_markers() -> None:
    """UT00-74 valid markers keep duplicates; malformed tokens are reported with offsets."""
    scan = nm.parse_markers("a [[n1]] b [[n12]] [[x1]] [[]] [[n 1]] [[n1]]")
    assert scan.markers == (
        nm.Marker("n1", 2, 8),
        nm.Marker("n12", 11, 18),
        nm.Marker("n1", 39, 45),
    )
    assert scan.ids == ("n1", "n12", "n1")
    assert scan.malformed == (
        nm.MalformedMarker("x1", 19, 25),
        nm.MalformedMarker("", 26, 30),
        nm.MalformedMarker("n 1", 31, 38),
    )


def test_ut00_75_compile_allowed_patterns() -> None:
    """UT00-75 five defaults compile in order; invalid lists raise without pattern text."""
    assert [p.pattern for p in ALLOWED] == DEFAULT_PATTERNS
    bad_inputs: list[object] = [[], ["x"] * 51, ["("], ["a" * 201], [5], r"\d{4}"]
    for bad in bad_inputs:
        with pytest.raises(ConfigError) as info:
            nm.compile_allowed_patterns(bad)  # type: ignore[arg-type]
        context = dict(info.value.context)
        assert not set(context) - {"count", "index"}


def test_ut00_76_find_uncited_defaults() -> None:
    """UT00-76 only the uncited numerals are reported; allowed ones and markers are exempt."""
    text = (
        "In Q3 2026 cost rose to [[n1]] from 1,200 on 2026-09-24 for INC0012345 and "
        "PAY-123 (up 12 %) in 2025."
    )
    hits = nm.find_uncited(text, ALLOWED)
    assert [h.text for h in hits] == ["1,200", "12 %"]
    assert hits[0].start == text.index("1,200")
    assert hits[1].start == text.index("12 %")


def test_ut00_77_unicode_and_long_text() -> None:
    """UT00-77 superscripts, fractions, non-ASCII digits and malformed markers are hits."""
    for text, expected in (
        ("up ²", "²"),
        ("about ½", "½"),
        ("٣ tickets", "٣"),
        ("[[12]] items", "12"),
    ):
        hits = nm.find_uncited(text, ALLOWED)
        assert [h.text for h in hits] == [expected]
    long_hits = nm.find_uncited("a" * 100_001, ALLOWED)
    assert long_hits == (nm.NumeralHit(nm.TOO_LONG_TEXT, 100_000, 100_001),)


@dataclass(frozen=True)
class _Ref:
    value: object
    unit: str
    format: str | None


def test_ut00_78_format_value() -> None:
    """UT00-78 every NumberRef format renders deterministically."""
    cases = [
        ("-1250000", "usd", "usd", "-$1,250,000.00"),
        (1840000, "usd", "usd_compact", "$1.84M"),
        (12500, "usd", "usd_compact", "$12.5K"),
        (950, "usd", "usd_compact", "$950"),
        (1204, "count", "int", "1,204"),
        (42, "pct", "pct1", "42.0%"),
        (0.3749, "ratio", "ratio2", "0.37"),
        (3.46, "hours", "hours1", "3.5 h"),
        (45.4, "minutes", "minutes0", "45 min"),
        (0.805, "probability", "prob2", "0.80"),
        (7.0, "score", "plain", "7"),
        (0.12344, "score", "plain", "0.1234"),
    ]
    for value, unit, fmt, expected in cases:
        assert nm.format_value(value, unit, fmt) == expected
    assert nm.format_number(_Ref("1250000.00", "usd", None)) == "$1.25M"


def test_ut00_79_format_edge_cases() -> None:
    """UT00-79 default formats, tier promotion, invalid input, negative zero, no exponent."""
    assert nm.format_value(7.5, "score", None) == "7.5"
    assert nm.format_value(999960, "usd", "usd_compact") == "$1.00M"
    assert nm.format_value(999.6, "usd", "usd_compact") == "$1.0K"
    for value, fmt in (
        (True, "int"),
        ("abc", "int"),
        (float("nan"), "int"),
        (None, "int"),
        (5, "pct3"),
    ):
        assert nm.format_value(value, "count", fmt) == nm.NOT_AVAILABLE
    assert nm.format_value(-0.001, "count", "int") == "0"
    assert nm.format_value(1e22, "score", "plain") == "10,000,000,000,000,000,000,000"


_values = st.one_of(
    st.decimals(min_value=-(10**15), max_value=10**15, allow_nan=False, allow_infinity=False),
    st.integers(min_value=-(10**15), max_value=10**15),
    st.floats(min_value=-1e15, max_value=1e15, allow_nan=False, allow_infinity=False),
)


@given(
    _values,
    st.sampled_from(sorted(nm.NUMBER_FORMATS)),
    st.sampled_from(["usd", "pct", "count", "hours", "minutes", "ratio", "score"]),
)
def test_pt00_06_format_alphabet(value: object, fmt: str, unit: str) -> None:
    """PT00-06 output is n/a or uses only the documented alphabet, with no exponent."""
    out = nm.format_value(value, unit, fmt)
    assert out == nm.format_value(value, unit, fmt)
    if out != nm.NOT_AVAILABLE:
        assert set(out) <= ALPHABET
        assert "e" not in out.lower()


_word = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=3, max_size=8)
_year = st.integers(min_value=1900, max_value=2099).map(str)
_date = st.dates(min_value=datetime.date(1900, 1, 1)).map(lambda d: d.isoformat())
_marker = st.integers(min_value=1, max_value=999).map(lambda n: f"[[n{n}]]")


@given(
    st.lists(st.one_of(_word, _year, _date, _marker), min_size=1, max_size=30),
    st.integers(min_value=0, max_value=99_999),
    st.data(),
)
def test_pt00_07_single_inserted_numeral(
    words: list[str], number: int, data: st.DataObject
) -> None:
    """PT00-07 clean text has no hit; one inserted integer gives exactly one hit."""
    assume(not 1900 <= number <= 2099)
    assert nm.find_uncited(" ".join(words), ALLOWED) == ()
    position = data.draw(st.integers(min_value=0, max_value=len(words)))
    new_words = [*words[:position], str(number), *words[position:]]
    text = " ".join(new_words)
    hits = nm.find_uncited(text, ALLOWED)
    assert [h.text for h in hits] == [str(number)]
    assert hits[0].start == len(" ".join(new_words[:position])) + (1 if position else 0)


def test_ut00_79_overflowing_values_are_not_available() -> None:
    """UT00-79 overflowing exponents and oversized ints give n/a for every format, never raise."""
    for value in ("1e9999999", "-1e9999999", "1e999999999999999999", 10**5000):
        for fmt in sorted(nm.NUMBER_FORMATS):
            assert nm.format_value(value, "usd", fmt) == nm.NOT_AVAILABLE


def test_st00_18_evasions_are_caught() -> None:
    """ST00-18 each evasion text yields a hit, never inside a valid marker."""
    texts = [
        "cost [[1,250]] USD",
        "１２ tickets",  # noqa: RUF001 - fullwidth digits are the ST00-18 evasion under test
        "³ outages",
        "Ⅻ teams",
        "[[n1]]12 more",
        "1\u200b200 users",
        "INC 42",
        "x" * 100_010 + "7" + "y" * 39,
    ]
    for text in texts:
        hits = nm.find_uncited(text, ALLOWED)
        assert hits, text[:40]
        markers = nm.parse_markers(text[: nm.MAX_SCAN_CHARS]).markers
        for hit in hits:
            assert not any(m.start <= hit.start < m.end for m in markers)
    assert nm.find_uncited(texts[-1], ALLOWED)[-1].text == nm.TOO_LONG_TEXT


def test_rf_display_strings_are_not_numbers() -> None:
    """RF-1 display strings passed as values format as n/a, never as a number."""
    for value in ("1,250", "$5", "12%", ""):
        assert nm.format_value(value, "usd", "usd") == nm.NOT_AVAILABLE


def test_rf_hits_keep_sign_and_symbol() -> None:
    """RF-4 the hit text keeps the currency sign, minus sign and attached percent."""
    hits = nm.find_uncited("pay $1,200 or -5 now, 12% more", ALLOWED)
    assert [h.text for h in hits] == ["$1,200", "-5", "12%"]
