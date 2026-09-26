"""Five-field cron in the business timezone and DST resolution (design 08 §5.11; U08-64, U08-65).

No extra dependency: `CronExpr.parse` reads `minute hour day-of-month month day-of-week` with
`*`, `*/n`, `a`, `a-b`, `a-b/n` and comma lists; day-of-week also takes `MON`-`SUN` and reads
`7` as Sunday. Day matching follows the Vixie rule. Local wall times resolve with
`resolve_local`: a DST gap moves to the next valid minute, an ambiguous time uses `fold=0`.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, NoReturn
from zoneinfo import ZoneInfo

from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation

__all__ = ["CronExpr", "resolve_local"]

_CRON_FIELDS: Final = 5
_NEXT_DAYS: Final = 1830
_LATEST_DAYS: Final = 366
_GAP_STEPS: Final = 180
# `latest_at_or_before` still resolves first-date candidates up to this far after the start
# minute: when `t` is in the second pass of a DST fold, a later wall time resolved with fold=0
# can be an instant at or before `t`. (`next_after` needs no margin: an earlier wall time
# always resolves at or before `t`.)
_FOLD_MARGIN: Final = dt.timedelta(hours=3)
_MINUTE: Final = dt.timedelta(minutes=1)
_DAY: Final = dt.timedelta(days=1)
_NUMBER: Final = re.compile(r"[0-9]+")
_MAX_DIGITS: Final = 9
_ITEM: Final = re.compile(r"(?P<base>\*|[^-/]+|[^-/]+-[^-/]+)(?:/(?P<step>[^/]*))?")
_WEEKDAYS: Final = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
# (name, low, high) per field; day-of-week accepts 7 and maps it to 0 afterwards.
_SPECS: Final = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
)


class _FieldError(Exception):
    """A field-level parse problem; `parse` turns it into `ConfigError`."""


def _fail(reason: str) -> NoReturn:
    raise _FieldError(reason)


def _value(token: str, names: bool) -> int:
    upper = token.upper()
    if names and upper in _WEEKDAYS:
        return _WEEKDAYS[upper]
    if _NUMBER.fullmatch(token) is None:
        _fail(f"has a non-numeric value '{token}'")
    # Clamp long digit strings (int() refuses > 4300 digits); the range check rejects them.
    return int(token) if len(token) <= _MAX_DIGITS else 10**_MAX_DIGITS


def _item(item: str, low: int, high: int, names: bool) -> range:
    match = _ITEM.fullmatch(item)
    if match is None:
        _fail(f"has a malformed item '{item}'")
    base, step_text = match["base"], match["step"]
    step = 1 if step_text is None else _value(step_text, names=False)
    if step < 1:
        _fail("step must be >= 1")
    if step_text is not None and base != "*" and "-" not in base:
        _fail(f"step needs '*' or a range in '{item}'")
    if base == "*":
        return range(low, high + 1, step)
    first, _, last = base.partition("-")
    start = _value(first, names)
    stop = _value(last, names) if last else start
    if not (low <= start <= high and low <= stop <= high):
        _fail(f"value out of range {low}-{high}")
    if start > stop:
        _fail(f"range '{base}' is reversed")
    return range(start, stop + 1, step)


def _field(text: str, low: int, high: int, names: bool) -> frozenset[int]:
    values: set[int] = set()
    for item in text.split(","):
        values.update(_item(item, low, high, names))
    return frozenset(values)


@dataclass(frozen=True)
class CronExpr:
    """A parsed five-field cron (U08-64); `text` is the original expression."""

    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    dom_star: bool
    dow_star: bool

    @classmethod
    def parse(cls, text: str) -> CronExpr:
        """Parse `text`; `ConfigError("invalid cron '<text>': <field> <reason>")` on failure."""
        fields = text.split()
        if len(fields) != _CRON_FIELDS:
            msg = f"invalid cron '{text}': fields expected {_CRON_FIELDS}, got {len(fields)}"
            raise ConfigError(msg)
        parsed: list[frozenset[int]] = []
        for raw, (name, low, high) in zip(fields, _SPECS, strict=True):
            try:
                parsed.append(_field(raw, low, high, names=name == "day-of-week"))
            except _FieldError as exc:
                msg = f"invalid cron '{text}': {name} {exc}"
                raise ConfigError(msg) from None
        minutes, hours, days, months, dow = parsed
        weekdays = frozenset(day % 7 for day in dow)
        return cls(text, minutes, hours, days, months, weekdays, fields[2] == "*", fields[4] == "*")

    def matches_date(self, day: dt.date) -> bool:
        """Whether `day` (local) is a fire date: month plus the Vixie day-of-month/week rule."""
        if day.month not in self.months:
            return False
        dom = day.day in self.days
        dow = day.isoweekday() % 7 in self.weekdays
        if self.dom_star or self.dow_star:
            return (self.dom_star or dom) and (self.dow_star or dow)
        return dom or dow

    def _times(self, day: dt.date, *, reverse: bool) -> Iterator[dt.datetime]:
        for hour in sorted(self.hours, reverse=reverse):
            for minute in sorted(self.minutes, reverse=reverse):
                yield dt.datetime.combine(day, dt.time(hour, minute))

    def next_after(self, t: dt.datetime, tz: ZoneInfo) -> dt.datetime:
        """First fire strictly after `t` as an aware UTC instant (walks at most 1 830 days)."""
        t = clock.ensure_utc(t)
        start = t.astimezone(tz).replace(second=0, microsecond=0, tzinfo=None) + _MINUTE
        day = start.date()
        for _ in range(_NEXT_DAYS):
            if self.matches_date(day):
                for naive in self._times(day, reverse=False):
                    if naive >= start and (fire := resolve_local(naive, tz)) > t:
                        return fire
            day += _DAY
        msg = f"cron never fires: {self.text}"
        raise ConfigError(msg)

    def latest_at_or_before(self, t: dt.datetime, tz: ZoneInfo) -> dt.datetime | None:
        """Latest fire at or before `t` within 366 days (aware UTC), else `None`."""
        t = clock.ensure_utc(t)
        start = t.astimezone(tz).replace(second=0, microsecond=0, tzinfo=None)
        ceiling = start + _FOLD_MARGIN
        day = start.date()
        # 367 dates (the start date and 366 back): a yearly cron is found across a leap year,
        # e.g. `30 23 1 3 *` at 2024-03-01 23:00 finds 2023-03-01, 366 dates back.
        for _ in range(_LATEST_DAYS + 1):
            if self.matches_date(day):
                for naive in self._times(day, reverse=True):
                    if naive <= ceiling and (fire := resolve_local(naive, tz)) <= t:
                        return fire
            day -= _DAY
        return None


def resolve_local(naive: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """Resolve a naive local time in `tz` to aware UTC (U08-65).

    A time in a DST gap moves forward minute by minute (at most 180 steps) to the first valid
    one; an ambiguous time uses `fold=0` only. Two gap minutes can resolve to one instant.
    """
    if naive.tzinfo is not None:
        msg = "resolve_local needs a naive datetime"
        raise SchemaViolation(msg)
    candidate = naive
    for _ in range(_GAP_STEPS + 1):
        instant = candidate.replace(tzinfo=tz, fold=0).astimezone(dt.UTC)
        if instant.astimezone(tz).replace(tzinfo=None) == candidate:
            return instant
        candidate += _MINUTE
    msg = f"local time cannot be resolved in {tz.key}"
    raise ConfigError(msg)
