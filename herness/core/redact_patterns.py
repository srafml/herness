"""Redaction detectors, prefilters, Luhn and normalization (U10-36, U10-37).

Each detector pairs a cheap prefilter with a ``find`` that yields ``(start, end, value)``.
The CREDENTIAL, URL_TOKEN and EMAIL finders and the scan helpers live in the private sibling
``_redact_find`` (T10-11b, size-forced). CARD, PHONE and IPv4 run their spec regexes only
inside digit windows, IPv6 only in hex runs holding two colons, so each yields what
``finditer`` over the whole text yields (UT10-92). Overlap resolution, protected pseudonym
tokens and replacement belong to ``Redactor.scan`` (U10-41). Spec regexes use ``[0-9]``
instead of ``\\d`` so non-ASCII digits never count as card or phone digits; ``\\w`` in
lookarounds stays Unicode-aware. ``EntityType`` and ``DETECTION_ORDER`` are declared here
because ``redact`` imports this module (spec §2 import order) and re-exports them (U10-35).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from herness.core._redact_find import (
    at_least_digits,
    enough_digits,
    find_credentials,
    find_emails,
    find_url_tokens,
    has_decimal,
    windows,
)

if TYPE_CHECKING:
    from herness.core.settings import RedactionConfig

__all__ = ["DETECTION_ORDER", "TOKEN_PATTERN", "Detector", "EntityType", "build_detectors"]
__all__ += ["luhn_valid", "normalize_value"]

EntityType = Literal[
    "EMAIL",
    "PHONE",
    "IP",
    "PERSON",
    "EMPLOYEE_ID",
    "USER_ID",
    "CREDENTIAL",
    "URL_TOKEN",
    "CARD",
    "NATIONAL_ID",
    "CUSTOM",
]
DETECTION_ORDER: Final[tuple[EntityType, ...]] = (
    "CREDENTIAL", "URL_TOKEN", "EMAIL", "CARD", "NATIONAL_ID", "EMPLOYEE_ID", "USER_ID", "PHONE",
    "IP", "PERSON", "CUSTOM",
)  # fmt: skip
TOKEN_PATTERN: Final = re.compile(
    r"\[(?:EMAIL|PHONE|IP|PERSON|EMPLOYEE_ID|USER_ID|CARD|NATIONAL_ID|CUSTOM)_[0-9a-f]{10}\]"
    r"|\[SECRET\]"
)

type Found = Iterator[tuple[int, int, str]]

_I: Final = re.IGNORECASE
_DIGITS: Final = "0123456789"
_MIN_COLONS: Final = 2
_NANP_DIGITS: Final = 10
_CREDENTIAL_HINTS: Final = ("=", ":", "-----", "AKIA", "gh", "xox", "eyJ", "AccountKey")
CARD_DIGITS: Final = range(13, 20)  # 13-19 digits
PHONE_DIGITS: Final = range(9, 16)  # 9-15 digits
LOCAL_PHONE_DIGITS: Final = 7  # `555-0123` (T10-11b, T09-15d addendum)
_CARD_WINDOW: Final = re.compile(r"[0-9][0-9 -]{12,}")
_CARD: Final = re.compile(r"\b(?:[0-9][ -]?){12,18}[0-9]\b")
_CARD_RUN: Final = re.compile(r"(?<!\w)[0-9](?:[ -]?[0-9]){12,}")  # runs of >= 13 digits
_WORD: Final = re.compile(r"\w")
# Account/card-shaped digit-only run, Luhn-optional (T10-11b ruling on the T09-15d addendum):
# 16-19 digits, no leading zero; 13-15 digit runs are PHONE already. Not after a word
# character, `.` or `+`, not a minus sign's number, not before a decimal or range tail.
_ACCOUNT: Final = re.compile(r"(?<![\w.+])[1-9][0-9]{15,18}(?!\w|[.-][0-9])")
_LUHN2: Final = (0, 2, 4, 6, 8, 1, 3, 5, 7, 9)  # Luhn term of a doubled digit
_DOUBLED: Final = str.maketrans(_DIGITS, "".join(map(str, _LUHN2)))

_PHONE_WINDOW: Final = re.compile(r"[(+0-9][0-9 ().+-]{7,}")  # class first: fast
# `(` opens a candidate only before a 1-4 digit area code and `)` (T10-11b, SN-3b).
_PHONE: Final = re.compile(r"(?<![\w+])(?:\+|\((?=[0-9]{1,4}\)))?[0-9][0-9 ().-]{7,20}[0-9](?!\w)")
_LOCAL_PHONE: Final = re.compile(r"(?<![\w+.-])[2-9][0-9]{2}-[0-9]{4}(?![\w-]|\.[0-9])")
_PHONE_EXCLUDE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,3}(?:\.[0-9]{1,3}){3}")
# A build id (impl 00 ``new_build_id``, T09-15d's verbatim-safe shape) is never PHONE (T06-29).
_BUILD_ID: Final = re.compile(r"(?<!\w)[0-9]{8}-[0-9]{6}-[0-9A-HJKMNP-TV-Z]{6}(?!\w)")
_BUILD_ID_LEN: Final = 22
_PHONE_MARKS: Final = str.maketrans("", "", "+() .-")

_OCTET: Final = r"(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])"
# A digit-run start (the lookbehind after the first digit keeps the fast first-character
# path) with three dots in its [0-9.] run: linear, and every IPv4 match lies in one.
_IPV4_WINDOW: Final = re.compile(r"[0-9](?<![0-9][0-9])(?:[0-9]*+\.){3}[0-9.]*+")
_IPV4: Final = re.compile(rf"\b{_OCTET}(?:\.{_OCTET}){{3}}\b")
_IPV6: Final = re.compile(r"(?<![\w:])[0-9A-Fa-f:]{2,39}(?![\w:])")
_IPV6_MAX: Final = 39
_COLON_PAIR: Final = re.compile(r":[0-9A-Fa-f]*:")
_HEX_COLON_RUN: Final = re.compile(r"[0-9A-Fa-f:]*")
_HEX_COLON: Final = frozenset("0123456789ABCDEFabcdef:")
_ENOUGH_CARD: Final = enough_digits(CARD_DIGITS.start)
_ENOUGH_PHONE: Final = enough_digits(LOCAL_PHONE_DIGITS)
_IPV4_LOOSE: Final = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}")
_LAST_FIRST: Final = re.compile(r"^([^,]+),\s*(.+)$")
_UPPER_IDS: Final = frozenset({"EMPLOYEE_ID", "USER_ID", "NATIONAL_ID", "CARD"})


@dataclass(frozen=True)
class Detector:
    """One entity detector: a cheap prefilter and a span finder (U10-36)."""

    type: EntityType
    prefilter: Callable[[str], bool]
    find: Callable[[str], Found]


def _spans(pattern: re.Pattern[str], text: str) -> Found:
    """Yield the non-empty span of every match of ``pattern``."""
    for match in pattern.finditer(text):
        start, end = match.span()
        if start < end:
            yield start, end, text[start:end]


def _has_digit(text: str) -> bool:
    return any(digit in text for digit in _DIGITS)


def _credential_prefilter(text: str) -> bool:
    return any(hint in text for hint in _CREDENTIAL_HINTS)


def _ip_prefilter(text: str) -> bool:
    return ("." in text and _has_digit(text)) or text.count(":") >= _MIN_COLONS


def _lead(source: str) -> re.Pattern[str] | None:
    """``source`` without its leading ``\\b``: it matches wherever ``source`` does (T10-11b)."""
    if not source.startswith(r"\b"):
        return None
    try:
        return re.compile(source[2:], _I)
    except re.error:
        return None


def _led_spans(pattern: re.Pattern[str], lead: re.Pattern[str] | None, text: str) -> Found:
    """``_spans(pattern, text)``, trying ``pattern`` only where ``lead`` finds a start.

    A leading ``\\b`` denies ``re`` its first-character fast path (~15 us per 1.5 KB), and
    every match of ``pattern`` starts where ``lead`` matches; an empty match falls back.
    """
    if lead is None:
        yield from _spans(pattern, text)
        return
    found: list[tuple[int, int]] = []
    pos = 0
    while pos <= len(text) and (candidate := lead.search(text, pos)) is not None:
        match = pattern.match(text, candidate.start())
        if match is None:
            pos = candidate.start() + 1
        elif match.end() == match.start():  # finditer's empty-match rules apply
            yield from _spans(pattern, text)
            return
        else:
            found.append(match.span())
            pos = match.end()
    for start, end in found:
        yield start, end, text[start:end]


def _pattern_detector(kind: EntityType, sources: Sequence[str], *, needs_digit: bool) -> Detector:
    """Config-pattern detector (whole match, case-insensitive); empty config never matches."""
    patterns = tuple((re.compile(source, _I), _lead(source)) for source in sources)

    def prefilter(text: str) -> bool:
        # Config patterns keep Unicode ``\d``, so any decimal digit passes (has_decimal).
        return bool(patterns) and (not needs_digit or has_decimal(text))

    def find(text: str) -> Found:
        for pattern, lead in patterns:
            yield from _led_spans(pattern, lead, text)

    return Detector(kind, prefilter, find)


def build_detectors(cfg: RedactionConfig) -> tuple[Detector, ...]:
    """Return the patterned detectors in ``DETECTION_ORDER``, without ``PERSON`` (U10-36)."""
    ids = cfg.id_patterns
    by_type: dict[EntityType, Detector] = {
        "CREDENTIAL": Detector("CREDENTIAL", _credential_prefilter, find_credentials),
        "URL_TOKEN": Detector("URL_TOKEN", lambda text: "://" in text, find_url_tokens),
        "EMAIL": Detector("EMAIL", lambda text: "@" in text, find_emails),
        "CARD": Detector("CARD", at_least_digits(CARD_DIGITS.start), find_cards),
        "NATIONAL_ID": _pattern_detector("NATIONAL_ID", cfg.national_id_patterns, needs_digit=True),
        "EMPLOYEE_ID": _pattern_detector(
            "EMPLOYEE_ID", ids.get("EMPLOYEE_ID", ()), needs_digit=True
        ),
        "USER_ID": _pattern_detector("USER_ID", ids.get("USER_ID", ()), needs_digit=True),
        "PHONE": Detector("PHONE", at_least_digits(LOCAL_PHONE_DIGITS), find_phones),
        "IP": Detector("IP", _ip_prefilter, find_ips),
        "CUSTOM": _pattern_detector(
            "CUSTOM", tuple(cfg.custom_patterns.values()), needs_digit=False
        ),
    }
    return tuple(by_type[kind] for kind in DETECTION_ORDER if kind != "PERSON")


def find_cards(text: str) -> Found:
    """CARD per digit window: Luhn-valid spec matches, run cuts, then account-shaped runs."""
    for pos, endpos in windows(_CARD_WINDOW, text, _ENOUGH_CARD):
        found: list[tuple[int, int]] = []
        for match in _CARD.finditer(text, pos, endpos):
            digits = match.group().replace(" ", "").replace("-", "")
            valid = len(digits) in CARD_DIGITS and luhn_valid(digits)
            if valid and not _in_build_id(text, *match.span()):
                found.append(match.span())
                yield match.start(), match.end(), match.group()
        for start, end, value in _card_cuts(text, pos, endpos, found):
            if not _in_build_id(text, start, end):
                yield start, end, value
        for match in _ACCOUNT.finditer(text, pos, endpos):
            if not _after_minus(text, match.start()):
                yield match.start(), match.end(), match.group()


def _after_minus(text: str, start: int) -> bool:
    """True when ``text[start - 1]`` is a minus sign (a ``-`` after no word character)."""
    return text[start - 1 : start] == "-" and (start == 1 or _WORD.match(text, start - 2) is None)


def _card_cuts(text: str, pos: int, endpos: int, found: Sequence[tuple[int, int]]) -> Found:
    """Run cuts find `<card> 10.2.3.4` (T10-10): leftmost, longest Luhn-valid 13-19 digits.

    A run that is exactly an accepted CARD match is skipped: every cut would overlap it.
    """
    for run in _CARD_RUN.finditer(text, pos, endpos):
        if run.span() in found:
            continue
        base, value = run.start(), run.group()
        at = [i for i, ch in enumerate(value) if ch in _DIGITS]
        cuts = {k for k in range(len(at) - 1) if at[k + 1] - at[k] > 1}  # sep after k
        if _WORD.match(text, run.end()) is None:
            cuts.add(len(at) - 1)
        sums = [[0], [0]]  # sums[p][k]: Luhn terms of k digits for a cut ending on parity p
        for k, d in enumerate(ord(value[i]) - 48 for i in at):
            sums[k % 2].append(sums[k % 2][-1] + d)
            sums[1 - k % 2].append(sums[1 - k % 2][-1] + _LUHN2[d])
        free = 0
        for first in range(len(at)):
            if first < free or (first and first - 1 not in cuts):
                continue
            for last in range(min(first + 18, len(at) - 1), first + 11, -1):
                if last in cuts and (sums[last % 2][last + 1] - sums[last % 2][first]) % 10 == 0:
                    yield base + at[first], base + at[last] + 1, value[at[first] : at[last] + 1]
                    free = last + 1
                    break


def _in_build_id(text: str, start: int, end: int) -> bool:
    """True when ``[start, end)`` lies inside a build-id span of ``text`` (T06-29; CARD too
    since T10-11b: the 14 digits of one build id in ten pass Luhn). A build id has at most
    8 digits between dashes, so a 9+ digit span inside one holds a dash."""
    if "-" not in text[start:end]:
        return False
    window = _BUILD_ID.finditer(text, max(0, end - _BUILD_ID_LEN), start + _BUILD_ID_LEN + 1)
    return any(m.start() <= start and end <= m.end() for m in window)


def find_phones(text: str) -> Found:
    """PHONE per digit window: spec candidates with 9-15 digits, not dates, IPv4 or build ids,
    then 7-digit local numbers ``NXX-XXXX`` (T10-11b, T09-15d addendum)."""
    for pos, endpos in windows(_PHONE_WINDOW, text, _ENOUGH_PHONE):
        for match in _PHONE.finditer(text, pos, endpos):
            start, value = match.start(), match.group()
            digits = len(value.translate(_PHONE_MARKS))  # a candidate is digits and marks
            while digits > PHONE_DIGITS[-1] and " " in value:  # merged run: cut
                value = value[: value.rindex(" ")].rstrip()
                digits = len(value.translate(_PHONE_MARKS))
            end = start + len(value)
            if _in_build_id(text, start, end):
                continue
            if digits in PHONE_DIGITS and not _PHONE_EXCLUDE.search(value):
                yield start, end, value
        for match in _LOCAL_PHONE.finditer(text, pos, endpos):
            yield match.start(), match.end(), match.group()


def _is_ipv6(value: str) -> bool:
    if value.count(":") < _MIN_COLONS:
        return False
    try:
        ipaddress.IPv6Address(value)
    except ValueError:
        return False
    return True


def find_ips(text: str) -> Found:
    """IPv4 in digit-dot windows, then IPv6 candidates in hex runs with two colons."""
    for pos, endpos in windows(_IPV4_WINDOW, text):
        for match in _IPV4.finditer(text, pos, endpos):
            yield match.start(), match.end(), match.group()
    after = 0
    for pair in _COLON_PAIR.finditer(text):
        if pair.start() < after:
            continue
        start, limit = pair.start(), max(0, pair.start() - _IPV6_MAX - 1)
        while start > limit and text[start - 1] in _HEX_COLON:
            start -= 1
        run = _HEX_COLON_RUN.match(text, pair.end())
        after = run.end() if run is not None else pair.end()
        for match in _IPV6.finditer(text, start, min(len(text), after + 1)):
            if _is_ipv6(match.group()):
                yield match.start(), match.end(), match.group()


def luhn_valid(digits: str) -> bool:
    """Luhn checksum over ASCII digits: double every second digit from the right (U10-37).

    ``_DOUBLED`` maps a digit to its doubled Luhn term (doubled, minus 9 when over 9).
    """
    doubled = digits[-2::-2].translate(_DOUBLED)
    return (sum(map(int, digits[-1::-2])) + sum(map(int, doubled))) % 10 == 0


def _normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in value if ch in _DIGITS)
    if len(digits) == _NANP_DIGITS and not value.lstrip().startswith("+"):
        digits = "1" + digits
    return "+" + digits


def _normalize_person(value: str) -> str:
    text = " ".join(value.casefold().split())
    match = _LAST_FIRST.match(text)
    if match:
        text = f"{match.group(2)} {match.group(1).rstrip()}"
    return text


def _normalize_ip(value: str) -> str:
    # Leading-zero octets ("01.2.3.004") match the IPv4 detector but ipaddress rejects them;
    # read them as decimal so one address yields one pseudonym. Unparsable stays unchanged.
    candidate = value
    if _IPV4_LOOSE.fullmatch(value):
        octets = [int(part) for part in value.split(".")]
        if max(octets) <= 255:  # noqa: PLR2004 - the dotted form is what ipaddress returns
            return ".".join(map(str, octets))
        candidate = ".".join(map(str, octets))
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return value


def normalize_value(type: EntityType, value: str) -> str:  # noqa: A002 - spec signature
    """Normalize a detected value before pseudonymization (U10-37)."""
    if type == "EMAIL":
        return value.strip().lower()
    if type == "PHONE":
        return _normalize_phone(value)
    if type == "PERSON":
        return _normalize_person(value)
    if type in _UPPER_IDS:
        return value.upper().replace(" ", "").replace("-", "")
    if type == "IP":
        return _normalize_ip(value)
    return value
