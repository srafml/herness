"""Terminal-safe text for untrusted output: model answers, memory content, errors (U09-100).

TH09-15: no escape sequence or control character reaches the terminal, and the text cannot
style itself through ``rich`` markup.
"""

from __future__ import annotations

import re
from typing import Final

from rich.markup import escape

__all__ = ["safe_terminal_text"]

# Step 1: CSI ``ESC [ params intermediates final`` and OSC ``ESC ] ... (BEL | ESC \)``.
_SEQUENCES: Final = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# Step 2: every C0 control but newline and tab, DEL and C1 (a lone ESC included).
_CONTROLS: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def safe_terminal_text(text: str, *, max_chars: int = 20000) -> str:
    """``text`` without escapes or controls, cut to ``max_chars`` with ``…``, markup-escaped."""
    clean = _CONTROLS.sub("", _SEQUENCES.sub("", text))
    if len(clean) > max_chars:
        clean = clean[: max(max_chars - 1, 0)] + "…"
    return escape(clean)
