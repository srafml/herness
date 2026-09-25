"""Tests for herness.core.time (U00-09 … U00-18)."""

import asyncio
import datetime
import math
import time
import zoneinfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation

pytestmark = pytest.mark.unit

UTC = datetime.UTC


def _tz(hours: float) -> datetime.timezone:
    return datetime.timezone(datetime.timedelta(hours=hours))


def test_ut00_09_now_is_utc() -> None:
    """UT00-09 now() returns an aware UTC datetime."""
    assert clock.now().tzinfo is UTC


def test_ut00_10_sleep_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT00-10 sleep delegates valid durations and rejects invalid ones."""
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", calls.append)
    clock.sleep(0.5)
    assert calls == [0.5]
    for bad in (-1, math.nan, 3601):
        with pytest.raises(SchemaViolation):
            clock.sleep(bad)


@pytest.mark.asyncio
async def test_ut00_11_asleep_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT00-11 asleep awaits asyncio.sleep for valid durations only."""
    calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await clock.asleep(0.1)
    assert calls == [0.1]
    with pytest.raises(SchemaViolation):
        await clock.asleep(-1)


def test_ut00_12_monotonic_non_decreasing() -> None:
    """UT00-12 monotonic never goes backwards."""
    values = [clock.monotonic() for _ in range(1000)]
    assert values == sorted(values)


def test_ut00_13_ensure_utc() -> None:
    """UT00-13 aware values convert to UTC; naive datetimes and dates are rejected."""
    value = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=_tz(2))
    assert clock.ensure_utc(value) == datetime.datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    with pytest.raises(SchemaViolation):
        clock.ensure_utc(datetime.datetime(2026, 9, 24))  # noqa: DTZ001
    with pytest.raises(SchemaViolation):
        clock.ensure_utc(datetime.date(2026, 9, 24))


def test_ut00_14_format_utc() -> None:
    """UT00-14 fixed-width zero-padded UTC text."""
    assert clock.format_utc(datetime.datetime(5, 1, 2, 3, 4, 5, 6, tzinfo=UTC)) == (
        "0005-01-02T03:04:05.000006Z"
    )
    text = clock.format_utc(datetime.datetime(2026, 9, 24, 5, 30, tzinfo=_tz(5.5)))
    assert text == "2026-09-24T00:00:00.000000Z"
    assert len(text) == clock.DB_TS_LEN


def test_ut00_15_parse_utc() -> None:
    """UT00-15 strict inverse of format_utc; invalid text never echoed in the error."""
    text = "2026-09-24T10:00:00.123456Z"
    assert clock.format_utc(clock.parse_utc(text)) == text
    for bad in (text[:-1], text + "0", text[:-1] + "0", "2026-02-30T00:00:00.000000Z"):
        with pytest.raises(SchemaViolation) as info:
            clock.parse_utc(bad)
        assert all(bad not in str(v) for v in info.value.context.values())


def test_ut00_16_parse_iso() -> None:
    """UT00-16 lenient ISO parsing that still rejects naive values.

    RF-3: operator input with surrounding whitespace and a lowercase z parses.
    """
    assert clock.parse_iso("2026-09-24") == datetime.datetime(2026, 9, 24, tzinfo=UTC)
    assert clock.parse_iso(" 2026-09-24T10:00:00z ") == datetime.datetime(
        2026, 9, 24, 10, tzinfo=UTC
    )
    assert clock.parse_iso("2026-09-24T10:00:00Z") == datetime.datetime(2026, 9, 24, 10, tzinfo=UTC)
    assert clock.parse_iso("2026-09-24T10:00:00+02:00") == datetime.datetime(
        2026, 9, 24, 8, tzinfo=UTC
    )
    for bad in ("2026-09-24T10:00:00", "1" * 65, ""):
        with pytest.raises(SchemaViolation):
            clock.parse_iso(bad)


def test_ut00_17_utc_day() -> None:
    """UT00-17 utc_day of 23:30 at -02:00 is the next day's date.

    RF-2: utc_day of an aware +05:30 time just after midnight is the previous UTC day.
    """
    value = datetime.datetime(2026, 9, 24, 23, 30, tzinfo=_tz(-2))
    assert clock.utc_day(value) == "2026-09-25"
    value = datetime.datetime(2026, 9, 25, 0, 10, tzinfo=_tz(5.5))
    assert clock.utc_day(value) == "2026-09-24"


def test_ut00_18_zone() -> None:
    """UT00-18 known zones resolve; bad names raise ConfigError with zone in context."""
    assert isinstance(clock.zone("Europe/London"), zoneinfo.ZoneInfo)
    for bad in ("Mars/Base", "../etc/passwd", ""):
        with pytest.raises(ConfigError) as info:
            clock.zone(bad)
        assert "zone" in info.value.context


@given(
    st.datetimes(
        min_value=datetime.datetime(1, 1, 2),  # noqa: DTZ001
        max_value=datetime.datetime(9999, 12, 30),  # noqa: DTZ001
        timezones=st.sampled_from([UTC, _tz(5.5), _tz(-8), _tz(14)]),
    ),
    st.datetimes(
        min_value=datetime.datetime(1, 1, 2),  # noqa: DTZ001
        max_value=datetime.datetime(9999, 12, 30),  # noqa: DTZ001
        timezones=st.just(UTC),
    ),
)
def test_pt00_02_round_trip_and_order(a: datetime.datetime, b: datetime.datetime) -> None:
    """PT00-02 parse_utc(format_utc(d)) round-trips and text order equals time order."""
    assert clock.parse_utc(clock.format_utc(a)) == a.astimezone(UTC)
    ta, tb = clock.format_utc(a), clock.format_utc(b)
    assert (ta < tb) == (a < b)


def test_st00_14_hostile_timestamps() -> None:
    """ST00-14 injected, oversized and full-width inputs raise without echoing input."""
    inputs = [
        "2026-01-01T00:00:00.000000Z' OR 1=1",
        "9" * 10_000,
        "２０２６-01-01T00:00:00.000000Z",  # noqa: RUF001 - deliberate full-width digits (ST00-14)
    ]
    for text in inputs:
        for parse in (clock.parse_utc, clock.parse_iso):
            with pytest.raises(SchemaViolation) as info:
                parse(text)
            assert all(text not in str(v) for v in info.value.context.values())
