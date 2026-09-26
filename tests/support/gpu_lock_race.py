"""Spawned child for the cross-process GPU lock test (impl 08 UT08-95; T08-17)."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def hold_lock(path: str, held: Any, release: Any, results: Any) -> None:  # pragma: no cover
    """Enter `GpuLock(path)`, set `held`, wait for `release`, then leave; report the outcome.

    When `release` is never set the parent kills the child while it holds the lock.
    """
    from herness.core.errors import ConfigError  # noqa: PLC0415 - child process only
    from herness.core.jobs.gpu_lock import GpuLock  # noqa: PLC0415 - child process only

    try:
        with GpuLock(Path(path)):
            held.set()
            release.wait(timeout=60)
    except ConfigError:
        results.put("held_elsewhere")
        return
    results.put("released")
