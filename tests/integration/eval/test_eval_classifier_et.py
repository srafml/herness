"""Phase 4 evaluation row of the classifier eval (U11-67, flow F11-09): ET11-03.

ET11-03 runs `herness eval --suite classifier` on the target PC against the phase 4 Laya
model directory and the frozen gold set: the gate cross-check must pass and the decider
comparison is published. The gold set needs the gold labelers of D17, so the row skips at
runtime with a reason naming D17 until the model directory and gold set are configured; with
them set it fails loudly so the run is done on the target PC. The CPU path of the cross-check
is proven on the fixture model directory by `test_eval_classifier_it.py` (IT11-25..IT11-27).
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.gpu]

_MODEL_DIR = "HERNESS_EVAL_LAYA_MODEL_DIR"  # data root holding the phase 4 Laya version
_GOLD = "HERNESS_EVAL_GOLD_SET"  # the frozen gold set of D17's labelers
_BLOCKED = (
    "blocked by D17 (gold labelers): needs the phase 4 Laya model directory and the frozen "
    "gold set on the target PC ({missing} not set)"
)


def _require(*names: str) -> None:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.skip(_BLOCKED.format(missing=", ".join(missing)))


def test_et11_03_phase4_gate_cross_check_and_decider_comparison() -> None:
    """ET11-03 phase 4 model dir and gold set: `herness eval --suite classifier`: gate
    cross-check passes; decider comparison published."""
    _require(_MODEL_DIR, _GOLD)
    pytest.fail("ET11-03 runs on the target PC with the phase 4 model and D17's gold set")
