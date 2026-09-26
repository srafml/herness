"""Escaping-first marker substitution for the HTML and Markdown reports (impl 09 U09-17, U09-18).

Model text is split into literal, number and uncited segments before any escaping; only the
output functions here produce markup, always escaping model text and building the evidence
anchors themselves (TH09-01, TH09-02, TH09-25). Marker parsing and number formatting come from
``herness.core.numbers`` (R-16). Pure.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

import markupsafe

from herness.core.errors import ReportContractError
from herness.core.numbers import NOT_AVAILABLE, format_number, parse_markers
from herness.core.types import NumberRef
from herness.reports.contract import UncitedHit

__all__ = [
    "Segment",
    "md_escape",
    "segment_text",
    "segments_to_html",
    "segments_to_md",
    "strip_images",
]

SegmentKind = Literal["text", "number", "uncited"]

_IMAGE_RE: Final = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_SPECIAL_RE: Final = re.compile(r"([\\`*_{}\[\]()#+!|$~])")
_NUM_LINK: Final = markupsafe.Markup('<a class="num" href="#ev-{}" title="{}">{}</a>')
_NUM_NA: Final = markupsafe.Markup('<span class="num-na">n/a</span>')
_UNCITED: Final = markupsafe.Markup('<mark class="uncited">{}</mark>')
_BR: Final = markupsafe.Markup("<br>")


@dataclass(frozen=True, slots=True)
class Segment:
    """A piece of model text: a literal run, a formatted number or an uncited span (raw text)."""

    kind: SegmentKind
    text: str
    query_id: str | None
    title: str


@dataclass(frozen=True, slots=True, order=True)
class _Cut:
    start: int
    end: int
    kind: SegmentKind
    marker_id: str = ""


def _title(ref: NumberRef) -> str:
    """``"<column> · <row_key>"``; row key as ``k=v`` pairs joined by ``, `` or ``single row``."""
    key = ", ".join(f"{k}={v}" for k, v in ref.row_key.items()) if ref.row_key else ""
    return f"{ref.column} · {key or 'single row'}"


def _number(ref: NumberRef | None) -> Segment:
    if ref is None:
        msg = "marker has no NumberRef"
        raise ReportContractError(msg)
    shown = format_number(ref)
    query_id = None if shown == NOT_AVAILABLE else ref.query_id
    return Segment("number", shown, query_id, _title(ref))


def _cuts(text: str, uncited: Sequence[UncitedHit]) -> list[_Cut]:
    markers = [_Cut(m.start, m.end, "number", m.id) for m in parse_markers(text).markers]
    spans = [_Cut(h.start, h.end, "uncited") for h in uncited if 0 <= h.start < h.end <= len(text)]
    return sorted(markers + spans)


def segment_text(
    text: str, numbers: Sequence[NumberRef], *, uncited: Sequence[UncitedHit] = ()
) -> list[Segment]:
    """Split ``text`` into text, number and uncited segments (U09-17), before any escaping.

    Raises ReportContractError for a marker without a NumberRef (unreachable after U09-08).
    A span overlapping an earlier cut is ignored.
    """
    refs = {ref.id: ref for ref in numbers}
    segments: list[Segment] = []
    pos = 0
    for cut in _cuts(text, uncited):
        if cut.start < pos:
            continue
        if cut.start > pos:
            segments.append(Segment("text", text[pos : cut.start], None, ""))
        if cut.kind == "number":
            segments.append(_number(refs.get(cut.marker_id)))
        else:
            segments.append(Segment("uncited", text[cut.start : cut.end], None, ""))
        pos = cut.end
    if pos < len(text):
        segments.append(Segment("text", text[pos:], None, ""))
    return segments


def _html(seg: Segment) -> markupsafe.Markup:
    if seg.kind == "number":
        if seg.query_id is None:
            return _NUM_NA
        return _NUM_LINK.format(seg.query_id, seg.title, seg.text)
    if seg.kind == "uncited":
        return _UNCITED.format(seg.text)
    return _BR.join(markupsafe.escape(line) for line in seg.text.split("\n"))


def segments_to_html(segments: Sequence[Segment]) -> markupsafe.Markup:
    """HTML with escaped text; only ``a.num``, ``span.num-na``, ``mark.uncited`` and ``br`` tags."""
    return markupsafe.Markup("").join(_html(seg) for seg in segments)


def strip_images(text: str) -> str:
    """Replace every Markdown image ``![alt](url)`` by its alt text."""
    return _IMAGE_RE.sub(r"\1", text)


def md_escape(text: str) -> str:
    """``&``, ``<``, ``>`` as entities, then a backslash before each Markdown special."""
    entities = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MD_SPECIAL_RE.sub(r"\\\1", entities)


def _md(seg: Segment) -> str:
    if seg.kind == "number":
        if seg.query_id is None:
            return NOT_AVAILABLE
        return f"[{md_escape(seg.text)}](#ev-{seg.query_id})"
    if seg.kind == "uncited":
        return "_[uncited]_ " + md_escape(seg.text)
    return md_escape(strip_images(seg.text))


def segments_to_md(segments: Sequence[Segment]) -> str:
    """Markdown with escaped text; the only links are ``(#ev-q_…)`` evidence anchors."""
    return "".join(_md(seg) for seg in segments)
