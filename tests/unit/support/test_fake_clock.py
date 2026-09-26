"""Tests for tests.support.fake_clock: FakeClock (U11-36)."""

from __future__ import annotations

import datetime
import threading
import time

import pytest
from tests.support.fake_clock import FIXTURE_START, FakeClock

from herness.core import time as clock
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit


def test_ut11_59_sleep_then_advance_sums_with_no_wall_clock_wait() -> None:
    """UT11-59 sleep(7) then advance(3): now() is +10s and the test itself takes < 1 s."""
    fc = FakeClock(FIXTURE_START)
    started = time.monotonic()

    fc.sleep(7)
    fc.advance(3)

    elapsed = time.monotonic() - started
    assert fc.now() == FIXTURE_START + datetime.timedelta(seconds=10)
    assert elapsed < 1.0


def test_ut11_59_rejects_naive_start() -> None:
    """UT11-59 a naive `start` is rejected with ConfigError."""
    with pytest.raises(ConfigError):
        FakeClock(datetime.datetime(2026, 9, 1))  # noqa: DTZ001 -- deliberately naive


def test_ut11_59_rejects_negative_seconds() -> None:
    """UT11-59 a negative duration to advance/sleep is rejected with ConfigError."""
    fc = FakeClock(FIXTURE_START)
    with pytest.raises(ConfigError):
        fc.advance(-1)
    with pytest.raises(ConfigError):
        fc.sleep(-1)


@pytest.mark.asyncio
async def test_ut11_59_asleep_advances_without_blocking() -> None:
    """UT11-59 `asleep` yields once then advances the same as `sleep`."""
    fc = FakeClock(FIXTURE_START)
    started = time.monotonic()

    await fc.asleep(5)

    elapsed = time.monotonic() - started
    assert fc.now() == FIXTURE_START + datetime.timedelta(seconds=5)
    assert elapsed < 1.0


def test_ut11_60_freezegun_matches_fake_time_and_never_goes_backwards(
    fake_clock: FakeClock,
) -> None:
    """UT11-60 entering the clock freezes `datetime.now(UTC)` to the fake time."""
    assert datetime.datetime.now(datetime.UTC) == FIXTURE_START

    fake_clock.advance(2)

    assert datetime.datetime.now(datetime.UTC) == FIXTURE_START + datetime.timedelta(seconds=2)
    assert fake_clock.now() == FIXTURE_START + datetime.timedelta(seconds=2)


def test_ut11_60_concurrent_advances_sum_and_track_freezegun(fake_clock: FakeClock) -> None:
    """UT11-60 advancing from many threads sums exactly; freezegun mirrors the final total."""
    per_thread = 1.0
    thread_count = 8

    def worker() -> None:
        fake_clock.advance(per_thread)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    expected = FIXTURE_START + datetime.timedelta(seconds=per_thread * thread_count)
    assert fake_clock.now() == expected
    assert datetime.datetime.now(datetime.UTC) == expected


def test_ut11_60_patches_herness_core_time_now(fake_clock: FakeClock) -> None:
    """UT11-60 `herness.core.time.now`/`sleep` report the fake clock while entered."""
    assert clock.now() == FIXTURE_START
    clock.sleep(4)
    assert clock.now() == FIXTURE_START + datetime.timedelta(seconds=4)
    assert fake_clock.now() == FIXTURE_START + datetime.timedelta(seconds=4)
