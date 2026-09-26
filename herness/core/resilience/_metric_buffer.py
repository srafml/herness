"""`MetricBuffer`, the in-memory metric aggregate held by `ProcessState.metric_buffer` (U08-19).

A private sibling of `metrics.py` so `_state.py` can build it without importing the recording
module (which imports `_state`). Plain data: every mutation happens under `ProcessState.lock`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# (name, sorted label items, component): the canonical aggregate key of U08-19.
type MetricKey = tuple[str, tuple[tuple[str, str], ...], str]


@dataclass
class MetricBuffer:
    """Counters summed per key, latest gauge per key, raw histogram observations."""

    counters: dict[MetricKey, float] = field(default_factory=dict)
    gauges: dict[MetricKey, tuple[float, datetime]] = field(default_factory=dict)
    histograms: list[tuple[MetricKey, float]] = field(default_factory=list)
    dropped: int = 0  # observations and gauge keys refused by the caps since the last flush
    # `herness.core.time.monotonic()` of the last flush; None on a fresh process buffer, whose
    # 10 s interval starts at its first recording (building one reads no clock).
    last_flush: float | None = None
