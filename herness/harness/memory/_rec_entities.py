"""Target labels of a `run_summary` text and its `data.entities` (impl 07 T07-26b).

Private sibling of `recommend` (kept apart for the §2 line budget; only `recommend` imports
it). `MemoryStore.purge(record_id)` (R-54) removes an item only on a structured citation, so
every target whose label the text names unchanged is also cited as `{"type", "id"}`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import JsonValue

from herness.harness.memory.policy import find_uncited_numerals

__all__ = ["labels_text"]


def labels_text(
    head: str, targets: Sequence[tuple[str, str, str]], room: int,
    allowed: Sequence[re.Pattern[str]],
) -> tuple[str, list[JsonValue]]:  # fmt: skip
    """`head` plus the `(type, id, redacted label)` targets that fit `room` (one with a numeral
    is left out), and the entities of those named unchanged (label equal to `type:id`)."""
    shown, cited = list[str](), dict[str, JsonValue]()
    for kind, ident, label in (t for t in targets if not find_uncited_numerals(t[2], allowed)):
        if (room := room - len(label) - len(", ")) < 0:
            break
        shown.append(label)
        if label == f"{kind}:{ident}":
            cited[label] = {"type": kind, "id": ident}
    text = head + (f" for {', '.join(shown)}" if shown else "")
    if len(shown) < len(targets):
        text += " and other targets" if shown else " for other targets"
    return text + ".", [*cited.values()]
