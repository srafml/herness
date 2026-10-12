"""Span-quality fixes and detector gaps of the redactor (impl 10 T10-11b).

UT10-93 SN-1 (secret URL query values are URL_TOKEN), UT10-94 SN-3a (no trailing period),
UT10-95 SN-3b (`(area) number` keeps its parenthesis), UT10-96 SN-2 (`F. Last` from the
display name), UT10-97 account/card-shaped digit runs, UT10-98 7-digit local phones and
UT10-99 the verbatim-safe shapes that stay unmasked. Values are synthetic: reserved
`202-555-01xx` style numbers, `synthetic` secrets, invented names, `4111` test cards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from herness.core.redact import Redactor
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig

pytestmark = pytest.mark.unit

_RED = Redactor(RedactionConfig(directory_file=None), bytes(range(32)))
_QNAME = "to" + "ken"  # built at runtime: detect-secrets keeps quiet


def _found(text: str) -> list[tuple[str, str]]:
    return [(span.type, text[span.start : span.end]) for span in _RED.scan(text)]


# --- UT10-93 SN-1: a secret-named URL query value is URL_TOKEN, not CREDENTIAL -----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"see https://portal.example.com/x?{_QNAME}=synthetic0a1b2c3d now",
            [("URL_TOKEN", "synthetic0a1b2c3d")],
        ),
        (
            "https://h.example/p?pass" + "word=synthetic1&x=1",
            [("URL_TOKEN", "synthetic1")],
        ),
        (f"{_QNAME}=synthetic99 outside any URL", [("CREDENTIAL", "synthetic99")]),
        (
            f"https://h.example/cb?code=x&{_QNAME}=synthetic7 and {_QNAME}=synthetic8",
            [("URL_TOKEN", "x"), ("URL_TOKEN", "synthetic7"), ("CREDENTIAL", "synthetic8")],
        ),
    ],
)
def test_ut10_93_url_query_secret_is_url_token(text: str, expected: list[tuple[str, str]]) -> None:
    """UT10-93 CREDENTIAL leaves a value that URL_TOKEN takes to URL_TOKEN (SN-1)."""
    assert _found(text) == expected
    assert all(span.replacement == "[SECRET]" for span in _RED.scan(text))


def test_ut10_93_credential_wider_than_the_query_value_stays_credential() -> None:
    """UT10-93 a `key=value#frag` credential reaching past the query value keeps CREDENTIAL."""
    text = f"https://h.example/p?{_QNAME}=synthetic5#frag"
    assert _found(text) == [("CREDENTIAL", "synthetic5#frag")]


# --- UT10-94 SN-3a: a sentence-ending period is not part of a secret value ---------------

_PW = "pass" + "word"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"Contact {_PW}=synthetic12345678. Next", [("CREDENTIAL", "synthetic12345678")]),
        (f"set {_PW}=synthetic1.", [("CREDENTIAL", "synthetic1")]),
        ("Authorization: Bearer abc.DEF-1..", [("CREDENTIAL", "abc.DEF-1")]),
        (f"keep {_PW}=a.b.c inside", [("CREDENTIAL", "a.b.c")]),
        (
            f"From host https://portal.example.com/x?{_QNAME}=synthetic0a1b. Next",
            [("URL_TOKEN", "synthetic0a1b")],
        ),
        (
            f"https://h.example/p?sig=AbC.&{_QNAME}=v.w.",
            [("URL_TOKEN", "AbC."), ("URL_TOKEN", "v.w")],
        ),
    ],
)
def test_ut10_94_trailing_period_is_not_masked(text: str, expected: list[tuple[str, str]]) -> None:
    """UT10-94 CREDENTIAL and URL_TOKEN spans end before a trailing period (SN-3a)."""
    assert _found(text) == expected


# --- UT10-95 SN-3b: `(area) number` is masked with its opening parenthesis ----------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Contact (202) 555-0101 (desk).", [("PHONE", "(202) 555-0101")]),
        ("call (415) 555-0142.", [("PHONE", "(415) 555-0142")]),
        ("x((202) 555-0177", [("PHONE", "(202) 555-0177")]),
        ("(0) 20 7946 0958", [("PHONE", "(0) 20 7946 0958")]),
        ("tel (+1 202 555 0101)", [("PHONE", "+1 202 555 0101")]),
        ("ref (2025550123)", [("PHONE", "2025550123")]),
        ("id(202) 555-0101", [("PHONE", "202) 555-0101")]),
        ("built (20260925-101500-ABCDEF)", []),
    ],
)
def test_ut10_95_area_code_parenthesis_is_masked(
    text: str, expected: list[tuple[str, str]]
) -> None:
    """UT10-95 `(` opens a PHONE span only before a 1-4 digit area code and `)` (SN-3b)."""
    assert _found(text) == expected


def test_ut10_95_parenthesis_does_not_change_the_pseudonym() -> None:
    """UT10-95 `(202) 555-0101` and `202-555-0101` normalize to one pseudonym."""
    assert _RED.pseudonym("PHONE", "(202) 555-0101") == _RED.pseudonym("PHONE", "202-555-0101")


# --- UT10-96 SN-2: the directory holds the `F. Last` form of each display name ------------


_NL = chr(10)


def _directory(tmp_path: Path, *names: str) -> NameDirectory:
    path = tmp_path / "directory.csv"
    lines = ["display_name,alt_names", *(f"{name}," for name in names)]
    path.write_text("".join(line + _NL for line in lines), encoding="utf-8")
    return NameDirectory.from_files(path, (), None)


def test_ut10_96_initial_and_last_name_is_person(tmp_path: Path) -> None:
    """UT10-96 `F. Last` is a variant of display name `First Last`, with its canonical name."""
    names = _directory(tmp_path, "Kalia Bramford", "Mi Dor Quorwick")
    text = "Reported by K. Bramford. Seen by M. Quorwick and k. bramford"
    assert names.find(text) == [
        (12, 23, "kalia bramford"),
        (33, 44, "mi dor quorwick"),
        (49, 60, "kalia bramford"),
    ]
    assert names.variant_count == 8  # 4 variants per display name


def test_ut10_96_initial_variant_shares_the_person_token(tmp_path: Path) -> None:
    """UT10-96 `F. Last`, `First Last` and `Last, First` redact to one PERSON token."""
    red = Redactor(
        RedactionConfig(directory_file=None), bytes(32), _directory(tmp_path, "Kalia Bramford")
    )
    tokens = {
        red.redact(text).text for text in ("K. Bramford", "Kalia Bramford", "Bramford, Kalia")
    }  # type: ignore[union-attr]
    assert len(tokens) == 1


def test_ut10_96_display_names_file_gets_the_initial_variant(tmp_path: Path) -> None:
    """UT10-96 lines of the display-names cache get the `F. Last` variant as well."""
    cache = tmp_path / "display_names.txt"
    cache.write_text("Sela Grompell" + _NL, encoding="utf-8")
    names = NameDirectory.from_files(None, (), cache)
    assert names.find("by S. Grompell.") == [(3, 14, "sela grompell")]
    assert names.find("by S Grompell") == []


# --- UT10-97 account/card-shaped digit runs (T09-15d addendum) ------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("acct 1234567890123456 closed", [("CARD", "1234567890123456")]),
        ("acct 12345678901234567.", [("CARD", "12345678901234567")]),
        ("iban 9876543210987654321 x", [("CARD", "9876543210987654321")]),
        ("acct 1234567890123456 2026", [("CARD", "1234567890123456")]),
        ("pan 4111111111111111", [("CARD", "4111111111111111")]),
        ("ref 1234567890123", [("PHONE", "1234567890123")]),
        ("ref 123456789012345", [("PHONE", "123456789012345")]),
    ],
)
def test_ut10_97_long_digit_runs_are_masked(text: str, expected: list[tuple[str, str]]) -> None:
    """UT10-97 a 16-19 digit run is CARD (Luhn-optional); 13-15 digit runs are PHONE."""
    assert _found(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "id 12345678901234567890",  # 20 digits: an id or a 64-bit unsigned integer
        "sys_id 23554300000000000000000000128668",
        "min -9223372036854775807::BIGINT",
        "ratio 1.1234567890123456",
        "value 1234567890123456.5",
        "key prometheus:0000000000000004",
        "query q_1234567890123456",
        "range 1234567890123456-7",
    ],
)
def test_ut10_97_other_long_numbers_stay(text: str) -> None:
    """UT10-97 longer runs, zero-padded keys, fractions, signed ints and ids are not CARD."""
    assert _found(text) == []


def test_ut10_97_account_pseudonym_ignores_spacing() -> None:
    """UT10-97 an account run normalizes like a card: one token per digit string."""
    assert _RED.pseudonym("CARD", "1234567890123456") == _RED.pseudonym(
        "CARD", "1234 5678 9012 3456"
    )


# --- UT10-98 7-digit local phone numbers (T09-15d addendum) --------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("call 555-0123 now", [("PHONE", "555-0123")]),
        ("desk (555-0199).", [("PHONE", "555-0199")]),
        ("Contact 867-5309, then", [("PHONE", "867-5309")]),
        ("Contact (202) 555-0101", [("PHONE", "(202) 555-0101")]),
    ],
)
def test_ut10_98_local_phone_is_masked(text: str, expected: list[tuple[str, str]]) -> None:
    """UT10-98 `NXX-XXXX` (exchange 2-9) is PHONE; a full number stays one span."""
    assert _found(text) == expected


@pytest.mark.parametrize(
    "text",
    ["rows 100-2000", "PAY-555-0123", "555-01234", "1555-0123", "555-0123-4", "v1.555-0123",
     "x555-0123", "555-0123.5", "2026-10-11"],
)  # fmt: skip
def test_ut10_98_other_dash_numbers_stay(text: str) -> None:
    """UT10-98 ranges from 0/1, keys, longer groups, versions and dates are not PHONE."""
    assert _found(text) == []


# --- UT10-99 verbatim-safe shapes stay unmasked (T09-15d, T06-29) -------------------------

_ULID = "01J9ZX3Q8K4M" + "2N6P7R5T0V1W2Y"  # built at runtime: detect-secrets keeps quiet


@pytest.mark.parametrize(
    "text",
    [
        "20260925-101500-ABCDEF",
        "20260227-035645-K7WY2A",  # its 14 digits pass Luhn: CARD-shaped
        "built 20261012-135715-686349 ok", "20260926-100000-000001",
        "build 20260926-100000-000001.",
        _ULID, "run_" + _ULID, "2026-10-11",
        "2026-10-11T10:15:00Z", "2026-10-11T10:15:00.123Z", "2026-10-11 10:15:00",
        "2026-10-11T10:15:00+02:00", "2026-10-11T10:15:00.123456+00:00",
        "2026-10-11T10:15:00.123456789Z", "10:15:00", "10:15:00.5", "q_0123456789abcdef",
        "q_1234567890123456", "1200.50", "0.75", "-0.5", "42", "7", "INC0012345", "CHG0012345",
        "PAY-1234",
    ],
)  # fmt: skip
def test_ut10_99_verbatim_safe_shapes_are_not_masked(text: str) -> None:
    """UT10-99 build ids, ULIDs, dates, timestamps, times, query ids, decimals and small
    integers (T09-15d verbatim-safe set) and ticket numbers yield no span."""
    assert _found(text) == []
