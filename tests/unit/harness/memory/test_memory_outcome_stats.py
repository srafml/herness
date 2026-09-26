"""Tests for herness.harness.memory.outcome_stats (impl 07 U07-83 .. U07-85; T07-17)."""

import math
from datetime import date, timedelta

import pytest

from herness.harness.memory.outcome_stats import (
    DidResult,
    MetricWeeks,
    Windows,
    classify_verdict,
    did_statistics,
    measurement_windows,
)
from herness.harness.memory.settings import OutcomeConfig

pytestmark = pytest.mark.unit

EFFECTIVE_AT = date(2026, 1, 1)
DEFAULT_WEEKS = MetricWeeks(
    measure_after_weeks=12, second_measure_weeks=26, window_weeks=10, settle_weeks=2
)


def test_ut07_69_windows_defaults_measurement_one() -> None:
    """UT07-69 defaults: m = 1 post is weeks 2-12 after effective_at, due at week 12 (R-34)."""
    windows = measurement_windows(EFFECTIVE_AT, 1, DEFAULT_WEEKS)
    assert windows == Windows(
        pre=(EFFECTIVE_AT - timedelta(weeks=10), EFFECTIVE_AT),
        post=(EFFECTIVE_AT + timedelta(weeks=2), EFFECTIVE_AT + timedelta(weeks=12)),
        due=EFFECTIVE_AT + timedelta(weeks=12),
    )


def test_ut07_69_windows_defaults_measurement_two() -> None:
    """UT07-69 defaults: m = 2 post is weeks 16-26 after effective_at, due at week 26 (R-34)."""
    windows = measurement_windows(EFFECTIVE_AT, 2, DEFAULT_WEEKS)
    assert windows == Windows(
        pre=(EFFECTIVE_AT - timedelta(weeks=10), EFFECTIVE_AT),
        post=(EFFECTIVE_AT + timedelta(weeks=16), EFFECTIVE_AT + timedelta(weeks=26)),
        due=EFFECTIVE_AT + timedelta(weeks=26),
    )


def test_ut07_69_windows_per_metric_override_shifts_due() -> None:
    """UT07-69 per-metric: raising measure_after_weeks past lag+L moves the m=1 due date only."""
    overridden = MetricWeeks(
        measure_after_weeks=20, second_measure_weeks=26, window_weeks=10, settle_weeks=2
    )
    windows = measurement_windows(EFFECTIVE_AT, 1, overridden)
    # post and pre depend only on window_weeks/settle_weeks, unchanged from the default case.
    assert windows.pre == (EFFECTIVE_AT - timedelta(weeks=10), EFFECTIVE_AT)
    assert windows.post == (EFFECTIVE_AT + timedelta(weeks=2), EFFECTIVE_AT + timedelta(weeks=12))
    # due = max(measure_after_weeks, settle+window) = max(20, 12) = 20 weeks.
    assert windows.due == EFFECTIVE_AT + timedelta(weeks=20)

    windows2 = measurement_windows(EFFECTIVE_AT, 2, overridden)
    # m = 2 never depends on measure_after_weeks.
    assert windows2.due == EFFECTIVE_AT + timedelta(weeks=26)


def test_ut07_70_did_statistics_synthetic_series() -> None:
    """UT07-70 a synthetic series with a zero control produces exact did/se/t/rel values."""
    weeks = [date(2026, 1, 5) + timedelta(weeks=i) for i in range(5)]
    target = dict(zip(weeks[:4], [10.0, 12.0, 20.0, 22.0], strict=True))
    control = dict.fromkeys(weeks[:4], 0.0)
    windows = Windows(pre=(weeks[0], weeks[2]), post=(weeks[2], weeks[4]), due=weeks[4])

    result = did_statistics(
        target, control, windows, better="higher", expected_delta=None, min_rel=0.05
    )

    assert result.n_pre == 2
    assert result.n_post == 2
    assert result.coverage == pytest.approx(1.0)
    assert result.mean_pre == pytest.approx(11.0)
    assert result.mean_post == pytest.approx(21.0)
    assert result.did == pytest.approx(10.0)
    assert result.se == pytest.approx(math.sqrt(2))
    assert result.t == pytest.approx(10.0 / math.sqrt(2))
    assert result.rel == pytest.approx(10.0 / 11.0)
    assert result.expected_rel == pytest.approx(0.05)


def test_ut07_70_did_statistics_lower_is_better_flips_sign() -> None:
    """UT07-70 `better="lower"` flips the sign of the improvement before rel/t."""
    weeks = [date(2026, 1, 5) + timedelta(weeks=i) for i in range(5)]
    target = dict(zip(weeks[:4], [10.0, 12.0, 20.0, 22.0], strict=True))
    control = dict.fromkeys(weeks[:4], 0.0)
    windows = Windows(pre=(weeks[0], weeks[2]), post=(weeks[2], weeks[4]), due=weeks[4])

    result = did_statistics(
        target, control, windows, better="lower", expected_delta=None, min_rel=0.05
    )

    assert result.did == pytest.approx(10.0)
    assert result.rel == pytest.approx(-10.0 / 11.0)
    assert result.t == pytest.approx(-10.0 / math.sqrt(2))


def test_ut07_70_did_statistics_expected_delta_sets_expected_rel() -> None:
    """UT07-70 an explicit expected_delta overrides the min_rel fallback for expected_rel."""
    weeks = [date(2026, 1, 5) + timedelta(weeks=i) for i in range(5)]
    target = dict(zip(weeks[:4], [10.0, 12.0, 20.0, 22.0], strict=True))
    control = dict.fromkeys(weeks[:4], 0.0)
    windows = Windows(pre=(weeks[0], weeks[2]), post=(weeks[2], weeks[4]), due=weeks[4])

    result = did_statistics(
        target, control, windows, better="higher", expected_delta=5.5, min_rel=0.05
    )
    # mean_pre = 11.0, so expected_rel = |5.5| / 11.0 = 0.5.
    assert result.expected_rel == pytest.approx(0.5)


def test_ut07_70_did_statistics_insufficient_weeks_yields_none() -> None:
    """UT07-70 fewer than 2 pre weeks (or 0 post weeks) leaves did/se/t/rel as None."""
    weeks = [date(2026, 1, 5) + timedelta(weeks=i) for i in range(4)]
    target = dict(zip(weeks[:3], [10.0, 20.0, 22.0], strict=True))
    control = dict.fromkeys(weeks[:3], 0.0)
    windows = Windows(pre=(weeks[0], weeks[1]), post=(weeks[1], weeks[3]), due=weeks[3])

    result = did_statistics(
        target, control, windows, better="higher", expected_delta=None, min_rel=0.05
    )
    assert result.n_pre == 1
    assert result.did is None
    assert result.se is None
    assert result.t is None
    assert result.rel is None
    assert result.expected_rel == pytest.approx(0.05)


CFG = OutcomeConfig()


def _did(**overrides: float | int | None) -> DidResult:
    base: dict[str, float | int | None] = {
        "n_pre": 10, "n_post": 10, "coverage": 0.9, "mean_pre": 100.0, "mean_post": 100.0,
        "did": 0.0, "se": 1.0, "t": 0.0, "rel": 0.0, "expected_rel": 0.05,
    }  # fmt: skip
    base.update(overrides)
    return DidResult(**base)  # type: ignore[arg-type]


def test_ut07_71_classify_verdict_paid_off() -> None:
    """UT07-71 t >= t_crit and rel >= max(min_rel, 0.5*expected_rel) classifies paid_off."""
    d = _did(t=3.0, rel=0.1)
    assert classify_verdict(d, has_control=True, cfg=CFG) == "paid_off"


def test_ut07_71_classify_verdict_worse() -> None:
    """UT07-71 t <= -t_crit and rel <= -min_rel classifies worse."""
    d = _did(t=-3.0, rel=-0.1)
    assert classify_verdict(d, has_control=True, cfg=CFG) == "worse"


def test_ut07_71_classify_verdict_no_effect() -> None:
    """UT07-71 a small rel with a tight relative standard error classifies no_effect."""
    d = _did(t=0.5, rel=0.01, se=1.0, mean_pre=100.0)
    assert classify_verdict(d, has_control=True, cfg=CFG) == "no_effect"


def test_ut07_71_classify_verdict_inconclusive_without_control() -> None:
    """UT07-71 no control series is conservatively inconclusive regardless of t/rel."""
    d = _did(t=5.0, rel=0.5)
    assert classify_verdict(d, has_control=False, cfg=CFG) == "inconclusive"


def test_ut07_72_seasonal_shift_in_all_peers_is_no_effect() -> None:
    """UT07-72 a seasonal trend shared by target and peers cancels in the DiD -> no_effect."""
    start = date(2026, 1, 5)
    weeks = [start + timedelta(weeks=i) for i in range(13)]
    # Both series trend upward identically; only a constant 20-unit gap survives differencing.
    target = {week: 100.0 + 5 * i for i, week in enumerate(weeks[:12])}
    control = {week: 80.0 + 5 * i for i, week in enumerate(weeks[:12])}
    windows = Windows(pre=(weeks[0], weeks[6]), post=(weeks[6], weeks[12]), due=weeks[11])

    result = did_statistics(
        target, control, windows, better="higher", expected_delta=None, min_rel=0.05
    )
    assert result.n_pre == 6
    assert result.n_post == 6
    assert result.coverage == pytest.approx(1.0)
    assert result.did == pytest.approx(0.0)
    assert result.se == pytest.approx(0.0)
    assert result.t == pytest.approx(0.0)
    assert result.rel == pytest.approx(0.0)

    assert classify_verdict(result, has_control=True, cfg=CFG) == "no_effect"


def test_ut07_73_classify_verdict_five_weeks_is_inconclusive() -> None:
    """UT07-73 only 5 pre weeks (below min_weeks=6) is inconclusive even with a clear effect."""
    d = _did(n_pre=5, n_post=10, t=5.0, rel=0.5)
    assert classify_verdict(d, has_control=True, cfg=CFG) == "inconclusive"
