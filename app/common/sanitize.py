"""Markdown sanitising of untrusted text for the dashboard (impl 09 U09-57 - U09-59; TH09-02).

Model output, redacted ticket text and memory content reach ``st.markdown`` only through these
functions, always with ``unsafe_allow_html=False``. Images, links other than allowed in-app
evidence anchors, raw HTML, LaTeX and Streamlit directives are neutralised; URLs are shown as
inline code. Every pattern is linear (no nested quantifiers), so hostile input cannot stall a
page. Pure.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import Final

from herness.core.numbers import NOT_AVAILABLE, format_number, parse_markers
from herness.core.types import NumberRef

__all__ = [
    "escape_markdown_chunk",
    "render_answer_markdown",
    "render_marked_markdown",
    "safe_markdown",
]

_MAX_CHARS: Final = 50_000
_MAX_NUMBERS: Final = 200
_UNRESOLVED: Final = "[unresolved]"
# C0 controls except \t and \n, DEL and C1 controls are removed (step 1).
_DROP: Final[dict[int, str | None]] = dict.fromkeys([*range(9), *range(11, 32), *range(127, 160)])
# Steps 2 and 3. Link text excludes brackets and an image target excludes parentheses, so each
# scan stops at the next candidate (linear); whatever these miss is escaped by step 5.
_IMAGE_RE: Final = re.compile(r"!\[([^\[\]]*)\]\([^()]*\)")
_LINK_RE: Final = re.compile(r'\[([^\[\]]*)\]\(([^()\s]*)(\s+"[^"]*")?\)')
_ANCHOR_RE: Final = re.compile(r"#ev-(q_[0-9a-f]{16})")
_QUERY_ID_RE: Final = re.compile(r"q_[0-9a-f]{16}")
# Step 4: autolinks, bare URLs and (GFM autolinks them too) e-mail addresses -> inline code.
_URL_RE: Final = re.compile(
    r"<(?P<auto>(?:https?|ftp|mailto):[^<>\s\x00]*)>"
    r"|(?P<bare>(?i:\b(?:https?://|www\.))[^\s\x00]*)"
    r"|(?P<mail>(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+)"
)
_URL_SAFE: Final = str.maketrans(
    {"`": None, "$": "%24", "<": "%3C", ">": "%3E", "[": "%5B", "]": "%5D"}
)
# Steps 5 and 6, plus backslash, brackets, parentheses, backticks and `!` (so no link, image or
# code span can be formed from the remaining literal text).
_ESCAPE: Final = str.maketrans(
    {"\\": "\\\\", "&": "&amp;", "<": "&lt;", ">": "&gt;", "$": "\\$", "[": "\\[", "]": "\\]",
     "(": "\\(", ")": "\\)", "`": "\\`", "!": "&#33;"}
)  # fmt: skip
_DIRECTIVE_RE: Final = re.compile(r":(?=[A-Za-z_]+\\\[|material/)")
_PLACEHOLDER_RE: Final = re.compile("\x00A([0-9]+)\x00")
_CHUNK_SPECIALS: Final = "\\`*_{}[]()#+-.!|$~:"
_CHUNK: Final[dict[int, str | None]] = {
    **_DROP,
    **{ord(c): "\\" + c for c in _CHUNK_SPECIALS},
    **{ord("&"): "&amp;", ord("<"): "&lt;", ord(">"): "&gt;"},
}


def _text(raw: str) -> str:
    """Escape literal text (steps 5 and 6)."""
    return _DIRECTIVE_RE.sub(r"\\:", raw.translate(_ESCAPE))


def _literal(raw: str) -> str:
    """URLs as inline code (step 4), everything else escaped."""
    parts: list[str] = []
    pos = 0
    for match in _URL_RE.finditer(raw):
        url = match.group("auto") or match.group(0)
        parts += [_text(raw[pos : match.start()]), "`" + url.translate(_URL_SAFE) + "`"]
        pos = match.end()
    parts.append(_text(raw[pos:]))
    return "".join(parts)


def _anchor(text: str, query_id: str) -> str:
    return f"[{_literal(text)}](#ev-{query_id})"


def safe_markdown(text: str, *, allowed_anchor_ids: Collection[str] = ()) -> str:
    """Untrusted text as Markdown without HTML, images, external links, LaTeX or directives."""
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + "…"
    text = _IMAGE_RE.sub(lambda m: m.group(1), text.translate(_DROP))
    allowed = frozenset(allowed_anchor_ids)
    anchors: list[str] = []

    def link(match: re.Match[str]) -> str:
        hit = _ANCHOR_RE.fullmatch(match.group(2))
        if hit is None or hit.group(1) not in allowed:
            return match.group(1)
        anchors.append(_anchor(match.group(1), hit.group(1)))
        return f"\x00A{len(anchors) - 1}\x00"

    text = _LINK_RE.sub(link, text)
    return _PLACEHOLDER_RE.sub(lambda m: anchors[int(m.group(1))], _literal(text))


def escape_markdown_chunk(text: str) -> str:
    """Escape a streamed chunk per character, so ``escape(a + b) == escape(a) + escape(b)``."""
    return text.translate(_CHUNK)


def _number_md(ref: NumberRef) -> str:
    shown = format_number(ref)
    if shown == NOT_AVAILABLE or _QUERY_ID_RE.fullmatch(ref.query_id) is None:
        return _text(shown)
    return f"[{_text(shown)}](#ev-{ref.query_id})"


def _first_free(content: str, shown: str, taken: Sequence[tuple[int, int]]) -> int | None:
    """Start of the first stand-alone occurrence of ``shown`` not overlapping ``taken``."""
    pattern = re.compile(r"(?<![\w.,])" + re.escape(shown) + r"(?![\w]|[.,][0-9])")
    for match in pattern.finditer(content):
        if all(match.end() <= start or end <= match.start() for start, end in taken):
            return match.start()
    return None


def render_answer_markdown(content: str, numbers: Sequence[NumberRef]) -> str:
    """Stored answer text with each number found linked to its evidence entry (U09-59)."""
    ordered = sorted(numbers, key=lambda ref: int(ref.id[1:]))[:_MAX_NUMBERS]
    spans: list[tuple[int, int, NumberRef]] = []
    for ref in ordered:
        shown = format_number(ref)
        if shown == NOT_AVAILABLE or _QUERY_ID_RE.fullmatch(ref.query_id) is None:
            continue
        start = _first_free(content, shown, [(s, e) for s, e, _ in spans])
        if start is not None:
            spans.append((start, start + len(shown), ref))
    parts: list[str] = []
    pos = 0
    for start, end, ref in sorted(spans, key=lambda span: span[0]):
        parts += [safe_markdown(content[pos:start]), _number_md(ref)]
        pos = end
    parts.append(safe_markdown(content[pos:]))
    return "".join(parts)


def render_marked_markdown(text: str, numbers: Sequence[NumberRef]) -> str:
    """Model text with ``[[nX]]`` markers: numbers linked, text sanitised (U09-59)."""
    refs = {ref.id: ref for ref in list(numbers)[:_MAX_NUMBERS]}
    parts: list[str] = []
    pos = 0
    for marker in parse_markers(text).markers:
        ref = refs.get(marker.id)
        parts += [
            safe_markdown(text[pos : marker.start]),
            _UNRESOLVED if ref is None else _number_md(ref),
        ]
        pos = marker.end
    parts.append(safe_markdown(text[pos:]))
    return "".join(parts)
