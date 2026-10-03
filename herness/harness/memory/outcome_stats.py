"""Pure outcome statistics: measurement windows, peer-adjusted DiD and verdicts.

Design 07 §5.9; DD14 resolved by R-34 (impl 07 §3.16, U07-83 .. U07-85). Every function here is
pure (no I/O, no clock reads) so `outcome.py` (T07-18) can call it from a job handler and tests
can exercise it directly with synthetic series.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import date, timedelta
from statistics import fmean, stdev
from typing import Literal, cast

from herness.harness.memory.settings import OutcomeConfig

Verdict = Literal["paid_off", "no_effect", "worse", "inconclusive"]
_MIN_PRE_WEEKS_FOR_STATS = 2


@dataclass(frozen=True)
class MetricWeeks:
    """The four `outcome` week counts resolved for one metric (`cfg.outcome` + `per_metric`)."""

    measure_after_weeks: int
    second_measure_weeks: int
    window_weeks: int
    settle_weeks: int


@dataclass(frozen=True)
class Windows:
    """Pre and post measurement windows and the due date for one measurement (U07-83)."""

    pre: tuple[date, date]
    post: tuple[date, date]
    due: date


@dataclass(frozen=True)
class DidResult:
    """Peer-adjusted difference-in-differences result (U07-84)."""

    n_pre: int
    n_post: int
    coverage: float | None
    mean_pre: float | None
    mean_post: float | None
    did: float | None
    se: float | None
    t: float | None
    rel: float | None
    expected_rel: float | None


def measurement_windows(effective_at: date, measurement: Literal[1, 2], w: MetricWeeks) -> Windows:
    """Pre/post windows and due date for `measurement` (design 07 §5.9, R-34, U07-83).

    `w.window_weeks` is `L`, `w.settle_weeks` is `lag`. Intervals are half-open `[start, end)`.
    """
    t0 = effective_at
    lag_and_l = timedelta(weeks=w.settle_weeks + w.window_weeks)
    pre = (t0 - timedelta(weeks=w.window_weeks), t0)
    if measurement == 1:
        post = (t0 + timedelta(weeks=w.settle_weeks), t0 + lag_and_l)
        due = t0 + timedelta(weeks=max(w.measure_after_weeks, w.settle_weeks + w.window_weeks))
    else:
        end = t0 + timedelta(weeks=w.second_measure_weeks)
        start = max(end - timedelta(weeks=w.window_weeks), t0 + lag_and_l)
        post, due = (start, end), end
    return Windows(pre=pre, post=post, due=due)


def metric_weeks(cfg: OutcomeConfig, metric: str) -> MetricWeeks:
    """The four week counts of `metric`: `cfg` with its `per_metric` override (T07-18)."""
    over = cast("Mapping[str, int]", cfg.per_metric.get(metric, {}))
    return MetricWeeks(*(over.get(f.name, getattr(cfg, f.name)) for f in fields(MetricWeeks)))


def _due_pair(w: MetricWeeks) -> tuple[int, int]:
    t0 = date(2000, 1, 3)  # any day: due dates are whole weeks after effective_at (U07-83)
    first, second = measurement_windows(t0, 1, w).due, measurement_windows(t0, 2, w).due
    return (first - t0).days // 7, (second - t0).days // 7


def due_weeks(cfg: OutcomeConfig) -> tuple[dict[str, tuple[int, int]], tuple[int, int]]:
    """`due_measurements` week pairs (U07-33): per overridden metric, and the default."""
    per = {name: _due_pair(metric_weeks(cfg, name)) for name in cfg.per_metric}
    return per, _due_pair(metric_weeks(cfg, ""))


def _in_window(day: date, window: tuple[date, date]) -> bool:
    start, end = window
    return start <= day < end


def _window_weeks(window: tuple[date, date]) -> float:
    start, end = window
    return (end - start).days / 7


def did_statistics(
    target: Mapping[date, float],
    control: Mapping[date, float],
    windows: Windows,
    *,
    better: Literal["higher", "lower"],
    expected_delta: float | None,
    min_rel: float,
) -> DidResult:
    """Peer-adjusted difference-in-differences over `windows` (design 07 §5.9, U07-84).

    `control(w)` is the caller's choice: the weekly peer median, or the prior-year value shifted
    by 364 days. Weeks absent from either series are dropped before differencing.
    """
    adj = {week: target[week] - control[week] for week in target.keys() & control.keys()}
    adj_pre = [value for week, value in adj.items() if _in_window(week, windows.pre)]
    adj_post = [value for week, value in adj.items() if _in_window(week, windows.post)]
    n_pre, n_post = len(adj_pre), len(adj_post)
    coverage = min(n_pre / _window_weeks(windows.pre), n_post / _window_weeks(windows.post))

    pre_target = [value for week, value in target.items() if _in_window(week, windows.pre)]
    post_target = [value for week, value in target.items() if _in_window(week, windows.post)]
    mean_pre = fmean(pre_target) if pre_target else None
    mean_post = fmean(post_target) if post_target else None

    did = se = t = rel = None
    if n_pre >= _MIN_PRE_WEEKS_FOR_STATS and n_post >= 1:
        did = fmean(adj_post) - fmean(adj_pre)
        se = stdev(adj_pre) * math.sqrt(1 / n_pre + 1 / n_post)
        sign = 1.0 if better == "higher" else -1.0
        impr = sign * did
        base = max(abs(mean_pre) if mean_pre is not None else 0.0, 1e-9)
        rel = impr / base
        if se > 0:
            t = impr / se
        elif impr == 0:
            t = 0.0
        else:
            t = math.inf if impr > 0 else -math.inf

    if expected_delta is not None and mean_pre is not None:
        expected_rel = abs(expected_delta) / max(abs(mean_pre), 1e-9)
    else:
        expected_rel = min_rel

    return DidResult(
        n_pre=n_pre, n_post=n_post, coverage=coverage, mean_pre=mean_pre, mean_post=mean_post,
        did=did, se=se, t=t, rel=rel, expected_rel=expected_rel,
    )  # fmt: skip


def classify_verdict(d: DidResult, *, has_control: bool, cfg: OutcomeConfig) -> Verdict:
    """Verdict table of design 07 §5.9, first match wins (U07-85; TH07-18 defaults conservative)."""
    if (
        not has_control
        or d.n_pre < cfg.min_weeks
        or d.n_post < cfg.min_weeks
        or d.coverage is None
        or d.coverage < cfg.min_coverage
        or d.t is None
        or d.rel is None
    ):
        return "inconclusive"

    expected_rel = d.expected_rel if d.expected_rel is not None else cfg.min_rel
    if d.t >= cfg.t_crit and d.rel >= max(cfg.min_rel, 0.5 * expected_rel):
        return "paid_off"
    if d.t <= -cfg.t_crit and d.rel <= -cfg.min_rel:
        return "worse"
    if (
        abs(d.rel) < cfg.min_rel
        and d.mean_pre is not None
        and d.mean_pre != 0
        and d.se is not None
        and (d.se / abs(d.mean_pre)) <= cfg.min_rel / 2
    ):
        return "no_effect"
    return "inconclusive"
