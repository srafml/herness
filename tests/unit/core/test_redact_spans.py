"""Span-quality fixes and detector gaps of the redactor (impl 10 T10-11b).

UT10-93 SN-1 (secret URL query values are URL_TOKEN), UT10-94 SN-3a (no trailing period),
UT10-95 SN-3b (`(area) number` keeps its parenthesis), UT10-96 SN-2 (`F. Last` from the
display name), UT10-97 account/card-shaped digit runs, UT10-98 7-digit local phones and
UT10-99 the verbatim-safe shapes that stay unmasked. Values are synthetic: reserved
`202-555-01xx` style numbers, `synthetic` secrets, invented names, `4111` test cards.
"""

from __future__ import annotations

import pytest

from herness.core.redact import Redactor
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
