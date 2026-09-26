"""Spawned children for the cross-process breaker tests (impl 08 IT08-01, BT08-05; T08-06).

Each child loads the test config, points the ops store at the parent's SQLite file and
binds the SQLite resilience backend before touching a breaker.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any


def _setup(cfg_dir: str, db_path: str) -> None:  # pragma: no cover - child
    import keyring  # noqa: PLC0415 - child process only
    from tests.support.fake_keyring import MemoryKeyring  # noqa: PLC0415 - child process only

    from herness.core.config import init_config  # noqa: PLC0415 - child process only
    from herness.core.resilience import bind_ops_backend  # noqa: PLC0415 - child process only
    from herness.store.ops import reset_connections  # noqa: PLC0415 - child process only
    from herness.store.ops.resilience import (  # noqa: PLC0415 - child process only
        SqliteResilienceBackend,
    )

    keyring.set_keyring(MemoryKeyring())
    init_config("local", config_dir=Path(cfg_dir), env={})
    reset_connections(path=Path(db_path))
    bind_ops_backend(SqliteResilienceBackend())


def race_worker(  # pragma: no cover - child
    cfg_dir: str, db_path: str, keys: list[str], barrier: Any, results: Any
) -> None:
    """IT08-01: wait at the barrier, then call `allow()` once per key; report the answers."""
    from herness.core.resilience import breaker  # noqa: PLC0415 - child process only

    _setup(cfg_dir, db_path)
    breakers = [breaker(key) for key in keys]
    barrier.wait(timeout=60)
    results.put([b.allow() for b in breakers])


def poll_worker(  # pragma: no cover - child
    cfg_dir: str, db_path: str, key: str, ready: Any, results: Any
) -> None:
    """BT08-05: prime the cache (closed), signal, then poll `state()` until it reads open;
    report the wall-clock time (`time.time()`) the open state was first seen."""
    from herness.core.resilience import breaker  # noqa: PLC0415 - child process only

    _setup(cfg_dir, db_path)
    b = breaker(key)
    if b.state() != "closed":
        results.put(None)
        return
    ready.set()
    deadline = time.monotonic() + 30
    while b.state() != "open" and time.monotonic() < deadline:
        time.sleep(0.001)
    results.put(time.time() if b.state() == "open" else None)
