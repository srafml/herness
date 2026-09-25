"""Security test for the truth isolation scan (ST11-02, TH11-02, U11-34)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.isolation import find_truth_references

pytestmark = pytest.mark.unit


def test_st11_02_reports_truth_reference_under_a_copy_of_herness(tmp_path: Path) -> None:
    """ST11-02 a planted truth_labels reference under a herness/ copy is reported."""
    fake_herness = tmp_path / "herness"
    module_dir = fake_herness / "leaky"
    module_dir.mkdir(parents=True)
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "reader.py").write_text(
        'PATH = "data/truth_labels.parquet"  # planted leak\n', encoding="utf-8"
    )

    findings = find_truth_references([fake_herness])

    assert findings == [("herness/leaky/reader.py", 1, "truth_labels")]


def test_st11_02_allowlisted_file_is_not_reported(tmp_path: Path) -> None:
    """ST11-02 the one allowlisted loader (herness/eval/truth.py) is never reported."""
    fake_herness = tmp_path / "herness"
    eval_dir = fake_herness / "eval"
    eval_dir.mkdir(parents=True)
    (eval_dir / "truth.py").write_text('PATH = "truth.json"\n', encoding="utf-8")

    assert find_truth_references([fake_herness]) == []
