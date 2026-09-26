"""Unit tests for herness.store.ops.privacy (impl 10 U10-105, U10-111; UT10-77, UT10-83)."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from herness.core.errors import ConfigError, NotFound
from herness.store.ops import migrate
from herness.store.ops.privacy import (
    DeletionRequest,
    create_deletion_request,
    deleted_record_ids,
    get_deletion_request,
    open_deletion_request,
    record_deletion_step,
    set_deletion_status,
)

pytestmark = pytest.mark.unit

_NOW = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=datetime.UTC)
_LATER = datetime.datetime(2026, 9, 24, 12, 30, 0, tzinfo=datetime.UTC)
_USER = "a" * 32
_RECORD = "servicenow:incident:INC0001"
_REASON = "TICKET-123"


@pytest.fixture
def migrated(ops_store: Path) -> Path:
    migrate()
    return ops_store


def _create(record_id: str = _RECORD, *, now: datetime.datetime = _NOW) -> DeletionRequest:
    return create_deletion_request(
        record_id=record_id, requested_by=_USER, reason_ref=_REASON, now=now
    )


# --- UT10-77: create_deletion_request idempotency ------------------------------------------


def test_ut10_77_create_twice_for_one_record_id_returns_the_open_row(migrated: Path) -> None:
    """UT10-77 a second create for the same record_id returns the existing pending row."""
    first = _create()
    second = _create(now=_LATER)
    assert first == second
    assert get_deletion_request(first.request_id) == first


def test_ut10_77_create_after_done_opens_a_new_request(migrated: Path) -> None:
    """UT10-77 once the open request is done, a new create starts a fresh request."""
    first = _create()
    set_deletion_status(first.request_id, "running")
    set_deletion_status(first.request_id, "done", completed_at=_LATER)
    second = _create(now=_LATER)
    assert second.request_id != first.request_id
    assert second.status == "pending"


# --- UT10-77: preconditions (ConfigError naming the field only, no value echo) -------------


@pytest.mark.parametrize(
    ("field", "record_id", "requested_by", "reason_ref"),
    [
        ("record_id", "BAD..RECORD", _USER, _REASON),
        ("record_id", "Servicenow:incident:INC1", _USER, _REASON),
        ("requested_by", _RECORD, "not-hex-not-32-chars-long!!", _REASON),
        ("requested_by", _RECORD, "A" * 32, _REASON),
        ("reason_ref", _RECORD, _USER, "bad ref with spaces"),
        ("reason_ref", _RECORD, _USER, ""),
    ],
)
def test_ut10_77_create_preconditions_name_the_field_only(
    migrated: Path, field: str, record_id: str, requested_by: str, reason_ref: str
) -> None:
    """UT10-77 a bad record_id, requested_by or reason_ref raises ConfigError naming only the
    field; the offending value never appears in the message."""
    with pytest.raises(ConfigError, match=f"invalid {field}") as exc_info:
        create_deletion_request(
            record_id=record_id, requested_by=requested_by, reason_ref=reason_ref, now=_NOW
        )
    bad_value = {"record_id": record_id, "requested_by": requested_by, "reason_ref": reason_ref}[
        field
    ]
    if bad_value:
        assert bad_value not in str(exc_info.value)


def test_ut10_77_open_deletion_request_rejects_bad_record_id(migrated: Path) -> None:
    """UT10-77 open_deletion_request validates record_id the same way as create."""
    with pytest.raises(ConfigError, match="invalid record_id"):
        open_deletion_request("Not Valid")


# --- UT10-77: get_deletion_request NotFound -------------------------------------------------


def test_ut10_77_get_deletion_request_of_an_unknown_id_is_not_found(migrated: Path) -> None:
    """UT10-77 get_deletion_request of an unknown request_id raises NotFound."""
    with pytest.raises(NotFound):
        get_deletion_request("del_unknown")


def test_ut10_77_set_deletion_status_of_an_unknown_id_is_not_found(migrated: Path) -> None:
    """UT10-77 set_deletion_status of an unknown request_id raises NotFound."""
    with pytest.raises(NotFound):
        set_deletion_status("del_unknown", "running")


def test_ut10_77_record_deletion_step_of_an_unknown_id_is_not_found(migrated: Path) -> None:
    """UT10-77 record_deletion_step of an unknown request_id raises NotFound."""
    with pytest.raises(NotFound):
        record_deletion_step("del_unknown", "1", status="done", at=_NOW, counts={})


# --- UT10-77: status transitions ------------------------------------------------------------


def test_ut10_77_pending_running_done_transition_succeeds(migrated: Path) -> None:
    """UT10-77 pending -> running -> done is the normal lifecycle."""
    req = _create()
    set_deletion_status(req.request_id, "running")
    assert get_deletion_request(req.request_id).status == "running"
    set_deletion_status(req.request_id, "done", completed_at=_LATER)
    done = get_deletion_request(req.request_id)
    assert done.status == "done"
    assert done.completed_at == _LATER


def test_ut10_77_running_failed_transition_succeeds(migrated: Path) -> None:
    """UT10-77 running -> failed is a valid transition."""
    req = _create()
    set_deletion_status(req.request_id, "running")
    set_deletion_status(req.request_id, "failed")
    assert get_deletion_request(req.request_id).status == "failed"


@pytest.mark.parametrize(
    ("start", "target"),
    [
        ("pending", "done"),
        ("pending", "failed"),
        ("done", "running"),
        ("failed", "running"),
        ("running", "running"),
    ],
)
def test_ut10_77_any_other_transition_is_rejected(migrated: Path, start: str, target: str) -> None:
    """UT10-77 any transition outside pending->running or running->done|failed raises
    ConfigError("invalid deletion status transition")."""
    req = _create()
    if start != "pending":
        set_deletion_status(req.request_id, "running")
    if start in ("done", "failed"):
        set_deletion_status(req.request_id, start)  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="invalid deletion status transition"):
        set_deletion_status(req.request_id, target)  # type: ignore[arg-type]


# --- UT10-77: record_deletion_step and step replacement -------------------------------------


def test_ut10_77_record_deletion_step_holds_3_and_3b(migrated: Path) -> None:
    """UT10-77 recording step 3 then 3b leaves both keys in steps."""
    req = _create()
    record_deletion_step(req.request_id, "3", status="done", at=_NOW, counts={"lake": 5})
    record_deletion_step(req.request_id, "3b", status="done", at=_LATER, counts={"memory": 2})
    updated = get_deletion_request(req.request_id)
    assert set(updated.steps) == {"3", "3b"}
    assert updated.steps["3"]["counts"] == {"lake": 5}
    assert updated.steps["3b"]["counts"] == {"memory": 2}


def test_ut10_77_record_deletion_step_replaces_an_earlier_entry(migrated: Path) -> None:
    """UT10-77 a second record_deletion_step for the same step replaces, not appends."""
    req = _create()
    record_deletion_step(req.request_id, "1", status="failed", at=_NOW, counts={}, error="Boom")
    record_deletion_step(req.request_id, "1", status="done", at=_LATER, counts={"n": 1})
    updated = get_deletion_request(req.request_id)
    assert updated.steps == {
        "1": {"status": "done", "at": updated.steps["1"]["at"], "counts": {"n": 1}, "error": None}
    }


@pytest.mark.parametrize(
    ("step", "status"),
    [("0", "done"), ("8", "done"), ("1", "ok")],
)
def test_ut10_77_record_deletion_step_preconditions(migrated: Path, step: str, status: str) -> None:
    """UT10-77 an unknown step or step-status raises ConfigError naming only the field."""
    req = _create()
    with pytest.raises(ConfigError):
        record_deletion_step(req.request_id, step, status=status, at=_NOW, counts={})  # type: ignore[arg-type]


# --- UT10-77: open_deletion_request newest pending/running -----------------------------------


def test_ut10_77_open_deletion_request_returns_the_newest_open_row(migrated: Path) -> None:
    """UT10-77 open_deletion_request returns the newest pending/running request, and None once
    it is done."""
    assert open_deletion_request(_RECORD) is None
    req = _create()
    assert open_deletion_request(_RECORD) == req
    set_deletion_status(req.request_id, "running")
    assert open_deletion_request(_RECORD) == get_deletion_request(req.request_id)
    set_deletion_status(req.request_id, "done", completed_at=_LATER)
    assert open_deletion_request(_RECORD) is None


# --- UT10-83: deleted_record_ids -------------------------------------------------------------


def _seed_deleted_record_ids() -> None:
    statuses = ("pending", "running", "done", "failed")
    for i, status in enumerate(statuses):
        record_id = f"servicenow:incident:INC{i:04d}"
        req = create_deletion_request(
            record_id=record_id, requested_by=_USER, reason_ref=_REASON, now=_NOW
        )
        if status != "pending":
            set_deletion_status(req.request_id, "running")
        if status in ("done", "failed"):
            set_deletion_status(req.request_id, status)  # type: ignore[arg-type]
    other = create_deletion_request(
        record_id="jira:issue:JIRA-1", requested_by=_USER, reason_ref=_REASON, now=_NOW
    )
    set_deletion_status(other.request_id, "running")
    set_deletion_status(other.request_id, "done", completed_at=_LATER)


def test_ut10_83_deleted_record_ids_returns_running_and_done_sorted_unique(
    migrated: Path,
) -> None:
    """UT10-83 only running/done requests for the source:entity prefix are returned, sorted
    and unique."""
    _seed_deleted_record_ids()
    assert deleted_record_ids("servicenow", "incident") == [
        "servicenow:incident:INC0001",
        "servicenow:incident:INC0002",
    ]


def test_ut10_83_deleted_record_ids_deduplicates_repeated_requests(migrated: Path) -> None:
    """UT10-83 a record deleted twice (two done requests) appears once."""
    for now in (_NOW, _LATER):
        req = create_deletion_request(
            record_id=_RECORD, requested_by=_USER, reason_ref=_REASON, now=now
        )
        set_deletion_status(req.request_id, "running")
        set_deletion_status(req.request_id, "done", completed_at=now)
    assert deleted_record_ids("servicenow", "incident") == [_RECORD]


@pytest.mark.parametrize(
    ("field", "source", "entity"),
    [
        ("source", "Servicenow", "incident"),
        ("source", "monitoring:tool", "incident"),
        ("entity", "servicenow", "In cident"),
        ("entity", "servicenow", ""),
    ],
)
def test_ut10_83_deleted_record_ids_preconditions(
    migrated: Path, field: str, source: str, entity: str
) -> None:
    """UT10-83 an invalid source or entity raises ConfigError naming only that parameter."""
    with pytest.raises(ConfigError, match=f"invalid {field}"):
        deleted_record_ids(source, entity)
