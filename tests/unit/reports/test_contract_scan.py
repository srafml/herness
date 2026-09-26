"""Tests for herness.reports.contract.scan_draft_uncited (T09-05; U09-09 via core.numbers)."""

from __future__ import annotations

import re
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.report_drafts import make_draft, paragraph, recommendation

from herness.core.numbers import parse_markers
from herness.core.types import ReportDraft
from herness.reports import contract as c
from herness.reports.settings import ReportsSection

pytestmark = pytest.mark.unit

PATTERNS: Final = ReportsSection().compiled_numeral_patterns


def _with_paragraphs(*texts: str) -> ReportDraft:
    paras = [paragraph(t, numbers=[], finding_ids=[]) for t in texts]
    return make_draft(sections=[{"id": "executive_summary", "title": "S", "paragraphs": paras}])


def test_ut09_06_allowed_patterns_no_hits() -> None:
    """UT09-06 years, dates, quarters and ticket ids are allowed: no hits."""
    draft = _with_paragraphs("In 2025 on 2026-01-02 in Q3 2026 INC0012345 and PAY-123")
    assert c.scan_draft_uncited(draft, PATTERNS) == []


def test_ut09_07_uncited_numerals_with_offsets() -> None:
    """UT09-07 "grew 42% to $1.2M": two hits with offsets in the paragraph."""
    hits = c.scan_draft_uncited(_with_paragraphs("grew 42% to $1.2M"), PATTERNS)
    where = "sections[0].paragraphs[0]"
    assert hits == [c.UncitedHit(where, "42%", 5, 8), c.UncitedHit(where, "$1.2M", 12, 17)]


def test_ut09_08_marker_is_not_a_hit() -> None:
    """UT09-08 "cost [[n1]] rose" with NumberRef n1: no hits."""
    draft = make_draft(sections=[{"id": "executive_summary", "title": "S", "paragraphs": [
        paragraph("cost [[n1]] rose"),
    ]}])  # fmt: skip
    assert c.scan_draft_uncited(draft, PATTERNS) == []


def test_ut09_94_hits_in_title_and_summary_carry_where() -> None:
    """UT09-94 hits in the title and a recommendation summary carry their field path."""
    draft = make_draft(
        title="Top 5 bets",
        recommendations=[recommendation(1, summary="It saves [[n2]] or 300 a year")],
        caveats=["About 12 teams", "none"],
    )
    hits = c.scan_draft_uncited(draft, PATTERNS)
    assert [(h.where, h.text, h.start) for h in hits] == [
        ("title", "5", 4),
        ("recommendations[0].summary", "300", 19),
        ("caveats[0]", "12", 6),
    ]


def test_ut09_94_hit_text_cut_to_uncited_text_max() -> None:
    """UT09-94 a long numeral span is cut to UNCITED_TEXT_MAX; offsets keep the full span."""
    (hit,) = c.scan_draft_uncited(_with_paragraphs("x " + "1" * 120), PATTERNS)
    assert hit.text == "1" * c.UNCITED_TEXT_MAX
    assert (hit.start, hit.end) == (2, 122)


_ALLOWED: Final = ("2025", "1999", "2026-01-02", "Q3 2026", "INC0012345", "CHG77", "PAY-123")
_WORDS: Final = ("cost", "rose", "to", "and", "units")
_numeral = st.integers(0, 99_999).flatmap(
    lambda n: st.sampled_from(
        [str(n), f"{n}.5", f"${n}", f"{n}%", f"{n}M", f"{n},{n % 1000:03d}", f"-{n}"]
    )
)
_token = st.one_of(
    st.sampled_from(_ALLOWED), st.sampled_from(_WORDS), st.sampled_from(["[[n1]]", "[[n12]]"]),
    _numeral,
)  # fmt: skip
_text = st.lists(_token, min_size=1, max_size=30).map(" ".join)


def _spans(pattern: re.Pattern[str], text: str) -> list[tuple[int, int]]:
    return [m.span() for m in pattern.finditer(text) if m.end() > m.start()]


@settings(max_examples=150, deadline=None)
@given(texts=st.lists(_text, min_size=1, max_size=3), title=_text)
def test_pt09_01_hits_cover_uncited_digits(texts: list[str], title: str) -> None:
    """PT09-01 hits lie in U09-04 fields, outside allowed matches and markers, cover every digit."""
    draft = _with_paragraphs(*texts).model_copy(update={"title": title})
    fields = {f.where: f.text for f in c.iter_text_fields(draft)}
    hits = c.scan_draft_uncited(draft, PATTERNS)
    for field_where, text in fields.items():
        markers = [(m.start, m.end) for m in parse_markers(text).markers]
        blanked = list(text)
        for start, end in markers:
            blanked[start:end] = " " * (end - start)
        allowed = [s for p in PATTERNS for s in _spans(p, "".join(blanked))]
        mine = [h for h in hits if h.where == field_where]
        for hit in mine:
            assert 0 <= hit.start < hit.end <= len(text)
            assert not any(a <= hit.start and hit.end <= b for a, b in allowed)
            assert not any(a < hit.end and hit.start < b for a, b in markers)
        for index, char in enumerate(text):
            outside = not any(a <= index < b for a, b in [*markers, *allowed])
            if char.isdigit() and outside:
                assert any(h.start <= index < h.end for h in mine), (field_where, text, index)
    assert {h.where for h in hits} <= set(fields)
