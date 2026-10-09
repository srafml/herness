"""Tests for tests.support.truth (U11-47): the label loader and the two truth fixtures.

The plugin maps UT11-44 to the `plant_value` test of `tests/unit/eval/test_truth.py`, so
these tests name the unit (U11-47) and not a second test ID.
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
    """U11-47 the session fixtures parse the committed 7-tiny and 42-tiny manifests."""
    assert (truth_7_tiny.seed, truth_7_tiny.scale) == (7, "tiny")
    assert (truth_42_tiny.seed, truth_42_tiny.scale) == (42, "tiny")
    assert truth_7_tiny.params_hash == truth_42_tiny.params_hash
    assert truth_7_tiny.dataset_root == "data"
    assert truth_7_tiny.row_counts["servicenow/incident"] > 0


def test_u11_47_load_truth_labels_schema() -> None:
    """U11-47 load_truth_labels returns the five label columns, one row per record and question."""
    for name in ("7-tiny", "42-tiny"):
        table = load_truth_labels(TRUTH_DIR / name)
        assert table.column_names == _LABEL_COLUMNS
        assert table.num_rows > 0
        pairs = zip(
            table.column("record_id").to_pylist(), table.column("question").to_pylist(), strict=True
        )
        assert len(set(pairs)) == table.num_rows
