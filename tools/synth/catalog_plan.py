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


def month_counts(params: SynthParams) -> dict[MonthKey, int]:
    """Background counts per (source, entity, month): preset totals split by days in span."""
    months: list[date] = []
    days: list[int] = []
    for offset in range((params.end - params.start).days + 1):
        day = params.start + timedelta(days=offset)
        if not months or day.month != months[-1].month:
            months.append(day.replace(day=1))
            days.append(0)
        days[-1] += 1
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


def apply_planners(stub: Catalog, params: SynthParams, planners: Sequence[Planner]) -> Catalog:
    counts = dict(stub.month_counts)
    for planner in planners:
        for key, extra in planner(stub, params).items():
            if key[0] in params.sources:
                counts[key] = counts.get(key, 0) + extra
    if any(n < 0 for n in counts.values()):
        msg = "planned plant counts make a month count negative"
        raise SynthUsageError(msg, key="preset")
    ordered = {key: counts[key] for key in sorted(counts)}
    seq: dict[MonthKey, int] = {}
    running: dict[tuple[str, str], int] = {}
    for (source, entity, month), n in ordered.items():
        seq[source, entity, month] = running.get((source, entity), 1)
        running[source, entity] = seq[source, entity, month] + n
    return dataclasses.replace(stub, month_counts=ordered, seq_start=seq)


__all__ = ["apply_planners", "month_counts", "split_counts"]
