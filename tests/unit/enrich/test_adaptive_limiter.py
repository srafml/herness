"""Tests for `AdaptiveLimiter` (U03-51, T03-11). The clock is injected; no real sleeps."""

from __future__ import annotations

import asyncio

import pytest
from structlog.testing import capture_logs

from herness.core.errors import ConfigError
from herness.enrich.deciders.jev_wire import AdaptiveLimiter

pytestmark = pytest.mark.unit


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _spin(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


class _Holders:
    """Tasks that each take a slot and hold it until told to release."""

    def __init__(self, limiter: AdaptiveLimiter) -> None:
        self.limiter = limiter
        self.active = 0
        self.peak = 0
        self.acquired = 0
        self.releases: list[asyncio.Event] = []
        self.tasks: list[asyncio.Task[None]] = []

    def start(self, count: int) -> None:
        for _ in range(count):
            event = asyncio.Event()
            self.releases.append(event)
            self.tasks.append(asyncio.create_task(self._hold(event)))

    async def _hold(self, release: asyncio.Event) -> None:
        async with self.limiter.slot():
            self.active += 1
            self.acquired += 1
            self.peak = max(self.peak, self.active)
            await release.wait()
            self.active -= 1

    async def finish(self) -> None:
        for event in self.releases:
            event.set()
        await asyncio.gather(*self.tasks)


@pytest.mark.asyncio
async def test_ut03_49_halves_for_60s_after_429() -> None:
    """UT03-49 capacity 8: after a 429, 4 concurrent for 60 s, 8 after."""
    clock = _FakeClock()
    limiter = AdaptiveLimiter(8, clock=clock)
    assert limiter.current_capacity == 8
    with capture_logs() as logs:
        limiter.on_rate_limited(None)
    assert [(e["event"], e["log_level"], e["capacity"]) for e in logs] == [
        ("enrich.decider.rate_limited", "warning", 8)
    ]
    holders = _Holders(limiter)
    holders.start(8)
    await _spin()
    assert (holders.active, holders.peak, limiter.current_capacity) == (4, 4, 4)

    clock.now = 30.0
    holders.releases[0].set()  # one slot frees; exactly one waiter takes it
    await _spin()
    assert (holders.active, holders.peak, holders.acquired) == (4, 4, 5)

    clock.now = 59.999
    assert limiter.current_capacity == 4
    clock.now = 60.0
    assert limiter.current_capacity == 8
    holders.releases[1].set()  # wakes the remaining waiters under the restored capacity
    await _spin()
    assert (holders.active, holders.acquired) == (6, 8)
    holders.start(3)
    await _spin()
    assert (holders.active, holders.peak, holders.acquired) == (8, 8, 10)
    await holders.finish()
    assert holders.acquired == 11


@pytest.mark.asyncio
async def test_ut03_49_retry_after_pauses_new_acquisitions() -> None:
    """UT03-49 new acquisitions wait until clock() >= pause_until (Retry-After)."""
    clock = _FakeClock()
    limiter = AdaptiveLimiter(8, clock=clock)
    limiter.on_rate_limited(5.0)
    holders = _Holders(limiter)
    holders.start(1)
    await _spin()
    assert holders.acquired == 0
    holders.tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await holders.tasks[0]

    clock.now = 5.0
    holders = _Holders(limiter)
    holders.start(4)
    await _spin()
    assert (holders.active, limiter.current_capacity) == (4, 4)
    await holders.finish()


@pytest.mark.asyncio
async def test_ut03_49_waiter_rechecks_after_pause_timeout() -> None:
    """UT03-49 a paused waiter re-checks the clock at most every pause remainder (<= 1 s)."""
    clock = _FakeClock()
    limiter = AdaptiveLimiter(2, clock=clock)
    limiter.on_rate_limited(0.01)  # wait timeout = 10 ms of loop time, not a test sleep
    holders = _Holders(limiter)
    holders.start(1)
    await _spin()
    assert holders.acquired == 0
    clock.now = 0.01
    releases = holders.releases[0]
    releases.set()
    await asyncio.wait_for(holders.tasks[0], timeout=5.0)
    assert (holders.acquired, limiter.current_capacity) == (1, 1)


@pytest.mark.asyncio
async def test_ut03_49_cancel_while_waiting_never_takes_a_slot() -> None:
    """UT03-49 a waiter cancelled before acquiring leaves the slot count unchanged."""
    limiter = AdaptiveLimiter(1, clock=_FakeClock())
    holders = _Holders(limiter)
    holders.start(2)
    await _spin()
    assert (holders.active, holders.acquired) == (1, 1)
    holders.tasks[1].cancel()
    with pytest.raises(asyncio.CancelledError):
        await holders.tasks[1]
    holders.releases[0].set()
    await holders.tasks[0]
    async with asyncio.timeout(0.5):  # a free slot is taken at once, no re-poll needed
        async with limiter.slot():
            pass


@pytest.mark.asyncio
async def test_ut03_49_cancel_during_release_does_not_leak_slot() -> None:
    """UT03-49 a holder cancelled while waiting for the lock on release still frees its slot."""
    limiter = AdaptiveLimiter(1, clock=_FakeClock())
    holders = _Holders(limiter)
    holders.start(1)
    await _spin()
    assert holders.active == 1
    cond = limiter._cond  # force lock contention on the release path
    await cond.acquire()
    holders.releases[0].set()
    await _spin()  # the holder left its body and now waits for the contended lock
    holders.tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await holders.tasks[0]
    cond.release()
    async with asyncio.timeout(0.5):
        async with limiter.slot():
            pass


def test_ut03_49_capacity_must_be_positive() -> None:
    """UT03-49 capacity < 1 is a precondition violation."""
    with pytest.raises(ConfigError):
        AdaptiveLimiter(0)
    assert AdaptiveLimiter(1).current_capacity == 1
