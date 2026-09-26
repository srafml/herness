"""Integration test for herness.harness.verifier.Verifier (IT05-07, flow F05-08).

Stand-in for spec 11's `lake_small` build and planted corpus (not built yet): the test-local
`wh-<id>.duckdb` of `_verifier_standin`, 13 planted error kinds and 50 correct items.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.harness_fakes import FakeOps
from tests.unit.harness import _verifier_standin as sd

from herness.core.types import ItemResult
from herness.harness.llm.settings import SqlSettings, VerifierSettings
from herness.harness.verifier import Verifier
from herness.harness.warehouse import WarehousePool

pytestmark = pytest.mark.integration

_ITEM_LISTS = ("uncited", "unknown_markers", "bad_refs", "unverified_findings")


@pytest.fixture
def corpus(tmp_path: Path) -> Iterator[tuple[Verifier, dict[str, str]]]:
    sd.make_build(tmp_path)
    pool = WarehousePool(tmp_path, SqlSettings())
    ops = FakeOps({sd.FND_VERIFIED: "verified", sd.FND_CHALLENGED: "challenged"})
    try:
        qids = {}
        for name, ev in sd.recorded_queries(pool).items():
            ops.record_evidence(ev)
            qids[name] = ev.query_id
        ops.record_evidence(sd.foreign_evidence(sd.OTHER_BUILD_SQL, build_id=sd.OTHER_BUILD_ID))
        ops.record_evidence(sd.foreign_evidence(sd.REJECTED_SQL))
        verifier = Verifier(
            ops, pool, VerifierSettings(), allowed_numeral_patterns=sd.PATTERNS, sql=SqlSettings()
        )
        yield verifier, qids
    finally:
        pool.close_all()


def _signals(result: ItemResult) -> set[str]:
    found = {check.result for check in result.checks if check.result != "match"}
    return found | {name for name in _ITEM_LISTS if getattr(result, name)}


def test_it05_07_all_planted_errors_detected(corpus: tuple[Verifier, dict[str, str]]) -> None:
    """IT05-07 each of the 13 planted error kinds is rejected with its expected signal."""
    verifier, qids = corpus
    planted = sd.planted_items(qids["team"])
    assert len(planted) == 13
    missed = []
    for kind, (item, signal) in planted.items():
        result = verifier.verify_numbers(item, sd.BUILD_ID)
        got = result.items[0]
        if result.passed or signal not in _signals(got):
            missed.append((kind, signal, sorted(_signals(got))))
    assert missed == []


def test_it05_07_no_false_rejections(corpus: tuple[Verifier, dict[str, str]]) -> None:
    """IT05-07 the 50 correct items (7.4 for 7.41667, USD strings, allowed numerals) pass."""
    verifier, qids = corpus
    items = sd.correct_items(qids)
    assert len(items) == 50
    texts = " ".join(item.text for item in items)
    for allowed in ("Q3 2026", "INC0012345", "2026-09-24"):
        assert allowed in texts
    assert any(n.value == 7.4 and n.column == "mttr_hours" for i in items for n in i.numbers)
    assert any(isinstance(n.value, str) and n.unit == "usd" for i in items for n in i.numbers)
    rejected = [
        (item.where, sorted(_signals(result.items[0])))
        for item in items
        if not (result := verifier.verify_numbers(item, sd.BUILD_ID)).passed
    ]
    assert rejected == []


def test_it05_07_batch_mixes_correct_and_planted(corpus: tuple[Verifier, dict[str, str]]) -> None:
    """IT05-07 one batch of all 63 items: exactly the 13 planted items fail."""
    verifier, qids = corpus
    correct = sd.correct_items(qids)
    planted = [item for item, _ in sd.planted_items(qids["team"]).values()]
    result = verifier._verify_items(correct + planted, sd.BUILD_ID)
    assert not result.passed
    assert [r.passed for r in result.items] == [True] * 50 + [False] * 13
