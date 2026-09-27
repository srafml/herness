"""Tests for herness.core.jobs.scheduler: schedule collection, catch-up firing and chains
(impl 08 U08-71 to U08-73; design 08 §5.11)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
import structlog
from tests.unit.core.jobs import _sched_env
from tests.unit.core.jobs._sched_env import (
    TZ,
    Clock,
    events,
    finish,
    jobs,
    local,
    reload_config,
)

from herness.core import config as c
from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.jobs import scheduler
from herness.core.jobs.cron import CronExpr
from herness.core.jobs.ports import JobRow, require_jobs_backend
from herness.core.resilience.settings import ChainStep
from herness.core.types import JobSpec

pytestmark = pytest.mark.unit
jobs_db, sched_db = _sched_env.jobs_db, _sched_env.sched_db  # fixtures

TUE, WED, SUN = (2026, 9, 22), (2026, 9, 23), (2026, 9, 20)
CONFIG_JOBS = ["nightly", "weekly_deep", "memory_maintenance", "outcomes"]


def _details(kind: str) -> list[dict[str, Any]]:
    return [json.loads(row["detail"]) for row in events(kind)]


# --- UT08-82: collect_schedules -----------------------------------------------------------


@pytest.mark.usefixtures("sched_db")
def test_ut08_82_collects_every_schedule_source() -> None:
    """UT08-82 jira sync and reconcile, maintenance from `backup.nightly_at` 01:30 (then purge)
    and every configured job; the disabled servicenow source adds nothing."""
    entries = {entry.name: entry for entry in scheduler.collect_schedules()}
    assert list(entries) == ["sync.jira", "reconcile.jira", "maintenance", *CONFIG_JOBS]
    sync, reconcile, maint = entries["sync.jira"], entries["reconcile.jira"], entries["maintenance"]
    assert (sync.cron.text, sync.catch_up_max, sync.idem_mode) == (
        "*/30 * * * *",
        timedelta(hours=1),
        "sync",
    )
    assert sync.job == ChainStep(kind="sync", gpu_class="none", payload={"source": "jira"})
    assert (reconcile.cron.text, reconcile.catch_up_max, reconcile.idem_mode) == (
        "0 3 * * SUN",
        timedelta(hours=12),
        "sched",
    )
    assert reconcile.job == ChainStep(
        kind="reconcile", gpu_class="none", payload={"source": "jira"}
    )
    assert (maint.cron.text, maint.catch_up_max) == ("30 1 * * *", timedelta(hours=12))
    assert maint.job.payload == {"action": "backup"}
    assert [step.payload for step in maint.then] == [{"action": "purge"}]
    nightly = entries["nightly"]
    assert (nightly.cron.text, nightly.catch_up_max, nightly.job.kind) == (
        "0 19 * * *",
        timedelta(hours=6),
        "build_pipeline",
    )
    assert [step.kind for step in nightly.then] == ["review", "review", "eval"]
    assert entries["outcomes"].catch_up_max == timedelta(days=2)
    assert entries["memory_maintenance"].catch_up_max == timedelta(hours=24)


def test_ut08_82_source_without_schedule_and_minute_catch_up(
    sched_db: Clock, tmp_path: Any
) -> None:
    """UT08-82 an enabled source without `schedule` gets only its reconcile entry; a `<n>m`
    catch-up and `backup.nightly_at` 23:05 give `5 23 * * *`."""
    del sched_db
    sources = "version: 1\nsources:\n  jira:\n    enabled: true\n    flavor: cloud\n" + (
        "    base_url: https://acme.atlassian.net\n"
        '    auth: {method: api_token, credentials: "secret:jira"}\n'
    )
    config_dir = tmp_path / "config"
    text = (config_dir / "resilience.yaml").read_text(encoding="utf-8")
    herness = (config_dir / "herness.yaml").read_text(encoding="utf-8")
    reload_config(
        config_dir,
        sources,
        resilience=text.replace(
            "maintenance: {catch_up_max: 12h}", "maintenance: {catch_up_max: 90m}"
        ),
        herness=herness + "backup: {nightly_at: '23:05'}\n",
    )
    entries = {entry.name: entry for entry in scheduler.collect_schedules()}
    assert "sync.jira" not in entries
    assert "reconcile.jira" in entries
    assert entries["maintenance"].cron.text == "5 23 * * *"
    assert entries["maintenance"].catch_up_max == timedelta(minutes=90)


@pytest.mark.usefixtures("sched_db")
def test_ut08_82_unparsable_cron_is_skipped_with_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-82 an entry whose cron does not parse is skipped with ERROR `jobs.schedule.error`;
    the other entries are still collected."""
    cfg = c.get_config()
    schedule = cfg.resilience.schedule
    broken = schedule.jobs[1].model_copy(update={"cron": "99 * * * *"})
    new_jobs = [schedule.jobs[0], broken, *schedule.jobs[2:]]
    patched = cfg.model_copy(
        update={
            "resilience": cfg.resilience.model_copy(
                update={"schedule": schedule.model_copy(update={"jobs": new_jobs})}
            )
        }
    )
    monkeypatch.setattr(scheduler, "get_config", lambda: patched)
    with structlog.testing.capture_logs() as logs:
        names = [entry.name for entry in scheduler.collect_schedules()]
    assert "weekly_deep" not in names
    assert "nightly" in names
    errors = [line for line in logs if line["event"] == "jobs.schedule.error"]
    assert [(e["log_level"], e["schedule"], e["error_type"]) for e in errors] == [
        ("error", "weekly_deep", "ConfigError")
    ]


@pytest.mark.usefixtures("sched_db")
def test_ut08_82_bad_catch_up_text_is_refused() -> None:
    """UT08-82 a catch-up text that settings validation would refuse raises ConfigError."""
    with pytest.raises(ConfigError, match="catch_up_max"):
        scheduler._duration("6 hours")


# --- UT08-79: catch-up firing -------------------------------------------------------------


def test_ut08_79_worker_down_18_to_20_enqueues_19_00_once(sched_db: Clock) -> None:
    """UT08-79 worker down 18:00-20:00: `nightly` is enqueued once with `fire_at` 19:00 and
    priority 60; later ticks and a restart (empty missed set) never enqueue it again."""
    now = sched_db.set(local(TUE, "20:00"))
    first = scheduler.run_scheduler(now)
    nightly = jobs("nightly")
    assert len(nightly) == 1
    fire_at = clock.format_utc(local(TUE, "19:00"))
    assert nightly[0].payload == {
        "stages": ["build", "enrich", "score", "dq", "promote"],
        "schedule": "nightly",
        "fire_at": fire_at,
    }
    assert (nightly[0].priority, nightly[0].kind, nightly[0].idem_key) == (
        60,
        "build_pipeline",
        f"sched:nightly:{fire_at}",
    )
    assert first.fired >= 1
    assert first.errors == 0
    fired = [d for d in _details("schedule_fired") if d["schedule"] == "nightly"]
    assert fired == [{"schedule": "nightly", "fire_at": fire_at}]
    finish(nightly[0].job_id, "done", now)  # a finished job still blocks re-creation
    scheduler._missed_reported.clear()
    again = scheduler.run_scheduler(sched_db.set(local(TUE, "20:30")))
    assert [row.kind for row in jobs("nightly")] == ["build_pipeline", "review"]  # + step 1
    assert require_jobs_backend().sched_fired("nightly", fire_at)
    scheduler.run_scheduler(sched_db.set(local(WED, "02:00")))  # fired: not missed
    assert again.errors == 0
    assert [d for d in _details("schedule_missed") if d["schedule"] == "nightly"] == []


def test_ut08_79_worker_down_18_to_02_reports_missed_once(sched_db: Clock) -> None:
    """UT08-79 worker down 18:00-02:00 (7 h > 6 h): no `nightly` job, one `schedule_missed`
    across repeated ticks."""
    now = sched_db.set(local(WED, "02:00"))
    first = scheduler.run_scheduler(now)
    second = scheduler.run_scheduler(sched_db.set(local(WED, "02:00") + timedelta(seconds=30)))
    assert jobs("nightly") == []
    missed = [d for d in _details("schedule_missed") if d["schedule"] == "nightly"]
    assert missed == [{"schedule": "nightly", "fire_at": clock.format_utc(local(TUE, "19:00"))}]
    assert first.missed >= 1
    assert second.missed == 0


def test_ut08_79_missed_set_is_bounded(sched_db: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-79 the missed-report set keeps at most its bound, evicting the oldest."""
    del sched_db
    monkeypatch.setattr(scheduler, "_MISSED_MAX", 2)
    for n in range(3):
        assert scheduler._note_missed(("s", str(n)))
    assert not scheduler._note_missed(("s", "2"))
    assert list(scheduler._missed_reported) == [("s", "1"), ("s", "2")]


# --- UT08-54: sync dedupe -----------------------------------------------------------------


def test_ut08_54_running_sync_blocks_a_second_job(sched_db: Clock) -> None:
    """UT08-54 while `sync` for jira runs, firing `sync.jira` makes no second job;
    `sched_fired` finds the fire through the payload."""
    scheduler.run_scheduler(sched_db.set(local(TUE, "10:00")))
    (sync,) = jobs("sync.jira")
    first_fire = clock.format_utc(local(TUE, "10:00"))
    assert (sync.idem_key, sync.payload["fire_at"], sync.priority) == ("sync:jira", first_fire, 60)
    finish(sync.job_id, "running", local(TUE, "10:00"))
    report = scheduler.run_scheduler(sched_db.set(local(TUE, "10:31")))
    assert [row.job_id for row in jobs("sync.jira")] == [sync.job_id]
    backend = require_jobs_backend()
    assert backend.sched_fired("sync.jira", first_fire)
    assert not backend.sched_fired("sync.jira", clock.format_utc(local(TUE, "10:30")))
    assert report.fired == 0
    finish(sync.job_id, "done", local(TUE, "10:40"))
    scheduler.run_scheduler(sched_db.set(local(TUE, "10:45")))
    assert [row.payload["fire_at"] for row in jobs("sync.jira")] == [
        first_fire,
        clock.format_utc(local(TUE, "10:30")),
    ]


# --- UT08-83: error boundary --------------------------------------------------------------


def test_ut08_83_failing_submit_is_logged_and_others_fire(
    sched_db: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-83 an entry whose submit raises SchemaViolation logs ERROR `jobs.schedule.error`
    with `schedule` and `error_type`; the other entries fire."""
    real = scheduler.submit

    def failing(spec: JobSpec, **kwargs: Any) -> tuple[str, bool]:
        if spec.kind == "build_pipeline":
            msg = "job payload rejected: test"
            raise SchemaViolation(msg)
        return real(spec, **kwargs)

    monkeypatch.setattr(scheduler, "submit", failing)
    with structlog.testing.capture_logs() as logs:
        report = scheduler.run_scheduler(sched_db.set(local(TUE, "20:00")))
    errors = [line for line in logs if line["event"] == "jobs.schedule.error"]
    assert [(e["log_level"], e["schedule"], e["error_type"]) for e in errors] == [
        ("error", "nightly", "SchemaViolation")
    ]
    assert report.errors == 1
    assert jobs("nightly") == []
    assert len(jobs("memory_maintenance")) == 1
    assert len(jobs("sync.jira")) == 1


# --- UT08-80: chains ----------------------------------------------------------------------


def _fire_nightly(clock_: Clock, day: tuple[int, int, int]) -> JobRow:
    scheduler.run_scheduler(clock_.set(local(day, "19:05")))
    (build,) = jobs("nightly")
    return build


def _reload(row: JobRow) -> JobRow:
    return require_jobs_backend().get_job(row.job_id) or row


def test_ut08_80_sunday_skips_reviews_and_disabled_eval(sched_db: Clock) -> None:
    """UT08-80 Sunday: both review steps `chain_skipped` (`skip_on`), eval `disabled`; chain
    repair writes no duplicate event and enqueues nothing."""
    build = _fire_nightly(sched_db, SUN)
    finish(build.job_id, "done", local(SUN, "20:00"))
    assert scheduler.advance_chain(_reload(build)) is None
    fire_at = build.payload["fire_at"]
    expected = [
        {"schedule": "nightly", "fire_at": fire_at, "step": 1, "reason": "skip_on"},
        {"schedule": "nightly", "fire_at": fire_at, "step": 2, "reason": "skip_on"},
        {"schedule": "nightly", "fire_at": fire_at, "step": 3, "reason": "disabled"},
    ]
    assert _details("chain_skipped") == expected
    assert [row["target"] for row in events("chain_skipped")] == [
        f"nightly:{fire_at}:{k}" for k in (1, 2, 3)
    ]
    report = scheduler.run_scheduler(sched_db.set(local(SUN, "21:00")))
    assert report.chains_advanced == 0
    assert _details("chain_skipped") == expected
    assert len(jobs("nightly")) == 1


def test_ut08_80_tuesday_steps_follow_done_and_stop_on_failure(sched_db: Clock) -> None:
    """UT08-80 Tuesday: step 1 is enqueued after the build is done, step 2 after step 1;
    step 2 `failed` → one `chain_broken`; repair never re-creates a finished step."""
    build = _fire_nightly(sched_db, TUE)
    fire_at = build.payload["fire_at"]
    assert scheduler.advance_chain(build) is None  # still queued
    finish(build.job_id, "done", local(TUE, "20:00"))
    report = scheduler.run_scheduler(sched_db.set(local(TUE, "20:01")))
    assert report.chains_advanced == 1
    step1 = jobs("nightly")[1]
    assert step1.payload == {
        "pipeline": "funding_review",
        "depth": "standard",
        "schedule": "nightly",
        "fire_at": fire_at,
        "step": 1,
    }
    assert (step1.kind, step1.gpu_class, step1.priority, step1.idem_key) == (
        "review",
        "reasoning",
        40,
        f"sched:nightly:{fire_at}:1",
    )
    assert scheduler.advance_chain(_reload(build)) == step1.job_id  # repair: same job
    finish(step1.job_id, "done", local(TUE, "22:00"))
    step2_id = scheduler.advance_chain(_reload(step1))
    assert step2_id is not None
    assert scheduler.advance_chain(_reload(build)) == step1.job_id  # finished step: no new job
    finish(step2_id, "failed", local(TUE, "23:00"))
    step2 = require_jobs_backend().get_job(step2_id)
    assert step2 is not None
    assert step2.payload["step"] == 2
    assert scheduler.advance_chain(step2) is None
    again = scheduler.run_scheduler(sched_db.set(local(TUE, "23:30")))
    assert again.chains_advanced == 0
    assert _details("chain_broken") == [
        {"schedule": "nightly", "fire_at": fire_at, "step": 2, "reason": "failed"}
    ]
    assert [row["target"] for row in events("chain_broken")] == [f"nightly:{fire_at}"]
    assert len(jobs("nightly")) == 3


@pytest.mark.usefixtures("sched_db")
@pytest.mark.parametrize(
    "payload",
    [
        {"schedule": "weekly_deep", "fire_at": "2026-09-20T01:00:00.000000Z"},  # no `then`
        {"schedule": "gone", "fire_at": "2026-09-20T01:00:00.000000Z"},
        {"schedule": 3, "fire_at": "2026-09-20T01:00:00.000000Z"},
        {"schedule": "nightly", "fire_at": 5},
        {"schedule": "nightly", "fire_at": "2026-09-20T01:00:00.000000Z", "step": "1"},
    ],
    ids=["no-then", "unknown", "bad-schedule", "bad-fire-at", "bad-step"],
)
def test_ut08_80_rows_outside_a_chain_advance_nothing(payload: dict[str, Any]) -> None:
    """UT08-80 a row of a schedule without `then`, unknown or malformed advances nothing."""
    row = JobRow(
        job_id="job_01J0000000000000000000000A",
        kind="review",
        gpu_class="none",
        status="done",
        priority=40,
        attempts=1,
        max_attempts=3,
        payload=payload,
    )
    assert scheduler.advance_chain(row) is None


def test_ut08_80_chain_repair_error_is_counted(
    sched_db: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-80 a chain step whose submit fails is logged as `jobs.schedule.error` and counted;
    the tick goes on."""
    build = _fire_nightly(sched_db, TUE)
    finish(build.job_id, "done", local(TUE, "20:00"))
    real = scheduler.submit

    def failing(spec: JobSpec, **kwargs: Any) -> tuple[str, bool]:
        if spec.kind == "review":
            msg = "job payload rejected: test"
            raise SchemaViolation(msg)
        return real(spec, **kwargs)

    monkeypatch.setattr(scheduler, "submit", failing)
    with structlog.testing.capture_logs() as logs:
        report = scheduler.run_scheduler(sched_db.set(local(TUE, "20:01")))
    assert (report.errors, report.chains_advanced) == (1, 0)
    errors = [line for line in logs if line["event"] == "jobs.schedule.error"]
    assert [(e["schedule"], e["error_type"]) for e in errors] == [("nightly", "SchemaViolation")]


@pytest.mark.usefixtures("sched_db")
def test_ut08_79_cron_without_a_fire_in_a_year_does_nothing() -> None:
    """UT08-79 a cron with no fire in the last 366 days (30 February) neither fires nor
    reports a miss."""
    entry = scheduler.ScheduleEntry(
        name="never",
        cron=CronExpr.parse("0 0 30 2 *"),
        catch_up_max=timedelta(hours=1),
        job=ChainStep(kind="eval", gpu_class="none"),
        then=(),
        idem_mode="sched",
    )
    assert scheduler._fire(entry, local(TUE, "20:00"), TZ) is None
    assert jobs() == []
