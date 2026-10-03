"""Pure-Python oracle for the org score (impl 04 PT04-07; design 04 §5.8 steps 3-5).

Robust z with the 1.4826 MAD and 1.2533 mean-absolute-deviation fallbacks, the Theil-Sen
slope (median of pairwise slopes, NULL below six points) and the single-group composite. The
medians interpolate like DuckDB's `median`/`mad` (VI04-06: `mad` is unscaled).
"""

import statistics
from collections.abc import Mapping, Sequence
from typing import Final

MIN_TREND_POINTS: Final = 6
CLIP: Final = 5.0


def clip(value: float) -> float:
    """`value` clipped to [-5, 5]."""
    return max(-CLIP, min(CLIP, value))


def robust_scale(xs: Sequence[float]) -> tuple[float | None, float | None]:
    """(median_g, denom) over the group's non-NULL values; denom None when it would be 0."""
    if not xs:
        return None, None
    med = statistics.median(xs)
    mad = statistics.median([abs(x - med) for x in xs])
    if mad > 0:
        return med, 1.4826 * mad
    meanad = sum(abs(x - med) for x in xs) / len(xs)
    return med, (1.2533 * meanad if meanad > 0 else None)


def robust_z(x: float | None, med: float | None, denom: float | None) -> float | None:
    """Unclipped z: None for a NULL x, 0 when the group has no spread."""
    if x is None or med is None:
        return None
    if denom is None:
        return 0.0
    return (x - med) / denom


def theil_sen(points: Mapping[int, float]) -> float | None:
    """Median of `(v_b - v_a) / (t_b - t_a)` over pairs `t_b > t_a`; None below six points."""
    if len(points) < MIN_TREND_POINTS:
        return None
    ts = sorted(points)
    slopes = [(points[b] - points[a]) / (b - a) for i, a in enumerate(ts) for b in ts[i + 1 :]]
    return statistics.median(slopes)


def composite(
    x: float | None,
    med: float | None,
    denom: float | None,
    slope: float | None,
    *,
    sign: float,
    trend_weight: float,
) -> float | None:
    """Composite of an entity scored on one metric (weight cancels): b + λ·trend_b."""
    z = robust_z(x, med, denom)
    if z is None:
        return None
    b = clip(sign * z)
    trend_b = clip(sign * slope * 12 / denom) if slope is not None and denom is not None else 0.0
    return b + trend_weight * trend_b
