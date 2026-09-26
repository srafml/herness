"""Tests for herness.reports._markup: segmenting and escaping model text (T09-07)."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import markupsafe
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from herness.core import errors as e
from herness.core.types import NumberRef
from herness.reports import _markup as mk
from herness.reports.contract import UncitedHit

pytestmark = pytest.mark.unit

Q1 = "q_0123456789abcdef"


def _ref(ref_id: str = "n1", value: object = 3, unit: str = "count", **kw: object) -> NumberRef:
    return NumberRef.model_validate(
        {"id": ref_id, "value": value, "unit": unit, "query_id": Q1, "column": "n",
         "row_key": None} | kw
    )  # fmt: skip


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.data: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.data.append(data)


def _parse(html: str) -> _Tags:
    parser = _Tags()
    parser.feed(html)
    parser.close()
    return parser


# --- U09-17 segment_text ------------------------------------------------------------------------


def test_ut09_25_segment_text_splits_markers() -> None:
    """UT09-25 literal runs and markers become text and number segments in order."""
    ref = _ref(row_key={"team": "a", "q": 2})
    segs = mk.segment_text("a [[n1]] b [[n1]]", [ref])
    assert [s.kind for s in segs] == ["text", "number", "text", "number"]
    assert segs[1] == mk.Segment("number", "3", Q1, "n · team=a, q=2")
    assert segs[0] == mk.Segment("text", "a ", None, "")
    assert mk.segment_text("", []) == []


def test_ut09_25_number_title_single_row_and_na() -> None:
    """UT09-25 a None row key gives "single row"; an n/a value has no query_id."""
    (seg,) = mk.segment_text("[[n1]]", [_ref()])
    assert seg.title == "n · single row"
    (na,) = mk.segment_text("[[n2]]", [_ref("n2", 10**70)])
    assert (na.text, na.query_id) == ("n/a", None)


def test_ut09_25_marker_without_ref_is_contract_error() -> None:
    """UT09-25 a marker with no NumberRef raises ReportContractError."""
    with pytest.raises(e.ReportContractError):
        mk.segment_text("[[n9]]", [_ref()])


def test_ut09_25_html_escapes_tags_and_links_number() -> None:
    """UT09-25 "<b>[[n1]]</b>" renders escaped tags and one a.num with an escaped title."""
    ref = _ref(column='c"<x>', row_key={"k": "<v>"})
    html = mk.segments_to_html(mk.segment_text("<b>[[n1]]</b>", [ref]))
    assert isinstance(html, markupsafe.Markup)
    assert html.startswith("&lt;b&gt;<a ")
    assert html.endswith("</a>&lt;/b&gt;")
    parsed = _parse(str(html))
    assert parsed.tags == [("a", {"class": "num", "href": f"#ev-{Q1}", "title": 'c"<x> · k=<v>'})]
    assert "&#34;&lt;x&gt;" in html


def test_ut09_25_html_newlines_and_na() -> None:
    """UT09-25 newlines become <br>; an n/a number becomes span.num-na."""
    segs = [mk.Segment("text", "a\nb", None, ""), mk.Segment("number", "n/a", None, "t")]
    assert mk.segments_to_html(segs) == 'a<br>b<span class="num-na">n/a</span>'


def test_ut09_26_uncited_wrapped_in_mark() -> None:
    """UT09-26 a non-strict hit is wrapped in mark.uncited around the escaped span."""
    text = "cost <i>$5</i> and [[n1]]"
    start = text.index("$5")
    hit = UncitedHit("title", "$5", start, start + 2)
    segs = mk.segment_text(text, [_ref()], uncited=[hit])
    assert [s.kind for s in segs] == ["text", "uncited", "text", "number"]
    assert "".join(s.text for s in segs) == "cost <i>$5</i> and 3"
    html = mk.segments_to_html(segs)
    assert html.startswith('cost &lt;i&gt;<mark class="uncited">$5</mark>&lt;/i&gt; and ')
    evil = mk.segments_to_html([mk.Segment("uncited", "<s>1</s>", None, "")])
    assert evil == '<mark class="uncited">&lt;s&gt;1&lt;/s&gt;</mark>'


def test_ut09_26_overlapping_uncited_span_ignored() -> None:
    """UT09-26 a hit overlapping an earlier cut is skipped rather than duplicating text."""
    text = "1 and 22 [[n1]]"
    hits = [UncitedHit("t", "1 and", 0, 5), UncitedHit("t", "and", 2, 5)]
    segs = mk.segment_text(text, [_ref()], uncited=hits)
    assert [(s.kind, s.text) for s in segs] == [
        ("uncited", "1 and"), ("text", " 22 "), ("number", "3"),
    ]  # fmt: skip
    over_marker = [UncitedHit("t", "x", 10, 12)]
    assert [s.kind for s in mk.segment_text(text, [_ref()], uncited=over_marker)] == [
        "text", "number",
    ]  # fmt: skip


def test_ut09_26_markdown_uncited_and_numbers() -> None:
    """UT09-26 Markdown: uncited tag, linked numbers and plain n/a."""
    segs = [
        mk.Segment("uncited", "$5", None, ""),
        mk.Segment("number", "$1.2M", Q1, "t"),
        mk.Segment("number", "n/a", None, "t"),
    ]
    assert mk.segments_to_md(segs) == f"_[uncited]_ \\$5[\\$1.2M](#ev-{Q1})n/a"


# --- U09-18 Markdown ----------------------------------------------------------------------------


def test_ut09_27_markdown_image_and_specials() -> None:
    """UT09-27 image reduced to alt text; all specials escaped; no raw link survives."""
    text = "![x](http://e) <script> $5 [a](http://b)"
    out = mk.segments_to_md([mk.Segment("text", text, None, "")])
    assert out == "x &lt;script&gt; \\$5 \\[a\\]\\(http://b\\)"
    assert "http://e" not in out
    assert "<" not in out
    assert ">" not in out


def test_ut09_27_md_escape_every_special() -> None:
    """UT09-27 md_escape escapes & < > then backslashes each special character."""
    specials = "\\`*_{}[]()#+!|$~"
    assert mk.md_escape(specials) == "".join("\\" + ch for ch in specials)
    assert mk.md_escape("a & <b>") == "a &amp; &lt;b&gt;"
    assert mk.md_escape("plain - text.") == "plain - text."


def test_ut09_27_strip_images() -> None:
    """UT09-27 strip_images keeps only the alt text of every image."""
    assert mk.strip_images("a ![one](u1) b ![](u2) c") == "a one b  c"
    assert mk.strip_images("[link](u)") == "[link](u)"


# --- PT09-03 ------------------------------------------------------------------------------------

_ALLOWED = {"a", "span", "mark", "br"}
_md_link = re.compile(r"(?<!\\)\[[^\]]*(?<!\\)\]\((?!#ev-q_[0-9a-f]{16}\))")
_numbers = st.lists(
    st.builds(
        _ref,
        st.sampled_from(["n1", "n2", "n3"]),
        st.one_of(
            st.integers(-(10**12), 10**12), st.floats(-1e9, 1e9, allow_nan=False), st.just(10**70)
        ),
    ),
    min_size=1,
    max_size=3,
    unique_by=lambda r: r.id,
)
_pieces = st.lists(
    st.one_of(
        st.text(max_size=20),
        st.sampled_from(["<", ">", "&", '"', "'", "<script>", "![x](y)", "[a](b)", "\n", "$"]),
        st.sampled_from(["[[n1]]", "[[n2]]", "[[n3]]"]),
    ),
    max_size=12,
)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(pieces=_pieces, numbers=_numbers)
def test_pt09_03_html_only_allowed_tags(pieces: list[str], numbers: list[NumberRef]) -> None:
    """PT09-03 any text and valid numbers give HTML with only a, span, mark and br tags."""
    ids = {n.id for n in numbers}
    text = "".join(p for p in pieces if not (p.startswith("[[n") and p[2:-2] not in ids))
    try:
        segs = mk.segment_text(text, numbers)
    except e.ReportContractError:
        return  # random text formed a marker with no NumberRef (unreachable after U09-08)
    parsed = _parse(str(mk.segments_to_html(segs)))
    assert {tag for tag, _ in parsed.tags} <= _ALLOWED
    for tag, attrs in parsed.tags:
        if tag == "a":
            assert attrs["class"] == "num"
            assert attrs["href"] == f"#ev-{Q1}"
    md = mk.segments_to_md(segs)
    assert "<" not in md
    assert ">" not in md
    assert _md_link.search(md) is None
