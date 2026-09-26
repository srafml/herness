"""Cross-process circuit breaker checks (impl 08 IT08-01, BT08-05; T08-06): two real processes
(multiprocessing spawn) sharing one migrated SQLite ops store."""

from __future__ import annotations

import functools
import multiprocessing
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from tests.support.breaker_race import poll_worker, race_worker
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core import time as clock
from herness.core.errors import SourceUnavailable
from herness.core.resilience import ProcessState, bind_ops_backend, breaker
from herness.core.resilience.ports import HealthRow
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.integration

RACES = 100


def _seed_row(row: HealthRow, _before: HealthRow | None) -> HealthRow:
    return row


@pytest.fixture
def shared(
    ops_store: OpsStoreHandle,
    tmp_path: Path,
    fake_keyring: MemoryKeyring,
    reset_process_state: ProcessState,
) -> Iterator[tuple[str, str]]:
    """(config dir, ops db path): the parent's store is bound and its config loaded."""
    del fake_keyring, reset_process_state
    cfg_dir = write_full_config(tmp_path / "cfgroot")
    c.init_config("local", config_dir=cfg_dir, env={})
    bind_ops_backend(SqliteResilienceBackend())
    yield str(cfg_dir), str(ops_store.db_path)
    c.reset_config()


def test_it08_01_one_probe_winner_per_race(shared: tuple[str, str]) -> None:
    """IT08-01 two processes race `allow()` on 100 open breakers whose probe is due: every
    race has exactly one winner (100 `allow()` calls per process)."""
    cfg_dir, db_path = shared
    backend = SqliteResilienceBackend()
    opened = clock.now() - timedelta(hours=1)
    keys = [f"src_{n:03d}" for n in range(RACES)]
    for key in keys:
        row = HealthRow(key, "open", 8, 1, opened, None, opened)
        backend.health_apply(key, functools.partial(_seed_row, row), opened)
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    procs = [
        ctx.Process(target=race_worker, args=(cfg_dir, db_path, keys, barrier, results))
        for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    answers = [results.get(timeout=120) for _ in procs]
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0
    assert all(len(a) == RACES for a in answers)
    winners = [int(first) + int(second) for first, second in zip(*answers, strict=True)]
    assert winners == [1] * RACES
    assert all(r is not None and r.state == "half_open" for r in map(backend.health_get, keys))


def test_bt08_05_open_seen_by_other_process_within_5_s(shared: tuple[str, str]) -> None:
    """BT08-05 a breaker opened in process A is seen as open by process B within 5 s."""
    cfg_dir, db_path = shared
    ctx = multiprocessing.get_context("spawn")
    ready, results = ctx.Event(), ctx.Queue()
    proc = ctx.Process(target=poll_worker, args=(cfg_dir, db_path, "jira", ready, results))
    proc.start()
    assert ready.wait(timeout=120)
    # B keeps polling; A opens 1 s into B's 5 s cache window. Opening in the same instant
    # B refreshed its cache is the worst case: 5 s plus one poll and one read.
    time.sleep(1.0)
    breaker("jira").force_open(SourceUnavailable("jira down"))
    opened_at = time.time()
    seen_at = results.get(timeout=60)
    proc.join(timeout=60)
    assert proc.exitcode == 0
    assert seen_at is not None
    assert seen_at - opened_at <= 5.0, seen_at - opened_at
