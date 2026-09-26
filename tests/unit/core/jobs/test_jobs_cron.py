"""Tests for herness.core.jobs.cron: parser, Vixie matching and DST resolution (T08-02)."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core import jobs
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.jobs.cron import CronExpr, resolve_local

pytestmark = pytest.mark.unit

LONDON = ZoneInfo("Europe/London")
ALL_MIN = frozenset(range(60))
ALL_DOM = frozenset(range(1, 32))
ALL_MONTH = frozenset(range(1, 13))
ALL_DOW = frozenset(range(7))


@pytest.mark.parametrize(
    ("text", "minutes", "hours", "weekdays", "dow_star"),
    [
        ("*/30 * * * *", {0, 30}, set(range(24)), ALL_DOW, True),
        ("0 3 * * SUN", {0}, {3}, {0}, False),
        ("0 19 * * 1-5", {0}, {19}, {1, 2, 3, 4, 5}, False),
        ("0,30 8-18 * * MON,WED", {0, 30}, set(range(8, 19)), {1, 3}, False),
    ],
)
def test_ut08_72_valid_crons(
    text: str, minutes: set[int], hours: set[int], weekdays: frozenset[int], dow_star: bool
) -> None:
    """UT08-72 valid crons parse to the expected sets and keep the original text."""
    cron = CronExpr.parse(text)
    assert (cron.text, cron.minutes, cron.hours) == (text, minutes, hours)
    assert (cron.days, cron.months, cron.weekdays) == (ALL_DOM, ALL_MONTH, weekdays)
    assert (cron.dom_star, cron.dow_star) == (True, dow_star)


def test_ut08_72_forms_and_names() -> None:
    """UT08-72 `a-b/n`, lowercase names, `7` as Sunday and a restricted day-of-month."""
    cron = CronExpr.parse("10-50/20 0 1,15 1-12/6 sat,Sun,7")
    assert cron.minutes == {10, 30, 50}
    assert (cron.days, cron.months, cron.weekdays) == ({1, 15}, {1, 7}, {0, 6})
    assert (cron.dom_star, cron.dow_star) == (False, False)
    assert CronExpr.parse("0 0 * * 7").weekdays == {0}
    assert CronExpr.parse("*/99999 * * * *").minutes == {0}  # a step beyond the range
    assert jobs.CronExpr is CronExpr
    assert jobs.resolve_local is resolve_local


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("60 * * * *", "minute value out of range 0-59"),
        ("* * *", "fields expected 5, got 3"),
        ("*/0 * * * *", "minute step must be >= 1"),
        ("5-1 * * * *", "minute range '5-1' is reversed"),
        ("* 24 * * *", "hour value out of range 0-23"),
        ("* * 0 * *", "day-of-month value out of range 1-31"),
        ("* * * 13 *", "month value out of range 1-12"),
        ("* * * * 8", "day-of-week value out of range 0-7"),
        ("99999 * * * *", "minute value out of range 0-59"),
        ("* * 1-" + "9" * 20 + " * *", "day-of-month value out of range 1-31"),
        ("* * * JAN *", "month has a non-numeric value 'JAN'"),
        ("5/2 * * * *", "minute step needs '*' or a range in '5/2'"),
        ("1,,2 * * * *", "minute has a malformed item ''"),
        ("*/ * * * *", "minute has a non-numeric value ''"),
        ("* * * * FRI/2", "day-of-week step needs '*' or a range in 'FRI/2'"),
    ],
)
def test_ut08_72_invalid_crons(text: str, reason: str) -> None:
    """UT08-72 invalid crons raise `ConfigError("invalid cron '<text>': <field> <reason>")`."""
    with pytest.raises(ConfigError) as info:
        CronExpr.parse(text)
    assert info.value.message == f"invalid cron '{text}': {reason}"


def _at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_ut08_73_vixie_or_rule() -> None:
    """UT08-73 both day fields restricted: either matches; one restricted: only that one."""
    both = CronExpr.parse("0 0 13 * FRI")  # 2026-04-10 is a Friday, 2026-04-13 a Monday
    assert both.next_after(_at(2026, 4, 10, 12), UTC) == _at(2026, 4, 13)
    assert both.next_after(_at(2026, 4, 13), UTC) == _at(2026, 4, 17)
    assert both.latest_at_or_before(_at(2026, 4, 12), UTC) == _at(2026, 4, 10)
    assert both.latest_at_or_before(_at(2026, 4, 13), UTC) == _at(2026, 4, 13)
    dom_only = CronExpr.parse("0 0 13 * *")
    assert dom_only.next_after(_at(2026, 4, 10, 12), UTC) == _at(2026, 4, 13)
    assert dom_only.next_after(_at(2026, 4, 13), UTC) == _at(2026, 5, 13)
    dow_only = CronExpr.parse("0 0 * * FRI")
    assert dow_only.next_after(_at(2026, 4, 10, 12), UTC) == _at(2026, 4, 17)
    assert not CronExpr.parse("0 0 * 5 *").matches_date(datetime(2026, 4, 1, tzinfo=UTC).date())


def test_ut08_73_strictly_after_and_at_or_before() -> None:
    """UT08-73 `next_after(t) > t` even on a fire minute; `latest <= t`; results are UTC."""
    cron = CronExpr.parse("*/30 * * * *")
    t = datetime(2026, 9, 26, 10, 30, 0, 1, tzinfo=UTC)
    assert cron.next_after(_at(2026, 9, 26, 10, 30), LONDON) == _at(2026, 9, 26, 11)
    assert cron.next_after(t, LONDON) == _at(2026, 9, 26, 11)
    assert cron.latest_at_or_before(t, LONDON) == _at(2026, 9, 26, 10, 30)
    assert cron.latest_at_or_before(_at(2026, 9, 26, 10, 29), LONDON) == _at(2026, 9, 26, 10)
    assert cron.next_after(t, LONDON).tzinfo is UTC


def test_ut08_73_never_fires_and_naive() -> None:
    """UT08-73 a cron that never fires raises in `next_after`, gives None in `latest`."""
    never = CronExpr.parse("0 0 30 2 *")
    with pytest.raises(ConfigError, match=r"^cron never fires: 0 0 30 2 \*$"):
        never.next_after(_at(2026, 1, 1), UTC)
    assert never.latest_at_or_before(_at(2026, 1, 1), UTC) is None
    with pytest.raises(SchemaViolation):
        never.next_after(datetime(2026, 1, 1), UTC)  # noqa: DTZ001 - naive on purpose


def test_ut08_74_gap_fires_at_next_valid_minute() -> None:
    """UT08-74 Europe/London 2026-03-29 (01:00-02:00 gap): `30 1 * * *` fires 02:00 local."""
    cron = CronExpr.parse("30 1 * * *")
    fire = cron.next_after(_at(2026, 3, 28, 12), LONDON)
    assert fire == _at(2026, 3, 29, 1)
    assert fire.astimezone(LONDON).replace(tzinfo=None) == datetime(2026, 3, 29, 2)  # noqa: DTZ001
    assert cron.next_after(fire, LONDON) == _at(2026, 3, 30, 0, 30)
    assert cron.latest_at_or_before(_at(2026, 3, 29, 1, 10), LONDON) == fire


def test_ut08_74_fold_fires_once_first_occurrence() -> None:
    """UT08-74 2026-10-25 (01:00-02:00 repeated): one fire, at the BST (fold=0) 01:30."""
    cron = CronExpr.parse("30 1 * * *")
    fire = cron.next_after(_at(2026, 10, 24, 12), LONDON)
    assert fire == _at(2026, 10, 25, 0, 30)
    assert cron.next_after(fire, LONDON) == _at(2026, 10, 26, 1, 30)
    # Inside the second pass of 01:xx the latest fire is still the first-pass 01:30.
    assert cron.latest_at_or_before(_at(2026, 10, 25, 1, 20), LONDON) == fire


def test_ut08_74_resolve_local() -> None:
    """UT08-74 gap minutes resolve to the gap end; ambiguous uses fold=0; aware is refused."""
    gap_a = resolve_local(datetime(2026, 3, 29, 1, 15), LONDON)  # noqa: DTZ001
    gap_b = resolve_local(datetime(2026, 3, 29, 1, 45), LONDON)  # noqa: DTZ001
    assert gap_a == gap_b == _at(2026, 3, 29, 1)
    assert resolve_local(datetime(2026, 10, 25, 1, 30), LONDON) == _at(2026, 10, 25, 0, 30)  # noqa: DTZ001
    assert resolve_local(datetime(2026, 7, 1, 12), LONDON) == _at(2026, 7, 1, 11)  # noqa: DTZ001
    with pytest.raises(SchemaViolation):
        resolve_local(_at(2026, 7, 1), LONDON)


def _item(low: int, high: int) -> st.SearchStrategy[str]:
    value = st.integers(low, high)
    pair = st.tuples(value, value).map(sorted)
    return st.one_of(
        value.map(str),
        pair.map(lambda p: f"{p[0]}-{p[1]}"),
        st.integers(1, 15).map(lambda n: f"*/{n}"),
        st.tuples(pair, st.integers(1, 5)).map(lambda x: f"{x[0][0]}-{x[0][1]}/{x[1]}"),
    )


def _field(low: int, high: int) -> st.SearchStrategy[str]:
    listed = st.lists(_item(low, high), min_size=1, max_size=3).map(",".join)
    return st.one_of(st.just("*"), listed)


# Day-of-month stops at 28 so every generated cron fires within a year.
CRONS = st.tuples(_field(0, 59), _field(0, 23), _field(1, 28), _field(1, 12), _field(0, 7)).map(
    " ".join
)
ZONES = st.sampled_from([UTC, LONDON, ZoneInfo("America/New_York"), ZoneInfo("Australia/Sydney")])
INSTANTS = st.datetimes(datetime(2024, 1, 1), datetime(2030, 1, 1)).map(  # noqa: DTZ001
    lambda d: d.replace(tzinfo=UTC)
)


@given(text=CRONS, t=INSTANTS, tz=ZONES)
def test_pt08_04_next_and_latest_agree(text: str, t: datetime, tz: ZoneInfo) -> None:
    """PT08-04 `next_after(t) > t`, latest(next) == next, and no fire in between."""
    cron = CronExpr.parse(text)
    fire = cron.next_after(t, tz)
    assert fire > t
    assert cron.latest_at_or_before(fire, tz) == fire
    before = cron.latest_at_or_before(fire - timedelta(microseconds=1), tz)
    assert before is None or before <= t
