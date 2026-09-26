"""Host GPU owner lock on `data/locks/gpu.lock` (impl 08 U08-82, TH08-09).

One process per host owns the GPU: the worker's GPU slot, or `run_inline` when no worker
lives. The lock is an OS byte-range lock on byte 0 (`msvcrt` on Windows, `fcntl.flock`
elsewhere), so the OS releases it when the owning process dies.
"""

from __future__ import annotations

import sys
from typing import IO, TYPE_CHECKING, Self

from herness.core.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

__all__ = ["GpuLock"]


def _lock(fh: IO[bytes]) -> None:
    """Non-blocking exclusive lock on byte 0; `OSError` when another owner holds it."""
    fh.seek(0)
    if sys.platform == "win32":
        import msvcrt  # noqa: PLC0415 - platform module

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - POSIX hosts
        import fcntl  # noqa: PLC0415 - platform module

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh: IO[bytes]) -> None:
    fh.seek(0)
    if sys.platform == "win32":
        import msvcrt  # noqa: PLC0415 - platform module

        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - POSIX hosts
        import fcntl  # noqa: PLC0415 - platform module

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class GpuLock:
    """Context manager: `with GpuLock(cfg.paths.data / "locks" / "gpu.lock"): ...` (U08-82)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: IO[bytes] | None = None

    def __enter__(self) -> Self:
        """Create the parent directory, open `a+b`, lock byte 0; `ConfigError` when held."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = self._path.open("a+b")
        try:
            _lock(fh)
        except OSError:
            fh.close()
            msg = "another GPU owner holds data/locks/gpu.lock"
            raise ConfigError(msg) from None
        self._fh = fh
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Unlock and close."""
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock(fh)
        finally:
            fh.close()
