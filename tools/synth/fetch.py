"""`_fetched_at` placement of lake rows into fetch partitions (U11-17, design §5.1.7).

`initial` (default): every background record lands in the initial load on day `end + 1`
at a uniform time of day; every re-emit (duplicate, later version, tombstone) lands in
one of the 14 daily increments, day `end + 1 + d` with `d` uniform in 1..14, also at a
uniform time of day. `daily`: every record lands at `_source_updated_at + U(0, 6 h)`, the
source timestamp coming from the caller's `updated_at_of` (see
`tools.synth.flatten.source_updated_at`). Draws only from `rng`.
"""

from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any, Final

import numpy as np

from tools.synth.params import SynthParams

__all__ = ["assign_fetch"]

_DAY_S: Final = 86_400.0
_DAILY_LAG_S: Final = 6 * 3_600.0
_INCREMENTS: Final = 14


def assign_fetch(
    records: list[dict[str, Any]],
    reemits: list[dict[str, Any]],
    params: SynthParams,
    rng: np.random.Generator,
    *,
    updated_at_of: Callable[[dict[str, Any]], datetime],
) -> list[tuple[dict[str, Any], datetime]]:
    """`(record, _fetched_at)` for `records` then `reemits`, in input order (aware UTC)."""
    if params.fetch_mode == "daily":
        rows = [*records, *reemits]
        lags = rng.uniform(0.0, _DAILY_LAG_S, len(rows))
        return [
            (rec, updated_at_of(rec) + timedelta(seconds=float(lag)))
            for rec, lag in zip(rows, lags, strict=True)
        ]
    load = datetime.combine(params.end + timedelta(days=1), time(), UTC)
    out = [
        (rec, load + timedelta(seconds=float(s)))
        for rec, s in zip(records, rng.uniform(0.0, _DAY_S, len(records)), strict=True)
    ]
    days = rng.integers(1, _INCREMENTS + 1, len(reemits))
    seconds = rng.uniform(0.0, _DAY_S, len(reemits))
    out.extend(
        (rec, load + timedelta(days=int(d), seconds=float(s)))
        for rec, d, s in zip(reemits, days, seconds, strict=True)
    )
    return out
