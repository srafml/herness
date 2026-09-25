"""Truth isolation gate test (U11-34, R-64): tests/unit/test_truth_isolation.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.isolation import find_truth_references

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_ut11_42_truth_isolation() -> None:
    """UT11-42 no module under herness/ or app/ references a truth file except the allowlist."""
    findings = find_truth_references([ROOT / "herness", ROOT / "app"])
    assert findings == []
