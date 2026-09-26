"""Reserved-range PII span injection and the made-up name list (U11-06, design §5.1.4).

TH11-01: every value comes from a reserved or fictional range, so no real personal data can
appear: `example.com`/`example.org` emails, `+1-202-555-01NN` phones (R-56), documentation
IP ranges, `4111` test cards and `synthetic`-prefixed credentials and tokens (DD11-18).
Names are invented syllable compounds.
"""

import dataclasses
import string
from collections.abc import Callable, Sequence
from typing import Final, Literal

import numpy as np

from tools.synth.params import SynthUsageError
from tools.synth.rng import stream_rng

type PiiType = Literal[
    "PERSON", "EMAIL", "PHONE", "IP", "EMPLOYEE_ID", "CARD", "CREDENTIAL", "URL_TOKEN"
]

STREAM_NAMES: Final = "names"
NAME_COUNT: Final = 500
MAX_SPANS: Final = 3

_TYPES: Final[tuple[PiiType, ...]] = (
    "PERSON",
    "EMAIL",
    "PHONE",
    "IP",
    "EMPLOYEE_ID",
    "CARD",
    "CREDENTIAL",
    "URL_TOKEN",
)
# 10 x 10 invented syllable compounds give 100 first and 100 last names.
_FIRST_HEADS: Final = ("Ka", "Lo", "Mi", "Ta", "Zo", "Ve", "Ri", "Na", "Bo", "Se")
_FIRST_TAILS: Final = ("ran", "lia", "vek", "dor", "mina", "tas", "reo", "lin", "zar", "phe")
_LAST_HEADS: Final = (
    "Quor",
    "Bram",
    "Tesk",
    "Vold",
    "Marn",
    "Frel",
    "Grom",
    "Hask",
    "Pell",
    "Dros",
)
_LAST_TAILS: Final = ("wick", "stone", "ley", "berg", "ov", "ane", "ford", "ix", "holm", "ard")
_FIRST_NAMES: Final = tuple(head + tail for head in _FIRST_HEADS for tail in _FIRST_TAILS)
_LAST_NAMES: Final = tuple(head + tail for head in _LAST_HEADS for tail in _LAST_TAILS)

_PHRASES: Final = ("contact {v}", "reported by {v}", "from host {v}")
_PHONE_FORMATS: Final = ("+1-202-555-01{nn}", "+1 202 555 01{nn}", "(202) 555-01{nn}")
_EMAIL_DOMAINS: Final = ("example.com", "example.org")
_IP_PREFIXES: Final = ("192.0.2.", "198.51.100.")
_ALNUM: Final = string.ascii_letters + string.digits
_HEX: Final = "0123456789abcdef"
_TICKET_PREFIXES: Final = ("INC", "CHG")
_SENTENCE_ENDS: Final = frozenset(".!?")
_CARD_PREFIX: Final = "4111"
_CARD_LENGTH: Final = 16
_LUHN_DOUBLE_LIMIT: Final = 9
_TICKET_SHARE: Final = 0.5


@dataclasses.dataclass(frozen=True, slots=True)
class PiiSpan:
    """One injected PII value: `text[start:end]` of field `field` is a value of `type`."""

    field: str
    start: int
    end: int
    type: PiiType


def build_name_list(seed: int) -> tuple[tuple[str, str], ...]:
    """Return 500 distinct invented (first, last) pairs drawn from `stream_rng(seed, "names")`."""
    rng = stream_rng(seed, STREAM_NAMES)
    picks = rng.choice(len(_FIRST_NAMES) * len(_LAST_NAMES), size=NAME_COUNT, replace=False)
    width = len(_LAST_NAMES)
    return tuple((_FIRST_NAMES[int(i) // width], _LAST_NAMES[int(i) % width]) for i in picks)


def luhn_valid(digits: str) -> bool:
    """Return True when `digits` is a non-empty ASCII digit string with a valid Luhn sum."""
    if not digits or not all(c in string.digits for c in digits):
        return False
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > _LUHN_DOUBLE_LIMIT:
                value -= 9
        total += value
    return total % 10 == 0


def _pick(rng: np.random.Generator, items: Sequence[str]) -> str:
    return items[int(rng.integers(len(items)))]


def _chars(rng: np.random.Generator, alphabet: str, count: int) -> str:
    return "".join(alphabet[int(i)] for i in rng.integers(len(alphabet), size=count))


def _card(rng: np.random.Generator) -> str:
    body = _CARD_PREFIX + _chars(rng, string.digits, _CARD_LENGTH - len(_CARD_PREFIX) - 1)
    # Exactly one of the ten check digits completes a valid Luhn sum.
    return next(body + d for d in string.digits if luhn_valid(body + d))


def _person(rng: np.random.Generator, names: Sequence[tuple[str, str]]) -> str:
    first, last = names[int(rng.integers(len(names)))]
    return _pick(rng, (f"{first} {last}", f"{last}, {first}", f"{first[0]}. {last}"))


def _email(rng: np.random.Generator, names: Sequence[tuple[str, str]]) -> str:
    first, last = names[int(rng.integers(len(names)))]
    return f"{first.lower()}.{last.lower()}@{_pick(rng, _EMAIL_DOMAINS)}"


def _phone(rng: np.random.Generator, _: Sequence[tuple[str, str]]) -> str:
    return _pick(rng, _PHONE_FORMATS).format(nn=f"{int(rng.integers(100)):02d}")


def _ip(rng: np.random.Generator, _: Sequence[tuple[str, str]]) -> str:
    return f"{_pick(rng, _IP_PREFIXES)}{int(rng.integers(1, 255))}"


def _employee(rng: np.random.Generator, _: Sequence[tuple[str, str]]) -> str:
    return f"E{int(rng.integers(1_000_000)):06d}"


def _credential(rng: np.random.Generator, _: Sequence[tuple[str, str]]) -> str:
    return "password=synthetic" + _chars(rng, _ALNUM, 8)


def _url_token(rng: np.random.Generator, _: Sequence[tuple[str, str]]) -> str:
    return "https://portal.example.com/x?token=synthetic" + _chars(rng, _HEX, 16)


_RENDERERS: Final[
    dict[PiiType, Callable[[np.random.Generator, Sequence[tuple[str, str]]], str]]
] = {
    "PERSON": _person,
    "EMAIL": _email,
    "PHONE": _phone,
    "IP": _ip,
    "EMPLOYEE_ID": _employee,
    "CARD": lambda rng, _: _card(rng),
    "CREDENTIAL": _credential,
    "URL_TOKEN": _url_token,
}


def _boundaries(text: str, spans: Sequence[PiiSpan]) -> list[int]:
    """Sentence boundaries of `text`: 0, the end, and after `.!?` + whitespace; never in a span."""
    points = {0, len(text)}
    for i, char in enumerate(text):
        if char in _SENTENCE_ENDS and (i + 1 == len(text) or text[i + 1].isspace()):
            points.add(i + 1)
    return sorted(p for p in points if not any(s.start < p < s.end for s in spans))


def _insert(text: str, spans: list[PiiSpan], at: int, piece: str) -> tuple[str, list[PiiSpan]]:
    """Insert `piece` at `at`, shifting spans that start at or after it."""
    shift = len(piece)
    moved = [
        dataclasses.replace(s, start=s.start + shift, end=s.end + shift) if s.start >= at else s
        for s in spans
    ]
    return text[:at] + piece + text[at:], moved


def inject_pii(
    text: str,
    field: str,
    rng: np.random.Generator,
    names: Sequence[tuple[str, str]],
    *,
    n_spans: int,
) -> tuple[str, list[PiiSpan]]:
    """Insert `n_spans` reserved-range PII values at sentence boundaries and record them."""
    if not 1 <= n_spans <= MAX_SPANS:
        msg = "n_spans must be in 1..3"
        raise SynthUsageError(msg, key="n_spans")
    if not names:
        msg = "names must not be empty"
        raise SynthUsageError(msg, key="names")
    spans: list[PiiSpan] = []
    for index in rng.integers(len(_TYPES), size=n_spans):
        kind = _TYPES[int(index)]
        value = _RENDERERS[kind](rng, names)
        prefix, _, suffix = _pick(rng, _PHRASES).partition("{v}")
        points = _boundaries(text, spans)
        at = points[int(rng.integers(len(points)))]
        lead = "" if at == 0 else " "
        piece = f"{lead}{prefix.capitalize()}{value}{suffix}." + (" " if at == 0 else "")
        start = at + len(lead) + len(prefix)
        text, spans = _insert(text, spans, at, piece)
        spans.append(PiiSpan(field=field, start=start, end=start + len(value), type=kind))
    if rng.random() < _TICKET_SHARE:
        anchor = spans[int(rng.integers(len(spans)))]
        ticket = f"{_pick(rng, _TICKET_PREFIXES)}{int(rng.integers(10_000_000)):07d}"
        text, spans = _insert(text, spans, anchor.end, f" {ticket}")
    return text, spans


__all__ = [
    "MAX_SPANS",
    "NAME_COUNT",
    "STREAM_NAMES",
    "PiiSpan",
    "PiiType",
    "build_name_list",
    "inject_pii",
    "luhn_valid",
]
