"""Unit tests for herness.store.ops.jobs (impl 08 U08-95; T08-11).

Backend half of the queue tests: rows are checked against the real migrated `job` DDL (impl 02
migration 002). The core halves (logging, metrics, events, `JobStateError` mapping) are T08-12's
and the supervisor card's; each test names the ID of the spec row it serves.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import JsonValue
from tests.support.ops_store import OpsStoreHandle

from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.core.ids import canonical_json
from herness.core.jobs.ports import ClaimSlot, JobRow, NewJob, NextJob, QueueStats, SchedCheck
from herness.core.types import GpuClass, JobKind
from herness.store import ops
from herness.store.ops import _job_sql
from herness.store.ops.core import read_all, read_one, run_write
from herness.store.ops.jobs import SqliteJobsBackend

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LEASE = timedelta(minutes=5)
GPU_KINDS: tuple[JobKind, ...] = ("build_pipeline",)
ALL_CLASSES: tuple[GpuClass, ...] = ("none", "reasoning", "decider", "large")


@pytest.fixture
def backend(ops_store: OpsStoreHandle) -> SqliteJobsBackend:
    del ops_store
    return SqliteJobsBackend()


def _new(  # noqa: PLR0913 - test row builder, one keyword per column under test
    job_id: str,
    kind: JobKind = "sync",
    *,
    gpu_class: GpuClass = "none",
    priority: int = 50,
    payload: Mapping[str, object] | None = None,
    idem: str | None = None,
    max_attempts: int = 3,
    at: datetime = T0,
    created: datetime | None = None,
) -> NewJob:
    return NewJob(
        job_id=job_id,
        kind=kind,
        gpu_class=gpu_class,
        priority=priority,
        payload=canonical_json(payload or {}).encode("utf-8"),
        idem_key=f"k:{job_id}" if idem is None else idem,
        max_attempts=max_attempts,
        scheduled_for=at,
        created_at=created or at,
    )


def _add(backend: SqliteJobsBackend, *jobs: NewJob) -> None:
    for job in jobs:
        assert backend.insert_job(job, sched_check=None) == (job.job_id, True)


def _claim(  # noqa: PLR0913 - mirrors the claim port with test defaults
    backend: SqliteJobsBackend,
    owner: str = "h:1:cpu0",
    *,
    now: datetime = T0,
    allowed: Sequence[GpuClass] = ("none",),
    exclusive: Sequence[JobKind] = (),
    job_id: str | None = None,
    min_priority: int | None = None,
    exempt: Sequence[JobKind] = (),
    slot: ClaimSlot = "cpu",
) -> JobRow | None:
    return backend.claim_job(
        owner=owner,
        now=now,
        lease_until=now + LEASE,
        allowed_classes=allowed,
        exclusive_kinds=exclusive,
        job_id=job_id,
        min_priority=min_priority,
        priority_exempt_kinds=exempt,
        slot=slot,
        gpu_slot_kinds=GPU_KINDS,
    )


def _set(job_id: str, **columns: object) -> None:
    """Force columns of one row (test setup only; names come from the test code)."""
    assignments = ", ".join(f"{name} = :{name}" for name in columns)
    sql = f"UPDATE job SET {assignments} WHERE job_id = :job_id"  # noqa: S608 - test names

    def update(conn: sqlite3.Connection) -> None:
        conn.execute(sql, {**columns, "job_id": job_id})

    run_write(update, op="test_setup")


def _row(backend: SqliteJobsBackend, job_id: str) -> JobRow:
    row = backend.get_job(job_id)
    assert row is not None
    return row


def _count() -> int:
    row = read_one("SELECT COUNT(*) FROM job")
    assert row is not None
    return int(row[0])


# --- insert_job -------------------------------------------------------------------------------


def test_ut08_52_insert_is_idempotent_on_the_active_idem_key(backend: SqliteJobsBackend) -> None:
    """UT08-52 same idem key while active → same id, not created; after finish → a new row."""
    assert backend.insert_job(_new("job_a", idem="kind:1"), sched_check=None) == ("job_a", True)
    assert backend.insert_job(_new("job_b", idem="kind:1"), sched_check=None) == ("job_a", False)
    assert _count() == 1
    claimed = _claim(backend)
    assert claimed is not None
    assert backend.finish_done("job_a", "h:1:cpu0", {"ok": True}, T0)
    assert backend.insert_job(_new("job_c", idem="kind:1"), sched_check=None) == ("job_c", True)
    row = _row(backend, "job_c")
    assert (row.status, row.attempts, row.payload, row.scheduled_for) == ("queued", 0, {}, T0)


def test_ut08_53_sched_check_returns_the_finished_fire(backend: SqliteJobsBackend) -> None:
    """UT08-53 a done job with `sched:nightly:<F>` blocks the fire: existing id, no new row."""
    fire = "2026-09-01T02:00:00.000000Z"
    key = f"sched:nightly:{fire}"
    payload: dict[str, object] = {"schedule": "nightly", "fire_at": fire}
    _add(backend, _new("job_a", "build_pipeline", idem=key, payload=payload))
    _set("job_a", status="done", finished_at=clock.format_utc(T0))
    check = SchedCheck("nightly", fire)
    again = _new("job_b", "build_pipeline", idem=key, payload=payload)
    assert backend.insert_job(again, sched_check=check) == ("job_a", False)
    assert _count() == 1


def test_ut08_53_sched_check_matches_the_payload_fire(backend: SqliteJobsBackend) -> None:
    """UT08-53 the same payload `schedule`/`fire_at` under another key also blocks; another
    fire of the schedule is created."""
    fire = "2026-09-01T02:00:00.000000Z"
    _add(backend, _new("job_a", payload={"schedule": "nightly", "fire_at": fire}, idem="x"))
    _set("job_a", status="failed")
    other = _new("job_b", idem="sched:nightly:other")
    assert backend.insert_job(other, sched_check=SchedCheck("nightly", fire)) == ("job_a", False)
    later = _new("job_c", idem="sched:nightly:later")
    assert backend.insert_job(later, sched_check=SchedCheck("nightly", "later")) == (
        "job_c",
        True,
    )
    assert _count() == 2


def test_ut08_53_sync_mode_schedule_fires_again(backend: SqliteJobsBackend) -> None:
    """UT08-53 a sync-mode schedule (idem `sync:<source>`, U08-72): an earlier finished fire
    does not block the next fire; the same fire again is deduped."""
    f1, f2 = "2026-09-01T06:00:00.000000Z", "2026-09-01T07:00:00.000000Z"

    def fire(job_id: str, at: str) -> tuple[str, bool]:
        job = _new(job_id, idem="sync:jira", payload={"schedule": "sync.jira", "fire_at": at})
        return backend.insert_job(job, sched_check=SchedCheck("sync.jira", at))

    assert fire("job_1", f1) == ("job_1", True)
    _set("job_1", status="done", finished_at=clock.format_utc(T0))
    assert fire("job_2", f2) == ("job_2", True)
    assert fire("job_3", f2) == ("job_2", False)
    _set("job_2", status="done", finished_at=clock.format_utc(T0))
    assert fire("job_4", f2) == ("job_2", False)
    assert _count() == 2


def test_ut08_53_sched_key_alone_blocks_the_fire(backend: SqliteJobsBackend) -> None:
    """UT08-53 a finished job holding `sched:<name>:<fire_at>` blocks that fire even when its
    payload carries no `schedule`."""
    fire = "2026-09-01T02:00:00.000000Z"
    _add(backend, _new("job_a", idem=f"sched:nightly:{fire}"))
    _set("job_a", status="done")
    again = _new("job_b", idem="other")
    assert backend.insert_job(again, sched_check=SchedCheck("nightly", fire)) == ("job_a", False)
    assert _count() == 1


# --- claim_job / claimable_counts -------------------------------------------------------------


def test_ut08_56_claims_due_jobs_in_priority_order(backend: SqliteJobsBackend) -> None:
    """UT08-56 order 90, 50, 10 among due `none` jobs; the future and `reasoning` jobs stay."""
    _add(
        backend,
        _new("job_10", priority=10),
        _new("job_90", priority=90),
        _new("job_50", priority=50),
        _new("job_future", priority=99, at=T0 + timedelta(hours=1)),
        _new("job_gpu", "review", gpu_class="reasoning", priority=95),
    )
    claimed = [_claim(backend) for _ in range(4)]
    assert [row.job_id if row else None for row in claimed] == ["job_90", "job_50", "job_10", None]
    first = claimed[0]
    assert first is not None
    assert first.status == "running"
    assert first.lease_owner == "h:1:cpu0"
    assert first.lease_expires_at == T0 + LEASE
    assert (first.attempts, first.started_at) == (1, T0)
    assert _row(backend, "job_future").status == "queued"
    assert _row(backend, "job_gpu").status == "queued"


def test_ut08_56_ties_break_on_scheduled_for_then_created_at(backend: SqliteJobsBackend) -> None:
    """UT08-56 equal priority: the earlier `scheduled_for`, then the earlier `created_at`."""
    early = T0 - timedelta(minutes=2)
    _add(
        backend,
        _new("job_c", at=early, created=T0),
        _new("job_b", at=early, created=early),
        _new("job_a", at=T0 - timedelta(minutes=1)),
    )
    order = [_claim(backend) for _ in range(3)]
    assert [row.job_id for row in order if row] == ["job_b", "job_c", "job_a"]


def test_ut08_56_yield_and_reclaim_keep_started_at(backend: SqliteJobsBackend) -> None:
    """UT08-56 `started_at` is kept from the first start; yield undoes the attempt charge."""
    _add(backend, _new("job_a"))
    assert _claim(backend) is not None
    later = T0 + timedelta(minutes=3)
    assert backend.finish_yield("job_a", "h:1:cpu0", later, later)
    row = _row(backend, "job_a")
    assert (row.status, row.attempts, row.scheduled_for, row.lease_owner) == (
        "queued",
        0,
        later,
        None,
    )
    again = _claim(backend, "h:1:cpu1", now=later)
    assert again is not None
    assert (again.attempts, again.started_at, again.lease_owner) == (1, T0, "h:1:cpu1")


def test_ut08_57_exclusive_kinds_never_run_together(backend: SqliteJobsBackend) -> None:
    """UT08-57 with `build_pipeline` running, `distill` waits and `sync` is claimed."""
    exclusive: tuple[JobKind, ...] = ("build_pipeline", "distill")
    _add(
        backend,
        _new("job_build", "build_pipeline", priority=90),
        _new("job_distill", "distill", priority=80),
        _new("job_sync", "sync", priority=10),
    )
    build = _claim(backend, "h:1:gpu", allowed=ALL_CLASSES, exclusive=exclusive, slot="gpu")
    assert build is not None
    assert build.job_id == "job_build"
    nxt = _claim(backend, allowed=ALL_CLASSES, exclusive=exclusive)
    assert nxt is not None
    assert nxt.job_id == "job_sync"
    assert _claim(backend, allowed=ALL_CLASSES, exclusive=exclusive) is None
    assert backend.finish_done("job_build", "h:1:gpu", {}, T0)
    last = _claim(backend, allowed=ALL_CLASSES, exclusive=exclusive)
    assert last is not None
    assert last.job_id == "job_distill"


def test_ut08_56_min_priority_admits_exempt_kinds(backend: SqliteJobsBackend) -> None:
    """UT08-56 chat-window rule: below `min_priority` only exempt kinds are claimable."""
    _add(backend, _new("job_low", priority=20), _new("job_chat", "chat", priority=5))
    assert _claim(backend, min_priority=50) is None
    chat = _claim(backend, min_priority=50, exempt=("chat",))
    assert chat is not None
    assert chat.job_id == "job_chat"
    low = _claim(backend, min_priority=None)
    assert low is not None
    assert low.job_id == "job_low"


def test_ut08_56_claim_by_job_id(backend: SqliteJobsBackend) -> None:
    """UT08-56 `job_id` claims exactly that job even when a better one is due."""
    _add(backend, _new("job_hi", priority=90), _new("job_lo", priority=10))
    row = _claim(backend, "h:1:cli", job_id="job_lo", slot="cli")
    assert row is not None
    assert row.job_id == "job_lo"
    assert _claim(backend, "h:1:cli", job_id="job_lo", slot="cli") is None
    assert _claim(backend, job_id="job_missing") is None


def test_ut08_107_slot_rule_for_none_class_jobs(backend: SqliteJobsBackend) -> None:
    """UT08-107 CPU owner gets only `sync`; GPU owner gets `build_pipeline` or `review` by
    priority, never `sync`; the CLI owner can claim any of them by `job_id` (R-43)."""
    _add(
        backend,
        _new("job_build", "build_pipeline", priority=60),
        _new("job_sync", "sync", priority=90),
        _new("job_review", "review", gpu_class="reasoning", priority=70),
    )
    gpu: Sequence[GpuClass] = ("reasoning", "none")
    first = _claim(backend, "h:1:gpu", allowed=gpu, slot="gpu")
    second = _claim(backend, "h:1:gpu", allowed=gpu, slot="gpu")
    assert [first and first.job_id, second and second.job_id] == ["job_review", "job_build"]
    assert _claim(backend, "h:1:gpu", allowed=gpu, slot="gpu") is None
    cpu = _claim(backend, "h:1:cpu0", allowed=("none",), slot="cpu")
    assert cpu is not None
    assert cpu.job_id == "job_sync"


def test_ut08_107_cpu_slot_skips_gpu_slot_kinds_cli_takes_them(
    backend: SqliteJobsBackend,
) -> None:
    """UT08-107 a `none`-class `build_pipeline` job is not for CPU slots; the CLI owner takes it."""
    _add(backend, _new("job_build", "build_pipeline"))
    assert _claim(backend, "h:1:cpu0", slot="cpu") is None
    row = _claim(backend, "h:1:cli", job_id="job_build", slot="cli")
    assert row is not None
    assert row.lease_owner == "h:1:cli"


def test_ut08_107_claimable_counts_per_class_for_the_gpu_slot(backend: SqliteJobsBackend) -> None:
    """UT08-107 counts per requested class (0 when none); `none` counts only GPU-slot kinds."""
    _add(
        backend,
        _new("job_build", "build_pipeline"),
        _new("job_sync", "sync"),
        _new("job_r1", "review", gpu_class="reasoning", priority=40),
        _new("job_r2", "review", gpu_class="reasoning", priority=10),
        _new("job_large", "review", gpu_class="large"),
        _new("job_later", "review", gpu_class="decider", at=T0 + timedelta(hours=1)),
    )
    counts = backend.claimable_counts(
        now=T0,
        classes=("reasoning", "decider", "none"),
        exclusive_kinds=(),
        min_priority=20,
        priority_exempt_kinds=(),
        gpu_slot_kinds=GPU_KINDS,
    )
    assert counts == {"reasoning": 1, "decider": 0, "none": 1}


def test_ut08_107_claim_uses_the_job_claim_index(backend: SqliteJobsBackend) -> None:
    """UT08-107 acceptance: EXPLAIN QUERY PLAN of the claim statement uses index `job_claim`."""
    params = {
        "owner": "h:1:gpu",
        "now": clock.format_utc(T0),
        "lease_until": clock.format_utc(T0 + LEASE),
        "allowed_classes": '["none","reasoning"]',
        "exclusive_kinds": '["build_pipeline"]',
        "job_id": None,
        "min_priority": None,
        "exempt_kinds": "[]",
        "slot": "gpu",
        "gpu_slot_kinds": '["build_pipeline"]',
    }
    del backend
    plan = read_all("EXPLAIN QUERY PLAN " + _job_sql.CLAIM, params)
    details = " | ".join(str(row["detail"]) for row in plan)
    assert "USING INDEX job_claim" in details or "USING COVERING INDEX job_claim" in details


# --- heartbeat, completions, state --------------------------------------------------------------


def test_ut08_59_heartbeat_extends_only_the_owners_lease(backend: SqliteJobsBackend) -> None:
    """UT08-59 heartbeat → True and a later lease for the owner; another owner → False."""
    _add(backend, _new("job_a"))
    assert _claim(backend) is not None
    later = T0 + timedelta(minutes=9)
    assert backend.heartbeat_job("job_a", "h:1:cpu0", later)
    assert _row(backend, "job_a").lease_expires_at == later
    assert not backend.heartbeat_job("job_a", "h:2:cpu0", later + LEASE)
    assert _row(backend, "job_a").lease_expires_at == later


def test_ut08_59_completions_are_owner_guarded(backend: SqliteJobsBackend) -> None:
    """UT08-59 done / requeue / failed write their columns; a wrong owner changes nothing."""
    _add(backend, _new("job_d"), _new("job_r"), _new("job_f"))
    for _ in range(3):
        assert _claim(backend) is not None
    error: dict[str, JsonValue] = {
        "class": "SourceUnavailable",
        "message": "down",
        "at": "x",
        "attempt": 1,
    }
    assert not backend.finish_done("job_d", "h:9:cpu0", {"n": 1}, T0)
    assert _row(backend, "job_d").status == "running"
    stop = T0 + timedelta(minutes=1)
    assert backend.finish_done("job_d", "h:1:cpu0", {"n": 1}, stop)
    assert backend.finish_requeue("job_r", "h:1:cpu0", stop, error)
    assert backend.finish_failed("job_f", "h:1:cpu0", error, stop)
    done, requeued, failed = (_row(backend, j) for j in ("job_d", "job_r", "job_f"))
    assert (done.status, done.result, done.finished_at, done.lease_owner) == (
        "done",
        {"n": 1},
        stop,
        None,
    )
    assert (requeued.status, requeued.scheduled_for, requeued.last_error) == (
        "queued",
        stop,
        error,
    )
    assert (requeued.attempts, requeued.lease_expires_at) == (1, None)
    assert (failed.status, failed.finished_at, failed.last_error) == ("failed", stop, error)
    assert not backend.finish_failed("job_f", "h:1:cpu0", error, stop)


def test_ut08_59_save_and_load_state(backend: SqliteJobsBackend) -> None:
    """UT08-59 `save_job_state` stores `{"state": …}` for the owner only; done replaces it."""
    _add(backend, _new("job_a"))
    assert backend.load_job_state("job_a") == {}
    assert backend.load_job_state("job_missing") == {}
    assert _claim(backend) is not None
    assert backend.save_job_state("job_a", "h:1:cpu0", b'{"page": 3, "ids": [1, 2]}')
    assert backend.load_job_state("job_a") == {"page": 3, "ids": [1, 2]}
    assert _row(backend, "job_a").result == {"state": {"page": 3, "ids": [1, 2]}}
    assert not backend.save_job_state("job_a", "h:2:cpu0", b'{"page": 9}')
    with pytest.raises(SchemaViolation):
        backend.save_job_state("job_a", "h:1:cpu0", b"{not json")
    assert backend.finish_done("job_a", "h:1:cpu0", {"rows": 7}, T0)
    assert backend.load_job_state("job_a") == {}


def test_ut08_59_load_state_ignores_a_non_object_state(backend: SqliteJobsBackend) -> None:
    """UT08-59 a `state` that is not an object loads as `{}`."""
    _add(backend, _new("job_a"))
    _set("job_a", result=json.dumps({"state": [1, 2]}))
    assert backend.load_job_state("job_a") == {}


# --- cancel / retry ---------------------------------------------------------------------------


def test_ut08_60_cancel_by_status(backend: SqliteJobsBackend) -> None:
    """UT08-60 queued → canceled; running → cancel_requested (lease kept); done and unknown →
    not_active; the canceled running job is finalised by its owner only."""
    _add(backend, _new("job_q", priority=10), _new("job_r", priority=90), _new("job_d"))
    assert _claim(backend) is not None  # job_r
    _set("job_d", status="done")
    stop = T0 + timedelta(minutes=2)
    assert backend.cancel_job("job_q", stop) == "canceled"
    assert backend.cancel_job("job_r", stop) == "cancel_requested"
    assert backend.cancel_job("job_d", stop) == "not_active"
    assert backend.cancel_job("job_x", stop) == "not_active"
    queued, running = _row(backend, "job_q"), _row(backend, "job_r")
    assert (queued.status, queued.finished_at) == ("canceled", stop)
    assert (running.status, running.lease_owner, running.finished_at) == (
        "canceled",
        "h:1:cpu0",
        None,
    )
    assert not backend.heartbeat_job("job_r", "h:1:cpu0", stop + LEASE)
    assert not backend.finalize_canceled("job_r", "h:2:cpu0", stop)
    assert backend.finalize_canceled("job_r", "h:1:cpu0", stop)
    final = _row(backend, "job_r")
    assert (final.status, final.finished_at, final.lease_owner, final.lease_expires_at) == (
        "canceled",
        stop,
        None,
        None,
    )


def test_ut08_61_retry_results(backend: SqliteJobsBackend) -> None:
    """UT08-61 failed → queued with attempts 0 and `last_error` kept; done → not_failed;
    unknown → missing; an active job holding the idem key → conflict (row unchanged)."""
    error: dict[str, JsonValue] = {"class": "Boom", "message": "m", "at": "t", "attempt": 3}
    _add(backend, _new("job_f", idem="same"))
    assert _claim(backend) is not None
    assert backend.finish_failed("job_f", "h:1:cpu0", error, T0)
    later = T0 + timedelta(hours=1)
    assert backend.retry_job("job_f", later) == "queued"
    row = _row(backend, "job_f")
    assert (row.status, row.attempts, row.scheduled_for, row.finished_at) == (
        "queued",
        0,
        later,
        None,
    )
    assert row.last_error == error
    assert backend.retry_job("job_f", later) == "not_failed"
    assert backend.retry_job("job_missing", later) == "missing"
    _set("job_f", status="failed")
    _add(backend, _new("job_new", idem="same"))
    assert backend.retry_job("job_f", later) == "conflict"
    assert _row(backend, "job_f").status == "failed"


# --- reads --------------------------------------------------------------------------------------


def test_ut08_63_get_and_list_jobs(backend: SqliteJobsBackend) -> None:
    """UT08-63 `get_job` parses to `JobRow` or None; `list_jobs` is newest first and filtered."""
    _add(
        backend,
        _new("job_1", created=T0),
        _new("job_2", "review", gpu_class="reasoning", created=T0 + timedelta(seconds=1)),
        _new("job_3", created=T0 + timedelta(seconds=2)),
    )
    assert backend.get_job("job_x") is None
    assert isinstance(backend.get_job("job_1"), JobRow)
    everything = backend.list_jobs(status=None, kind=None, limit=50)
    assert [row.job_id for row in everything] == ["job_3", "job_2", "job_1"]
    syncs = backend.list_jobs(status="queued", kind="sync", limit=1)
    assert [row.job_id for row in syncs] == ["job_3"]
    assert backend.list_jobs(status="running", kind=None, limit=10) == []


def test_ut08_54_sched_fired_and_recent_scheduled(backend: SqliteJobsBackend) -> None:
    """UT08-54 `sched_fired` sees a fire through the payload in any status;
    `recent_scheduled` lists done or failed scheduled jobs finished since."""
    fire = "2026-09-01T06:00:00.000000Z"
    _add(
        backend,
        _new("job_s", payload={"schedule": "sync.jira", "fire_at": fire}),
        _new("job_old", payload={"schedule": "nightly", "fire_at": "a"}),
        _new("job_plain"),
    )
    assert backend.sched_fired("sync.jira", fire)
    assert not backend.sched_fired("sync.jira", "2026-09-02T06:00:00.000000Z")
    assert backend.recent_scheduled(T0) == []
    _set("job_s", status="done", finished_at=clock.format_utc(T0 + timedelta(minutes=1)))
    _set("job_old", status="failed", finished_at=clock.format_utc(T0 - timedelta(days=2)))
    _set("job_plain", status="done", finished_at=clock.format_utc(T0 + timedelta(minutes=1)))
    assert [row.job_id for row in backend.recent_scheduled(T0)] == ["job_s"]


def test_ut08_63_rekey_on_window(backend: SqliteJobsBackend) -> None:
    """UT08-63 `rekey_on` finds the queued/running/done rekey job scheduled in [start, end)."""
    night = T0 + timedelta(hours=10)
    rekey = {"action": "rekey"}
    _add(
        backend,
        _new("job_rekey", "maintenance", gpu_class="decider", payload=rekey, at=night),
        _new("job_other", "maintenance", payload={"action": "vacuum"}, at=night),
        _new("job_edge", "maintenance", payload=rekey, at=night + timedelta(hours=2)),
    )
    found = backend.rekey_on(night, night + timedelta(hours=2))
    assert found is not None
    assert found.job_id == "job_rekey"
    _set("job_rekey", status="canceled")
    assert backend.rekey_on(night, night + timedelta(hours=2)) is None


def test_ut08_63_queue_stats(backend: SqliteJobsBackend) -> None:
    """UT08-63 `queue_stats`: queued count, next job in claim order, failed since, dead letters."""
    empty = backend.queue_stats(T0, T0 - timedelta(hours=24))
    assert empty == QueueStats(queued=0, next_job=None, failed_24h=0, dead_letters=0)
    _add(backend, _new("job_future", priority=99, at=T0 + timedelta(hours=1)))
    only_future = backend.queue_stats(T0, T0 - timedelta(hours=24))
    assert only_future.next_job == NextJob("job_future", "sync", 99, T0 + timedelta(hours=1))
    _add(
        backend,
        _new("job_lo", priority=10),
        _new("job_hi", "review", gpu_class="reasoning", priority=70),
        _new("job_f1"),
        _new("job_f2"),
    )
    _set("job_f1", status="failed", finished_at=clock.format_utc(T0 - timedelta(hours=1)))
    _set("job_f2", status="failed", finished_at=clock.format_utc(T0 - timedelta(days=3)))
    stats = backend.queue_stats(T0, T0 - timedelta(hours=24))
    assert stats == QueueStats(
        queued=3,
        next_job=NextJob("job_hi", "review", 70, T0),
        failed_24h=1,
        dead_letters=2,
    )


def test_ut08_63_postpone_class(backend: SqliteJobsBackend) -> None:
    """UT08-63 `postpone_class` moves only the due queued jobs of that class."""
    until = T0 + timedelta(minutes=10)
    _add(
        backend,
        _new("job_r1", "review", gpu_class="reasoning"),
        _new("job_r2", "review", gpu_class="reasoning", at=T0 + timedelta(hours=2)),
        _new("job_n", gpu_class="none"),
    )
    assert backend.postpone_class("reasoning", T0, until) == 1
    assert _row(backend, "job_r1").scheduled_for == until
    assert _row(backend, "job_r2").scheduled_for == T0 + timedelta(hours=2)
    assert _row(backend, "job_n").scheduled_for == T0


# --- reaper, crash recovery -------------------------------------------------------------------


def test_ut08_62_reap_requeues_then_fails_expired_leases(backend: SqliteJobsBackend) -> None:
    """UT08-62 expired running jobs at attempts 1/3 and 3/3: first `queued` with `LeaseExpired`,
    second `failed`; both are reported so the caller emits two `lease_expired` events."""
    _add(
        backend,
        _new("job_a", "sync"),
        _new("job_b", "review", gpu_class="reasoning"),
        _new("job_live"),
    )
    for _ in range(3):
        assert _claim(backend, allowed=ALL_CLASSES, slot="cli") is not None
    _set("job_b", attempts=3)
    _set("job_live", lease_expires_at=clock.format_utc(T0 + timedelta(hours=1)))
    now = T0 + LEASE + timedelta(seconds=1)
    assert backend.reap_expired(now) == [("job_a", "sync", "queued"), ("job_b", "review", "failed")]
    a, b, live = (_row(backend, j) for j in ("job_a", "job_b", "job_live"))
    stamp = clock.format_utc(now)
    assert (a.status, a.lease_owner, a.lease_expires_at, a.scheduled_for) == (
        "queued",
        None,
        None,
        now,
    )
    assert a.last_error == {"class": "LeaseExpired", "at": stamp, "attempt": 1}
    assert (b.status, b.finished_at, b.lease_owner) == ("failed", now, None)
    assert b.last_error == {"class": "LeaseExpired", "at": stamp, "attempt": 3}
    assert live.status == "running"
    assert backend.reap_expired(now) == []


def test_ut08_62_requeue_owned_and_running_on_host(backend: SqliteJobsBackend) -> None:
    """UT08-62 crash recovery: `running_on_host` escapes LIKE wildcards; `requeue_owned` runs
    the reaper statements for the listed owners whatever their lease."""
    _add(backend, _new("job_a", priority=90), _new("job_b", priority=50), _new("job_c", priority=9))
    assert _claim(backend, "h_1:7:cpu0") is not None
    assert _claim(backend, "hx1:8:cpu0") is not None
    assert _claim(backend, "h%:9:cpu0") is not None
    assert [row.job_id for row in backend.running_on_host("h_1")] == ["job_a"]
    assert [row.job_id for row in backend.running_on_host("h%")] == ["job_c"]
    assert backend.running_on_host("h") == []
    assert backend.requeue_owned([], T0) == []
    assert backend.requeue_owned(["h_1:7:cpu0", "nobody:1:gpu"], T0) == [
        ("job_a", "sync", "queued")
    ]
    assert _row(backend, "job_a").status == "queued"
    assert _row(backend, "job_b").status == "running"


def test_st08_08_stale_owner_writes_change_nothing(backend: SqliteJobsBackend) -> None:
    """ST08-08 owner A's lease is reaped and B reclaims: A's completions, state save and
    heartbeat change 0 rows; B's result is kept (TH08-08)."""
    _add(backend, _new("job_a"))
    a, b = "host-a:1:cpu0", "host-b:2:cpu0"
    assert _claim(backend, a) is not None
    reaped_at = T0 + LEASE + timedelta(seconds=1)
    assert backend.reap_expired(reaped_at) == [("job_a", "sync", "queued")]
    reclaimed = _claim(backend, b, now=reaped_at)
    assert reclaimed is not None
    assert (reclaimed.lease_owner, reclaimed.attempts) == (b, 2)
    error: dict[str, JsonValue] = {"class": "Boom", "message": "m", "at": "t", "attempt": 1}
    assert not backend.save_job_state("job_a", a, b'{"from": "a"}')
    assert not backend.heartbeat_job("job_a", a, reaped_at + LEASE)
    assert not backend.finish_done("job_a", a, {"from": "a"}, reaped_at)
    assert not backend.finish_yield("job_a", a, reaped_at, reaped_at)
    assert not backend.finish_requeue("job_a", a, reaped_at, error)
    assert not backend.finish_failed("job_a", a, error, reaped_at)
    assert not backend.finalize_canceled("job_a", a, reaped_at)
    row = _row(backend, "job_a")
    assert (row.status, row.lease_owner, row.result, row.attempts) == ("running", b, None, 2)
    assert backend.finish_done("job_a", b, {"from": "b"}, reaped_at)
    assert not backend.finish_done("job_a", a, {"from": "a"}, reaped_at)
    assert _row(backend, "job_a").result == {"from": "b"}


def test_ut08_63_sqlite_jobs_backend_is_exported() -> None:
    """UT08-63 the package re-exports `SqliteJobsBackend` (block "08 jobs")."""
    assert ops.SqliteJobsBackend is SqliteJobsBackend
