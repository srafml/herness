"""UT09-71: ``herness._cli.term.safe_terminal_text`` (U09-100; TH09-15)."""

from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

from herness._cli.term import safe_terminal_text

pytestmark = pytest.mark.unit

_FORBIDDEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\x1b[31mred\x1b[0m b", "ared b"),  # CSI (SGR)
        ("x\x1b[2Jy\x1b[?25l", "xy"),  # CSI clear screen, private mode
        ("p\x1b]52;c;SGVsbG8=\x07q", "pq"),  # OSC 52 clipboard, BEL terminated
        ("p\x1b]0;title\x1b\\q", "pq"),  # OSC window title, ST terminated
        ("c1\x9b31m\x85\x90done", "c131mdone"),  # C1 controls removed one by one
        ("bell\x07 nul\x00 del\x7f bs\x08 cr\r", "bell nul del bs cr"),
        ("keep\nlines\tand tabs", "keep\nlines\tand tabs"),
        ("lone \x1b escape", "lone  escape"),
    ],
)
def test_ut09_71_escapes_and_controls_removed(raw: str, expected: str) -> None:
    """UT09-71 CSI, OSC, C0 (but newline and tab), DEL and C1 are removed."""
    assert safe_terminal_text(raw) == expected


def test_ut09_71_rich_markup_escaped() -> None:
    """UT09-71 rich markup is escaped: it prints literally and styles or links nothing."""
    raw = "[bold red]boom[/] [link=https://evil.example]x[/link]"
    text = safe_terminal_text(raw)
    out = io.StringIO()
    Console(file=out, width=200, force_terminal=True, highlight=False).print(text)
    assert out.getvalue() == raw + "\n"  # no SGR, no OSC 8 hyperlink: plain literal text


def test_ut09_71_cut_to_max_chars_with_ellipsis() -> None:
    """UT09-71 the text is cut to ``max_chars`` with an ellipsis; shorter text is unchanged."""
    assert safe_terminal_text("abcdef", max_chars=4) == "abc…"
    assert safe_terminal_text("abcd", max_chars=4) == "abcd"
    assert len(safe_terminal_text("x" * 30_000)) == 20_000


def test_ut09_71_output_never_holds_forbidden_characters() -> None:
    """UT09-71 every code point below 0xA0 that is not allowed is gone from the output."""
    raw = "".join(chr(i) for i in range(0xA0)) * 3
    out = safe_terminal_text(raw)
    assert _FORBIDDEN.search(out) is None
    assert "\x1b" not in out
