"""Decider protocol, primary rules, pure gate and resolution reference (impl 03 §2).

T03-11 adds the `Decider` protocol (U03-48); the primary rules and resolution land here
with T03-17.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from herness.core.types import DecisionInput, DecisionOutput, QuestionSet

__all__ = ["Decider"]


@runtime_checkable
class Decider(Protocol):
    """Interchangeable classification backend (U03-48, spec 00 §6).

    `decide` returns one `DecisionOutput` per input, in input order; deciders never
    calibrate and never gate. `version` is stable for the life of the instance, except
    `JevHostedDecider`, which fixes it on the first `health()` (U03-56). `health()` raises
    `ModelUnavailable` on any failure. Implementations state their own concurrency.
    """

    name: str
    version: str

    def decide(
        self, items: Sequence[DecisionInput], questions: QuestionSet
    ) -> list[DecisionOutput]:
        """Classify `items` against `questions`; one output per input, in input order."""
        ...

    def health(self) -> None:
        """Check the backend is usable. Raises ModelUnavailable on any failure."""
        ...
