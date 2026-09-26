"""Redaction value types (U10-35); the Redactor itself lands with U10-40 onwards.

``EntityType`` and ``DETECTION_ORDER`` are declared in ``redact_patterns`` (which this
module imports, spec §2 import order) and re-exported here as the public names.
"""

from __future__ import annotations

from dataclasses import dataclass

from herness.core.redact_patterns import DETECTION_ORDER, EntityType

__all__ = ["DETECTION_ORDER", "EntityType", "RedactionResult", "Span"]


@dataclass(frozen=True, order=True)
class Span:
    """One detected span ``[start, end)`` with its replacement; orders by ``start`` first."""

    start: int
    end: int
    type: EntityType
    replacement: str


@dataclass(frozen=True)
class RedactionResult:
    """Redacted text and per-type counts; ``counts`` holds only types with count >= 1."""

    text: str
    counts: dict[EntityType, int]
