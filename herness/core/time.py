"""UTC clock, bounded sleeps, fixed-width UTC timestamp text and time zones (design 00 §8).

Callers import this module as ``from herness.core import time as clock`` and call
``clock.now()``, so the test FakeClock (impl 11) can patch the module attributes.
"""

from __future__ import annotations

import asyncio
import datetime
import math
import re
import time
import zoneinfo
from typing import Final

from herness.core.errors import ConfigError, SchemaViolation

DB_TS_LEN: Final = 27
MAX_SLEEP_S: Final = 3600.0
_MAX_ISO_CHARS: Final = 64
_MAX_ZONE_CHARS: Final = 64
_DB_TS_RE: Final = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{6})Z", re.ASCII
)
_DATE_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_ZONE_RE: Final = re.compile(r"[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*")


def now() -> datetime.datetime:
    """Return the current wall-clock time as an aware UTC datetime; the only clock read."""
    return datetime.datetime.now(datetime.UTC)


def _check_duration(seconds: float) -> None:
    msg = "invalid sleep duration"
    if not math.isfinite(seconds):
        raise SchemaViolation(msg, seconds=repr(seconds))
    if seconds < 0 or seconds > MAX_SLEEP_S:
        raise SchemaViolation(msg, seconds=seconds)


def sleep(seconds: float) -> None:
    """Block the thread for 0..3600 s. Raises SchemaViolation (seconds) when out of range."""
    _check_duration(seconds)
    time.sleep(seconds)


async def asleep(seconds: float) -> None:
    """Non-blocking bounded sleep. Raises SchemaViolation (seconds) when out of range."""
    _check_duration(seconds)
    await asyncio.sleep(seconds)


def monotonic() -> float:
    """Monotonic seconds for measuring durations."""
    return time.monotonic()


def ensure_utc(dt: datetime.date) -> datetime.datetime:
    """Reject naive datetimes and normalise aware ones to UTC.

    Raises SchemaViolation (got) for a non-datetime and SchemaViolation for a naive value.
    """
    if not isinstance(dt, datetime.datetime):
        msg = "expected datetime"
        raise SchemaViolation(msg, got=type(dt).__name__)
    if dt.tzinfo is None or dt.utcoffset() is None:
        msg = "naive datetime rejected"
        raise SchemaViolation(msg)
    return dt.astimezone(datetime.UTC)


def format_utc(dt: datetime.datetime) -> str:
    """Fixed-width UTC text ``YYYY-MM-DDTHH:MM:SS.ffffffZ``. Raises as ensure_utc."""
    u = ensure_utc(dt)
    return (
        f"{u.year:04d}-{u.month:02d}-{u.day:02d}T"
        f"{u.hour:02d}:{u.minute:02d}:{u.second:02d}.{u.microsecond:06d}Z"
    )


def parse_utc(text: object) -> datetime.datetime:
    """Strict inverse of format_utc. Raises SchemaViolation (length); input never echoed."""
    msg = "bad timestamp text"
    if not isinstance(text, str):
        raise SchemaViolation(msg, length=-1)
    if len(text) != DB_TS_LEN:
        raise SchemaViolation(msg, length=len(text))
    match = _DB_TS_RE.fullmatch(text)
    if match is None:
        raise SchemaViolation(msg, length=len(text))
    year, month, day, hour, minute, second, microsecond = (int(g) for g in match.groups())
    try:
        return datetime.datetime(
            year, month, day, hour, minute, second, microsecond, tzinfo=datetime.UTC
        )
    except ValueError as exc:
        raise SchemaViolation(msg, length=len(text)) from exc


def parse_iso(text: str) -> datetime.datetime:
    """Lenient ISO-8601 parser for operator input. Raises SchemaViolation (length)."""
    s = text.strip()
    msg = "bad ISO timestamp"
    if not 0 < len(s) <= _MAX_ISO_CHARS:
        raise SchemaViolation(msg, length=len(s))
    try:
        if _DATE_RE.fullmatch(s):
            day = datetime.date.fromisoformat(s)
            return datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.UTC)
        if s[-1] in "Zz":
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
    except ValueError as exc:
        raise SchemaViolation(msg, length=len(s)) from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        msg = "naive datetime rejected"
        raise SchemaViolation(msg)
    return dt.astimezone(datetime.UTC)


def utc_day(dt: datetime.datetime) -> str:
    """UTC date text ``YYYY-MM-DD``. Raises as ensure_utc."""
    return format_utc(dt)[:10]


def zone(name: str) -> zoneinfo.ZoneInfo:
    """Resolve an IANA time zone from tzdata. Raises ConfigError (zone) when invalid."""
    msg = "unknown time zone"
    if not 0 < len(name) <= _MAX_ZONE_CHARS or _ZONE_RE.fullmatch(name) is None or ".." in name:
        raise ConfigError(msg, zone=name)
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ConfigError(msg, zone=name) from exc
