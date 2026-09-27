"""Blackboard contention (impl 06 IT06-16 T17, ST06-10 TH06-10; T06-08 acceptance).

50 concurrent Skeptic/Verifier transitions on one finding, through one shared writer and
through several Blackboards with their own writer threads (real SQLite lock contention on
the migrated tmp ops store): exactly one compare-and-set wins and no call fails with
`database is locked`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import pytest
from tests.unit.harness import _blackboard_env as env_mod
from tests.unit.harness._blackboard_env import BbEnv, args, challenge, task_spec, verification

from herness.harness.blackboard import Blackboard
from herness.store.ops import get_findings, read_one

pytestmark = pytest.mark.integration

bb_env = env_mod.bb_env  # fixtures
test_redactor = env_mod.test_redactor

CALLS = 50
type Call = Callable[[], Coroutine[Any, Any, bool]]
BOARDS = 5


def _revision_tasks(run_id: str) -> int:
    row = read_one(
        "SELECT count(*) AS n FROM task WHERE run_id = ? AND role = 'analyst'", (run_id,)
    )
    assert row is not None
    return int(row["n"]) - 1  # minus the posting task


def _calls(env: BbEnv, fid: str, boards: list[Blackboard]) -> list[Call]:
    """25 revise challenges and 25 gate 1 passes, spread over `boards`."""
    calls: list[Call] = []
    for i in range(CALLS):
        bb = boards[i % len(boards)]
        if i % 2 == 0:
            spec = task_spec(env.run_id, n=100 + i)
            ch = challenge(fid, "revise")
            calls.append(partial(bb.challenge, fid, ch, revision=spec))
        else:
            calls.append(partial(bb.mark_verified, fid, verification()))
    return calls


def _run_threads(calls: list[Call]) -> list[bool | BaseException]:
    """Each call on its own thread with its own event loop (true cross-writer concurrency)."""

    def one(call: Call) -> bool | BaseException:
        try:
            return asyncio.run(call())
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            return exc

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(one, calls))


def _check_one_winner(env: BbEnv, fid: str, results: list[bool | BaseException]) -> None:
    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == [], [f"{type(e).__name__}: {e}" for e in errors]
    assert sum(1 for r in results if r is True) == 1
    status = get_findings([fid])[fid].status
    assert status in {"challenged", "verified"}
    assert _revision_tasks(env.run_id) == (1 if status == "challenged" else 0)


def test_it06_16_fifty_calls_one_shared_writer(bb_env: BbEnv) -> None:
    """IT06-16 50 concurrent challenge/mark_verified through one writer: one winner."""
    fid = bb_env.bb.post(bb_env.ctx(), **args(bb_env.ops_qid))

    async def main() -> list[bool | BaseException]:
        calls = _calls(bb_env, fid, [bb_env.bb])
        return list(await asyncio.gather(*(c() for c in calls), return_exceptions=True))

    _check_one_winner(bb_env, fid, asyncio.run(main()))


def test_it06_16_fifty_calls_across_writers(bb_env: BbEnv) -> None:
    """IT06-16 50 calls over 5 Blackboards with own writers: one winner, no lock errors."""
    fid = bb_env.bb.post(bb_env.ctx(), **args(bb_env.ops_qid))
    boards = [bb_env.bb, *(bb_env.blackboard() for _ in range(BOARDS - 1))]
    _check_one_winner(bb_env, fid, _run_threads(_calls(bb_env, fid, boards)))


def test_st06_10_parallel_reject_and_verify(bb_env: BbEnv) -> None:
    """ST06-10 parallel reject and verify on one finding across writers: exactly one wins."""
    fid = bb_env.bb.post(bb_env.ctx(), **args(bb_env.ops_qid))
    boards = [bb_env.bb, bb_env.blackboard()]
    calls: list[Call] = []
    for i in range(20):
        bb = boards[i % 2]
        if i % 2 == 0:
            calls.append(partial(bb.reject, fid, "verifier_fail", verification()))
        else:
            calls.append(partial(bb.mark_verified, fid, verification()))
    results = _run_threads(calls)
    assert [r for r in results if isinstance(r, BaseException)] == []
    assert sum(1 for r in results if r is True) == 1
    found = get_findings([fid])[fid]
    assert found.status in {"rejected", "verified"}
    assert found.verification is not None
    assert (found.verification.reason == "verifier_fail") == (found.status == "rejected")
