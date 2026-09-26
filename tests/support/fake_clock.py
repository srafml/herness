"""Deterministic fake clock (U11-36).

Patches `herness.core.time` (module attributes `now`, `sleep`, `asleep`) and keeps
`freezegun.freeze_time` in step, so backoff, `Retry-After`, lease and schedule tests
(design §5.2) run with no wall-clock wait.
"""

from __future__ import annotations

import asyncio
import datetime
import threading
from collections.abc import Iterator
from types import TracebackType
from typing import Self

import freezegun
import pytest
from freezegun.api import FrozenDateTimeFactory

from herness.core import errors
from herness.core import time as clock

FIXTURE_START: datetime.datetime = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)

_PATCHED_ATTRS: tuple[str, ...] = ("now", "sleep", "asleep")


def _check_start(start: datetime.datetime) -> None:
    if start.tzinfo is None or start.utcoffset() != datetime.timedelta(0):
        msg = "fake clock start must be a timezone-aware UTC datetime"
        raise errors.ConfigError(msg)


def _check_seconds(seconds: float) -> None:
    if seconds < 0:
        msg = "fake clock duration must be non-negative"
        raise errors.ConfigError(msg, seconds=seconds)


class FakeClock:
    """Deterministic time for backoff, `Retry-After`, lease and schedule tests (design §5.2).

    Time never goes backwards. `advance` (and the `sleep`/`asleep` it backs) is guarded by
    a lock, so concurrent callers see a consistent, additive total.
    """

    def __init__(self, start: datetime.datetime) -> None:
        _check_start(start)
        self._current = start
        self._lock = threading.Lock()
        self._freezer: freezegun.api._freeze_time | None = None
        self._frozen: FrozenDateTimeFactory | None = None
        self._saved: dict[str, object] = {}

    def now(self) -> datetime.datetime:
        """Current fake time."""
        with self._lock:
            return self._current

    def advance(self, seconds: float) -> datetime.datetime:
        """Move the fake clock forward by `seconds` and return the new time.

        Raises ConfigError (seconds) when `seconds` is negative. Moves the active
        freezegun freezer (if entered) to the new time under the same lock, so
        `datetime.now(UTC)` and `now()` never disagree.
        """
        _check_seconds(seconds)
        with self._lock:
            self._current = self._current + datetime.timedelta(seconds=seconds)
            if self._frozen is not None:
                self._frozen.move_to(self._current)
            return self._current

    def sleep(self, seconds: float) -> None:
        """Advance by `seconds` without blocking. Raises as `advance`."""
        self.advance(seconds)

    async def asleep(self, seconds: float) -> None:
        """Yield once, then advance by `seconds` without blocking. Raises as `advance`."""
        _check_seconds(seconds)
        await asyncio.sleep(0)
        self.advance(seconds)

    def __enter__(self) -> Self:
        if self._freezer is not None:
            msg = "fake clock already entered"
            raise errors.ConfigError(msg)
        self._saved = {name: getattr(clock, name) for name in _PATCHED_ATTRS}
        clock.now = self.now  # type: ignore[method-assign]
        clock.sleep = self.sleep  # type: ignore[method-assign]
        clock.asleep = self.asleep  # type: ignore[method-assign]
        freezer = freezegun.freeze_time(self._current, tick=False)
        self._frozen = freezer.start()
        self._freezer = freezer
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._freezer is not None:
            self._freezer.stop()
            self._freezer = None
            self._frozen = None
        for name, value in self._saved.items():
            setattr(clock, name, value)
        self._saved = {}


@pytest.fixture
def fake_clock() -> Iterator[FakeClock]:
    """Function-scoped `FakeClock` starting 2026-09-01T00:00:00Z (U11-36)."""
    with FakeClock(FIXTURE_START) as fc:
        yield fc
