"""Compiled redaction detectors, prefilters, Luhn and normalization (U10-36, U10-37).

Each detector pairs a cheap prefilter with a ``find`` that yields ``(start, end, value)``.
Overlap resolution, protected pseudonym tokens and replacement belong to ``Redactor.scan``
(U10-41). Spec regexes use ``[0-9]`` instead of ``\\d`` so non-ASCII digits never count as
card or phone digits; ``\\w`` in lookarounds stays Unicode-aware. ``EntityType`` and
``DETECTION_ORDER`` are declared here because ``redact`` imports this module (spec §2
import order) and re-exports them (U10-35).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import unquote_plus, urlsplit

if TYPE_CHECKING:
    from herness.core.settings import RedactionConfig

__all__ = [
    "DETECTION_ORDER",
    "TOKEN_PATTERN",
    "Detector",
    "EntityType",
    "build_detectors",
    "luhn_valid",
    "normalize_value",
]

type EntityType = Literal[
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
    "CREDENTIAL",
    "URL_TOKEN",
    "EMAIL",
    "CARD",
    "NATIONAL_ID",
    "EMPLOYEE_ID",
    "USER_ID",
    "PHONE",
    "IP",
    "PERSON",
    "CUSTOM",
)
TOKEN_PATTERN: Final = re.compile(
    r"\[(?:EMAIL|PHONE|IP|PERSON|EMPLOYEE_ID|USER_ID|CARD|NATIONAL_ID|CUSTOM)_[0-9a-f]{10}\]"
    r"|\[SECRET\]"
)

type Found = Iterator[tuple[int, int, str]]

_I: Final = re.IGNORECASE
_DIGITS: Final = "0123456789"
_CARD_DIGITS: Final = range(13, 20)  # 13-19 digits
_PHONE_DIGITS: Final = range(9, 16)  # 9-15 digits
_MIN_COLONS: Final = 2
_NANP_DIGITS: Final = 10

# CREDENTIAL (a)-(f), in spec order; the int is the span group (0 = whole match).
_CREDENTIAL: Final[tuple[tuple[re.Pattern[str], int], ...]] = (
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", _I
        ),
        0,
    ),
    (re.compile(r"\bauthorization\s*:\s*(?:bearer|basic)\s+([A-Za-z0-9._~+/=-]+)", _I), 1),
    (
        re.compile(
            r"\b(?:password|passwd|pwd|secret|api[_-]?key|token)\s*[:=]\s*([^\s&\"'<>,;]+)", _I
        ),
        1,
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"), 0),
    (re.compile(r"\bxox[abpors]-[A-Za-z0-9-]{10,200}\b"), 0),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), 0),
    (re.compile(r"AccountKey=([A-Za-z0-9+/=]{20,})", _I), 1),
    (re.compile(r"\b[a-z][a-z0-9+.-]*://([^\s/:@]+:[^\s/@]+)@", _I), 1),
)
_CREDENTIAL_HINTS: Final = ("=", ":", "-----", "AKIA", "gh", "xox", "eyJ", "AccountKey")

_URL: Final = re.compile(r"\bhttps?://[^\s<>\"']+", _I)
_URL_KEYS: Final = frozenset(
    {"token", "access_token", "key", "sig", "signature", "code", "password", "sv", "se"}
)

_EMAIL: Final = re.compile(r"\b[a-z0-9._%+-]{1,64}@[a-z0-9.-]{1,253}\.[a-z]{2,24}\b", _I)
_CARD: Final = re.compile(r"\b(?:[0-9][ -]?){12,18}[0-9]\b")

_PHONE: Final = re.compile(r"(?<![\w+])\+?[0-9][0-9 ().-]{7,20}[0-9](?!\w)")
_PHONE_EXCLUDE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,3}(?:\.[0-9]{1,3}){3}")

_OCTET: Final = r"(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])"
_IPV4: Final = re.compile(rf"\b{_OCTET}(?:\.{_OCTET}){{3}}\b")
_IPV6: Final = re.compile(r"(?<![\w:])[0-9A-Fa-f:]{2,39}(?![\w:])")
_IPV4_LOOSE: Final = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}")

_LAST_FIRST: Final = re.compile(r"^([^,]+),\s*(.+)$")
_UPPER_IDS: Final = frozenset({"EMPLOYEE_ID", "USER_ID", "NATIONAL_ID", "CARD"})


@dataclass(frozen=True)
class Detector:
    """One entity detector: a cheap prefilter and a span finder (U10-36)."""

    type: EntityType
    prefilter: Callable[[str], bool]
    find: Callable[[str], Found]


def _spans(pattern: re.Pattern[str], text: str, group: int = 0) -> Found:
    """Yield non-empty spans of ``group`` for every match of ``pattern``."""
    for match in pattern.finditer(text):
        start, end = match.span(group)
        if start < end:
            yield start, end, text[start:end]


def _ascii_digits(text: str) -> int:
    return sum(text.count(digit) for digit in _DIGITS)


def _has_digit(text: str) -> bool:
    return any(digit in text for digit in _DIGITS)


def _credential_prefilter(text: str) -> bool:
    return any(hint in text for hint in _CREDENTIAL_HINTS)


def _find_credentials(text: str) -> Found:
    for pattern, group in _CREDENTIAL:
        yield from _spans(pattern, text, group)


def _query_values(url: str, offset: int) -> Found:
    """Yield the raw value substring of each secret-named query parameter of ``url``."""
    try:
        query = urlsplit(url).query
    except ValueError:
        return
    if not query:
        return
    pos = offset + url.index("?") + 1
    for piece in query.split("&"):
        name, sep, value = piece.partition("=")
        if sep and value and unquote_plus(name).lower() in _URL_KEYS:
            start = pos + len(name) + 1
            yield start, start + len(value), value
        pos += len(piece) + 1


def _find_url_tokens(text: str) -> Found:
    for match in _URL.finditer(text):
        yield from _query_values(match.group(), match.start())


def _find_cards(text: str) -> Found:
    for start, end, value in _spans(_CARD, text):
        digits = "".join(ch for ch in value if ch in _DIGITS)
        if len(digits) in _CARD_DIGITS and luhn_valid(digits):
            yield start, end, value


def _find_phones(text: str) -> Found:
    for start, end, value in _spans(_PHONE, text):
        if _ascii_digits(value) in _PHONE_DIGITS and not _PHONE_EXCLUDE.search(value):
            yield start, end, value


def _ip_prefilter(text: str) -> bool:
    return ("." in text and _has_digit(text)) or text.count(":") >= _MIN_COLONS


def _is_ipv6(value: str) -> bool:
    if value.count(":") < _MIN_COLONS:
        return False
    try:
        ipaddress.IPv6Address(value)
    except ValueError:
        return False
    return True


def _find_ips(text: str) -> Found:
    yield from _spans(_IPV4, text)
    for start, end, value in _spans(_IPV6, text):
        if _is_ipv6(value):
            yield start, end, value


def _pattern_detector(kind: EntityType, sources: Sequence[str], *, needs_digit: bool) -> Detector:
    """Config-pattern detector (whole match, case-insensitive); empty config never matches."""
    patterns = tuple(re.compile(source, _I) for source in sources)

    def prefilter(text: str) -> bool:
        return bool(patterns) and (not needs_digit or _has_digit(text))

    def find(text: str) -> Found:
        for pattern in patterns:
            yield from _spans(pattern, text)

    return Detector(kind, prefilter, find)


def build_detectors(cfg: RedactionConfig) -> tuple[Detector, ...]:
    """Return the patterned detectors in ``DETECTION_ORDER``, without ``PERSON`` (U10-36)."""
    ids = cfg.id_patterns
    by_type: dict[EntityType, Detector] = {
        "CREDENTIAL": Detector("CREDENTIAL", _credential_prefilter, _find_credentials),
        "URL_TOKEN": Detector("URL_TOKEN", lambda text: "://" in text, _find_url_tokens),
        "EMAIL": Detector("EMAIL", lambda text: "@" in text, lambda text: _spans(_EMAIL, text)),
        "CARD": Detector(
            "CARD", lambda text: _ascii_digits(text) >= _CARD_DIGITS.start, _find_cards
        ),
        "NATIONAL_ID": _pattern_detector("NATIONAL_ID", cfg.national_id_patterns, needs_digit=True),
        "EMPLOYEE_ID": _pattern_detector(
            "EMPLOYEE_ID", ids.get("EMPLOYEE_ID", ()), needs_digit=True
        ),
        "USER_ID": _pattern_detector("USER_ID", ids.get("USER_ID", ()), needs_digit=True),
        "PHONE": Detector(
            "PHONE", lambda text: _ascii_digits(text) >= _PHONE_DIGITS.start, _find_phones
        ),
        "IP": Detector("IP", _ip_prefilter, _find_ips),
        "CUSTOM": _pattern_detector(
            "CUSTOM", tuple(cfg.custom_patterns.values()), needs_digit=False
        ),
    }
    return tuple(by_type[kind] for kind in DETECTION_ORDER if kind != "PERSON")


def luhn_valid(digits: str) -> bool:
    """Luhn checksum over ASCII digits: double every second digit from the right (U10-37)."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2:
            value *= 2
            if value > 9:  # noqa: PLR2004 - Luhn: subtract 9 when > 9
                value -= 9
        total += value
    return total % 10 == 0


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
        candidate = ".".join(str(int(part)) for part in value.split("."))
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
