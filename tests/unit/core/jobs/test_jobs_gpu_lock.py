"""Tests of U08-82: GpuLock, the host GPU owner lock (TH08-09)."""

from __future__ import annotations

import multiprocessing
import re
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

import pytest
from tests.support.gpu_lock_race import hold_lock

from herness.core.errors import ConfigError
from herness.core.jobs import gpu_lock
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


def test_ut08_95_file_closed_when_lock_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-95 a failed lock closes the file it opened before raising ConfigError."""
    opened: list[IO[bytes]] = []

    def refuse(fh: IO[bytes]) -> None:
        opened.append(fh)
        raise OSError(36, "locked")

    monkeypatch.setattr(gpu_lock, "_lock", refuse)
    with pytest.raises(ConfigError):
        GpuLock(tmp_path / "gpu.lock").__enter__()
    assert len(opened) == 1
    assert opened[0].closed


def test_ut08_95_exit_unlocks_before_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-95 __exit__ unlocks the locked handle while it is still open, then closes it."""
    unlocked: list[tuple[IO[bytes], bool]] = []
    real_unlock = gpu_lock._unlock

    def spy(fh: IO[bytes]) -> None:
        unlocked.append((fh, fh.closed))
        real_unlock(fh)

    monkeypatch.setattr(gpu_lock, "_unlock", spy)
    lock = GpuLock(tmp_path / "gpu.lock")
    with lock:
        fh = lock._fh
    assert fh is not None
    assert unlocked == [(fh, False)]
    assert fh.closed


def test_ut08_95_unlock_releases_while_handle_open(tmp_path: Path) -> None:
    """UT08-95 `_unlock` alone releases the byte lock: a new GpuLock succeeds while the first
    handle stays open, so the release does not come from the close."""
    path = tmp_path / "gpu.lock"
    with path.open("a+b") as fh:
        gpu_lock._lock(fh)
        with pytest.raises(ConfigError):
            GpuLock(path).__enter__()
        gpu_lock._unlock(fh)
        assert not fh.closed
        with GpuLock(path):
            pass
