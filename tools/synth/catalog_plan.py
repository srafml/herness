"""Record plan of the synthetic catalog: month counts and seq_start (U11-04 step 11).

Split out of `tools.synth.catalog` for the module line budget.
"""

import dataclasses
import math
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Final

from tools.synth.catalog_rows import Catalog, MonthKey, Planner
from tools.synth.params import SynthParams, SynthUsageError

METRICS_PER_SERVICE: Final = 4


def split_counts(total: int, weights: Sequence[float]) -> list[int]:
    """Split `total` proportionally to `weights` with largest-remainder rounding."""
    quotas = [total * w / math.fsum(weights) for w in weights]
    counts = [math.floor(q) for q in quotas]
    remainder = total - sum(counts)
    if not 0 <= remainder <= len(counts):  # floors never overshoot; float noise is < 1
        msg = "largest-remainder split out of range"
        raise AssertionError(msg)
    order = sorted(range(len(quotas)), key=lambda i: (counts[i] - quotas[i], i))
    for i in order[:remainder]:
        counts[i] += 1
    return counts


def span_months(params: SynthParams) -> list[tuple[date, int]]:
    """(first day of month, days of the month inside the span) from `start` to `end`."""
    months: list[date] = []
    days: list[int] = []
    for offset in range((params.end - params.start).days + 1):
        day = params.start + timedelta(days=offset)
        if not months or day.month != months[-1].month:
            months.append(day.replace(day=1))
            days.append(0)
        days[-1] += 1
    return list(zip(months, days, strict=True))


def month_counts(params: SynthParams) -> dict[MonthKey, int]:
    """Background counts per (source, entity, month): preset totals split by days in span."""
    months = [m for m, _ in span_months(params)]
    days = [n for _, n in span_months(params)]
    p = params.preset
    totals = {("servicenow", "incident"): p.incidents, ("servicenow", "change_request"): p.changes}
    totals |= {("servicenow", "problem"): p.problems, ("jira", "issue"): p.jira_issues}
    totals |= {("monitoring", "event"): p.events}
    counts: dict[MonthKey, int] = {}
    for (source, entity), total in totals.items():
        if source in params.sources:
            split = zip(months, split_counts(total, days), strict=True)
            counts |= {(source, entity, m): n for m, n in split}
    if "monitoring" in params.sources:
        per_day = p.metric_services * METRICS_PER_SERVICE
        pairs = zip(months, days, strict=True)
        counts |= {("monitoring", "metric_daily", m): per_day * n for m, n in pairs}
    return counts


def planner_id(planner: Planner) -> str:
    """Stable name of a planner (module and qualified name) used as the `plant_seq` key."""
    return f"{planner.__module__}.{planner.__qualname__}"


def plant_range(cat: Catalog, planner: Planner, key: MonthKey) -> tuple[int, int]:
    """(first seq, count) of `planner`'s plant records in month `key`; (0, 0) if none."""
    return cat.plant_seq.get((planner_id(planner), key), (0, 0))


def background_count(cat: Catalog, key: MonthKey) -> int:
    """Background records of month `key`: its planned count minus the plant records."""
    plants = sum(n for (_, k), (_, n) in cat.plant_seq.items() if k == key)
    return cat.month_counts.get(key, 0) - plants


def apply_planners(stub: Catalog, params: SynthParams, planners: Sequence[Planner]) -> Catalog:
    """Add planned plant counts, then number each month: background records first, then
    each planner's positive count in planner order (`plant_seq`). A negative count (T6
    thinning) shrinks the background instead."""
    counts = dict(stub.month_counts)
    booked: list[tuple[str, MonthKey, int]] = []
    for planner in planners:
        for key, extra in planner(stub, params).items():
            if key[0] in params.sources:
                counts[key] = counts.get(key, 0) + extra
                if extra > 0:
                    booked.append((planner_id(planner), key, extra))
    if any(n < 0 for n in counts.values()):
        msg = "planned plant counts make a month count negative"
        raise SynthUsageError(msg, key="preset")
    ordered = {key: counts[key] for key in sorted(counts)}
    seq: dict[MonthKey, int] = {}
    running: dict[tuple[str, str], int] = {}
    for (source, entity, month), n in ordered.items():
        seq[source, entity, month] = running.get((source, entity), 1)
        running[source, entity] = seq[source, entity, month] + n
    cursor = {key: seq[key] + n for key, n in ordered.items()}
    plant_seq: dict[tuple[str, MonthKey], tuple[int, int]] = {}
    for pid, key, n in reversed(booked):  # plants fill the month's tail, last planner last
        cursor[key] -= n
        plant_seq[pid, key] = (cursor[key], n)
    return dataclasses.replace(stub, month_counts=ordered, seq_start=seq, plant_seq=plant_seq)


__all__ = [
    "apply_planners",
    "background_count",
    "month_counts",
    "planner_id",
    "plant_range",
    "span_months",
    "split_counts",
]
