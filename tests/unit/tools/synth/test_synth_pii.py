"""Tests for tools.synth.pii (U11-06): UT11-08 and PT11-02."""

import itertools
import re

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tools.synth.params import SynthUsageError
from tools.synth.pii import PiiSpan, build_name_list, inject_pii, luhn_valid

pytestmark = pytest.mark.unit

_NAMES = build_name_list(42)
_NAME = r"[A-Z][a-z]+"
_PATTERNS = {
    "PERSON": re.compile(rf"{_NAME} {_NAME}|{_NAME}, {_NAME}|[A-Z]\. {_NAME}"),
    "EMAIL": re.compile(r"[a-z]+\.[a-z]+@example\.(com|org)"),
    "PHONE": re.compile(r"\+1-202-555-01\d\d|\+1 202 555 01\d\d|\(202\) 555-01\d\d"),
    "IP": re.compile(r"(192\.0\.2|198\.51\.100)\.(\d{1,3})"),
    "EMPLOYEE_ID": re.compile(r"E\d{6}"),
    "CARD": re.compile(r"4111\d{12}"),
    "CREDENTIAL": re.compile(r"password=synthetic[A-Za-z0-9]{8}"),
    "URL_TOKEN": re.compile(r"https://portal\.example\.com/x\?token=synthetic[0-9a-f]{16}"),
}
_BASE = "Users report slow pages. The team restarted the pool. Service is back."


def _check_spans(text: str, spans: list[PiiSpan], field: str) -> None:
    ordered = sorted(spans, key=lambda span: span.start)
    for left, right in itertools.pairwise(ordered):
        assert left.end <= right.start
    for span in spans:
        assert span.field == field
        assert 0 <= span.start < span.end <= len(text)
        value = text[span.start : span.end]
        match = _PATTERNS[span.type].fullmatch(value)
        assert match is not None, (span.type, value)
        if span.type == "IP":
            assert 0 <= int(match.group(2)) <= 255
        if span.type == "CARD":
            assert luhn_valid(value)
        if span.type == "PERSON":
            assert any(first in value and last in value for first, last in _NAMES) or any(
                value == f"{first[0]}. {last}" for first, last in _NAMES
            )


def test_ut11_08_spans_index_reserved_values() -> None:
    """UT11-08 1,000 texts with 1-3 spans: exact spans, reserved ranges, Luhn cards."""
    rng = np.random.default_rng(1108)
    seen: set[str] = set()
    tickets = 0
    for i in range(1000):
        n_spans = 1 + i % 3
        text, spans = inject_pii(_BASE, "description", rng, _NAMES, n_spans=n_spans)
        assert len(spans) == n_spans
        _check_spans(text, spans, "description")
        seen.update(span.type for span in spans)
        tickets += bool(re.search(r"\b(INC|CHG)\d{7}\b", text))
    assert seen == set(_PATTERNS)
    assert 400 <= tickets <= 600


def test_ut11_08_ticket_number_is_not_masked() -> None:
    """UT11-08 INC/CHG numbers sit next to a span and are never inside one."""
    rng = np.random.default_rng(4)
    for _ in range(200):
        text, spans = inject_pii(_BASE, "description", rng, _NAMES, n_spans=2)
        for match in re.finditer(r"(INC|CHG)\d{7}", text):
            assert all(match.end() <= s.start or match.start() >= s.end for s in spans)
            assert any(match.start() == s.end + 1 for s in spans)


def test_ut11_08_luhn_and_name_list() -> None:
    """UT11-08 luhn_valid; 500 distinct made-up names, stable per seed."""
    assert luhn_valid("4111111111111111")
    assert not luhn_valid("4111111111111112")
    assert not luhn_valid("")
    assert not luhn_valid("4111-1111")
    assert len(_NAMES) == len(set(_NAMES)) == 500
    assert build_name_list(42) == _NAMES
    assert build_name_list(43) != _NAMES


def test_ut11_08_n_spans_out_of_range_fails() -> None:
    """UT11-08 n_spans outside 1..3 or an empty name list is a usage error."""
    rng = np.random.default_rng(0)
    for bad in (0, 4):
        with pytest.raises(SynthUsageError):
            inject_pii(_BASE, "description", rng, _NAMES, n_spans=bad)
    with pytest.raises(SynthUsageError):
        inject_pii(_BASE, "description", rng, (), n_spans=1)


@settings(max_examples=200, deadline=None)
@given(
    text=st.text(st.characters(min_codepoint=32, max_codepoint=126), max_size=2000),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
    n_spans=st.integers(min_value=1, max_value=3),
)
def test_pt11_02_spans_index_values_and_never_overlap(text: str, seed: int, n_spans: int) -> None:
    """PT11-02 for any printable base text and seed, spans index values and never overlap."""
    out, spans = inject_pii(text, "summary", np.random.default_rng(seed), _NAMES, n_spans=n_spans)
    assert len(spans) == n_spans
    _check_spans(out, spans, "summary")
