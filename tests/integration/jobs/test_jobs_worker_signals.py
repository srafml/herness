"""Worker shutdown on console signals (impl 08 IT08-08, F08-09; T08-21): the worker runs as a
subprocess (list argv; Windows `CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT`, POSIX
`SIGINT`) with a long handler that checkpoints through `save_state`."""

from __future__ import annotations

from typing import Any

import pytest
from tests.support.worker_env import WAIT_S, WorkerEnv, interrupt, wait_for

from herness.core.jobs.ports import require_jobs_backend

pytestmark = pytest.mark.integration


def _state_n(job_id: str) -> int:
    return int(require_jobs_backend().load_job_state(job_id).get("n", 0))


def _workers() -> dict[str, Any]:
    return {w.worker_id: w for w in require_jobs_backend().list_workers()}


def _new_worker(known: set[str]) -> str:
    """The id of the worker row that appears next (the venv launcher's pid is not the
    worker's on Windows, so rows are matched by appearance, not by `Popen.pid`)."""
    found: list[str] = []

    def appeared() -> bool:
        found[:] = [w for w in _workers() if w not in known]
        return len(found) == 1

    wait_for(appeared, what="worker row")
    known.add(found[0])
    return found[0]


def _status(worker_id: str) -> str:
    return str(_workers()[worker_id].status)


def test_it08_08_one_signal_yields_and_keeps_state(worker_env: WorkerEnv) -> None:
    """IT08-08 one SIGINT (Windows `CTRL_BREAK_EVENT`): the job yields back to `queued`,
    attempts unchanged, its checkpoint kept; the worker exits 0 with its row `stopped`."""
    job_id = worker_env.enqueue("checkpoint")
    proc = worker_env.spawn_worker("--gpu-classes", "none", "--concurrency", "1")
    worker_id = _new_worker(set())
    wait_for(lambda: _state_n(job_id) >= 2, what="checkpoints")
    interrupt(proc)
    assert proc.wait(timeout=WAIT_S) == 0, worker_env.output()
    row = worker_env.job(job_id)
    assert (row.status, row.attempts, row.lease_owner) == ("queued", 0, None)
    assert _state_n(job_id) >= 2
    assert _status(worker_id) == "stopped"
    yields = [e for e in worker_env.events("job_yield") if e["job_id"] == job_id]
    assert [e["stop_reason"] for e in yields] == ["shutdown"]


def test_it08_08_two_signals_terminate_then_next_start_requeues(worker_env: WorkerEnv) -> None:
    """IT08-08 two signals: the second terminates at once and the job stays `running` under
    the old owner; the next worker start requeues it (crash recovery) and runs it again."""
    job_id = worker_env.enqueue("checkpoint_ignore")
    first = worker_env.spawn_worker("--gpu-classes", "none", "--concurrency", "1")
    known: set[str] = set()
    first_id = _new_worker(known)
    wait_for(lambda: _state_n(job_id) >= 2, what="checkpoints")
    interrupt(first)
    wait_for(lambda: _status(first_id) == "draining", what="draining row")
    interrupt(first)
    assert first.wait(timeout=WAIT_S) == 0, worker_env.output()
    row = worker_env.job(job_id)
    assert row.status == "running"
    assert row.lease_owner == f"{first_id}:cpu0"
    assert row.attempts == 1
    second = worker_env.spawn_worker("--gpu-classes", "none", "--concurrency", "1")
    second_id = _new_worker(known)
    wait_for(
        lambda: worker_env.job(job_id).lease_owner == f"{second_id}:cpu0",
        what="reclaim by the next worker",
    )
    expired = [e for e in worker_env.events("lease_expired") if e["job_id"] == job_id]
    assert [e["outcome"] for e in expired] == ["queued"]
    assert worker_env.job(job_id).attempts == 2  # the recovery charged nothing
    resumed = _state_n(job_id)
    wait_for(lambda: _state_n(job_id) >= resumed + 2, what="checkpoints of attempt 2")
    interrupt(second)
    wait_for(lambda: _status(second_id) == "draining", what="draining row")
    interrupt(second)
    assert second.wait(timeout=WAIT_S) == 0, worker_env.output()
