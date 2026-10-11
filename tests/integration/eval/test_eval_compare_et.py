"""Phase 7 evaluation rows of the comparisons (U11-71, U11-68): ET11-04 and ET11-05.

Both need real models on the GPU and approved profiles: ET11-04 runs `herness eval --compare
<profile>` over fast, standard and deep on synthetic and real builds (gate G7.1); ET11-05
runs the golden subset without `truth_ref` and the synthetic-only questions on a real build.
The card is blocked by D5 (hybrid and premium approval), so the rows skip at runtime with a
reason naming D5 until the approved profiles and real models are configured. The CPU path
of `--compare` is proven on tiny_build by `test_eval_compare_it.py`.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.gpu]

_APPROVED = "HERNESS_EVAL_APPROVED_PROFILES"  # set once D5 approves hybrid and premium
_REAL_BUILD = "HERNESS_EVAL_REAL_BUILD"  # data root of a real build with real models
_BLOCKED = (
    "blocked by D5 (hybrid and premium approval): needs approved profiles and real models "
    "on the GPU ({missing} not set)"
)


def _require(*names: str) -> None:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.skip(_BLOCKED.format(missing=", ".join(missing)))


def test_et11_04_compare_profiles_over_depths() -> None:
    """ET11-04 `herness eval --compare <profile>` over depths on synthetic and real builds:
    identical numbers across profiles; deep ≥ standard + 3 pp or documented."""
    _require(_APPROVED, _REAL_BUILD)
    pytest.fail("ET11-04 runs on the target PC once D5 is approved (phase 7 gate G7.1)")


def test_et11_05_real_build_subset_meets_the_floor() -> None:
    """ET11-05 real build, golden subset without `truth_ref` and synthetic-only questions:
    correctness ≥ floor."""
    _require(_REAL_BUILD)
    pytest.fail("ET11-05 runs on the target PC with real models (phase 7)")
