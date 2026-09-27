"""Tests for the `Decider` protocol (U03-48, T03-11).

UT03-45 checks a minimal conforming stub, each decider class built so far (T03-14:
`LayaDecider`, constructed without loading; T03-12: `OpenJevDecider`, constructed without
any HTTP; T03-13: `JevHostedDecider`, no guard touched; T03-15: `LlmDecider` over the
test-local fake client, no call made) and non-conforming objects; later decider cards
extend the table.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import SecretStr
from tests.unit.enrich._fake_llm import FakeLLMClient, votes_by_seed

from herness.core.types import DecisionInput, DecisionOutput, QuestionSet
from herness.enrich.decide import Decider
from herness.enrich.deciders.jev_hosted import JevHostedDecider
from herness.enrich.deciders.laya import LayaDecider
from herness.enrich.deciders.llm import LlmDecider
from herness.enrich.deciders.openjev import OpenJevDecider
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import JevSettings, LayaSettings, OpenJevSettings

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


def _openjev() -> OpenJevDecider:
    return OpenJevDecider(OpenJevSettings(), api_key=None, image_tag="0.4.0", samples=None)


def _jev() -> JevHostedDecider:
    return JevHostedDecider(JevSettings(), api_key=SecretStr("unit-jev-token"), samples=None)


def _llm() -> LlmDecider:
    client = FakeLLMClient(votes_by_seed({}))
    return LlmDecider(client, version="local/qwen", votes=3, temperature=0.7, max_concurrency=1)


@pytest.mark.parametrize("factory", [_StubDecider, _laya, _openjev, _jev, _llm])
def test_ut03_45_decider_classes_conform(factory: object) -> None:
    """UT03-45 each decider class is an instance of the runtime-checkable Decider."""
    assert callable(factory)
    assert isinstance(factory(), Decider)


@pytest.mark.parametrize("obj", [_NoHealth(), _NoVersion(), object()])
def test_ut03_45_non_conforming_objects_rejected(obj: object) -> None:
    """UT03-45 objects missing a protocol member are not Deciders."""
    assert not isinstance(obj, Decider)
