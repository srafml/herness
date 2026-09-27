"""Tests for herness.core.jobs.scheduler.schedule_rekey and the rekey night of the scheduler
(impl 08 U08-72 to U08-74; design 08 §5.11 "Rekey"; TH08-02)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pytest
import structlog
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.core.jobs import _sched_env
from tests.unit.core.jobs._sched_env import (
    Clock,
    events,
    finish,
    jobs,
    local,
)

from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.jobs import scheduler
from herness.core.jobs.ports import require_jobs_backend

pytestmark = pytest.mark.unit
jobs_db, sched_db = _sched_env.jobs_db, _sched_env.sched_db  # fixtures

NEXT_KEY = "redact.hmac_key.next"
FRI, SAT, NEXT_SAT = (2026, 9, 25), (2026, 9, 26), (2026, 10, 3)


def _secret() -> str:
    return bytes(range(100, 132)).hex()  # built at run time: no hex literal for detect-secrets


def _key_id() -> str:
    return hashlib.sha256(bytes.fromhex(_secret())).hexdigest()[:8]


@pytest.fixture
def next_key(fake_keyring: MemoryKeyring) -> str:
    fake_keyring.set_password("herness", NEXT_KEY, _secret())
    return _secret()


@pytest.mark.usefixtures("next_key")
@pytest.mark.parametrize(
    ("now", "fire"),
    [
        (local(FRI, "10:00"), local(SAT, "19:00")),
        (local(SAT, "10:00"), local(NEXT_SAT, "19:00")),  # < 12 h notice: next Saturday
    ],
    ids=["friday", "saturday"],
)
def test_ut08_81_plans_the_next_saturday_with_notice(
    sched_db: Clock, now: datetime, fire: datetime
) -> None:
    """UT08-81 Friday 10:00 → Saturday 19:00 the same week; Saturday 10:00 → next Saturday
    19:00; idem `rekey:<8 hex>`, `maintenance` decider priority 70, `rekey_planned`."""
    del sched_db
    with structlog.testing.capture_logs() as logs:
        job_id = scheduler.schedule_rekey(now=now)
    (row,) = jobs()
    key_id = _key_id()
    assert row.job_id == job_id
    assert (row.kind, row.gpu_class, row.priority) == ("maintenance", "decider", 70)
    assert row.payload == {"action": "rekey", "key_id": key_id}
    assert (row.idem_key, row.scheduled_for) == (f"rekey:{key_id}", fire)
    (event,) = events("rekey_planned")
    assert event["job_id"] == job_id
    assert json.loads(event["detail"]) == {"fire_at": clock.format_utc(fire), "key_id": key_id}
    assert _secret() not in repr(logs)
    assert _secret() not in repr(events("rekey_planned"))


@pytest.mark.usefixtures("next_key")
def test_ut08_81_defaults_to_the_clock_and_is_idempotent(sched_db: Clock) -> None:
    """UT08-81 without `now` the fake clock is used; a second call returns the same job."""
    sched_db.set(local(FRI, "10:00"))
    first = scheduler.schedule_rekey()
    assert scheduler.schedule_rekey() == first
    assert [row.scheduled_for for row in jobs()] == [local(SAT, "19:00")]


def test_ut08_81_notice_boundary_counts_the_exact_minute(sched_db: Clock) -> None:
    """UT08-81 `earliest` exactly on a fire keeps that fire; one second later moves a week."""
    del sched_db
    assert scheduler._first_fire_at_or_after(local(SAT, "19:00")) == local(SAT, "19:00")
    later = local(SAT, "19:00").replace(second=1)
    assert scheduler._first_fire_at_or_after(later) == local(NEXT_SAT, "19:00")


@pytest.mark.usefixtures("sched_db")
def test_ut08_81_missing_secret_is_a_config_error() -> None:
    """UT08-81 no `redact.hmac_key.next` → ConfigError; nothing is enqueued."""
    with pytest.raises(ConfigError, match="secret not found"):
        scheduler.schedule_rekey(now=local(FRI, "10:00"))
    assert jobs() == []


@pytest.mark.usefixtures("sched_db")
@pytest.mark.parametrize("value", ["not-a-hex-key-value", "ab" * 31], ids=["text", "62-hex"])
def test_ut08_81_malformed_secret_never_shows_its_value(
    fake_keyring: MemoryKeyring, value: str
) -> None:
    """UT08-81 a secret that is not 64 hex chars → ConfigError without the value (TH08-02)."""
    fake_keyring.set_password("herness", NEXT_KEY, value)
    with pytest.raises(ConfigError) as caught:
        scheduler.schedule_rekey(now=local(FRI, "10:00"))
    assert value not in str(caught.value)
    assert "64 hex" in caught.value.message
    assert jobs() == []


@pytest.mark.usefixtures("next_key")
def test_ut08_80_rekey_night_marks_build_and_skips_standard_reviews(sched_db: Clock) -> None:
    """UT08-80 on a rekey night the `nightly` build payload has `rekey_night` and the
    standard reviews are `chain_skipped` with reason `rekey`; other nights are unmarked."""
    scheduler.schedule_rekey(now=local(FRI, "10:00"))
    scheduler.run_scheduler(sched_db.set(local(FRI, "19:01")))
    scheduler.run_scheduler(sched_db.set(local(SAT, "19:01")))
    friday, saturday = jobs("nightly")
    assert "rekey_night" not in friday.payload
    assert saturday.payload["rekey_night"] is True
    finish(saturday.job_id, "done", local(SAT, "23:00"))
    backend_row = require_jobs_backend().get_job(saturday.job_id)
    assert backend_row is not None
    assert scheduler.advance_chain(backend_row) is None
    reasons = [json.loads(row["detail"])["reason"] for row in events("chain_skipped")]
    assert reasons == ["rekey", "rekey", "disabled"]
