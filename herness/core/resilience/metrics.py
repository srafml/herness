"""Component metric recording for the ops `metric_sample` table (impl 08 U08-19 … U08-22,
U08-103; ENG §4, delta E5, R-12).

L0 callers record into the process's `MetricBuffer` (`ProcessState.metric_buffer`, guarded by
`ProcessState.lock`); `flush_metrics` moves it into rows through the bound ops port, whose
`insert_metric_samples` is the single writer `herness.store.ops.metrics.record_metric_samples`.
Metrics are best effort: a flush problem is logged and never raised to the recording call.
"""

from __future__ import annotations

import functools
import math
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Final, Literal, cast

from pydantic import ValidationError

from herness.core import time as clock
from herness.core.errors import ConfigError, HernessError
from herness.core.logging import get_logger
from herness.core.resilience._metric_buffer import MetricBuffer, MetricKey
from herness.core.resilience._state import process_state
from herness.core.types import MetricSample

METRIC_FLUSH_INTERVAL_S: Final = 10
METRIC_BUFFER_MAX: Final = 10_000  # histogram observations per buffer (TH08-10)
METRIC_GAUGE_KEYS_MAX: Final = 1000
HISTOGRAM_FLUSH_AT: Final = 1000
MAX_LABELS: Final = 6

_NAME_RE: Final = re.compile(r"herness_[a-z][a-z0-9]*(_[a-z0-9]+)+")
_COMPONENT_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
_LABEL_KEY_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
_LABEL_VALUE_RE: Final = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")
_UNITS: Final[dict[str, frozenset[str]]] = {
    "counter": frozenset({"total", "seconds", "bytes", "ratio", "count", "mb"}),
    "histogram": frozenset({"total", "seconds", "bytes", "ratio", "count", "mb"}),
    "gauge": frozenset({"bytes", "ratio", "count", "mb", "seconds"}),
    "timed": frozenset({"seconds"}),
}

type _Kind = Literal["counter", "histogram", "gauge", "timed"]

_log: Final = get_logger("resilience")


def _invalid(name: str, what: str) -> ConfigError:
    msg = f"invalid metric {name[:80]}: {what}"
    return ConfigError(msg)


@functools.lru_cache(maxsize=1024)
def _check_name(name: str, component: str, kind: _Kind) -> None:
    """Validate once per (name, component, kind); invalid input is never cached (it raises)."""
    if _NAME_RE.fullmatch(name) is None:
        raise _invalid(name, "name")
    parts = name.split("_")
    if parts[-1] not in _UNITS[kind]:
        raise _invalid(name, f"unit {parts[-1]} not allowed for a {kind}")
    if component != parts[1] or _COMPONENT_RE.fullmatch(component) is None:
        raise _invalid(name, "component must equal the second name segment")


def _key(name: str, component: str, labels: Mapping[str, str] | None, kind: _Kind) -> MetricKey:
    _check_name(name, component, kind)
    pairs = cast("Mapping[object, object]", labels or {})  # checked at run time, not trusted
    if len(pairs) > MAX_LABELS:
        raise _invalid(name, f"more than {MAX_LABELS} labels")
    for key, value in pairs.items():
        if not isinstance(key, str) or _LABEL_KEY_RE.fullmatch(key) is None:
            raise _invalid(name, "label key")
        if not isinstance(value, str) or _LABEL_VALUE_RE.fullmatch(value) is None:
            raise _invalid(name, f"label value of {key}")  # the value itself is never echoed
    return name, tuple(sorted((labels or {}).items())), component


def _number(name: str, value: float, *, negative_ok: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _invalid(name, "value must be a number")
    number = float(value)
    if not math.isfinite(number) or (number < 0 and not negative_ok):
        raise _invalid(name, "value must be finite" + ("" if negative_ok else " and >= 0"))
    return number


def _interval_due(buffer: MetricBuffer) -> bool:
    return clock.monotonic() - buffer.last_flush >= METRIC_FLUSH_INTERVAL_S


def record_counter(
    name: str,
    value: float = 1.0,
    *,
    component: str,
    labels: Mapping[str, str] | None = None,
) -> None:
    """Add ``value`` to the counter aggregate of (name, labels, component) (U08-19).

    Raises ConfigError naming the metric for an invalid name, labels or value.
    """
    key = _key(name, component, labels, "counter")
    amount = _number(name, value)
    state = process_state()
    with state.lock:
        buffer = state.metric_buffer
        buffer.counters[key] = buffer.counters.get(key, 0.0) + amount
        due = _interval_due(buffer)
    if due:
        flush_metrics()


def record_histogram(
    name: str,
    value: float,
    *,
    component: str,
    labels: Mapping[str, str] | None = None,
) -> None:
    """Append one histogram observation (U08-20); past 10 000 it is dropped and counted.

    Raises ConfigError naming the metric for an invalid name, labels or value.
    """
    key = _key(name, component, labels, "histogram")
    amount = _number(name, value)
    state = process_state()
    with state.lock:
        buffer = state.metric_buffer
        if len(buffer.histograms) >= METRIC_BUFFER_MAX:
            buffer.dropped += 1
        else:
            buffer.histograms.append((key, amount))
        due = len(buffer.histograms) >= HISTOGRAM_FLUSH_AT or _interval_due(buffer)
    if due:
        flush_metrics()


def record_gauge(
    name: str,
    value: float,
    *,
    component: str,
    labels: Mapping[str, str] | None = None,
) -> None:
    """Set the latest value of a gauge (U08-103); last write wins, at most 1 000 keys.

    Raises ConfigError naming the metric for an invalid name, labels or a non-finite value.
    """
    key = _key(name, component, labels, "gauge")
    number = _number(name, value, negative_ok=True)
    state = process_state()
    with state.lock:
        buffer = state.metric_buffer
        if key not in buffer.gauges and len(buffer.gauges) >= METRIC_GAUGE_KEYS_MAX:
            buffer.dropped += 1
        else:
            buffer.gauges[key] = (number, clock.now())
        due = _interval_due(buffer)
    if due:
        flush_metrics()


@contextmanager
def timed(name: str, *, component: str, labels: Mapping[str, str] | None = None) -> Iterator[None]:
    """Record the block's `perf_counter` elapsed seconds as a histogram on exit, normal or
    exception (U08-21). The name (unit `seconds`) and labels are checked on entry."""
    _key(name, component, labels, "timed")
    started = time.perf_counter()
    try:
        yield
    finally:
        record_histogram(name, time.perf_counter() - started, component=component, labels=labels)


def _sample(
    key: MetricKey, kind: Literal["counter", "gauge", "histogram"], value: float, ts: datetime
) -> MetricSample:
    name, items, component = key
    return MetricSample(
        ts=ts, name=name, kind=kind, value=value, labels=dict(items), component=component
    )


def _samples(buffer: MetricBuffer, now: datetime) -> list[MetricSample]:
    rows = [_sample(key, "counter", total, now) for key, total in buffer.counters.items()]
    rows += [_sample(key, "gauge", value, at) for key, (value, at) in buffer.gauges.items()]
    rows += [_sample(key, "histogram", value, now) for key, value in buffer.histograms]
    return rows


def flush_metrics() -> int:
    """Move the buffer into `metric_sample` rows and return the number written (U08-22).

    With the ops port unbound the buffer is kept (bounded by its caps) and 0 is returned.
    A store failure logs `resilience.metrics.flush_failed` and discards the rows.
    """
    state = process_state()
    ops = state.ops
    if ops is None:
        return 0
    with state.lock:
        buffer = state.metric_buffer
        state.metric_buffer = MetricBuffer(last_flush=clock.monotonic())
    if buffer.dropped:
        _log.warning("resilience.metrics.dropped", count=buffer.dropped)
    rows = len(buffer.counters) + len(buffer.gauges) + len(buffer.histograms)
    if not rows:
        return 0
    try:
        return ops.insert_metric_samples(_samples(buffer, clock.now()))
    except (HernessError, ValidationError) as exc:
        _log.warning("resilience.metrics.flush_failed", rows=rows, error_type=type(exc).__name__)
        return 0
