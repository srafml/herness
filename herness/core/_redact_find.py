"""Text-anchored span finders and scan helpers of the redaction detectors (U10-36).

Size-forced private sibling of ``redact_patterns`` (T10-11b ruling): CREDENTIAL, URL_TOKEN
and EMAIL finders, plus the digit-window and case-folding helpers that the CARD, PHONE and
IP finders in ``redact_patterns`` use. It imports no other core module (spec §2 import
order). Each finder yields ``(start, end, value)`` exactly as ``finditer`` over the spec
regex would, but tries the regex only where a match can start: at literal anchors found
with ``str.find`` (``@``, ``://``, keywords). A regex that starts with ``\b`` or a
lookbehind has no first-character fast path in ``re``, so a whole-text pass costs 15-30 us
per 1.5 KB record (T10-11b profile). A search with ``pos``/``endpos`` still sees the real
characters before ``pos``; ``windows`` ends each window two characters past its run, so
lookaheads see what they would see in the whole text.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Final
from urllib.parse import unquote_plus, urlsplit

__all__ = ["ascii_digits", "at_least_digits", "enough_digits", "find_credentials"]
__all__ += ["find_emails", "find_url_tokens", "folded", "has_decimal", "windows"]

type Found = Iterator[tuple[int, int, str]]

_I: Final = re.IGNORECASE
_DIGITS: Final = "0123456789"
_WINDOW_TAIL: Final = 2  # characters past a run that a lookahead may inspect

# CREDENTIAL (a): the spec's `-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END ...` is
# quadratic on repeated BEGINs without an END; _find_pem gives the same spans in linear time.
_PEM_BEGIN: Final = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", _I)
_PEM_END: Final = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----", _I)
# CREDENTIAL (b)-(d) before the JWT, in spec order: pattern, span group, the literals every
# match starts with, and whether they are matched case-insensitively (looked up lower-cased).
type _Keyed = tuple[re.Pattern[str], int, tuple[str, ...], bool]
_CREDENTIAL: Final[tuple[_Keyed, ...]] = (
    (
        re.compile(r"\bauthorization\s*:\s*(?:bearer|basic)\s+([A-Za-z0-9._~+/=-]+)", _I),
        1,
        ("authorization",),
        True,
    ),
    (
        re.compile(
            r"\b(?:password|passwd|pwd|secret|api[_-]?key|token)\s*[:=]\s*([^\s&\"'<>,;]+)", _I
        ),
        1,
        ("passw", "pwd", "secret", "api", "token"),
        True,
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0, ("AKIA",), False),
    (
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
        0,
        ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),
        False,
    ),
    (re.compile(r"\bxox[abpors]-[A-Za-z0-9-]{10,200}\b"), 0, ("xox",), False),
)
# CREDENTIAL (e) after the JWT; (f) URL user info, scheme bounded to 32 characters (linear).
_ACCOUNT_KEY: Final[_Keyed] = (
    re.compile(r"AccountKey=([A-Za-z0-9+/=]{20,})", _I),
    1,
    ("accountkey=",),
    True,
)
_USERINFO: Final = re.compile(r"\b[a-z][a-z0-9+.-]{0,31}://([^\s/:@]+:[^\s/@]+)@", _I)
_USERINFO_TAIL: Final = re.compile(r"://[^\s/:@]+:[^\s/@]+@")
_SCHEME_MAX: Final = 32
# CREDENTIAL (d) JWT, spec `\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}`:
# quadratic as a regex on `eyJ-` repeats, so _find_jwts yields the same spans from
# precomputed maximal runs of the segment class, in linear time and without a length cap.
_JWT_START: Final = re.compile(r"\beyJ")
_JWT_RUN: Final = re.compile(r"[A-Za-z0-9_-]+")
_JWT_MIN_SEGMENT: Final = 5
_JWT_SEGMENTS: Final = 3

_URL: Final = re.compile(r"\bhttps?://[^\s<>\"']+", _I)
_URL_KEYS: Final = frozenset(
    {"token", "access_token", "key", "sig", "signature", "code", "password", "sv", "se"}
)

_EMAIL: Final = re.compile(r"\b[a-z0-9._%+-]{1,64}@[a-z0-9.-]{1,253}\.[a-z]{2,24}\b", _I)
_EMAIL_BEFORE: Final = 64  # local part
_EMAIL_AFTER: Final = 300  # domain (<= 278) plus the closing boundary

_ANY_DECIMAL: Final = re.compile(r"\d")
_FOLD_CHARS: Final = "\u0130\u0131\u017f\u212a"  # dotted I, dotless i, long s, Kelvin
_FOLD: Final = str.maketrans(dict(zip(_FOLD_CHARS, "iisk", strict=True)))


def ascii_digits(text: str) -> int:
    """Count of ASCII digits in ``text``."""
    return sum(map(text.count, _DIGITS))


def enough_digits(count: int) -> re.Pattern[str]:
    """Matches at ``pos`` when ``text[pos:endpos]`` holds ``count`` ASCII digits (linear)."""
    return re.compile(rf"[^0-9]*+(?:[0-9][^0-9]*+){{{count - 1}}}[0-9]")


def at_least_digits(count: int) -> Callable[[str], bool]:
    """Predicate ``ascii_digits(text) >= count`` that stops at the ``count``-th digit."""
    match = enough_digits(count).match
    return lambda text: match(text) is not None


def has_decimal(text: str) -> bool:
    """True when ``text`` holds a Unicode decimal digit (``\\d``, category Nd)."""
    return _ANY_DECIMAL.search(text) is not None


def _occurrences(hay: str, key: str) -> Iterator[int]:
    at = hay.find(key)
    while at >= 0:
        yield at
        at = hay.find(key, at + 1)


def _matches(pattern: re.Pattern[str], text: str, starts: Iterable[int]) -> Iterator[re.Match[str]]:
    """``pattern.finditer(text)`` when every match starts at one of the sorted ``starts``."""
    after = 0
    for pos in starts:
        if pos >= after and (match := pattern.match(text, pos)) is not None:
            yield match
            after = match.end()


def _group(matches: Iterable[re.Match[str]], text: str, group: int = 0) -> Found:
    """Non-empty spans of ``group``."""
    for match in matches:
        start, end = match.span(group)
        if start < end:
            yield start, end, text[start:end]


def windows(
    run: re.Pattern[str], text: str, enough: re.Pattern[str] | None = None
) -> list[tuple[int, int]]:
    """``(pos, endpos)`` per ``run`` match (holding ``enough`` digits), ``endpos`` past the
    run so that lookaheads see what they would see in the whole text."""
    size = len(text)
    spans = [(m.start(), min(size, m.end() + _WINDOW_TAIL)) for m in run.finditer(text)]
    if enough is None:
        return spans
    return [(pos, end) for pos, end in spans if enough.match(text, pos, end) is not None]


def folded(text: str) -> str:
    """``text`` lower-cased, same length, with re's case-insensitive ASCII letters as ASCII.

    Under ``re.IGNORECASE`` exactly four non-ASCII characters match an ASCII letter
    (``_FOLD``), and U+0130 is the only character whose ``lower()`` is longer than one.
    """
    if not text.isascii() and any(char in text for char in _FOLD_CHARS):
        text = text.translate(_FOLD)
    return text.lower()


def _keyed(text: str, low: str, keyed: _Keyed) -> Found:
    pattern, group, keys, fold = keyed
    hay = low if fold else text
    starts = sorted({at for key in keys for at in _occurrences(hay, key)})
    return _group(_matches(pattern, text, starts), text, group)


def _find_pem(text: str) -> Found:
    """Each BEGIN up to the nearest following END; stop at the first BEGIN with no END."""
    pos = 0
    while begin := _PEM_BEGIN.search(text, pos):
        end = _PEM_END.search(text, begin.end())
        if end is None:
            return
        yield begin.start(), end.end(), text[begin.start() : end.end()]
        pos = end.end()


def _jwt_end(text: str, pos: int, run_end: Callable[[int], int]) -> int | None:
    """End of the JWT whose first segment starts at ``pos`` (after ``eyJ``), else None."""
    for segment in range(_JWT_SEGMENTS):
        end = run_end(pos)
        if end - pos < _JWT_MIN_SEGMENT:
            return None
        if segment == _JWT_SEGMENTS - 1:
            return end
        if text[end : end + 1] != ".":
            return None
        pos = end + 1
    return None  # pragma: no cover - the last segment always returns


def _find_jwts(text: str) -> Found:
    starts = [match.start() for match in _JWT_START.finditer(text)]
    if not starts:
        return
    runs = [match.span() for match in _JWT_RUN.finditer(text)]
    run_starts = [begin for begin, _ in runs]

    def run_end(pos: int) -> int:
        # Greedy segment end: the end of the maximal run holding pos (pos if none does).
        index = bisect_right(run_starts, pos) - 1
        return runs[index][1] if index >= 0 and runs[index][1] > pos else pos

    after = 0
    for start in starts:
        if start < after:
            continue
        end = _jwt_end(text, start + 3, run_end)
        if end is not None:
            yield start, end, text[start:end]
            after = end


def find_credentials(text: str) -> Found:
    """CREDENTIAL (a)-(f) in spec order."""
    return _credentials(text, list(_occurrences(text, "://")))


def _credentials(text: str, schemes: Sequence[int]) -> Found:
    if "-----" in text:
        yield from _find_pem(text)
    low = folded(text)
    for keyed in _CREDENTIAL:
        yield from _keyed(text, low, keyed)
    if "eyJ" in text:
        yield from _find_jwts(text)
    yield from _keyed(text, low, _ACCOUNT_KEY)
    tails = [at for at in schemes if _USERINFO_TAIL.match(text, at)]
    starts = sorted({pos for at in tails for pos in range(max(0, at - _SCHEME_MAX), at)})
    yield from _group(_matches(_USERINFO, text, starts), text, 1)


def _raw_query(url: str) -> str:
    """The query of ``url``; when urlsplit rejects it, the text between ``?`` and ``#``."""
    try:
        return urlsplit(url).query
    except ValueError:
        # Fail closed: a malformed host must not hide secret query parameters.
        start, frag = url.find("?"), url.find("#")
        if start < 0 or 0 <= frag < start:
            return ""
        return url[start + 1 : frag] if frag >= 0 else url[start + 1 :]


def _query_values(url: str, offset: int) -> Found:
    """Yield the raw value substring of each secret-named query parameter of ``url``."""
    query = _raw_query(url)
    if not query:
        return
    pos = offset + url.index("?") + 1
    for piece in query.split("&"):
        name, sep, value = piece.partition("=")
        if sep and value and unquote_plus(name).lower() in _URL_KEYS:
            start = pos + len(name) + 1
            yield start, start + len(value), value
        pos += len(piece) + 1


def _url_tokens(text: str, schemes: Sequence[int]) -> Found:
    starts = sorted({pos for at in schemes for pos in (at - 5, at - 4) if pos >= 0})
    for match in _matches(_URL, text, starts):
        yield from _query_values(match.group(), match.start())


def find_url_tokens(text: str) -> Found:
    """URL_TOKEN: secret-named query values of each ``http(s)://`` URL."""
    return _url_tokens(text, list(_occurrences(text, "://")))


def find_emails(text: str) -> Found:
    """EMAIL: the spec regex searched around each ``@`` (local part <= 64 characters)."""
    after = 0
    for at in _occurrences(text, "@"):
        pos = max(after, at - _EMAIL_BEFORE)
        match = _EMAIL.search(text, pos, min(len(text), at + _EMAIL_AFTER))
        if match is not None and match.start() <= at:  # its `@` is this one
            yield match.start(), match.end(), match.group()
            after = match.end()
