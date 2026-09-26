"""Pure window and `as_of` arithmetic in the business timezone (design 04 §3.1, §4.3).

A window is half-open: `start` inclusive, `end` exclusive, both local dates. Timestamps are
local midnight converted to UTC, so DST days bind correctly.
"""

import calendar
import dataclasses
import datetime
import types
from collections.abc import Mapping
from typing import Final, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from herness.core.errors import ConfigError, ToolInputError
from herness.metrics.settings import Period

__all__ = [
    "DEFAULT_PERIOD_COUNTS",
    "MAX_WINDOW_MONTHS",
    "Window",
    "custom_window",
    "default_window",
    "resolve_as_of",
]

# Fallback for tests only; production passes `defaults.windows` (U04-31).
DEFAULT_PERIOD_COUNTS: Final[Mapping[str, int]] = types.MappingProxyType(
    {"week": 26, "month": 24, "quarter": 8}
)
MAX_WINDOW_MONTHS: Final = 36
_T12W_DAYS: Final = 84
_MONTHS_PER_QUARTER: Final = 3
_MONTHS_PER_YEAR: Final = 12


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        msg = "unknown time zone"
        raise ConfigError(msg) from exc


def _local_midnight_utc(day: datetime.date, tz: str) -> datetime.datetime:
    local = datetime.datetime.combine(day, datetime.time(0), tzinfo=_zone(tz))
    return local.astimezone(datetime.UTC)


@dataclasses.dataclass(frozen=True, slots=True)
class Window:
    """One resolved half-open time window in the business timezone (U04-29)."""

    period: Period
    start: datetime.date
    end: datetime.date
    as_of: datetime.date
    tz: str
    is_custom: bool

    def __post_init__(self) -> None:
        if not self.start < self.end:
            msg = "window start must be before end"
            raise ConfigError(msg)
        _zone(self.tz)

    @property
    def start_ts(self) -> datetime.datetime:
        """Local midnight of `start` as an aware UTC datetime."""
        return _local_midnight_utc(self.start, self.tz)

    @property
    def end_ts(self) -> datetime.datetime:
        """Local midnight of `end` as an aware UTC datetime."""
        return _local_midnight_utc(self.end, self.tz)

    @property
    def as_of_ts(self) -> datetime.datetime:
        """Local midnight of `as_of` as an aware UTC datetime."""
        return _local_midnight_utc(self.as_of, self.tz)

    def binds(self) -> dict[str, object]:
        """The window bind values named in U04-29."""
        return {
            "window_start": self.start_ts,
            "window_end": self.end_ts,
            "window_start_date": self.start,
            "window_end_date": self.end,
            "as_of": self.as_of,
            "as_of_ts": self.as_of_ts,
            "tz": self.tz,
        }


def resolve_as_of(
    build_started_at: datetime.datetime, tz: str, override: datetime.date | None, /
) -> datetime.date:
    """`override` when set, else the build start's date in the business timezone (U04-30)."""
    if build_started_at.tzinfo is None or build_started_at.utcoffset() is None:
        msg = "naive build start time"
        raise ConfigError(msg)
    if override is not None:
        return override
    return build_started_at.astimezone(_zone(tz)).date()


def _add_months(day: datetime.date, months: int) -> datetime.date:
    """Shift by calendar months, clamping the day to the target month's length."""
    index = day.year * _MONTHS_PER_YEAR + (day.month - 1) + months
    year, month0 = divmod(index, _MONTHS_PER_YEAR)
    last = calendar.monthrange(year, month0 + 1)[1]
    return datetime.date(year, month0 + 1, min(day.day, last))


def _count(counts: Mapping[str, int], key: str) -> int:
    if key not in counts:
        msg = f"window counts need {key}"
        raise ConfigError(msg)
    return counts[key]


def _default_bounds(
    period: Period, as_of: datetime.date, counts: Mapping[str, int]
) -> tuple[datetime.date, datetime.date]:
    if period not in get_args(Period):
        msg = "unknown period"
        raise ConfigError(msg)
    for key in ("week", "month", "quarter"):
        _count(counts, key)
    if period == "week":
        end = as_of - datetime.timedelta(days=as_of.weekday())
        return end - datetime.timedelta(days=7 * counts["week"]), end
    if period == "month":
        end = as_of.replace(day=1)
        return _add_months(end, -counts["month"]), end
    if period == "quarter":
        first_month = (as_of.month - 1) // _MONTHS_PER_QUARTER * _MONTHS_PER_QUARTER + 1
        end = datetime.date(as_of.year, first_month, 1)
        return _add_months(end, -_MONTHS_PER_QUARTER * counts["quarter"]), end
    if period == "t12w":
        return as_of - datetime.timedelta(days=_T12W_DAYS), as_of
    return _add_months(as_of, -_MONTHS_PER_YEAR), as_of  # t12m


def default_window(
    period: Period, as_of: datetime.date, tz: str, counts: Mapping[str, int], /
) -> Window:
    """Last N complete periods before `as_of`, or the rolling t12w/t12m window (U04-31)."""
    start, end = _default_bounds(period, as_of, counts)
    return Window(period, start, end, as_of, tz, is_custom=False)


def custom_window(
    period: Period, start: datetime.date, end: datetime.date, as_of: datetime.date, tz: str, /
) -> Window:
    """Caller-supplied `[start, end)` window, at most 36 calendar months long (U04-32)."""
    if not start < end:
        msg = "window start must be before end"
        raise ToolInputError(msg)
    if end > _add_months(start, MAX_WINDOW_MONTHS):
        msg = "window longer than 36 months"
        raise ToolInputError(msg)
    return Window(period, start, end, as_of, tz, is_custom=True)
