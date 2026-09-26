"""Security tests for herness.store.ops.core (ST02-18; TH02-18: long lock holders)."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.support.ops_store import OpsStoreHandle

from herness.core.errors import StoreBusy
from herness.store.ops import _shims, core

pytestmark = pytest.mark.integration

_POLICY_MAX_ELAPSED_S = 30.0


def _insert(name: str) -> Callable[[sqlite3.Connection], object]:
    return lambda conn: conn.execute("INSERT INTO item (name) VALUES (?)", (name,))


def _hold(path: Path, begin: str, seconds: float, ready: threading.Event) -> None:
    other = sqlite3.connect(path, isolation_level=None)
    try:
        other.execute(begin)
        other.execute("SELECT count(*) FROM item").fetchone()
        ready.set()
        time.sleep(seconds)
        other.execute("COMMIT")
    finally:
        other.close()


def test_st02_18_long_holders_never_hang_writers(
    ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST02-18 a 5 s reader does not block writes (WAL); a 12 s BEGIN IMMEDIATE holder makes
    the writer's first attempt raise StoreBusy, and run_write completes within the 30 s policy.

    Controller ruling (spec note on ST02-18): with busy_timeout 10 s the retry after the first
    StoreBusy outlasts the 12 s holder, so a caller-visible StoreBusy is unreachable here.
    """
    core.run_write(lambda c: c.execute("CREATE TABLE item (name TEXT)"), op="create_item")

    # Phase 1: a read transaction held for 5 s; writes during it succeed at once (WAL).
    ready = threading.Event()
    reader = threading.Thread(target=_hold, args=(ops_store.db_path, "BEGIN", 5.0, ready))
    reader.start()
    assert ready.wait(5)
    for i in range(3):
        start = time.monotonic()
        core.run_write(_insert(f"during-read-{i}"), op="insert_item")
        assert time.monotonic() - start < 1.0
    assert reader.is_alive()
    reader.join()

    # Phase 2: a writer holds BEGIN IMMEDIATE for 12 s.
    attempts: list[int] = []
    busy: list[int] = []  # attempt numbers that raised StoreBusy
    real_retry = _shims.retry_call

    def watching_retry(name: str, fn: Callable[[], object]) -> object:
        def attempt() -> object:
            attempts.append(len(attempts) + 1)
            try:
                return fn()
            except StoreBusy:
                busy.append(attempts[-1])
                raise

        return real_retry(name, attempt)

    monkeypatch.setattr(_shims, "retry_call", watching_retry)
    ready = threading.Event()
    writer = threading.Thread(
        target=_hold, args=(ops_store.db_path, "BEGIN IMMEDIATE", 12.0, ready)
    )
    writer.start()
    assert ready.wait(5)
    start = time.monotonic()
    try:
        core.run_write(_insert("after-writer"), op="insert_item")
    except StoreBusy:
        outcome = "busy"
    else:
        outcome = "written"
    elapsed = time.monotonic() - start
    writer.join()
    assert busy[:1] == [1], "the first attempt of the blocked writer did not raise StoreBusy"
    assert elapsed < _POLICY_MAX_ELAPSED_S
    names = {row["name"] for row in core.read_all("SELECT name FROM item")}
    assert {"during-read-0", "during-read-1", "during-read-2"} <= names
    # The row exists exactly when run_write returned normally.
    assert ("after-writer" in names) == (outcome == "written")
