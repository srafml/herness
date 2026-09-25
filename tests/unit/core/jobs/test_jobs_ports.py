"""Tests for herness.core.jobs.ports: row models and jobs backend binding (T08-03)."""

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from herness.core import jobs
from herness.core.errors import ConfigError
from herness.core.jobs import JobRow, NewJob, SchedCheck, WorkerRow, ports
from herness.core.resilience import ProcessState

pytestmark = pytest.mark.unit

_TS = "2026-09-25T10:11:12.123456Z"
_WHEN = datetime(2026, 9, 25, 10, 11, 12, 123456, tzinfo=UTC)


def _db_row(**overrides: object) -> dict[str, Any]:
    """A `job` row as sqlite3.Row mapping: JSON and timestamps as TEXT."""
    row: dict[str, Any] = {
        "job_id": "job_01J8Z6Q4V5W6X7Y8Z9A0B1C2D3",
        "kind": "sync",
        "gpu_class": "none",
        "status": "running",
        "priority": 60,
        "payload": json.dumps({"source": "jira", "entity": {"id": 7}}),
        "idem_key": "sync:jira",
        "result": json.dumps({"state": {"cursor": 3}}),
        "attempts": 1,
        "max_attempts": 5,
        "last_error": None,
        "lease_owner": "host1:4242:cpu0",
        "lease_expires_at": _TS,
        "scheduled_for": _TS,
        "created_at": _TS,
        "started_at": _TS,
        "finished_at": None,
    }
    row.update(overrides)
    return row


def test_ut08_63_valid_db_row_parses_to_jobrow() -> None:
    """UT08-63 a valid job row (TEXT JSON and ts columns) parses to JobRow."""
    row = JobRow.model_validate(_db_row())
    assert row.kind == "sync"
    assert row.status == "running"
    assert row.payload == {"source": "jira", "entity": {"id": 7}}
    assert row.result == {"state": {"cursor": 3}}
    assert row.last_error is None
    assert row.finished_at is None
    assert row.lease_expires_at == _WHEN
    assert row.created_at == _WHEN
    assert row.scheduled_for is not None
    assert row.scheduled_for.tzinfo is UTC


def test_ut08_63_jobrow_is_frozen_with_read_only_payload() -> None:
    """UT08-63 JobRow is frozen and its payload is a read-only mapping (R-42)."""
    row = JobRow.model_validate(_db_row())
    with pytest.raises(ValidationError):
        row.status = "done"  # type: ignore[misc]
    with pytest.raises(TypeError):
        row.payload["source"] = "other"  # type: ignore[index]
    assert JobRow.model_validate(row.model_dump()) == row
    assert JobRow.model_validate_json(row.model_dump_json()) == row


def test_ut08_63_jobrow_accepts_python_values() -> None:
    """UT08-63 already-decoded values parse; aware non-UTC datetimes normalise to UTC."""
    plus2 = timezone(timedelta(hours=2))
    row = JobRow.model_validate(
        _db_row(
            payload={"a": 1},
            result=None,
            last_error={"class": "StoreBusy", "attempt": 1},
            started_at=datetime(2026, 9, 25, 12, 11, 12, 123456, tzinfo=plus2),
        )
    )
    assert row.payload == {"a": 1}
    assert row.last_error == {"class": "StoreBusy", "attempt": 1}
    assert row.started_at == _WHEN
    assert row.started_at.tzinfo is UTC


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "bogus"},
        {"status": "bogus"},
        {"gpu_class": "huge"},
        {"payload": "{not json"},
        {"payload": json.dumps([1, 2])},
        {"created_at": "2026-09-25 10:11:12"},
        {"created_at": 1_758_795_072},
        {"created_at": datetime(2026, 9, 25)},  # noqa: DTZ001
        {"extra_column": 1},
    ],
)
def test_ut08_63_invalid_rows_rejected(overrides: dict[str, object]) -> None:
    """UT08-63 invalid literals, JSON, ts text, naive datetimes and extra columns fail."""
    with pytest.raises(ValidationError):
        JobRow.model_validate(_db_row(**overrides))


def test_ut08_63_worker_row_parses_json_and_flags() -> None:
    """UT08-63 a worker row parses current_jobs JSON, ts text and the 0/1 faults flag."""
    current = [{"job_id": "job_x", "slot": "gpu", "kind": "sync", "note": None}]
    row = WorkerRow.model_validate(
        {
            "worker_id": "host1:4242",
            "host": "host1",
            "pid": 4242,
            "gpu_slot": 1,
            "cpu_slots": 2,
            "gpu_class_loaded": "swapping",
            "requested_class": None,
            "status": "running",
            "current_jobs": json.dumps(current),
            "started_at": _TS,
            "heartbeat_at": _TS,
            "version": "0.1.0",
            "faults_enabled": 1,
        }
    )
    assert row.current_jobs == current
    assert row.faults_enabled is True
    assert row.heartbeat_at == _WHEN
    with pytest.raises(ValidationError):
        WorkerRow.model_validate({**row.model_dump(), "gpu_slot": 2})


def test_ut08_63_new_job_is_strict_and_bounded() -> None:
    """UT08-63 NewJob holds the insert columns; bounds and aware datetimes are enforced."""
    base: dict[str, Any] = {
        "job_id": "job_01J8Z6Q4V5W6X7Y8Z9A0B1C2D3",
        "kind": "review",
        "gpu_class": "reasoning",
        "priority": 40,
        "payload": b'{"a":1}',
        "idem_key": "review:0123456789abcdef",
        "max_attempts": 3,
        "scheduled_for": _WHEN,
        "created_at": _WHEN,
    }
    job = NewJob(**base)
    assert job.status == "queued"
    assert job.attempts == 0
    for bad in ({"priority": 101}, {"max_attempts": 0}, {"payload": '{"a":1}'}):
        with pytest.raises(ValidationError):
            NewJob(**{**base, **bad})
    with pytest.raises(ValidationError):
        NewJob(**{**base, "created_at": datetime(2026, 1, 1)})  # noqa: DTZ001


def test_ut08_63_sched_check_fields() -> None:
    """UT08-63 SchedCheck is the (schedule, fire_at) pair of the scheduler dedupe."""
    check = SchedCheck("nightly_sync", _TS)
    assert (check.schedule, check.fire_at) == ("nightly_sync", _TS)


def test_ut08_63_jobs_backend_binding(reset_process_state: ProcessState) -> None:
    """UT08-63 bind_jobs_backend sets the port; unbound use and None raise ConfigError."""
    with pytest.raises(ConfigError) as err:
        ports.require_jobs_backend()
    assert str(err.value) == (
        "jobs backend not bound; call herness.store.ops.resilience.bind_core_backends()"
    )
    with pytest.raises(ConfigError):
        jobs.bind_jobs_backend(None)  # type: ignore[arg-type]
    backend, other = object(), object()
    jobs.bind_jobs_backend(backend)  # type: ignore[arg-type]
    jobs.bind_jobs_backend(other)  # type: ignore[arg-type]
    assert ports.require_jobs_backend() is other
    assert reset_process_state.jobs is other


def test_ut08_63_lazy_exports_resolve() -> None:
    """UT08-63 herness.core.jobs re-exports its names lazily; unknown names raise."""
    assert jobs.__all__ == tuple(sorted(jobs._EXPORTS))
    for name in jobs.__all__:
        assert getattr(jobs, name) is getattr(ports, name)
    with pytest.raises(AttributeError):
        _ = jobs.not_a_name
