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
from tests.support._f08_swarm import link_evidence
from tests.support.fault_env import FaultEnv
from tests.support.ops_store import OpsStoreHandle
from tests.support.worker_bootstrap import BOOTSTRAP, FAST_JOBS
from tests.support.worker_env import WorkerEnv

from herness.core import time as clock
from herness.core.errors import StoreBusy
from herness.core.jobs.ports import require_jobs_backend
from herness.core.jobs.supervisor import run_worker
from herness.core.resilience import ProcessState, process_state
from herness.core.resilience.faults import FaultPlan
from herness.store.ops import _shims as ops_shims
from herness.store.ops import query_findings, select_runs, select_tasks
from herness.store.ops.core import read_all

LEASE_S: Final = int(FAST_JOBS["lease_s"])
REQUEUE_SAME_HOST_S: Final = 10
OTHER_OWNER_MARGIN_S: Final = 30


def _evidence_rows() -> int:
    [total] = read_all("SELECT count(*) AS n FROM evidence")
    return int(total["n"])


def _links(run_id: str) -> tuple[list[tuple[str, str]], Counter[str]]:
    """`(team, query_id)` of every stored `evidence_use` row of `run_id` (sorted, duplicates
    kept) and the number of links per task."""
    rows = read_all("SELECT query_id, task_id FROM evidence_use WHERE run_id = ?", (run_id,))
    teams = {t.task_id: t.spec.scope.entity_ids[0] for t in select_tasks(run_id)}
    pairs = sorted((teams[str(r["task_id"])], str(r["query_id"])) for r in rows)
    return pairs, Counter(str(r["task_id"]) for r in rows)


def _finding_links(run_id: str) -> list[tuple[str, str]]:
    """`(entity, cited query id)` of every number of every finding of `run_id`."""
    findings = query_findings(run_id, limit=500)
    return sorted((f.entity_id, n.query_id) for f in findings for n in f.numbers)


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
    the run is `done`, its finding count equals a clean run's, no finding is posted twice,
    the per-task evidence links (`evidence_use`, recorded by each scripted analyst before its
    finding, as `run_sql` would) and the evidence each finding cites equal the clean run's,
    no task has a second link and no evidence row is written again.

    Driver level with the T06-22 stub pipeline, as the driver's own run tests do: a real
    review cannot pass the record step until T06-22b (carry-over T06-26)."""
    link_evidence(monkeypatch, drv.agents)  # the child does the same (`build_drv`)
    clean = asyncio.run(drv.swarm().start(drv.request(depth="standard")))
    assert clean.status == "done"
    clean_findings = query_findings(clean.run_id, limit=500)
    clean_links, clean_per_task = _links(clean.run_id)
    assert len(clean_links) == 3  # non-empty baseline: one link per analyst task
    assert set(clean_per_task.values()) == {1}
    evidence_before = _evidence_rows()
    fault_env([{"point": "swarm.after_finding_write", "action": "kill", "nth": 2}])
    env = fj.child_env(monkeypatch, reset_process_state)

    fj.killed(fj.child(env, "swarm", str(drv.cfg_root), str(ops_store.data_root)))
    [run] = select_runs(statuses={"running"})
    assert len(query_findings(run.run_id, limit=500)) == 2  # committed before the kill
    assert len(_links(run.run_id)[0]) >= 2  # the child linked them before it died
    res = asyncio.run(drv.swarm().resume(run.run_id))

    assert res.status == "done"
    findings = query_findings(run.run_id, limit=500)
    assert len(findings) == len(clean_findings)
    claims = Counter((f.entity_id, f.claim) for f in findings)
    assert claims == Counter((f.entity_id, f.claim) for f in clean_findings)
    per_task = Counter(f.task_id for f in findings)
    assert set(per_task.values()) == {1}  # the re-run task did not post its finding again
    assert all(t.status == "done" for t in select_tasks(run.run_id))
    links, per_task = _links(run.run_id)
    assert links == clean_links  # the killed tasks' links were made once, re-run or not
    assert set(per_task.values()) == {1}
    assert _finding_links(run.run_id) == _finding_links(clean.run_id) != []
    assert _evidence_rows() == evidence_before  # no evidence row written again


def test_ft08_07_claim_then_kill_requeued_on_restart_and_completed(
    worker_env: WorkerEnv,
    reset_process_state: ProcessState,
    fault_env: FaultEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FT08-07 F7 (worker restart): a process claims a job as `<host>:<pid>:cpu0` and dies at
    `job.after_claim kill`; the worker restarts 1 s after the claim (`herness.core.time.now`
    pinned there) and requeues it at that very instant, while the dead claim's lease is still
    valid: start-up recovery, not lease expiry, within 10 s. The recovery charges no attempt
    and the restarted worker completes the job on attempt 2."""
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
    assert row.lease_expires_at is not None
    restart_at = row.lease_expires_at - timedelta(seconds=LEASE_S - 1)  # claim + 1 s
    monkeypatch.setattr(clock, "now", lambda: restart_at)
    assert run_worker(gpu_classes=["none"], once=True, bootstrap=BOOTSTRAP) == 0

    done = worker_env.job(job_id)
    assert (done.status, done.attempts) == ("done", 2)  # the dead claim's attempt, then this
    assert done.result is not None
    assert done.result["attempt"] == 2
    [expired] = [e for e in fj.job_events("lease_expired") if e["job_id"] == job_id]
    assert expired["outcome"] == "queued"
    [worker] = require_jobs_backend().list_workers()
    assert worker.started_at == restart_at
    requeued_at = clock.parse_utc(expired["ts"])
    assert requeued_at < row.lease_expires_at  # the dead lease had not run out
    assert requeued_at - worker.started_at <= timedelta(seconds=REQUEUE_SAME_HOST_S)


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


def _record_busy_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The `op` of every ops-store write attempt the plan failed with StoreBusy (this process)."""
    hit: list[str] = []
    real = ops_shims.fault_point

    def spy(name: str, **labels: str) -> None:
        try:
            real(name, **labels)
        except StoreBusy:
            hit.append(labels.get("kind", ""))
            raise

    monkeypatch.setattr(ops_shims, "fault_point", spy)
    return hit


def test_ft08_11_store_busy_writes_retried_no_lost_job_state(
    worker_env: WorkerEnv, fault_env: FaultEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT08-11 F11: `sqlite.write error:StoreBusy count=4`, enabled once the worker has
    started (and inherited by its job child): the worker's next ops-store writes, starting
    with a `job`-table write of its tick, fail four times and are retried (`sqlite_write`), so
    every write succeeds; the job runs once and ends `done` with its result, the worker row
    reaches `stopped` and the `job_done` event is stored: no job state is lost."""
    job_id = worker_env.enqueue()
    sup = worker_env.supervisor(concurrency=1, once=True)
    assert sup.start() is None  # registration done before the plan exists
    fault_env([{"point": "sqlite.write", "action": "error:StoreBusy", "count": 4}])
    busy_ops = _record_busy_writes(monkeypatch)

    worker_env.drive(sup, lambda: worker_env.job(job_id).status == "done")
    assert sup.stop() == 0

    plan = process_state().fault_plan
    assert isinstance(plan, FaultPlan)
    assert plan.counters[0] > 4  # all four injected failures fired, later writes passed
    assert len(busy_ops) == 4
    # the first hit is the tick's reaper write on the `job` table (the others land on the
    # `retry` event rows it records); that write then succeeded on retry
    assert busy_ops[0].startswith("job_"), busy_ops
    row = worker_env.job(job_id)
    assert (row.status, row.attempts) == ("done", 1)
    assert row.result is not None
    assert row.result["attempt"] == 1
    assert row.lease_owner is None
    [worker] = require_jobs_backend().list_workers()
    assert worker.status == "stopped"
    assert [e["job_id"] for e in fj.job_events("job_done")] == [job_id]
