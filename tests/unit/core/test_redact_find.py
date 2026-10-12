"""Tests for herness.core._redact_find, the anchored span finders (impl 10 T10-11b).

UT10-92 pins every finder to the whole-text ``finditer`` of the module's own spec patterns:
the finders only decide *where* a pattern is tried (literal anchors, digit windows), never
what it matches. The reference functions below are the pre-T10-11b detector bodies.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from herness.core import _redact_find as rf
from herness.core import redact_patterns as rp

pytestmark = pytest.mark.unit

type Found = Iterator[tuple[int, int, str]]

_KEY_TAIL = "PRIVATE " + "KEY-----"  # split so secret scanners see no PEM header
_ATOMS = (
    "0", "1", "2", "5", "9", "4111", "202", "555", "0123", "0101", " ", " ", "-", ".", ":",
    "::", "(", ")", "+", "+1", "@", "a", "e", "f", "Z", "_", "=", "&", "?", ";", ",", "'", '"',
    "/", "://", "http", "https", "token", "password", "PWD", "secret", "api_key", "API-KEY",
    "Authorization", ": Bearer ", "AKIA", "ghp_", "xoxb-", "eyJ", "AccountKey=", "mongodb",
    "-----BEGIN RSA " + _KEY_TAIL, "-----END RSA " + _KEY_TAIL, "example.com", "sig=",
    "\n", "\x00", "\u00e9", "\u0130", "\u0131", "\u017f", "\u212a", "pa\u017fsword", "TO\u212aEN",
    "1234567890123456", "20260925-101500-ABCDEF", "2026-09-24", "10.1.2.3", "fe80::1",
    "INC0012345", "1234 5678 9012 3456", "4111 1111 1111 1111", "E123456", "123-45-6789",
)  # fmt: skip
_texts = st.lists(st.sampled_from(_ATOMS), max_size=40).map("".join)


def _group(matches: Iterable[re.Match[str]], text: str, group: int = 0) -> list[tuple[int, int]]:
    return [(s, e) for s, e, _ in rf._group(matches, text, group)]


def _reference_credentials(text: str) -> list[tuple[int, int]]:
    out = [(s, e) for s, e, _ in rf._find_pem(text)]
    for pattern, group, _keys, _fold in rf._CREDENTIAL:
        out += _group(pattern.finditer(text), text, group)
    out += [(s, e) for s, e, _ in rf._find_jwts(text)]
    out += _group(rf._ACCOUNT_KEY[0].finditer(text), text, 1)
    return out + _group(rf._USERINFO.finditer(text), text, 1)


def _reference_url_tokens(text: str) -> list[tuple[int, int]]:
    return [
        (s, e)
        for m in rf._URL.finditer(text)
        for s, e, _ in rf._query_values(m.group().rstrip("."), m.start())
    ]


def _accepted(found: Iterable[tuple[int, int, str]]) -> list[tuple[int, int]]:
    """First-wins non-overlapping spans (U10-41 step 2), sorted."""
    held: list[tuple[int, int]] = []
    for start, end, _ in found:
        if all(end <= s or start >= e for s, e in held):
            held.append((start, end))
    return sorted(held)


def _reference_cards(text: str) -> Found:
    for m in rp._CARD.finditer(text):
        digits = m.group().replace(" ", "").replace("-", "")
        if len(digits) in rp.CARD_DIGITS and rp.luhn_valid(digits):
            yield m.start(), m.end(), m.group()
    yield from rp._card_cuts(text, 0, len(text), [])


def _reference_phones(text: str) -> Found:
    for m in rp._PHONE.finditer(text):
        start, value = m.start(), m.group()
        while rf.ascii_digits(value) > rp.PHONE_DIGITS[-1] and " " in value:
            value = value[: value.rindex(" ")].rstrip()
        end = start + len(value)
        ids = [b.span() for b in rp._BUILD_ID.finditer(text)]
        if any(s <= start and end <= e for s, e in ids):
            continue
        if rf.ascii_digits(value) in rp.PHONE_DIGITS and not rp._PHONE_EXCLUDE.search(value):
            yield start, end, value


def _reference_ips(text: str) -> list[tuple[int, int]]:
    out = [m.span() for m in rp._IPV4.finditer(text)]
    return out + [m.span() for m in rp._IPV6.finditer(text) if rp._is_ipv6(m.group())]


@settings(max_examples=800)
@given(_texts)
def test_ut10_92_finders_equal_whole_text_finditer(text: str) -> None:
    """UT10-92 each anchored finder yields what finditer over the whole text yields."""
    schemes = [m.start() for m in re.finditer("://", text)]
    assert [(s, e) for s, e, _ in rf._credentials(text, schemes)] == _reference_credentials(text)
    assert [(s, e) for s, e, _ in rf.find_url_tokens(text)] == _reference_url_tokens(text)
    emails = [(s, e) for s, e, _ in rf.find_emails(text)]
    assert emails == [m.span() for m in rf._EMAIL.finditer(text)]
    assert _accepted(rp.find_cards(text)) == _accepted(_reference_cards(text))
    assert _accepted(rp.find_phones(text)) == _accepted(_reference_phones(text))
    assert sorted((s, e) for s, e, _ in rp.find_ips(text)) == sorted(_reference_ips(text))


@settings(max_examples=400)
@given(_texts)
def test_ut10_92_config_patterns_equal_finditer(text: str) -> None:
    """UT10-92 a config pattern with a leading \\b is tried only at its lead's matches."""
    for source in (r"\bE\d{6}\b", r"\b\d{3}-\d{2}-\d{4}\b", r"\bfoo|bar[0-9]", r"\bx*", r"a\b"):
        pattern = re.compile(source, re.IGNORECASE)
        lead = rp._lead(source)
        assert list(rp._led_spans(pattern, lead, text)) == list(rp._spans(pattern, text))


def test_ut10_92_fold_covers_every_case_insensitive_ascii_letter() -> None:
    """UT10-92 exactly the four _FOLD characters match an ASCII letter under IGNORECASE, and
    U+0130 is the only character whose lower() is longer than one character."""
    letter = re.compile("[a-z]", re.IGNORECASE)
    matches = [chr(c) for c in range(0x80, 0x110000) if letter.match(chr(c))]
    assert "".join(matches) == rf._FOLD_CHARS
    assert [c for c in range(0x110000) if len(chr(c).lower()) != 1] == [0x130]
    text = "x\u0130\u0131\u017f\u212aA\u00e9"
    assert rf.folded(text) == "xiisk" + "a\u00e9"
    assert len(rf.folded(text)) == len(text)


@pytest.mark.parametrize("count", [1, 7, 9, 13])
def test_ut10_92_at_least_digits(count: int) -> None:
    """UT10-92 the digit-threshold prefilter equals counting ASCII digits."""
    check = rf.at_least_digits(count)
    for text in ("", "a1b2c3", "1" * (count - 1), "1" * count, "x" + "9 " * count, "\u0661" * 20):
        assert check(text) == (rf.ascii_digits(text) >= count), text
