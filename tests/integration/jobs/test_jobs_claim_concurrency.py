"""Concurrent claim checks (impl 08 IT08-02, F08-03, U08-48; T08-11 acceptance): 8 threads,
each with its own ops-store connection, claim and complete jobs from one migrated store.
"""

from __future__ import annotations

import itertools
import random
import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from tests.support.ops_store import OpsStoreHandle

from herness.core import time as clock
from herness.core.types import GpuClass, JobKind
from herness.store.ops import _job_sql
from herness.store.ops.core import read_all, run_write
from herness.store.ops.jobs import SqliteJobsBackend

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
JOBS = 5_000
THREADS = 8
CLAIMS_PER_THREAD = 1_000
EXCLUSIVE: tuple[JobKind, ...] = ("build_pipeline", "distill")
KINDS: tuple[JobKind, ...] = ("sync", "review", "reconcile", "build_pipeline", "distill", "eval")
CLASSES: tuple[GpuClass, ...] = ("none", "reasoning", "decider", "large")

type _Claim = tuple[str, str, int]  # job_id, kind, priority


def _seed(count: int) -> None:
    rng = random.Random(0)
    stamp = clock.format_utc(T0 - timedelta(minutes=1))
    rows = [
        (
            f"job_{i:05d}",
            rng.choice(KINDS),
            rng.choice(CLASSES),
            rng.randint(0, 100),
            "{}",
            f"k:{i}",
            3,
            stamp,
            stamp,
        )
        for i in range(count)
    ]

    def insert(conn: sqlite3.Connection) -> None:
        conn.executemany(
            "INSERT INTO job (job_id, kind, gpu_class, priority, payload, idem_key, max_attempts,"
            " scheduled_for, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    run_write(insert, op="test_seed")


class _Exclusive:
    """Counts exclusive jobs held between claim and completion (never more than one)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = 0
        self.violations = 0
        self.errors: list[BaseException] = []

    def enter(self) -> None:
        with self.lock:
            if self.running:
                self.violations += 1
            self.running += 1

    def leave(self) -> None:
        with self.lock:
            self.running -= 1


def _worker(
    index: int,
    backend: SqliteJobsBackend,
    exclusive: _Exclusive,
    out: list[_Claim],
    attempts: int = CLAIMS_PER_THREAD,
) -> None:
    owner = f"h:{index}:cli"
    for _ in range(attempts):
        row = backend.claim_job(
            owner=owner,
            now=T0,
            lease_until=T0 + timedelta(minutes=5),
            allowed_classes=CLASSES,
            exclusive_kinds=EXCLUSIVE,
            job_id=None,
            min_priority=None,
            priority_exempt_kinds=(),
            slot="cli",
            gpu_slot_kinds=("build_pipeline",),
        )
        if row is None:
            continue
        held = row.kind in EXCLUSIVE
        if held:
            exclusive.enter()
        out.append((row.job_id, row.kind, row.priority))
        if held:
            exclusive.leave()  # before the commit that lets another exclusive job start
        if not backend.finish_done(row.job_id, owner, {"n": 1}, T0):
            exclusive.errors.append(AssertionError(row.job_id))


def _non_increasing(claims: Sequence[_Claim]) -> bool:
    priorities = [priority for _, kind, priority in claims if kind not in EXCLUSIVE]
    return all(a >= b for a, b in itertools.pairwise(priorities))


def test_it08_02_eight_threads_never_double_claim(ops_store: OpsStoreHandle) -> None:
    """IT08-02 5 000 jobs, 8 threads x 1 000 claims each completing its job: no job claimed
    twice; per-thread non-exclusive claims in non-increasing priority (every job is due);
    never two exclusive kinds running at once."""
    del ops_store
    _seed(JOBS)
    backend = SqliteJobsBackend()
    exclusive = _Exclusive()
    claims: list[list[_Claim]] = [[] for _ in range(THREADS)]
    threads = [
        threading.Thread(target=_worker, args=(i, backend, exclusive, claims[i]), daemon=True)
        for i in range(THREADS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)
        assert not thread.is_alive()
    claimed = [job_id for per_thread in claims for job_id, _, _ in per_thread]
    assert len(claimed) == len(set(claimed))
    assert sum(1 for per_thread in claims if per_thread) >= 2  # threads really competed
    assert any(kind in EXCLUSIVE for per_thread in claims for _, kind, _ in per_thread)
    assert exclusive.violations == 0
    assert exclusive.errors == []
    assert all(_non_increasing(per_thread) for per_thread in claims)
    rows = read_all("SELECT status, attempts, COUNT(*) FROM job GROUP BY status, attempts")
    by_state = {(row[0], row[1]): row[2] for row in rows}
    assert by_state.get(("done", 1), 0) == len(claimed)
    assert sum(by_state.values()) == JOBS
    assert set(by_state) <= {("done", 1), ("queued", 0)}
    # Exclusive jobs skipped while another one ran may remain; one more owner drains them.
    rest: list[_Claim] = []
    _worker(THREADS, backend, exclusive, rest, attempts=JOBS - len(claimed) + 1)
    assert set(claimed).isdisjoint(job_id for job_id, _, _ in rest)
    assert len(claimed) + len(rest) == JOBS
    final = read_all("SELECT status, attempts, COUNT(*) FROM job GROUP BY status, attempts")
    assert [tuple(row) for row in final] == [("done", 1, JOBS)]


def test_it08_02_claim_plan_uses_the_job_claim_index(ops_store: OpsStoreHandle) -> None:
    """IT08-02 acceptance: with 5 000 rows, EXPLAIN QUERY PLAN of the claim uses `job_claim`."""
    del ops_store
    _seed(JOBS)
    params = {
        "owner": "h:1:gpu",
        "now": clock.format_utc(T0),
        "lease_until": clock.format_utc(T0 + timedelta(minutes=5)),
        "allowed_classes": '["none","reasoning"]',
        "exclusive_kinds": '["build_pipeline","distill"]',
        "job_id": None,
        "min_priority": 30,
        "exempt_kinds": '["chat"]',
        "slot": "gpu",
        "gpu_slot_kinds": '["build_pipeline"]',
    }
    plan = read_all("EXPLAIN QUERY PLAN " + _job_sql.CLAIM, params)
    details = [str(row["detail"]) for row in plan]
    assert any(d.startswith("SEARCH j USING INDEX job_claim") for d in details), details
    assert not any(detail.startswith(("SCAN j ", "SCAN job", "SCAN r ")) for detail in details), (
        details
    )
