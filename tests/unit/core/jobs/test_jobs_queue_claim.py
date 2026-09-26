"""Tests for herness.core.jobs.queue: claim, cancel, retry, get, list_jobs and worker_alive
(impl 08 U08-48, U08-51 to U08-54; T08-12) against the real migrated ops store.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from tests.unit.core.jobs._queue_env import jobs_db  # noqa: F401 - fixture

from herness.core import time as clock
from herness.core.errors import ConfigError, JobStateError
from herness.core.jobs import queue
from herness.core.jobs.ports import JobRow, WorkerRow, require_jobs_backend
from herness.core.resilience import ProcessState, process_state
from herness.core.types import GpuClass, JobKind

pytestmark = pytest.mark.unit

ALL: tuple[GpuClass, ...] = ("none", "reasoning", "decider", "large")


def _add(
    kind: JobKind = "sync",
    gpu_class: GpuClass = "none",
    priority: int | None = None,
    *,
    at: datetime | None = None,
    n: int = 0,
) -> str:
    return queue.enqueue(kind, {"n": n}, gpu_class, priority, at)


def _claim(owner: str = "h:1:cpu0", allowed: Sequence[GpuClass] = ("none",)) -> JobRow | None:
    return queue.claim(owner=owner, allowed_classes=allowed)


def _fail(job_id: str) -> None:
    """Claim ``job_id`` as the CLI slot and mark it failed."""
    assert queue.claim(owner="h:1:cli", allowed_classes=ALL, job_id=job_id) is not None
    now = datetime.now(UTC)
    assert require_jobs_backend().finish_failed(job_id, "h:1:cli", {"type": "X"}, now)


def _events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [line for line in logs if line["event"] == name]


# --- UT08-56, UT08-57, UT08-107: claim ------------------------------------------------------


@pytest.mark.usefixtures("jobs_db")
def test_ut08_56_claims_in_priority_order_and_skips_future() -> None:
    """UT08-56 priorities 10/90/50 plus a future job: claimed 90, 50, 10; future untouched."""
    ids = {p: _add(priority=p, n=p) for p in (10, 90, 50)}
    future = _add(priority=100, at=datetime.now(UTC) + timedelta(hours=1), n=1)
    _add("review", "reasoning", 95, n=2)  # other class: not claimable with ["none"]
    claimed = [_claim() for _ in range(4)]
    assert [row.job_id if row else None for row in claimed] == [ids[90], ids[50], ids[10], None]
    first = claimed[0]
    assert first is not None
    assert (first.status, first.lease_owner, first.attempts) == ("running", "h:1:cpu0", 1)
    assert first.lease_expires_at is not None
    assert first.started_at is not None
    assert first.lease_expires_at - first.started_at == timedelta(seconds=300)
    assert queue.get(future).status == "queued"
    waits = [k for k, _ in process_state().metric_buffer.histograms]
    assert waits == [("herness_jobs_queue_wait_seconds", (("kind", "sync"),), "jobs")] * 3


@pytest.mark.usefixtures("jobs_db")
def test_ut08_56_claim_logs_claimed() -> None:
    """UT08-56 a claim logs `jobs.job.claimed` with job_id, kind, attempt, owner and wait."""
    job_id = _add()
    with structlog.testing.capture_logs() as logs:
        assert _claim("host-a:42:cpu1") is not None
    (line,) = _events(logs, "jobs.job.claimed")
    assert (line["job_id"], line["kind"], line["attempt"], line["owner"]) == (
        job_id,
        "sync",
        1,
        "host-a:42:cpu1",
    )
    assert line["wait_s"] >= 0


@pytest.mark.usefixtures("jobs_db")
def test_ut08_57_exclusive_kind_waits_for_running_build() -> None:
    """UT08-57 with a `build_pipeline` running, `sync` is claimed and `distill` is not."""
    build = _add("build_pipeline")
    assert _claim("h:1:gpu", ALL) is not None
    assert queue.get(build).status == "running"
    distill = _add("distill", priority=90)
    sync = _add("sync")
    row = queue.claim(owner="h:1:cli", allowed_classes=ALL)
    assert row is not None
    assert row.job_id == sync
    assert queue.claim(owner="h:1:cli", allowed_classes=ALL) is None
    assert queue.get(distill).status == "queued"


@pytest.mark.usefixtures("jobs_db")
def test_ut08_107_slot_rule_for_none_class_jobs() -> None:
    """UT08-107 CPU owner gets only `sync`; GPU owner gets `build_pipeline` or `review` by
    priority, never `sync`; the CLI owner can claim any of them by job_id (R-43)."""
    build = _add("build_pipeline")  # none, priority 60
    sync = _add("sync")  # none, priority 60
    review = _add("review", "reasoning", 70)
    both: Sequence[GpuClass] = ("reasoning", "none")
    assert queue.claim(owner="h:1:cpu0", allowed_classes=["none"]) is not None  # sync first
    assert queue.get(sync).status == "running"
    assert queue.claim(owner="h:1:cpu1", allowed_classes=["none"]) is None  # never the build
    assert queue.cancel(sync) == "cancel_requested"
    sync_again = _add("sync", n=5)  # the GPU slot must not take a CPU kind
    gpu = [queue.claim(owner="h:1:gpu", allowed_classes=both) for _ in range(3)]
    assert [row.job_id if row else None for row in gpu] == [review, build, None]
    assert queue.get(sync_again).status == "queued"


@pytest.mark.usefixtures("jobs_db")
def test_ut08_107_cli_owner_claims_any_kind_by_id() -> None:
    """UT08-107 `h:1:cli` claims a GPU-slot kind, a CPU kind and a reasoning job by job_id."""
    ids = [_add("build_pipeline"), _add("sync"), _add("review", "reasoning")]
    for job_id in ids:
        row = queue.claim(owner="h:1:cli", allowed_classes=ALL, job_id=job_id)
        assert row is not None
        assert row.job_id == job_id


@pytest.mark.usefixtures("jobs_db")
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"owner": "h:1:tpu"}, "owner"),
        ({"owner": "h 1:1:gpu"}, "owner"),
        ({"owner": "h:1:cpu123"}, "owner"),
        ({"owner": 7}, "owner"),
        ({"allowed_classes": []}, "non-empty"),
        ({"allowed_classes": "none"}, "non-empty"),
        ({"allowed_classes": ["none", "huge"]}, "unknown GPU class"),
        ({"job_id": "job_x"}, "job_id"),
    ],
)
def test_ut08_56_invalid_claim_arguments(kwargs: dict[str, Any], match: str) -> None:
    """UT08-56 invalid owner, classes or job id → ConfigError (U08-48 Errors)."""
    args: dict[str, Any] = {"owner": "h:1:cpu0", "allowed_classes": ["none"]} | kwargs
    with pytest.raises(ConfigError, match=match):
        queue.claim(**args)


# --- UT08-60, UT08-61, ST08-11: cancel and retry -----------------------------------------


@pytest.mark.usefixtures("jobs_db")
def test_ut08_60_cancel_results() -> None:
    """UT08-60 queued → canceled, running → cancel_requested, done and unknown → not_active."""
    queued, running, done = _add(n=1), _add(n=2), _add(n=3)
    for job_id in (running, done):
        assert queue.claim(owner="h:1:cli", allowed_classes=ALL, job_id=job_id) is not None
    assert require_jobs_backend().finish_done(done, "h:1:cli", {}, datetime.now(UTC))
    results = [queue.cancel(job_id) for job_id in (queued, running, done, "job_unknown")]
    assert results == ["canceled", "cancel_requested", "not_active", "not_active"]
    assert queue.get(queued).finished_at is not None
    assert queue.get(running).lease_owner == "h:1:cli"


@pytest.mark.usefixtures("jobs_db")
def test_ut08_61_retry_failed_and_refusals() -> None:
    """UT08-61 failed → queued, attempts 0; done job, active idem key → JobStateError."""
    failed = _add(n=1)
    _fail(failed)
    queue.retry(failed)
    row = queue.get(failed)
    assert (row.status, row.attempts, row.lease_owner, row.finished_at) == ("queued", 0, None, None)
    assert row.last_error == {"type": "X"}
    done = _add(n=2)
    assert queue.claim(owner="h:1:cli", allowed_classes=ALL, job_id=done) is not None
    assert require_jobs_backend().finish_done(done, "h:1:cli", {}, datetime.now(UTC))
    with pytest.raises(JobStateError, match="job is not failed") as not_failed:
        queue.retry(done)
    assert not_failed.value.job_id == done
    clash = _add(n=3)
    _fail(clash)
    _add(n=3)  # same payload → same default idem key, now active again
    with pytest.raises(JobStateError, match="an active job with the same idem_key exists"):
        queue.retry(clash)
    with pytest.raises(JobStateError, match="unknown job"):
        queue.retry("job_missing")


@pytest.mark.usefixtures("jobs_db")
def test_st08_11_cancel_and_retry_are_logged_with_job_id() -> None:
    """ST08-11 cancel and retry log `jobs.job.cancel_requested` / `retry_requested` with job_id."""
    job_id = _add()
    failed = _add(n=9)
    _fail(failed)
    with structlog.testing.capture_logs() as logs:
        queue.cancel(job_id)
        queue.retry(failed)
        with pytest.raises(JobStateError):
            queue.retry(job_id)
    cancels = _events(logs, "jobs.job.cancel_requested")
    retries = _events(logs, "jobs.job.retry_requested")
    assert [(e["job_id"], e["result"], e["log_level"]) for e in cancels] == [
        (job_id, "canceled", "info")
    ]
    assert [(e["job_id"], e["result"]) for e in retries] == [
        (failed, "queued"),
        (job_id, "not_failed"),
    ]


# --- UT08-63: get and list_jobs -----------------------------------------------------------


@pytest.mark.usefixtures("jobs_db")
def test_ut08_63_get_and_list_jobs() -> None:
    """UT08-63 get of an unknown id → JobStateError; valid rows parse to JobRow, newest first."""
    with pytest.raises(JobStateError, match="unknown job"):
        queue.get("job_x")
    first = _add("sync", n=1)
    second = _add("review", "reasoning", n=2)
    rows = queue.list_jobs()
    assert all(isinstance(row, JobRow) for row in rows)
    assert [row.job_id for row in rows] == [second, first]
    assert [row.job_id for row in queue.list_jobs(kind="review")] == [second]
    assert [row.job_id for row in queue.list_jobs(status="queued", limit=1)] == [second]
    assert queue.list_jobs(status="failed") == []


@pytest.mark.usefixtures("jobs_db")
@pytest.mark.parametrize(
    "kwargs",
    [{"limit": 0}, {"limit": 1001}, {"limit": True}, {"status": "bogus"}, {"kind": "bogus"}],
)
def test_ut08_63_invalid_list_filters(kwargs: dict[str, Any]) -> None:
    """UT08-63 `list_jobs(limit=0)`, a bad limit, status or kind → ConfigError."""
    with pytest.raises(ConfigError):
        queue.list_jobs(**kwargs)


# --- UT08-65 core half: worker_alive -------------------------------------------------------


T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _worker(worker_id: str, status: str, age_s: int) -> None:
    beat = T0 - timedelta(seconds=age_s)
    row = WorkerRow.model_validate(
        {
            "worker_id": worker_id,
            "host": "h",
            "pid": 1,
            "gpu_slot": 1,
            "cpu_slots": 2,
            "gpu_class_loaded": "none",
            "status": status,
            "started_at": beat,
            "heartbeat_at": beat,
            "version": "1",
        }
    )
    require_jobs_backend().upsert_worker(row)


@pytest.mark.usefixtures("jobs_db")
def test_ut08_65_worker_alive_uses_three_heartbeats(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-65 (core half) heartbeat_s 30: a beat 89 s old is alive, 91 s old is not; stopped
    workers never count."""
    monkeypatch.setattr(clock, "now", lambda: T0)
    assert queue.worker_alive() is False
    _worker("w_old", "running", 91)
    _worker("w_stopped", "stopped", 1)
    assert queue.worker_alive() is False
    _worker("w_new", "draining", 89)
    assert queue.worker_alive() is True


@pytest.mark.usefixtures("jobs_db")
def test_ut08_65_unbound_backend_is_config_error(reset_process_state: ProcessState) -> None:
    """UT08-65 with no jobs backend bound the queue API raises ConfigError (U08-41)."""
    del reset_process_state
    from herness.core.resilience import reset_process_state as fresh  # noqa: PLC0415

    fresh()
    with pytest.raises(ConfigError, match="jobs backend not bound"):
        queue.worker_alive()
