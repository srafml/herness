"""Tests for herness.metrics.windows (U04-29 … U04-32)."""

import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from herness.core.errors import ConfigError, ToolInputError
from herness.metrics.windows import (
    DEFAULT_PERIOD_COUNTS,
    MAX_WINDOW_MONTHS,
    Window,
    custom_window,
    default_window,
    resolve_as_of,
)

pytestmark = pytest.mark.unit

TZ = "America/New_York"
UTC = dt.UTC
COUNTS = {"week": 2, "month": 3, "quarter": 2}
MONDAY = dt.date(2024, 3, 11)
WEDNESDAY = dt.date(2024, 3, 13)
MONTH_START = dt.date(2024, 3, 1)


def _d(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


# --- UT04-27: as_of and default windows ------------------------------------------------------


def test_ut04_27_resolve_as_of_uses_business_timezone() -> None:
    """UT04-27 as_of is the build start's date in the business timezone."""
    started = dt.datetime(2024, 3, 11, 3, 0, tzinfo=UTC)  # 23:00 on 10 March in New York
    assert resolve_as_of(started, TZ, None) == _d("2024-03-10")
    assert resolve_as_of(started, "UTC", None) == _d("2024-03-11")


def test_ut04_27_resolve_as_of_override_and_naive() -> None:
    """UT04-27 scoring.as_of overrides; a naive build start time is a ConfigError."""
    started = dt.datetime(2024, 3, 11, 3, 0, tzinfo=UTC)
    assert resolve_as_of(started, TZ, _d("2023-12-31")) == _d("2023-12-31")
    with pytest.raises(ConfigError, match="naive build start time"):
        resolve_as_of(dt.datetime(2024, 3, 11, 3, 0), TZ, None)  # noqa: DTZ001 - naive on purpose


@pytest.mark.parametrize(
    ("as_of", "start", "end"),
    [
        (MONDAY, "2024-02-26", "2024-03-11"),
        (WEDNESDAY, "2024-02-26", "2024-03-11"),
        (MONTH_START, "2024-02-12", "2024-02-26"),  # 1 March 2024 is a Friday
    ],
)
def test_ut04_27_default_week_window_complete_weeks(as_of: dt.date, start: str, end: str) -> None:
    """UT04-27 week windows end on the Monday on or before as_of (complete weeks only)."""
    w = default_window("week", as_of, TZ, COUNTS)
    assert (w.start, w.end, w.as_of, w.is_custom) == (_d(start), _d(end), as_of, False)
    assert w.end <= as_of
    assert w.start.weekday() == 0
    assert w.end.weekday() == 0


@pytest.mark.parametrize(
    ("as_of", "month", "quarter"),
    [
        (MONDAY, ("2023-12-01", "2024-03-01"), ("2023-07-01", "2024-01-01")),
        (MONTH_START, ("2023-12-01", "2024-03-01"), ("2023-07-01", "2024-01-01")),
        (_d("2024-04-01"), ("2024-01-01", "2024-04-01"), ("2023-10-01", "2024-04-01")),
        (_d("2024-12-31"), ("2024-09-01", "2024-12-01"), ("2024-04-01", "2024-10-01")),
    ],
)
def test_ut04_27_default_month_and_quarter_windows(
    as_of: dt.date, month: tuple[str, str], quarter: tuple[str, str]
) -> None:
    """UT04-27 month/quarter windows cover the last N complete periods before as_of."""
    m = default_window("month", as_of, TZ, COUNTS)
    q = default_window("quarter", as_of, TZ, COUNTS)
    assert (m.start, m.end) == (_d(month[0]), _d(month[1]))
    assert (q.start, q.end) == (_d(quarter[0]), _d(quarter[1]))
    assert m.end <= as_of
    assert q.end <= as_of


def test_ut04_27_default_rolling_windows_end_at_as_of() -> None:
    """UT04-27 t12w is the 84 days before as_of; t12m the 12 months before; both end at as_of."""
    w = default_window("t12w", WEDNESDAY, TZ, COUNTS)
    assert (w.start, w.end) == (WEDNESDAY - dt.timedelta(days=84), WEDNESDAY)
    m = default_window("t12m", WEDNESDAY, TZ, COUNTS)
    assert (m.start, m.end) == (_d("2023-03-13"), WEDNESDAY)


def test_ut04_27_default_counts_and_missing_key() -> None:
    """UT04-27 the test fallback counts match the spec; a missing count key is a ConfigError."""
    assert dict(DEFAULT_PERIOD_COUNTS) == {"week": 26, "month": 24, "quarter": 8}
    w = default_window("week", MONDAY, TZ, DEFAULT_PERIOD_COUNTS)
    assert w.end - w.start == dt.timedelta(weeks=26)
    with pytest.raises(ConfigError):
        default_window("month", MONDAY, TZ, {"week": 1, "quarter": 1})


def test_ut04_27_window_timestamps_and_binds() -> None:
    """UT04-27 Window timestamps are local midnight in UTC (DST aware); binds match U04-29."""
    w = Window("week", _d("2024-03-10"), _d("2024-03-11"), _d("2024-03-11"), TZ, False)
    assert w.start_ts == dt.datetime(2024, 3, 10, 5, 0, tzinfo=UTC)
    assert w.end_ts == dt.datetime(2024, 3, 11, 4, 0, tzinfo=UTC)
    assert w.as_of_ts == w.end_ts
    assert w.start_ts.utcoffset() == dt.timedelta(0)
    assert w.binds() == {
        "window_start": w.start_ts,
        "window_end": w.end_ts,
        "window_start_date": w.start,
        "window_end_date": w.end,
        "as_of": w.as_of,
        "as_of_ts": w.as_of_ts,
        "tz": TZ,
    }
    local = w.start_ts.astimezone(ZoneInfo(TZ))
    assert (local.hour, local.minute) == (0, 0)


def test_ut04_27_unknown_period_is_config_error() -> None:
    """UT04-27 a period outside the literal is a ConfigError."""
    with pytest.raises(ConfigError, match="unknown period"):
        default_window("year", MONDAY, TZ, COUNTS)  # type: ignore[arg-type]


def test_ut04_27_window_invariants() -> None:
    """UT04-27 Window rejects start >= end and unknown time zones; it is immutable."""
    with pytest.raises(ConfigError):
        Window("week", MONDAY, MONDAY, MONDAY, TZ, False)
    with pytest.raises(ConfigError):
        Window("week", MONDAY, WEDNESDAY, MONDAY, "Mars/Olympus", False)
    with pytest.raises(ConfigError):
        Window("week", MONDAY, WEDNESDAY, MONDAY, "", False)
    w = Window("week", MONDAY, WEDNESDAY, MONDAY, TZ, False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        w.start = WEDNESDAY  # type: ignore[misc]


# --- UT04-28: t12m day clamping -------------------------------------------------------------


def test_ut04_28_t12m_clamps_leap_day() -> None:
    """UT04-28 t12m from as_of 2024-02-29 starts on 2023-02-28."""
    w = default_window("t12m", _d("2024-02-29"), TZ, COUNTS)
    assert (w.start, w.end) == (_d("2023-02-28"), _d("2024-02-29"))


def test_ut04_28_month_window_crosses_year() -> None:
    """UT04-28 month arithmetic crosses year boundaries (24 months back from Jan)."""
    w = default_window("month", _d("2024-01-15"), TZ, DEFAULT_PERIOD_COUNTS)
    assert (w.start, w.end) == (_d("2022-01-01"), _d("2024-01-01"))


# --- UT04-29: custom windows ----------------------------------------------------------------


def test_ut04_29_custom_window_valid() -> None:
    """UT04-29 a custom window keeps its bounds unaligned and is marked custom."""
    w = custom_window("month", _d("2024-01-10"), _d("2024-02-20"), WEDNESDAY, TZ)
    assert (w.start, w.end, w.as_of, w.is_custom, w.period) == (
        _d("2024-01-10"),
        _d("2024-02-20"),
        WEDNESDAY,
        True,
        "month",
    )


@pytest.mark.parametrize(
    ("start", "end"), [("2024-02-01", "2024-02-01"), ("2024-02-02", "2024-02-01")]
)
def test_ut04_29_start_not_before_end(start: str, end: str) -> None:
    """UT04-29 start >= end is a ToolInputError."""
    with pytest.raises(ToolInputError, match="window start must be before end"):
        custom_window("week", _d(start), _d(end), WEDNESDAY, TZ)


@pytest.mark.parametrize(
    ("start", "end", "ok"),
    [
        ("2021-01-01", "2024-01-01", True),  # exactly 36 months
        ("2021-01-01", "2024-02-01", False),  # 37 months
        ("2021-01-01", "2024-01-02", False),  # 36 months and a day
        ("2021-01-31", "2024-01-31", True),
        ("2020-02-29", "2023-02-28", True),  # clamped day
        ("2020-02-29", "2023-03-01", False),
    ],
)
def test_ut04_29_thirty_six_month_cap(start: str, end: str, *, ok: bool) -> None:
    """UT04-29 windows longer than 36 calendar months are a ToolInputError."""
    assert MAX_WINDOW_MONTHS == 36
    if ok:
        assert custom_window("quarter", _d(start), _d(end), WEDNESDAY, TZ).end == _d(end)
    else:
        with pytest.raises(ToolInputError, match="window longer than 36 months"):
            custom_window("quarter", _d(start), _d(end), WEDNESDAY, TZ)
