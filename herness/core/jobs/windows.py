"""Schedule-window evaluation and preemption math (U08-66, U08-68, U08-69; design 08 §5.10).

Windows come from `cfg.resilience.schedule.windows` and are read in
`cfg.weights.business_timezone`. Local window bounds go through `resolve_local` (DST gap →
next valid minute, ambiguous → `fold=0`), so adjacent windows share one boundary instant.
Every returned instant is aware UTC. No store access; the functions are pure given config.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.jobs.cron import resolve_local
from herness.core.logging import get_logger

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from herness.core.resilience.settings import WindowSpec
    from herness.core.types import GpuClass

__all__ = ["ActiveWindow", "next_window_allowing", "preempt_deadline", "window_at"]

_DAYS: Final = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_DAY: Final = dt.timedelta(days=1)
_HOUR: Final = dt.timedelta(hours=1)
_MINUTE: Final = dt.timedelta(minutes=1)
_SEARCH: Final = dt.timedelta(days=8)  # U08-68 walk limit

_log = get_logger("jobs")


@dataclass(frozen=True, slots=True)
class ActiveWindow:
    """One occurrence of a schedule window: `start_at <= t < end_at`, both aware UTC."""

    spec: WindowSpec
    start_at: dt.datetime
    end_at: dt.datetime


def _schedule() -> tuple[Sequence[WindowSpec], ZoneInfo]:
    cfg = get_config()
    return cfg.resilience.schedule.windows, clock.zone(cfg.weights.business_timezone)


def _local(day: dt.date, hhmm: str, tz: ZoneInfo) -> dt.datetime:
    hours, _, minutes = hhmm.partition(":")
    naive = dt.datetime.combine(day, dt.time(int(hours), int(minutes)))
    return resolve_local(naive, tz)


def _occurrences(
    windows: Sequence[WindowSpec], now: dt.datetime, tz: ZoneInfo
) -> Iterator[ActiveWindow]:
    """Occurrences starting on the local date of `now` or the day before (U08-66 step 2)."""
    today = now.astimezone(tz).date()
    for window in windows:
        for day in (today, today - _DAY):
            if window.days is not None and _DAYS[day.weekday()] not in window.days:
                continue
            end_day = day + _DAY if window.end <= window.start else day
            start_at, end_at = _local(day, window.start, tz), _local(end_day, window.end, tz)
            if start_at <= now < end_at:
                yield ActiveWindow(window, start_at, end_at)


def _match(windows: Sequence[WindowSpec], now: dt.datetime, tz: ZoneInfo) -> ActiveWindow | None:
    found = list(_occurrences(windows, now, tz))
    return max(found, key=lambda w: w.start_at) if found else None


def window_at(now: dt.datetime) -> ActiveWindow:
    """The window active at `now` in the business time zone (U08-66).

    None matching (a DST-shortened minute) → the window matched at `now + 1 hour`; several
    matching → the one with the latest `start_at`. Raises SchemaViolation for a naive `now`
    and ConfigError when no window covers either instant (config not validated by U08-67).
    """
    if now.tzinfo is None:
        msg = "window_at needs an aware datetime"
        raise SchemaViolation(msg)
    windows, tz = _schedule()
    active = _match(windows, now, tz) or _match(windows, now + _HOUR, tz)
    if active is None:
        msg = "no schedule window covers the instant; check schedule.windows"
        raise ConfigError(msg)
    return active


def next_window_allowing(cls: GpuClass, now: dt.datetime) -> dt.datetime:
    """The first instant at or after `now` whose window allows `cls` (U08-68).

    Walks successive windows for at most 8 days; none found → `now + 1 day` and a WARNING
    `jobs.window.class_never_allowed`.
    """
    if cls == "none":
        return now
    window = window_at(now)
    if cls in window.spec.classes:
        return now
    limit = now + _SEARCH
    while window.end_at <= limit:
        window = window_at(window.end_at)  # end_at strictly grows, so the walk ends
        if cls in window.spec.classes:
            return window.start_at
    _log.warning("jobs.window.class_never_allowed", gpu_class=cls)
    return now + _DAY


def preempt_deadline(
    cls: GpuClass, class_since: dt.datetime, now: dt.datetime
) -> dt.datetime | None:
    """When `stop("preempt")` is due for a running GPU job of class `cls` (U08-69).

    `None` when the class is `none`, allowed in the active window, or was loaded inside it
    (an in-job switch, allowed until the next boundary). Otherwise the active window's start,
    plus the previous window's `overrun_max_min` unless the active window is `hard_start`.
    """
    if cls == "none":
        return None
    window = window_at(now)
    if cls in window.spec.classes or class_since >= window.start_at:
        return None
    if window.spec.hard_start:
        return window.start_at
    previous = window_at(window.start_at - _MINUTE)
    return window.start_at + dt.timedelta(minutes=previous.spec.overrun_max_min)
