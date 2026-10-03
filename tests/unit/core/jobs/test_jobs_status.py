"""Tests of U08-91 (status_snapshot), U08-92 (health) and U08-93 (enqueue_resume) (T08-22).

The snapshot reads the real migrated ops store through the bound SQLite backends at fixed
instants (a fixed `clock.now`); the chat-policy keys come from monkeypatched `chat_policy` and
`chat_next_live_at` (their own tests are UT08-90 to UT08-93), and the local GPU worker's
service health comes from a fake `gpu_state` reader. Nothing depends on the wall clock.
"""

from __future__ import annotations

import re
import socket
import sqlite3
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.unit.core.jobs import _queue_env

from herness.core import time as clock
from herness.core.errors import JobStateError
from herness.core.ids import IdKind, new_id, new_ulid
from herness.core.jobs import status
from herness.core.jobs.ports import JobRow, WorkerRow, require_jobs_backend
from herness.core.jobs.queue import claim, enqueue
from herness.core.jobs.scheduler import collect_schedules
from herness.core.jobs.status import (
    ComponentHealth,
    ResumeResult,
    StatusSnapshot,
    enqueue_resume,
    health,
    status_snapshot,
)
from herness.core.jobs.windows import window_at
from herness.core.resilience import ProcessState
from herness.core.resilience._state import require_ops_backend
from herness.core.resilience.ports import EventRow, HealthRow
from herness.store.ops.core import run_write

pytestmark = pytest.mark.unit
jobs_db = _queue_env.jobs_db  # fixture

SPEC = Path(__file__).resolve().parents[4] / "docs" / "impl" / "08-resilience-and-jobs.impl.md"
T0 = datetime(2026, 9, 1, tzinfo=UTC)  # the fixed clock start
CHAT_AT = datetime(2026, 9, 1, 18, tzinfo=UTC)
OWNER = "h:1:cpu0"
LOCAL = socket.gethostname().lower()


class FakeClock:
    """`herness.core.time.now` pinned to a fixed instant that moves only on `advance`.

    Only `clock.now` is replaced (no freezegun): the code under test reads time only there,
    and a frozen `datetime` during lazy third-party imports is avoided.
    """

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now


@pytest.fixture
def env(jobs_db: ProcessState, monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """`jobs_db` on a fixed clock with the chat-policy keys stubbed."""
    del jobs_db
    fake = FakeClock(T0)
    monkeypatch.setattr(clock, "now", fake.now)
    monkeypatch.setattr(status, "chat_policy", lambda now: "defer")
    monkeypatch.setattr(status, "chat_next_live_at", lambda now: CHAT_AT)
    return fake


# --- seeding helpers ----------------------------------------------------------------------------


def _worker(  # noqa: PLR0913 - keyword-only seeding knobs of one worker row
    worker_id: str,
    *,
    host: str = "otherhost",
    age_s: float = 1.0,
    gpu_slot: int = 1,
    loaded: str = "reasoning",
    status: str = "running",
    faults: bool = False,
    current_jobs: list[dict[str, Any]] | None = None,
) -> WorkerRow:
    beat = clock.now() - timedelta(seconds=age_s)
    row = WorkerRow(
        worker_id=worker_id, host=host, pid=4242, gpu_slot=gpu_slot, cpu_slots=2,
        gpu_class_loaded=loaded, status=status, current_jobs=current_jobs or [],  # type: ignore[arg-type]
        started_at=beat, heartbeat_at=beat, version="0.0.0", faults_enabled=faults,
    )  # fmt: skip
    require_jobs_backend().upsert_worker(row)
    return row


def _event(kind: str, at: datetime) -> None:
    row = EventRow(f"evt_{new_ulid()}", at, kind, "jobs", None, None, None, None, {})
    require_ops_backend().insert_event(row)


def _breaker(key: str, state: str, trips: int = 1) -> HealthRow:
    now = clock.now()
    opened = None if state == "closed" else now - timedelta(seconds=10)
    row = HealthRow(key, state, 0, trips, opened, None, now)  # type: ignore[arg-type]
    require_ops_backend().health_apply(key, lambda _before: row, now)
    return row


def _claimed(kind: Any = "sync", owner: str = OWNER, **payload: Any) -> JobRow:
    job_id = enqueue(kind, {"n": new_ulid(), **payload}, "none")
    row = claim(owner=owner, allowed_classes=["none"], job_id=job_id)
    assert row is not None
    return row


def _failed(finished_at: datetime) -> str:
    row = _claimed()
    error = {"class": "SchemaViolation", "message": "x", "at": "", "attempt": 1}
    assert require_jobs_backend().finish_failed(row.job_id, OWNER, error, finished_at)
    return row.job_id


def _spec_keys() -> list[str]:
    """The key column of the U08-91 table in docs/impl/08 §3.17."""
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("#### U08-91", 1)[1].split("#### U08-92", 1)[0]
    keys: list[str] = []
    for line in section.splitlines():
        match = re.match(r"\| (`[^|]+`) \|", line)
        if match and match.group(1) != "`Key`":
            keys += re.findall(r"`([a-z_0-9]+)`", match.group(1))
    return keys


# --- UT08-100 -----------------------------------------------------------------------------------


def test_ut08_100_snapshot_keys_equal_the_spec_table(env: FakeClock) -> None:
    """UT08-100 acceptance: the `status_snapshot` keys equal the §3.17 table, in order, and
    `StatusSnapshot` declares exactly those keys as required."""
    keys = _spec_keys()
    assert len(keys) == 15
    snap = status_snapshot()
    assert list(snap) == keys
    assert StatusSnapshot.__required_keys__ == frozenset(keys)
    assert StatusSnapshot.__optional_keys__ == frozenset()


def test_ut08_100_snapshot_counts_equal_the_seeded_rows(env: FakeClock) -> None:  # noqa: PLR0915 - one seeded store, every key checked
    """UT08-100 `ops_db` seeded with workers, jobs, events and breakers: every key present and
    every count equals the seeded rows."""
    now = env.now()
    running = _claimed()
    _worker("gpu-w", current_jobs=[{"job_id": running.job_id, "note": "step 3"}])
    _worker("cpu-w", gpu_slot=0, loaded="none", status="stopped", age_s=600)
    rekey_id = enqueue(
        "maintenance", {"action": "rekey"}, "none", scheduled_for=now + timedelta(hours=5)
    )
    enqueue("maintenance", {"action": "backup"}, "none", scheduled_for=now + timedelta(hours=1))
    high = enqueue("review", {"n": 1}, "reasoning", priority=90)
    _failed(now - timedelta(hours=1))
    _failed(now - timedelta(hours=30))
    for kind in ("retry", "retry", "job_done", "gpu_swap", "breaker_open"):
        _event(kind, now - timedelta(hours=2))
    _event("retry", now - timedelta(hours=25))  # outside the 24 h window
    _event("not_counted", now - timedelta(hours=2))
    _breaker("model:local-30b", "open", trips=2)
    _breaker("source:files", "half_open")
    _breaker("source:jira", "closed")

    env.advance(30)
    snap = status_snapshot()

    assert set(snap) == set(_spec_keys())
    by_id = {w["worker_id"]: w for w in snap["workers"]}
    assert set(by_id) == {"gpu-w", "cpu-w"}
    assert by_id["gpu-w"]["heartbeat_age_s"] == pytest.approx(31.0)
    assert by_id["gpu-w"]["services"] == {}  # not this host's worker: no health GET
    assert by_id["cpu-w"]["status"] == "stopped"
    assert snap["gpu"] == "available"
    active = window_at(env.now())
    assert snap["window"]["name"] == active.spec.name
    assert snap["window"]["end_at"] == snap["next_window"]["start_at"]
    assert (snap["chat_mode"], snap["chat_next_live_at"]) == ("defer", CHAT_AT)
    (run,) = snap["running"]
    assert (run["job_id"], run["kind"], run["attempt"]) == (running.job_id, "sync", 1)
    assert (run["max_attempts"], run["slot"]) == (running.max_attempts, "cpu0")
    assert run["heartbeat_note"] == "step 3"
    assert run["lease_left_s"] is not None
    assert 0 < run["lease_left_s"] <= 300
    assert snap["planned_rekey"] == {"job_id": rekey_id, "scheduled_for": now + timedelta(hours=5)}
    assert snap["queue"]["queued"] == 3
    assert snap["queue"]["next_job"] is not None
    assert snap["queue"]["next_job"]["job_id"] == high
    assert (snap["failed_24h"], snap["dead_letters"]) == (1, 2)
    breakers = {b["key"]: b for b in snap["breakers"]}
    assert set(breakers) == {"model:local-30b", "source:files"}
    assert breakers["model:local-30b"]["state"] == "open"
    assert breakers["model:local-30b"]["trips"] == 2
    assert breakers["model:local-30b"]["since"] == now - timedelta(seconds=10)
    assert breakers["model:local-30b"]["probe_due"] is not None
    assert breakers["model:local-30b"]["probe_due"] > now - timedelta(seconds=10)
    assert breakers["source:files"]["probe_due"] is None
    counters = snap["counters_24h"]
    assert list(counters) == list(status.COUNTER_KINDS)
    assert (counters["retry"], counters["job_done"], counters["gpu_swap"]) == (2, 1, 1)
    assert counters["breaker_open"] == 1
    assert sum(counters.values()) == 5
    names = [e.name for e in collect_schedules()]
    assert [s["name"] for s in snap["schedules"]] == names
    assert all(s["next_fire_at"] > env.now() for s in snap["schedules"])
    assert snap["faults_enabled"] is False


def test_ut08_100_empty_store_snapshot(env: FakeClock) -> None:
    """UT08-100 an empty store: no workers (`gpu` = `no_worker`), nothing running or queued,
    no rekey planned, every counter 0."""
    snap = status_snapshot(env.now())
    assert snap["workers"] == []
    assert snap["gpu"] == "no_worker"
    assert snap["running"] == []
    assert snap["planned_rekey"] is None
    assert snap["queue"] == {"queued": 0, "next_job": None}
    assert set(snap["counters_24h"].values()) == {0}
    assert snap["breakers"] == []


def test_ut08_100_gpu_unavailable_after_a_failed_swap(env: FakeClock) -> None:
    """UT08-100 `gpu`: `unavailable` when the GPU worker has class `none` and the latest
    `gpu_swap_failed` is newer than the latest `gpu_swap`; a later swap makes it available."""
    now = env.now()
    _worker("gpu-w", loaded="none")
    assert status_snapshot(now)["gpu"] == "available"  # no failed swap at all
    _event("gpu_swap_failed", now - timedelta(minutes=1))
    assert status_snapshot(now)["gpu"] == "unavailable"
    _event("gpu_swap", now - timedelta(minutes=5))
    assert status_snapshot(now)["gpu"] == "unavailable"
    _event("gpu_swap", now - timedelta(seconds=1))
    assert status_snapshot(now)["gpu"] == "available"
    _worker("gpu-w", loaded="none", age_s=3600)  # dead GPU worker
    assert status_snapshot(now)["gpu"] == "no_worker"


def test_ut08_100_local_gpu_worker_reports_service_health(
    env: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-100 `services` comes from the cached `gpu_state` reader only for this host's
    alive GPU worker, for the services of its loaded class."""
    asked: list[str] = []

    class Reader:
        def service_healthy(self, name: str) -> bool:
            asked.append(name)
            return False

    monkeypatch.setattr(status, "gpu_state", Reader)
    _worker("local-gpu", host=LOCAL, loaded="reasoning")
    _worker("local-cpu", host=LOCAL, gpu_slot=0, loaded="none")
    _worker("remote-gpu", host="otherhost", loaded="decider")
    by_id = {w["worker_id"]: w for w in status_snapshot(env.now())["workers"]}
    assert by_id["local-gpu"]["services"] == {"vllm-reasoning": False}
    assert by_id["local-cpu"]["services"] == {}
    assert by_id["remote-gpu"]["services"] == {}
    assert asked == ["vllm-reasoning"]


def test_ut08_100_faults_flag_from_workers_or_this_process(
    env: FakeClock, jobs_db: ProcessState
) -> None:
    """UT08-100 TH08-06: `faults_enabled` is true when an alive worker has the flag or this
    process loaded a fault plan; a dead worker's flag does not count."""
    _worker("dead", faults=True, age_s=3600)
    assert status_snapshot(env.now())["faults_enabled"] is False
    _worker("alive", faults=True)
    assert status_snapshot(env.now())["faults_enabled"] is True
    _worker("alive", faults=False)
    jobs_db.faults_enabled = True
    assert status_snapshot(env.now())["faults_enabled"] is True


def test_ut08_100_snapshot_uses_the_clock_by_default(
    env: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-100 `now` defaults to the (fake) clock: windows and schedules use it."""
    seen: list[datetime] = []
    monkeypatch.setattr(status, "chat_policy", lambda now: seen.append(now) or "defer")
    status_snapshot()
    assert seen == [env.now()]


# --- UT08-101 -----------------------------------------------------------------------------------


def test_ut08_101_health_down_degraded_ok(env: FakeClock) -> None:
    """UT08-101 no worker → `down`; worker + open breaker → `degraded`; clean → `ok`."""
    assert health() == ComponentHealth("down", "no worker heartbeat")
    _worker("w")
    _breaker("model:local-30b", "open")
    verdict = health()
    assert (verdict.status, verdict.reason) == ("degraded", "breaker open")
    _breaker("model:local-30b", "closed")
    assert health() == ComponentHealth("ok", "ok")


def test_ut08_101_health_lists_every_degraded_condition(
    env: FakeClock, jobs_db: ProcessState
) -> None:
    """UT08-101 dead letters in 24 h, faults enabled and an unavailable GPU each degrade the
    health; the reason lists them all. A half-open breaker and an old dead letter do not."""
    now = env.now()
    _worker("w", loaded="none")
    _breaker("source:files", "half_open")
    _failed(now - timedelta(hours=30))
    assert health(now).status == "ok"
    _failed(now - timedelta(hours=1))
    jobs_db.faults_enabled = True
    _event("gpu_swap_failed", now - timedelta(minutes=1))
    assert health(now) == ComponentHealth(
        "degraded", "dead letters in 24 h; faults enabled; gpu unavailable"
    )


def test_ut08_101_stale_or_stopped_worker_is_down(env: FakeClock) -> None:
    """UT08-101 the U08-54 rule: a heartbeat older than 3 x `heartbeat_s` or a `stopped`
    worker counts as no worker."""
    _worker("old", age_s=3 * 30 + 1)
    _worker("stopped", status="stopped")
    assert health(env.now()).status == "down"


def test_ut08_101_component_health_is_frozen() -> None:
    """UT08-101 `ComponentHealth` is a frozen dataclass."""
    verdict = ComponentHealth("ok", "ok")
    with pytest.raises(FrozenInstanceError):
        verdict.status = "down"  # type: ignore[misc]


# --- UT08-102 -----------------------------------------------------------------------------------


def _run(kind: str, run_status: str) -> str:
    run_id = new_id(IdKind.RUN)

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at)"
            " VALUES (?, ?, 'standard', 'default', 'h', ?, ?)",
            (run_id, kind, run_status, clock.format_utc(T0)),
        )

    run_write(insert, op="test_setup")
    return run_id


def test_ut08_102_resume_done_running_and_chat(env: FakeClock) -> None:
    """UT08-102 runs `done`, `running`, `chat`: `done` → `job_id None`; `running` → a review
    job with idem key `resume:<run_id>`, priority 80, class `reasoning`; `chat` →
    JobStateError."""
    done = _run("org_review", "done")
    assert enqueue_resume(done) == ResumeResult(None, False, "done")

    running = _run("org_review", "running")
    res = enqueue_resume(running)
    assert (res.created, res.run_status) == (True, "running")
    assert res.job_id is not None
    job = require_jobs_backend().get_job(res.job_id)
    assert job is not None
    assert (job.kind, job.gpu_class, job.priority, job.status) == (
        "review",
        "reasoning",
        80,
        "queued",
    )
    assert job.idem_key == f"resume:{running}"
    assert dict(job.payload) == {"run_id": running, "resume": True}
    again = enqueue_resume(running)
    assert again == ResumeResult(res.job_id, False, "running")  # deduped by the idem key

    chat = _run("chat", "running")
    with pytest.raises(JobStateError, match="chat runs resume through their chat job") as info:
        enqueue_resume(chat)
    assert info.value.run_id == chat


def test_ut08_102_force_and_retry_dead_flags(env: FakeClock) -> None:
    """UT08-102 `force` resumes a finished run; `force` and `retry_dead` land in the payload."""
    for run_status in ("done", "partial", "failed", "canceled"):
        run_id = _run("funding_review", run_status)
        assert enqueue_resume(run_id).job_id is None
        res = enqueue_resume(run_id, force=True, retry_dead=True)
        assert res.job_id is not None
        assert res.run_status == run_status
        job = require_jobs_backend().get_job(res.job_id)
        assert job is not None
        assert dict(job.payload) == {
            "run_id": run_id, "resume": True, "force": True, "retry_dead": True
        }  # fmt: skip
    planning = _run("eval", "planning")
    res = enqueue_resume(planning, retry_dead=True)
    job = require_jobs_backend().get_job(res.job_id or "")
    assert job is not None
    assert dict(job.payload) == {"run_id": planning, "resume": True, "retry_dead": True}


def test_ut08_102_unknown_run_raises(env: FakeClock) -> None:
    """UT08-102 an unknown run id → JobStateError("unknown run")."""
    missing = new_id(IdKind.RUN)
    with pytest.raises(JobStateError, match="unknown run") as info:
        enqueue_resume(missing)
    assert info.value.run_id == missing


def test_cv_t08_22_module_exports() -> None:
    """UT08-100 (cv) the §2 public names of `status`."""
    assert sorted(status.__all__) == sorted(
        ["StatusSnapshot", "status_snapshot", "ComponentHealth", "health", "ResumeResult",
         "enqueue_resume"]
    )  # fmt: skip
