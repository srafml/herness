"""Tests of U08-82: GpuLock, the host GPU owner lock (TH08-09)."""

from __future__ import annotations

import multiprocessing
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.support.gpu_lock_race import hold_lock

from herness.core.errors import ConfigError
from herness.core.jobs.gpu_lock import GpuLock

pytestmark = pytest.mark.unit

_CTX = multiprocessing.get_context("spawn")


@pytest.fixture
def holder(tmp_path: Path) -> Iterator[tuple[Path, Any, Any, Any]]:
    """A spawned child that holds `GpuLock` on `<tmp>/locks/gpu.lock` until released."""
    path = tmp_path / "locks" / "gpu.lock"
    held, release, results = _CTX.Event(), _CTX.Event(), _CTX.Queue()
    proc = _CTX.Process(target=hold_lock, args=(str(path), held, release, results))
    proc.start()
    assert held.wait(timeout=60), "child never took the lock"
    try:
        yield path, proc, release, results
    finally:
        # A killed waiter leaves the event's condition unnotifiable: set it only while alive.
        if proc.is_alive():
            release.set()
            proc.join(timeout=30)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=30)


def test_ut08_95_second_process_refused_until_first_exits(
    holder: tuple[Path, Any, Any, Any],
) -> None:
    """UT08-95 a second process gets ConfigError; after the first exits it succeeds."""
    path, proc, release, results = holder
    with pytest.raises(ConfigError, match=re.escape("another GPU owner holds data/locks/gpu.lock")):
        GpuLock(path).__enter__()
    release.set()
    proc.join(timeout=30)
    assert results.get(timeout=30) == "released"
    with GpuLock(path):
        assert path.is_file()


def test_ut08_95_lock_released_when_owner_dies(holder: tuple[Path, Any, Any, Any]) -> None:
    """UT08-95 the OS releases the lock when the owning process dies."""
    path, proc, _release, _results = holder
    with pytest.raises(ConfigError):
        GpuLock(path).__enter__()
    proc.kill()
    proc.join(timeout=30)
    with GpuLock(path):
        pass


def test_ut08_95_same_process_second_lock_refused(tmp_path: Path) -> None:
    """UT08-95 a second GpuLock on the same file is refused; the lock reopens after exit."""
    path = tmp_path / "nested" / "locks" / "gpu.lock"
    with GpuLock(path) as first:
        assert isinstance(first, GpuLock)
        assert path.parent.is_dir()
        with pytest.raises(ConfigError):
            GpuLock(path).__enter__()
    lock = GpuLock(path)
    with lock:
        pass
    lock.__exit__(None, None, None)  # a second exit is a no-op
