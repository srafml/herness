"""Tests for tests.support.truth (U11-47, UT11-44): the label loader and the two truth fixtures.

UT11-44 is the test ID of U11-47; the repo test-ID scan (IT11-31) needs every test to carry one.
"""

from __future__ import annotations

import pytest
from tests.support.truth import TRUTH_DIR, load_truth_labels

from herness.eval.truth import TruthManifest

pytestmark = pytest.mark.unit

_LABEL_COLUMNS = ["record_id", "content_hash", "question", "answer", "pii_spans"]


def test_u11_47_truth_fixtures_load_manifests(
    truth_7_tiny: TruthManifest, truth_42_tiny: TruthManifest
) -> None:
    """UT11-44 the session fixtures parse the committed 7-tiny and 42-tiny manifests."""
    assert (truth_7_tiny.seed, truth_7_tiny.scale) == (7, "tiny")
    assert (truth_42_tiny.seed, truth_42_tiny.scale) == (42, "tiny")
    assert truth_7_tiny.params_hash == truth_42_tiny.params_hash
    assert truth_7_tiny.dataset_root == "data"
    assert truth_7_tiny.row_counts["servicenow/incident"] > 0


def test_u11_47_load_truth_labels_schema() -> None:
    """UT11-44 load_truth_labels returns the five label columns, one row per record and question."""
    for name in ("7-tiny", "42-tiny"):
        table = load_truth_labels(TRUTH_DIR / name)
        assert table.column_names == _LABEL_COLUMNS
        assert table.num_rows > 0
        pairs = zip(
            table.column("record_id").to_pylist(), table.column("question").to_pylist(), strict=True
        )
        assert len(set(pairs)) == table.num_rows
