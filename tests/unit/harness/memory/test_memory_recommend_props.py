"""Property tests for herness.harness.memory.recommend.outcome_adjustment (U07-80): PT07-07."""

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from herness.core import time as clock
from herness.core.types import NumberRef, RecommendationDraft
from herness.harness.memory.recommend import outcome_adjustment
from herness.harness.memory.settings import FeedbackConfig
from herness.store.ops import SimilarityRow

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
_VERDICTS = ("paid_off", "no_effect", "worse", "inconclusive")
_REF = NumberRef(id="n1", value=1.0, unit="pct", query_id="q_" + "a" * 16, column="c",
                 row_key=None)  # fmt: skip
_DRAFT = RecommendationDraft(
    rank=1, kind="fund", target_type="service", target_id="svc_a",
    summary="Fund the team to lift [[n1]].", numbers=[_REF], expected_metric="mttr",
    expected_delta_ref="n1", expected_usd_ref=None, finding_ids=["fnd_" + "0" * 26],
)  # fmt: skip


@st.composite
def _prior(draw: st.DrawFn, verdicts: tuple[str, ...] = _VERDICTS) -> SimilarityRow:
    n = draw(st.integers(min_value=0, max_value=10**6))
    days = draw(st.floats(min_value=-10.0, max_value=5000.0, allow_nan=False))
    return SimilarityRow(
        rec_id=f"rec_{n:026d}", kind=draw(st.sampled_from(["fund", "org_action"])),
        target_type=draw(st.sampled_from(["service", "team"])),
        target_id=draw(st.sampled_from(["svc_a", "svc_b", "team_x"])),
        expected_metric=draw(st.sampled_from(["mttr", "cost", None])),
        summary=draw(st.sampled_from(["Fund the team.", "Merge rotas [[n1]].", "x"])),
        verdict=draw(st.sampled_from(verdicts)),
        measured_at=clock.format_utc(NOW - timedelta(days=days)), query_id=None,
    )  # fmt: skip


@st.composite
def _cfg(draw: st.DrawFn) -> FeedbackConfig:
    lo = draw(st.floats(min_value=-0.9, max_value=-0.01))
    hi = draw(st.floats(min_value=0.01, max_value=0.9))
    c_lo = draw(st.floats(min_value=0.01, max_value=0.5))
    c_hi = draw(st.floats(min_value=0.51, max_value=0.99))
    return FeedbackConfig(
        sim_threshold=draw(st.floats(min_value=0.01, max_value=1.0)),
        alpha=draw(st.floats(min_value=0.01, max_value=5.0)),
        k0=draw(st.floats(min_value=0.01, max_value=5.0)),
        delta_bounds=(lo, hi), confidence_bounds=(c_lo, c_hi),
    )  # fmt: skip


def _embed(text: str) -> np.ndarray:
    seed = sum(text.encode("utf-8"))
    vec = np.random.default_rng(seed).normal(size=8)
    return vec / np.linalg.norm(vec)


def _run(base: float, priors: list[SimilarityRow], cfg: FeedbackConfig) -> Any:
    return outcome_adjustment(
        _DRAFT, base, priors=priors, embed=_embed, related=lambda a, b: a < b, cfg=cfg, now=NOW
    )


_base = st.floats(min_value=0.0, max_value=1.0)


def _rid(p: SimilarityRow) -> str:
    return p["rec_id"]


def _priors(verdicts: tuple[str, ...] = _VERDICTS) -> st.SearchStrategy[list[SimilarityRow]]:
    return st.lists(_prior(verdicts), max_size=30, unique_by=_rid)


@settings(max_examples=200, deadline=None)
@given(base=_base, priors=_priors(), cfg=_cfg())
def test_pt07_07_delta_and_confidence_bounded(
    base: float, priors: list[SimilarityRow], cfg: FeedbackConfig
) -> None:
    """PT07-07 Δ and confidence always within the type and configured bounds."""
    adj = _run(base, priors, cfg)
    assert max(-0.25, cfg.delta_bounds[0]) <= adj.delta <= min(0.15, cfg.delta_bounds[1])
    lo, hi = max(0.05, cfg.confidence_bounds[0]), min(0.95, cfg.confidence_bounds[1])
    assert lo <= adj.confidence <= hi
    assert len(adj.similar) <= 20
    if not adj.similar:
        assert adj.delta == 0.0
    assert _run(base, list(reversed(priors)), cfg) == adj


@settings(max_examples=100, deadline=None)
@given(base=_base, priors=_priors(("paid_off", "inconclusive")))
def test_pt07_07_positive_outcomes_never_lower(base: float, priors: list[SimilarityRow]) -> None:
    """PT07-07 only positive outcomes: Δ ≥ 0 and confidence ≥ clamp(base)."""
    adj = _run(base, priors, FeedbackConfig())
    assert adj.delta >= 0.0
    assert adj.confidence >= min(0.95, max(0.05, base))


@settings(max_examples=100, deadline=None)
@given(base=_base, priors=_priors(("worse", "no_effect")))
def test_pt07_07_negative_outcomes_never_raise(base: float, priors: list[SimilarityRow]) -> None:
    """PT07-07 only negative outcomes: Δ ≤ 0 and confidence ≤ clamp(base)."""
    adj = _run(base, priors, FeedbackConfig())
    assert adj.delta <= 0.0
    assert adj.confidence <= min(0.95, max(0.05, base))
