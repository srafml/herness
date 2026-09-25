"""Number markers, the uncited-numeral scanner and NumberRef formatting (design 00 §12.1).

The single implementation (R-16) used by the Verifier (impl 05), the renderer (impl 09)
and impl 06, 07 and 11. Pure: no I/O, clock, logging or configuration access.
"""

from __future__ import annotations

import bisect
import decimal
import re
import types
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from herness.core.errors import ConfigError

MARKER_RE: Final = re.compile(r"\[\[(n[0-9]{1,39})\]\]")
ANY_MARKER_RE: Final = re.compile(r"\[\[([^\[\]]{0,40})\]\]")
MARKER_ID_RE: Final = re.compile(r"^n[0-9]+$")
NUMERAL_RE: Final = re.compile(r"(?<![\w.])[-+]?\$?\d[\d,]*(\.\d+)?\s*(%|k|K|M|bn|x)?(?!\w)")
MAX_SCAN_CHARS: Final = 100_000
HIT_TEXT_MAX: Final = 80
MAX_ALLOWED_PATTERNS: Final = 50
MAX_PATTERN_CHARS: Final = 200
TOO_LONG_TEXT: Final = "<text too long>"
NOT_AVAILABLE: Final = "n/a"
NUMBER_FORMATS: Final[frozenset[str]] = frozenset(
    {"usd", "usd_compact", "int", "pct1", "ratio2", "hours1", "minutes0", "prob2", "plain"}
)
DEFAULT_FORMAT_BY_UNIT: Final[Mapping[str, str]] = types.MappingProxyType(
    {
        "usd": "usd_compact",
        "pct": "pct1",
        "count": "int",
        "hours": "hours1",
        "minutes": "minutes0",
        "ratio": "ratio2",
    }
)
_NON_ASCII: Final = re.compile(r"[^\x00-\x7f]")
_CTX: Final = decimal.Context(prec=60, rounding=decimal.ROUND_HALF_EVEN)
_GROUP: Final = 3
_THOUSAND: Final = decimal.Decimal(1000)
_TIERS: Final = (
    (decimal.Decimal("1e9"), 2, "B"),
    (decimal.Decimal("1e6"), 2, "M"),
    (decimal.Decimal("1e3"), 1, "K"),
    (decimal.Decimal(1), 0, ""),
)
_SIMPLE: Final[Mapping[str, tuple[int, str, str]]] = types.MappingProxyType(
    {
        "usd": (2, "$", ""),
        "int": (0, "", ""),
        "pct1": (1, "", "%"),
        "ratio2": (2, "", ""),
        "prob2": (2, "", ""),
        "hours1": (1, "", " h"),
        "minutes0": (0, "", " min"),
    }
)


@dataclass(frozen=True, slots=True)
class Marker:
    """A valid number marker ``[[nK]]`` and its offsets."""

    id: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class MalformedMarker:
    """A double-bracket token that is not a valid marker (inner text, max 40 chars)."""

    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class MarkerScan:
    """Result of parse_markers, each tuple sorted by start."""

    markers: tuple[Marker, ...]
    malformed: tuple[MalformedMarker, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        """Marker ids in text order, duplicates kept."""
        return tuple(marker.id for marker in self.markers)


@dataclass(frozen=True, slots=True)
class NumeralHit:
    """An uncited numeral (text at most HIT_TEXT_MAX characters) and its offsets."""

    text: str
    start: int
    end: int


class FormattableNumber(Protocol):
    """Read-only view of a NumberRef (impl 05) that format_number needs."""

    @property
    def value(self) -> float | int | str: ...

    @property
    def unit(self) -> str: ...

    @property
    def format(self) -> str | None: ...


class _SpanIndex:
    """Answers "is [s, e) inside some span" in O(log n)."""

    def __init__(self, spans: Sequence[tuple[int, int]]) -> None:
        ordered = sorted(spans)
        self._starts = [start for start, _ in ordered]
        self._max_end: list[int] = []
        best = -1
        for _, end in ordered:
            best = max(best, end)
            self._max_end.append(best)

    def covers(self, start: int, end: int) -> bool:
        index = bisect.bisect_right(self._starts, start) - 1
        return index >= 0 and self._max_end[index] >= end


def parse_markers(text: str) -> MarkerScan:
    """Find every valid marker and every malformed double-bracket token. Raises nothing."""
    markers: list[Marker] = []
    malformed: list[MalformedMarker] = []
    for match in ANY_MARKER_RE.finditer(text[:MAX_SCAN_CHARS]):
        inner = match.group(1)
        if MARKER_ID_RE.fullmatch(inner):
            markers.append(Marker(inner, match.start(), match.end()))
        else:
            malformed.append(MalformedMarker(inner, match.start(), match.end()))
    return MarkerScan(tuple(markers), tuple(malformed))


def compile_allowed_patterns(patterns: Sequence[object]) -> tuple[re.Pattern[str], ...]:
    """Validate and compile reports.allowed_numeral_patterns once.

    Raises ConfigError (count or index); pattern text is never copied into the error.
    """
    if isinstance(patterns, str):
        msg = "allowed numeral patterns must be a list"
        raise ConfigError(msg)
    if not 0 < len(patterns) <= MAX_ALLOWED_PATTERNS:
        msg = "allowed numeral pattern count out of range"
        raise ConfigError(msg, count=len(patterns))
    compiled: list[re.Pattern[str]] = []
    for index, pattern in enumerate(patterns):
        if not isinstance(pattern, str) or not 0 < len(pattern) <= MAX_PATTERN_CHARS:
            msg = "invalid allowed numeral pattern"
            raise ConfigError(msg, index=index)
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            msg = "allowed numeral pattern does not compile"
            raise ConfigError(msg, index=index) from exc
    return tuple(compiled)


def _blank_markers(scan: str) -> str:
    chars = list(scan)
    for marker in parse_markers(scan).markers:
        chars[marker.start : marker.end] = " " * (marker.end - marker.start)
    return "".join(chars)


def find_uncited(text: str, allowed: Sequence[re.Pattern[str]]) -> tuple[NumeralHit, ...]:
    """Return every numeral outside a valid marker that no allowed pattern covers.

    Raises nothing. Text past MAX_SCAN_CHARS always yields a TOO_LONG_TEXT hit.
    """
    scan = text[:MAX_SCAN_CHARS]
    blanked = _blank_markers(scan)
    allowed_index = _SpanIndex(
        [m.span() for p in allowed for m in p.finditer(blanked) if m.end() > m.start()]
    )
    hits: list[NumeralHit] = []
    numeral_spans: list[tuple[int, int]] = []
    for match in NUMERAL_RE.finditer(blanked):
        start, end = match.span()
        numeral_spans.append((start, end))
        while end > start and blanked[end - 1].isspace():
            end -= 1
        if not allowed_index.covers(start, end):
            hits.append(NumeralHit(scan[start:end][:HIT_TEXT_MAX], start, end))
    numeral_index = _SpanIndex(numeral_spans)
    for match in _NON_ASCII.finditer(blanked):
        char, index = match.group(), match.start()
        if char.isspace() or unicodedata.numeric(char, None) is None:
            continue
        if not (numeral_index.covers(index, index + 1) or allowed_index.covers(index, index + 1)):
            hits.append(NumeralHit(char, index, index + 1))
    if len(text) > MAX_SCAN_CHARS:
        hits.append(NumeralHit(TOO_LONG_TEXT, MAX_SCAN_CHARS, len(text)))
    return tuple(sorted(hits, key=lambda hit: (hit.start, hit.end)))


def _to_decimal(value: object) -> decimal.Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str | decimal.Decimal):
        return None
    try:
        number = _CTX.create_decimal(str(value))
    except decimal.InvalidOperation:
        return None
    return number if number.is_finite() else None


def _quantize(number: decimal.Decimal, places: int) -> decimal.Decimal:
    return number.quantize(decimal.Decimal(1).scaleb(-places), context=_CTX)


def _group(whole: str) -> str:
    parts: list[str] = []
    while len(whole) > _GROUP:
        parts.insert(0, whole[-_GROUP:])
        whole = whole[:-_GROUP]
    parts.insert(0, whole)
    return ",".join(parts)


def _render(q: decimal.Decimal, prefix: str, suffix: str) -> str:
    whole, _, frac = format(abs(q), "f").partition(".")
    sign = "-" if q < 0 else ""
    return sign + prefix + _group(whole) + ("." + frac if frac else "") + suffix


def _usd_compact(number: decimal.Decimal) -> str:
    tier = next(
        i for i, (limit, _, _) in enumerate(_TIERS) if abs(number) >= limit or i == len(_TIERS) - 1
    )
    while True:
        limit, places, suffix = _TIERS[tier]
        q = _quantize(_CTX.divide(number, limit), places)
        if abs(q) >= _THOUSAND and tier > 0:
            tier -= 1
            continue
        return _render(q, "$", suffix)


def _plain(number: decimal.Decimal) -> str:
    if number == number.to_integral_value():
        return _render(_quantize(number, 0), "", "")
    q = _quantize(number, 4)
    whole, _, frac = format(abs(q), "f").partition(".")
    frac = frac.rstrip("0")
    sign = "-" if q < 0 else ""
    return sign + _group(whole) + ("." + frac if frac else "")


def format_value(value: object, unit: str, fmt: str | None) -> str:
    """Display a number for every NumberRef.format value; invalid input gives "n/a".

    Raises nothing. Output is fixed-point, locale-independent and deterministic.
    """
    chosen = fmt if fmt is not None else DEFAULT_FORMAT_BY_UNIT.get(unit, "plain")
    if chosen not in NUMBER_FORMATS:
        return NOT_AVAILABLE
    number = _to_decimal(value)
    if number is None:
        return NOT_AVAILABLE
    try:
        if chosen == "usd_compact":
            return _usd_compact(number)
        if chosen == "plain":
            return _plain(number)
        places, prefix, suffix = _SIMPLE[chosen]
        return _render(_quantize(number, places), prefix, suffix)
    except decimal.InvalidOperation:
        return NOT_AVAILABLE


def format_number(ref: FormattableNumber) -> str:
    """Display a NumberRef value: format_value(ref.value, ref.unit, ref.format)."""
    return format_value(ref.value, ref.unit, ref.format)
