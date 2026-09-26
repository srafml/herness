"""Tests for the `Decider` protocol (U03-48, T03-11).

UT03-45 checks a minimal conforming stub, each decider class built so far (T03-14:
`LayaDecider`, constructed without loading) and non-conforming objects; later decider
cards extend the table.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from herness.core.types import DecisionInput, DecisionOutput, QuestionSet
from herness.enrich.decide import Decider
from herness.enrich.deciders.laya import LayaDecider
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import LayaSettings

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


def _laya() -> LayaDecider:
    paths = EnrichPaths(
        data_root=Path.cwd().resolve(), embedding_path="data/e", laya_current_file="data/c"
    )
    return LayaDecider(LayaSettings(device="cpu"), paths=paths, version="laya-20261004-1")


@pytest.mark.parametrize("factory", [_StubDecider, _laya])
def test_ut03_45_decider_classes_conform(factory: object) -> None:
    """UT03-45 each decider class is an instance of the runtime-checkable Decider."""
    assert callable(factory)
    assert isinstance(factory(), Decider)


@pytest.mark.parametrize("obj", [_NoHealth(), _NoVersion(), object()])
def test_ut03_45_non_conforming_objects_rejected(obj: object) -> None:
    """UT03-45 objects missing a protocol member are not Deciders."""
    assert not isinstance(obj, Decider)
