"""Table-cell formatting over herness.core.numbers (R-16) and confidence labels (impl 09 §3.3).

No formatting rule lives here: every number is printed by the impl 00 formatter, so a table
cell and a NumberRef marker showing the same value print the same text. Pure.
"""

from __future__ import annotations

from decimal import Decimal, DecimalException
from typing import Final, Literal

from herness.core import numbers

NOT_LINKED: Final = "—"
_HIGH: Final = Decimal("0.7")
_MEDIUM: Final = Decimal("0.4")

ConfidenceLabel = Literal["high", "medium", "low", "unknown"]


def format_value(value: Decimal | int | float | None, fmt: str) -> str:
    """Format a raw table cell with the NumberRef rules (U09-102). Raises nothing.

    None gives an em dash (the cell is not linked); a value that ``Decimal(str(value))``
    cannot convert, or a non-finite one, gives ``n/a``.
    """
    if value is None:
        return NOT_LINKED
    try:
        number = Decimal(str(value))
    except (DecimalException, ValueError):  # ValueError: int beyond the str digit limit
        return numbers.NOT_AVAILABLE
    if not number.is_finite():
        return numbers.NOT_AVAILABLE
    return numbers.format_value(number, "", fmt)


def confidence_label(confidence: float | Decimal | None) -> ConfidenceLabel:
    """Label a 0-1 confidence (U09-12): >= 0.7 high, >= 0.4 medium, else low; None unknown."""
    if confidence is None:
        return "unknown"
    number = Decimal(str(confidence))
    if number.is_nan():
        return "low"
    if number >= _HIGH:
        return "high"
    return "medium" if number >= _MEDIUM else "low"
