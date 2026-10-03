"""Benchmark measurement helpers (T05-28): repeats, the median and the uniform report line.

Every impl 05 benchmark warms up, measures its statistic over the full stated dataset
`REPEATS` times and gates on the median, so one noisy repeat on a loaded host neither passes
nor fails a target alone. `report` writes one line per statistic to stderr:
`BT05-xx <stat> <median> <unit> (target <target>) repeats=[<r1>, <r2>, <r3>]`.
"""

from __future__ import annotations

import statistics
import sys
from collections.abc import Sequence
from typing import Final

__all__ = ["REPEATS", "report"]

REPEATS: Final = 3


def report(bench_id: str, stat: str, values: Sequence[float], unit: str, target: str) -> float:
    """Write the uniform stderr line for `values` (one per repeat); return their median."""
    if len(values) < REPEATS:
        msg = f"{bench_id} {stat}: {len(values)} repeats, need at least {REPEATS}"
        raise ValueError(msg)
    median = statistics.median(values)
    each = ", ".join(f"{value:.4f}" for value in values)
    sys.stderr.write(f"{bench_id} {stat} {median:.4f} {unit} (target {target}) repeats=[{each}]\n")
    return median
