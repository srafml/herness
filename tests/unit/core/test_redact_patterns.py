"""Tests for herness.core.redact_patterns and the redact value types (impl 10 T10-08).

UT10-36 and UT10-38 name ``scan`` (U10-41, a later card); per the controller ruling they are
driven straight from ``build_detectors``: prefilter, then ``find``, per detector in order, with
the first-wins overlap rule of U10-41 step 2 in the ``_detect`` helper below. They will be
re-pointed at ``Redactor.scan`` when U10-41 lands.
"""

from __future__ import annotations

import dataclasses
import time
from typing import get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from herness.core import redact, redact_patterns
from herness.core.redact import DETECTION_ORDER, RedactionResult, Span
from herness.core.redact_patterns import (
    TOKEN_PATTERN,
    Detector,
    build_detectors,
    luhn_valid,
    normalize_value,
)
from herness.core.settings import RedactionConfig

pytestmark = pytest.mark.unit

_CFG = RedactionConfig(
    id_patterns={"EMPLOYEE_ID": (r"\bE\d{6}\b",), "USER_ID": (r"\bU[0-9]{5}\b",)},
    custom_patterns={"project": r"\bPRJ-[0-9]{4}\b"},
)
_DETECTORS = build_detectors(_CFG)


def _detect(text: str, detectors: tuple[Detector, ...] = _DETECTORS) -> list[tuple[str, str]]:
    """Accept spans in detector order, dropping any that overlap an accepted span."""
    accepted: list[tuple[int, int, str]] = []
    for det in detectors:
        if not det.prefilter(text):
            continue
        for start, end, _value in det.find(text):
            if all(end <= s or start >= e for s, e, _ in accepted):
                accepted.append((start, end, det.type))
    return [(kind, text[s:e]) for s, e, kind in sorted(accepted)]


def _raw(text: str) -> list[tuple[str, int, int, str]]:
    """Every span every detector yields (prefilter applied), no overlap handling."""
    return [
        (det.type, start, end, value)
        for det in _DETECTORS
        if det.prefilter(text)
        for start, end, value in det.find(text)
    ]


# --- UT10-36: types and detector table --------------------------------------------------


def test_ut10_36_entity_types_and_order() -> None:
    """UT10-36 EntityType members, DETECTION_ORDER and the detector order."""
    members = set(get_args(redact.EntityType))
    assert members == {
        "EMAIL", "PHONE", "IP", "PERSON", "EMPLOYEE_ID", "USER_ID", "CREDENTIAL",
        "URL_TOKEN", "CARD", "NATIONAL_ID", "CUSTOM",
    }  # fmt: skip
    assert DETECTION_ORDER == (
        "CREDENTIAL", "URL_TOKEN", "EMAIL", "CARD", "NATIONAL_ID", "EMPLOYEE_ID", "USER_ID",
        "PHONE", "IP", "PERSON", "CUSTOM",
    )  # fmt: skip
    assert redact.EntityType is redact_patterns.EntityType
    assert redact.DETECTION_ORDER is redact_patterns.DETECTION_ORDER
    assert tuple(d.type for d in _DETECTORS) == tuple(t for t in DETECTION_ORDER if t != "PERSON")


def test_ut10_36_value_types() -> None:
    """UT10-36 Span orders by start and is frozen; RedactionResult is frozen."""
    a = Span(5, 9, "EMAIL", "[EMAIL_0123456789]")
    b = Span(1, 3, "PHONE", "[PHONE_0123456789]")
    assert sorted([a, b]) == [b, a]
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.start = 0  # type: ignore[misc]
    res = RedactionResult("x", {"EMAIL": 1})
    assert res.counts == {"EMAIL": 1}
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.text = "y"  # type: ignore[misc]
    det = _DETECTORS[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        det.type = "EMAIL"  # type: ignore[misc]


_POSITIVE: list[tuple[str, str, str]] = [
    # CREDENTIAL: PEM, authorization, key=value, known formats, AccountKey, URL user info
    (
        "k -----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY----- z",
        "CREDENTIAL",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    ),
    ("Authorization: Bearer abc.DEF-123_x", "CREDENTIAL", "abc.DEF-123_x"),
    ("authorization : basic dXNlcjpwYXNz", "CREDENTIAL", "dXNlcjpwYXNz"),
    ("password=hunter2&x=1", "CREDENTIAL", "hunter2"),
    ("API-KEY: s3cr3t,", "CREDENTIAL", "s3cr3t"),
    ("pwd = x1y2", "CREDENTIAL", "x1y2"),
    ("token:abc", "CREDENTIAL", "abc"),
    ("key AKIAABCDEFGHIJKLMNOP here", "CREDENTIAL", "AKIAABCDEFGHIJKLMNOP"),
    ("ghp_" + "a" * 36, "CREDENTIAL", "ghp_" + "a" * 36),
    ("xoxb-1234567890-abc", "CREDENTIAL", "xoxb-1234567890-abc"),
    ("jwt eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM", "CREDENTIAL", "eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM"),
    ("AccountKey=" + "A1b2+/=" * 4 + ";", "CREDENTIAL", "A1b2+/=" * 4),
    ("mongodb://svc:Pa55word@db.local/x", "CREDENTIAL", "svc:Pa55word"),
    # URL_TOKEN
    ("see https://h.example/p?sig=AbC%2F&x=1 now", "URL_TOKEN", "AbC%2F"),
    ("https://h.example/p?a=1&SV=2020-01-01#frag", "URL_TOKEN", "2020-01-01"),
    ("http://h.example/?access_token=zz9", "URL_TOKEN", "zz9"),
    # EMAIL
    ("mail Jane.Doe+x@Corp.Example.com.", "EMAIL", "Jane.Doe+x@Corp.Example.com"),
    # CARD
    ("card 4111 1111 1111 1111 exp", "CARD", "4111 1111 1111 1111"),
    ("card 4111-1111-1111-1111", "CARD", "4111-1111-1111-1111"),
    ("pan 4012888888881881", "CARD", "4012888888881881"),
    # NATIONAL_ID, EMPLOYEE_ID, USER_ID, CUSTOM (config patterns)
    ("ssn 123-45-6789 on file", "NATIONAL_ID", "123-45-6789"),
    ("emp E123456 left", "EMPLOYEE_ID", "E123456"),
    ("emp e123456 left", "EMPLOYEE_ID", "e123456"),
    ("user U12345", "USER_ID", "U12345"),
    ("ticket PRJ-0042", "CUSTOM", "PRJ-0042"),
    # PHONE
    ("call +1 415 555 0142", "PHONE", "+1 415 555 0142"),
    ("call (415) 555-0142.", "PHONE", "415) 555-0142"),
    ("tel 020 7946 0958", "PHONE", "020 7946 0958"),
    # IP
    ("host 10.1.2.3 up", "IP", "10.1.2.3"),
    ("host 255.255.255.255", "IP", "255.255.255.255"),
    ("v6 2001:db8::1 up", "IP", "2001:db8::1"),
    ("v6 fe80::1:2", "IP", "fe80::1:2"),
]


@pytest.mark.parametrize(("text", "kind", "matched"), _POSITIVE)
def test_ut10_36_positive(text: str, kind: str, matched: str) -> None:
    """UT10-36 each positive string yields the expected span and type."""
    assert (kind, matched) in _detect(text)


_NEGATIVE: list[tuple[str, str]] = [
    ("-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----", "CREDENTIAL"),
    ("akiaabcdefghijklmnop", "CREDENTIAL"),
    ("ghp_short", "CREDENTIAL"),
    ("passwords are rotated", "CREDENTIAL"),
    ("https://h.example/p?page=2&id=7", "URL_TOKEN"),
    ("https://h.example/token/abc", "URL_TOKEN"),
    ("user at example dot com", "EMAIL"),
    ("a@b", "EMAIL"),
    ("4111 1111 1111 1112", "CARD"),
    ("411111111111", "CARD"),
    ("12345678901234567890", "CARD"),
    ("123-456-789", "NATIONAL_ID"),
    ("E1234567", "EMPLOYEE_ID"),
    ("U1234", "USER_ID"),
    ("XU12345", "USER_ID"),
    ("U123456", "USER_ID"),
    ("PRJ-42", "CUSTOM"),
    ("XPRJ-0042", "CUSTOM"),
    ("INC0012345", "PHONE"),
    ("CHG0012345", "PHONE"),
    ("12345678", "PHONE"),
    ("1234567890123456", "PHONE"),
    ("at 2026-09-24 21:14", "PHONE"),
    ("10.1.2.3 55512", "PHONE"),
    ("x12345678901", "PHONE"),
    ("256.1.2.3", "IP"),
    ("1.2.3", "IP"),
    ("12:30:45", "IP"),
    ("std::vector", "IP"),
    ("deadbeef:cafe", "IP"),
]


@pytest.mark.parametrize(("text", "kind"), _NEGATIVE)
def test_ut10_36_negative(text: str, kind: str) -> None:
    """UT10-36 each negative string yields no span of the given type."""
    assert all(k != kind for k, _ in _detect(text))


def test_ut10_36_prefilters() -> None:
    """UT10-36 prefilters are false for text that cannot match and true for text that can."""
    by_type = {d.type: d for d in _DETECTORS}
    assert not by_type["CREDENTIAL"].prefilter("plain words only")
    assert not by_type["URL_TOKEN"].prefilter("no scheme here")
    assert not by_type["EMAIL"].prefilter("no at sign")
    assert not by_type["CARD"].prefilter("123456789012")
    assert by_type["CARD"].prefilter("1234567890123")
    assert not by_type["PHONE"].prefilter("12345678")
    assert by_type["PHONE"].prefilter("123456789")
    assert not by_type["IP"].prefilter("a.b and c:d")
    assert by_type["IP"].prefilter("a::b")
    assert by_type["IP"].prefilter("v1.2")
    assert not by_type["EMPLOYEE_ID"].prefilter("no digits")
    assert by_type["CUSTOM"].prefilter("anything")
    # Non-ASCII digits do not count towards the ASCII digit thresholds (ruling 1).
    assert not by_type["PHONE"].prefilter(chr(0x0661) * 20)


def test_ut10_36_find_values_and_groups() -> None:
    """UT10-36 find yields (start, end, value) with the value equal to the span text."""
    text = "Authorization: Bearer tok123 and a@b.co"
    for kind, start, end, value in _raw(text):
        assert 0 <= start < end <= len(text)
        assert value == text[start:end], kind


def test_ut10_36_non_ascii_digits_ignored() -> None:
    """UT10-36 card and phone patterns match ASCII digits only (ruling 1)."""
    arabic = (chr(0x0664) + chr(0x0661) * 3 + " ") * 4  # Arabic-Indic "4111 " x4
    assert _detect(arabic) == []


def test_ut10_36_empty_config_detectors() -> None:
    """UT10-36 empty USER_ID and CUSTOM config yield nothing; zero-length matches skipped."""
    dets = build_detectors(
        RedactionConfig(
            id_patterns={"EMPLOYEE_ID": (), "USER_ID": ()},
            national_id_patterns=(r"x*",),
        )
    )
    by_type = {d.type: d for d in dets}
    assert not by_type["USER_ID"].prefilter("U12345")
    assert list(by_type["CUSTOM"].find("PRJ-0042")) == []
    assert list(by_type["NATIONAL_ID"].find("1 yy 2")) == []
    assert list(by_type["NATIONAL_ID"].find("1 xx 2")) == [(2, 4, "xx")]


def test_ut10_36_url_token_edge_cases() -> None:
    """UT10-36 URL_TOKEN skips empty values, bare names and URLs without a query."""
    text = "https://h.example/p?token=&key&code=ok#sig=no https://h.example/plain"
    assert [(d.type, v) for d in _DETECTORS[1:2] for _, _, v in d.find(text)] == [
        ("URL_TOKEN", "ok")
    ]


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("http://[bad/?sig=SECRETSIG", "SECRETSIG"),
        ("http://[::1/?code=abc", "abc"),
        ("http://h]/?key=K1", "K1"),
        ("see http://h]/p?x=1&access_token=AT9#sig=frag", "AT9"),
    ],
)
def test_ut10_36_url_token_fails_closed_on_malformed_url(text: str, value: str) -> None:
    """UT10-36 a URL urlsplit rejects still yields its secret query values (fail closed)."""
    by_type = {d.type: d for d in _DETECTORS}
    found = [(text[s:e], v) for s, e, v in by_type["URL_TOKEN"].find(text)]
    assert found == [(value, value)]


def test_ut10_36_malformed_inputs_yield_nothing() -> None:
    """UT10-36 a malformed URL without a query, or with '#' first, and a one-colon IPv6."""
    by_type = {d.type: d for d in _DETECTORS}
    assert list(by_type["URL_TOKEN"].find("http://[bad/p#x?token=abc")) == []
    assert list(by_type["URL_TOKEN"].find("http://[bad/p")) == []
    assert list(by_type["IP"].find("ab:cd and 1::g")) == []


@pytest.mark.parametrize(
    "ssn",
    [
        "".join(chr(0x0660 + int(c)) if c.isdigit() else c for c in "123-45-6789"),
        "".join(chr(0xFF10 + int(c)) if c.isdigit() else c for c in "123-45-6789"),
    ],
    ids=["arabic_indic", "fullwidth"],
)
def test_ut10_36_config_patterns_unicode_digits(ssn: str) -> None:
    """UT10-36 config-pattern prefilter admits Unicode digits that the config regex matches."""
    text = f"id {ssn} on file"
    assert ("NATIONAL_ID", ssn) in _detect(text)


def test_ut10_36_pem_multiple_blocks_and_unterminated() -> None:
    """UT10-36 PEM: each BEGIN to the nearest END; a trailing BEGIN without END is ignored."""
    one = "-----BEGIN PRIVATE KEY-----\nA\n-----END PRIVATE KEY-----"
    two = "-----BEGIN EC PRIVATE KEY-----\nB\n-----END EC PRIVATE KEY-----"
    text = f"{one} mid {two} -----BEGIN PRIVATE KEY----- tail"
    pem = [text[s:e] for s, e, _ in _DETECTORS[0].find(text) if text[s:e].startswith("-----")]
    assert pem == [one, two]


_ADVERSARIAL = {
    "pem_begin_no_end": "-----BEGIN PRIVATE KEY-----\n" * 20_000,
    "jwt_runs": "eyJ-" * 100_000,
    "url_scheme": "a." * 200_000,
    "url_userinfo": "a://" + "b:" * 100_000,
    "phone_digits": "1 " * 200_000,
    "email_dots": "a@" + "b." * 200_000,
}


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
def test_ut10_36_detectors_linear_on_adversarial_input(name: str) -> None:
    """UT10-36 every detector finishes adversarial 400-540 KB inputs well under a second."""
    text = _ADVERSARIAL[name]
    started = time.perf_counter()
    for det in _DETECTORS:
        if det.prefilter(text):
            for _ in det.find(text):
                pass
    assert time.perf_counter() - started < 1.0


def test_ut10_36_pem_block_wins_over_inner_credentials() -> None:
    """UT10-36 a PEM block is yielded before inner key=value credentials."""
    pem = "-----BEGIN PRIVATE KEY-----\npassword=abc\n-----END PRIVATE KEY-----"
    assert _detect(pem) == [("CREDENTIAL", pem)]


# --- UT10-38 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "INC0012345, 2026-09-24 21:14, 10.1.2.3, +1 415 555 0142",
        "INC0012345\n2026-09-24 21:14\n10.1.2.3\n+1 415 555 0142",
    ],
)
def test_ut10_38_ticket_date_ip_phone(text: str) -> None:
    """UT10-38 only the phone is detected as PHONE; ticket and date yield nothing."""
    found = _detect(text)
    assert [m for k, m in found if k == "PHONE"] == ["+1 415 555 0142"]
    assert [k for k, _ in found if k not in {"PHONE", "IP"}] == []
    # 10.1.2.3 is an IP (not a phone); IP detection is its own row (UT10-41).
    assert [m for k, m in found if k == "IP"] == ["10.1.2.3"]


@pytest.mark.parametrize("text", ["INC0012345", "2026-09-24 21:14", "+1 415 555 0142"])
def test_ut10_38_each_alone(text: str) -> None:
    """UT10-38 each input alone: only the phone yields a span."""
    expected = [("PHONE", text)] if text.startswith("+") else []
    assert _detect(text) == expected


# --- UT10-39 (normalize_value rows) and U10-37 ------------------------------------------


def test_ut10_39_person_formats_normalize_equal() -> None:
    """UT10-39 Jane Doe, Doe, Jane and JANE  DOE normalize to one value."""
    values = {normalize_value("PERSON", v) for v in ("Jane Doe", "Doe, Jane", "JANE  DOE")}
    assert values == {"jane doe"}
    assert normalize_value("PERSON", "  Doe ,\tJane  ") == "jane doe"


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("EMAIL", "  Jane.Doe@Corp.COM ", "jane.doe@corp.com"),
        ("PHONE", "(415) 555-0142", "+14155550142"),
        ("PHONE", "+1 415 555 0142", "+14155550142"),
        ("PHONE", "+44 20 7946 0958", "+442079460958"),
        ("PHONE", "+415 555 0142", "+4155550142"),
        ("PHONE", "020 7946 0958", "+02079460958"),
        ("EMPLOYEE_ID", "e 123-456", "E123456"),
        ("USER_ID", "u-12345", "U12345"),
        ("NATIONAL_ID", "123 45-6789", "123456789"),
        ("CARD", "4111-1111 1111-1111", "4111111111111111"),
        ("IP", "2001:DB8:0:0::1", "2001:db8::1"),
        ("IP", "10.1.2.3", "10.1.2.3"),
        ("IP", "01.2.3.004", "1.2.3.4"),
        ("IP", "not-an-ip", "not-an-ip"),
        ("IP", "999.1.2.3", "999.1.2.3"),
        ("CUSTOM", " PRJ-0042 ", " PRJ-0042 "),
        ("CREDENTIAL", "abc", "abc"),
    ],
)
def test_ut10_39_normalize_value(kind: str, value: str, expected: str) -> None:
    """UT10-39 normalize_value rules per type (U10-37)."""
    assert normalize_value(kind, value) == expected  # type: ignore[arg-type]


def test_ut10_39_luhn_known_values() -> None:
    """UT10-39 luhn_valid on known card numbers (U10-37)."""
    assert luhn_valid("4111111111111111")
    assert luhn_valid("79927398713")
    assert not luhn_valid("79927398710")
    assert luhn_valid("0")


# --- PT10-04 ------------------------------------------------------------------------------


def _reference_luhn(digits: str) -> bool:
    """Reference Luhn via the doubled-digit table, independent of the unit's loop."""
    doubled = (0, 2, 4, 6, 8, 1, 3, 5, 7, 9)
    nums = [int(c) for c in digits][::-1]
    total = sum(nums[0::2]) + sum(doubled[n] for n in nums[1::2])
    return total % 10 == 0


@settings(max_examples=500)
@given(st.text(alphabet="0123456789", min_size=0, max_size=40))
def test_pt10_04_luhn_matches_reference(digits: str) -> None:
    """PT10-04 luhn_valid equals a reference implementation for random digit strings."""
    assert luhn_valid(digits) == _reference_luhn(digits)


# --- Controller verification: idempotence over existing tokens (impl 00 deviation 10) ----

_TOKEN_NAMES = ("EMAIL", "PHONE", "IP", "PERSON", "EMPLOYEE_ID", "USER_ID", "CARD",
                "NATIONAL_ID", "CUSTOM")  # fmt: skip
_tokens = st.one_of(
    st.just("[SECRET]"),
    st.builds(
        lambda n, h: f"[{n}_{h}]",
        st.sampled_from(_TOKEN_NAMES),
        st.text(alphabet="0123456789abcdef", min_size=10, max_size=10),
    ),
)
# Surrounding text without ':', '=', '@', '/', '?', '&': those only bring a token inside a
# credential or URL value group, which the Redactor's protected ranges cover (U10-41 step 1).
_filler = st.text(alphabet="abcXYZ0123456789 .,-()+_\n", max_size=30)


def _hits_token(text: str) -> list[tuple[str, int, int, str]]:
    ranges = [m.span() for m in TOKEN_PATTERN.finditer(text)]
    return [hit for hit in _raw(text) if any(hit[1] < e and hit[2] > s for s, e in ranges)]


@settings(max_examples=400)
@given(_filler, _tokens, _filler, _tokens, _filler)
def test_cv_t10_08_no_span_inside_existing_token(a: str, t1: str, b: str, t2: str, c: str) -> None:
    """CV-T10-08 built-in detectors yield no span overlapping an existing pseudonym token."""
    assert _hits_token(a + t1 + b + t2 + c) == []


@pytest.mark.parametrize(
    "text",
    [
        "[PHONE_0123456789]",
        "call [PHONE_0123456789] or [EMAIL_abcdef0123]",
        "ip [IP_0000000000], card [CARD_4111111111], id [NATIONAL_ID_1234567890]",
        "+1 [PHONE_4155550142] 4155550142",
        "mail [EMAIL_abcdef0123]@corp.example.com",
        "[SECRET] [PERSON_abcdefabcd] [EMPLOYEE_ID_0123456789]",
    ],
)
def test_cv_t10_08_known_tokens_untouched(text: str) -> None:
    """CV-T10-08 typical scrubbed log lines: no detector span overlaps a token."""
    assert _hits_token(text) == []


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("token: [SECRET]", "CREDENTIAL"),
        ("password=[PHONE_0123456789]", "CREDENTIAL"),
        ("https://h.example/?sig=[SECRET]", "URL_TOKEN"),
    ],
)
def test_cv_t10_08_credential_value_group_covers_token(text: str, kind: str) -> None:
    """CV-T10-08 known exception: a credential or URL value group can be an existing token.

    The detectors alone yield it; U10-41 step 1 (protected TOKEN_PATTERN ranges) drops it.
    """
    assert [h[0] for h in _hits_token(text)] == [kind]
