"""FT08-06, FT08-07 and FT08-11 of the impl 08 fault suite (T08-24).

Private sibling of `tests/fault/test_resilience_f01_f12.py`, which imports and collects these
tests (the ENG 400-line limit splits the suite); they run under its `fault` marker and with its
fixtures. Kills happen in real child processes (`tests.support._f08_children`); waits are the
blocking end of a child or of an in-process `run_worker(once=True)`, never a sleep. Time
bounds are asserted on lease arithmetic and stored timestamps.
"""

from __future__ import annotations

import asyncio
import socket
from collections import Counter
from datetime import timedelta
from typing import Final

import pytest
from tests.integration.harness._swarm_run_env import Drv
from tests.support import _f08_jobs as fj
from tests.support.fault_env import FaultEnv
from tests.support.ops_store import OpsStoreHandle
from tests.support.worker_bootstrap import BOOTSTRAP, FAST_JOBS
from tests.support.worker_env import WorkerEnv

from herness.core import time as clock
from herness.core.jobs.ports import require_jobs_backend
from herness.core.jobs.supervisor import run_worker
from herness.core.resilience import ProcessState, process_state
from herness.core.resilience.faults import FaultPlan
from herness.store.ops import query_findings, select_runs, select_tasks
from herness.store.ops.core import read_all

LEASE_S: Final = int(FAST_JOBS["lease_s"])
REQUEUE_SAME_HOST_S: Final = 10
OTHER_OWNER_MARGIN_S: Final = 30


def _evidence_counts() -> tuple[int, list[tuple[str, str, str | None, int]]]:
    """(evidence rows, every `evidence_use` key stored more than once)."""
    [total] = read_all("SELECT count(*) AS n FROM evidence")
    dupes = read_all(
        "SELECT query_id, run_id, task_id, count(*) AS n FROM evidence_use"
        " GROUP BY query_id, run_id, task_id HAVING count(*) > 1"
    )
    return int(total["n"]), [(d["query_id"], d["run_id"], d["task_id"], d["n"]) for d in dupes]


def test_ft08_06_kill_after_finding_write_then_resume_no_duplicates(
    drv: Drv,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fault_env: FaultEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FT08-06 F6: a standard review run in a child process is killed by
    `swarm.after_finding_write kill nth=2` (the second finding is committed, then the process
    dies mid fan-out); `resume` (`Swarm.resume`, what `herness resume` runs) finishes it:
    the run is `done`, its finding count equals a clean run's, no finding is posted twice and
    no evidence row or `evidence_use` row is duplicated.

    Driver level with the T06-22 stub pipeline (IT06-01/02 shape): a real review cannot pass
    the record step until T06-22b (carry-over T06-26)."""
    clean = asyncio.run(drv.swarm().start(drv.request(depth="standard")))
    assert clean.status == "done"
    clean_findings = query_findings(clean.run_id, limit=500)
    evidence_before, _ = _evidence_counts()
    fault_env([{"point": "swarm.after_finding_write", "action": "kill", "nth": 2}])
    env = fj.child_env(monkeypatch, reset_process_state)

    fj.killed(fj.child(env, "swarm", str(drv.cfg_root), str(ops_store.data_root)))
    [run] = select_runs(statuses={"running"})
    assert len(query_findings(run.run_id, limit=500)) == 2  # committed before the kill
    res = asyncio.run(drv.swarm().resume(run.run_id))

    assert res.status == "done"
    findings = query_findings(run.run_id, limit=500)
    assert len(findings) == len(clean_findings)
    claims = Counter((f.entity_id, f.claim) for f in findings)
    assert claims == Counter((f.entity_id, f.claim) for f in clean_findings)
    per_task = Counter(f.task_id for f in findings)
    assert set(per_task.values()) == {1}  # the re-run task did not post its finding again
    assert all(t.status == "done" for t in select_tasks(run.run_id))
    assert _evidence_counts() == (evidence_before, [])


def test_ft08_07_claim_then_kill_requeued_on_restart_and_completed(
    worker_env: WorkerEnv,
    reset_process_state: ProcessState,
    fault_env: FaultEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FT08-07 F7 (worker restart): a process claims a job as `<host>:<pid>:cpu0` and dies at
    `job.after_claim kill`; the restarted worker requeues it during start-up (within 10 s of
    its start, by the stored timestamps), the recovery charges no attempt, and the job is
    completed by the restarted worker on attempt 2."""
    job_id = worker_env.enqueue()
    fault_env([{"point": "job.after_claim", "action": "kill"}])
    env = fj.child_env(monkeypatch, reset_process_state)
    host = socket.gethostname().lower()

    fj.killed(fj.child(env, "claim", host))
    row = worker_env.job(job_id)
    assert (row.status, row.attempts) == ("running", 1)
    assert row.lease_owner is not None
    assert row.lease_owner.startswith(f"{host}:")
    assert row.lease_owner.endswith(":cpu0")
    assert run_worker(gpu_classes=["none"], once=True, bootstrap=BOOTSTRAP) == 0

    done = worker_env.job(job_id)
    assert (done.status, done.attempts) == ("done", 2)  # the dead claim's attempt, then this
    assert done.result is not None
    assert done.result["attempt"] == 2
    [expired] = [e for e in fj.job_events("lease_expired") if e["job_id"] == job_id]
    assert expired["outcome"] == "queued"
    [worker] = require_jobs_backend().list_workers()
    requeued_after = clock.parse_utc(expired["ts"]) - worker.started_at
    assert timedelta(0) <= requeued_after <= timedelta(seconds=REQUEUE_SAME_HOST_S)


def test_ft08_07_claim_then_kill_requeued_by_reaper_within_lease_plus_30s(
    worker_env: WorkerEnv,
    reset_process_state: ProcessState,
    fault_env: FaultEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FT08-07 F7 (no restart): the dying claimant is another host's worker, so no start-up
    recovery applies; a running worker's reaper, ticked on explicit instants after the
    claim, leaves the lease alone while it is valid and requeues the job at most
    `lease_s` + 30 s after the claim, with one `lease_expired` event."""
    job_id = worker_env.enqueue()
    sup = worker_env.supervisor(concurrency=0)
    assert sup.start() is None
    fault_env([{"point": "job.after_claim", "action": "kill"}])
    env = fj.child_env(monkeypatch, reset_process_state)

    fj.killed(fj.child(env, "claim", "otherhost"))
    row = worker_env.job(job_id)
    assert row.status == "running"
    assert row.lease_expires_at is not None
    claimed_at = row.lease_expires_at - timedelta(seconds=LEASE_S)
    requeued_at = None
    for second in range(LEASE_S + OTHER_OWNER_MARGIN_S + 1):
        sup.tick(claimed_at + timedelta(seconds=second))
        if worker_env.job(job_id).status == "queued":
            requeued_at = second
            break
    assert sup.stop() == 0

    assert requeued_at is not None
    assert LEASE_S <= requeued_at <= LEASE_S + OTHER_OWNER_MARGIN_S
    expired = [e for e in fj.job_events("lease_expired") if e["job_id"] == job_id]
    assert [e["outcome"] for e in expired] == ["queued"]


def test_ft08_11_store_busy_writes_retried_no_lost_job_state(
    worker_env: WorkerEnv, fault_env: FaultEnv
) -> None:
    """FT08-11 F11: `sqlite.write error:StoreBusy count=4` in the worker (and its job
    child, which inherits the plan): the first four ops-store writes of each process fail and
    are retried under `sqlite_write` (6 attempts), so every write succeeds; the job runs once
    and ends `done` with its result, the worker row reaches `stopped` and the `job_done`
    event is stored: no job state is lost."""
    job_id = worker_env.enqueue()
    fault_env([{"point": "sqlite.write", "action": "error:StoreBusy", "count": 4}])

    assert run_worker(gpu_classes=["none"], once=True, bootstrap=BOOTSTRAP) == 0

    plan = process_state().fault_plan
    assert isinstance(plan, FaultPlan)
    assert plan.counters[0] > 4  # all four injected failures fired, later writes passed
    row = worker_env.job(job_id)
    assert (row.status, row.attempts) == ("done", 1)
    assert row.result is not None
    assert row.result["attempt"] == 1
    assert row.lease_owner is None
    [worker] = require_jobs_backend().list_workers()
    assert worker.status == "stopped"
    assert [e["job_id"] for e in fj.job_events("job_done")] == [job_id]
