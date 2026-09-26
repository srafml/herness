"""Tests for the `Decider` protocol (U03-48, T03-11).

No decider class exists yet (T03-12 onwards add them), so UT03-45 checks a minimal
conforming stub and non-conforming objects; later decider cards extend the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from herness.core.types import DecisionInput, DecisionOutput, QuestionSet
from herness.enrich.decide import Decider

pytestmark = pytest.mark.unit


class _StubDecider:
    name = "stub"
    version = "stub-1"

    def decide(
        self, items: Sequence[DecisionInput], questions: QuestionSet
    ) -> list[DecisionOutput]:
        return []

    def health(self) -> None:
        return None


class _NoHealth:
    name = "stub"
    version = "stub-1"

    def decide(
        self, items: Sequence[DecisionInput], questions: QuestionSet
    ) -> list[DecisionOutput]:
        return []


class _NoVersion:
    name = "stub"

    def decide(
        self, items: Sequence[DecisionInput], questions: QuestionSet
    ) -> list[DecisionOutput]:
        return []

    def health(self) -> None:
        return None


@pytest.mark.parametrize("obj", [_StubDecider()])
def test_ut03_45_decider_classes_conform(obj: object) -> None:
    """UT03-45 each decider class is an instance of the runtime-checkable Decider."""
    assert isinstance(obj, Decider)


@pytest.mark.parametrize("obj", [_NoHealth(), _NoVersion(), object()])
def test_ut03_45_non_conforming_objects_rejected(obj: object) -> None:
    """UT03-45 objects missing a protocol member are not Deciders."""
    assert not isinstance(obj, Decider)
