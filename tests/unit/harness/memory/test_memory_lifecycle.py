"""Tests for herness.harness.memory.lifecycle.MemoryLifecycle (impl 07 U07-51 … U07-56, T07-09)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._lifecycle_env import (
    REVIEWER,
    make_lifecycle,
    review_of,
    row_of,
    seed_item,
)
from tests.unit.harness.memory._write_env import NOW, PLANTED_EMAIL, review_rows

from herness.core import time as clock
from herness.core.errors import PolicyViolation, ToolInputError
from herness.core.ids import new_ulid
from herness.harness.memory import lifecycle as lifecycle_mod
from herness.harness.memory.policy import APPROVAL_FLOOR
from herness.harness.memory.types import MemoryNotFound
from herness.store.ops import memory as ops
from herness.store.ops import shared

pytestmark = pytest.mark.unit

_MISSING = "mem_" + new_ulid()


def _events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == name]


# ---------------------------------------------------------------- UT07-32 approve


def test_ut07_32_approve_without_confidence_applies_the_floor(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-32 approve without confidence: active, confidence raised to the 0.8 floor."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item(confidence=0.5)
    review_id = row_of(memory_id)["data"]["review_item_id"]
    with capture_logs() as logs:
        item = env.lifecycle.approve(memory_id, REVIEWER, now=NOW)
    assert APPROVAL_FLOOR == 0.8
    assert (item.status, item.confidence) == ("active", 0.8)
    assert row_of(memory_id)["status"] == "active"
    data = row_of(memory_id)["data"]
    assert (data["approved_by"], data["approved_at"]) == (REVIEWER, clock.format_utc(NOW))
    assert data["approval_note"] is None
    review = review_of(review_id)
    assert (review.status, review.decided_by) == ("approved", REVIEWER)
    assert [(e, a) for e, a, _ in env.audits] == [("review_decision", REVIEWER)]
    assert env.vectors.calls == [([memory_id], "active")]
    (event,) = _events(logs, "memory.item.approved")
    assert event["memory_id"] == memory_id
    assert event["review_item_id"] == review_id
    assert event["derived_review_item_id"] is None
    assert event["kind"] == "glossary"


def test_ut07_32_approve_with_confidence_and_redacted_note(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-32 an explicit confidence is kept (even below the floor); the note is redacted."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item(confidence=0.5)
    note = f"ok, confirmed by {PLANTED_EMAIL}"
    item = env.lifecycle.approve(memory_id, REVIEWER, note, 0.6, now=NOW)
    assert item.confidence == 0.6
    stored = row_of(memory_id)["data"]["approval_note"]
    assert PLANTED_EMAIL not in stored
    review = review_of(row_of(memory_id)["data"]["review_item_id"])
    assert review.note == stored


def test_ut07_32_higher_confidence_is_kept(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-32 an item above the floor keeps its confidence; the default clock stamps it."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item(confidence=0.9)
    assert env.lifecycle.approve(memory_id, REVIEWER).confidence == 0.9


def test_ut07_32_missing_item_raises_memory_not_found(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-32 an unknown id: MemoryNotFound("memory_item", id)."""
    env = make_lifecycle(tmp_path, monkeypatch)
    with pytest.raises(MemoryNotFound) as info:
        env.lifecycle.approve(_MISSING, REVIEWER, now=NOW)
    assert (info.value.kind, info.value.ident) == ("memory_item", _MISSING)


def test_ut07_32_repeat_approve_is_idempotent(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-32 approving an approved item returns it unchanged without a second decision."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item()
    first = env.lifecycle.approve(memory_id, REVIEWER, now=NOW)
    again = env.lifecycle.approve(memory_id, "c" * 32, now=NOW + timedelta(hours=1))
    assert again == first
    assert len(env.audits) == 1
    assert row_of(memory_id)["data"]["approved_by"] == REVIEWER


@pytest.mark.parametrize("status", ["active", "rejected", "expired", "candidate"])
def test_ut07_32_not_pending_raises_policy_violation(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """UT07-32 a non-pending item (active without approved_by included) is refused."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item(status=status)
    with pytest.raises(PolicyViolation, match=r"approve\.not_pending") as info:
        env.lifecycle.approve(memory_id, REVIEWER, now=NOW)
    assert info.value.details["rule"] == "approve.not_pending"
    assert row_of(memory_id)["status"] == status


def test_ut07_32_status_is_rechecked_inside_the_transaction(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-32 a rejection racing the approval is seen inside the run_write: nothing changes."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item()
    real = ops.get_memory_items
    calls: list[int] = []

    def racing(ids: list[str], *, conn: Any = None) -> list[ops.MemoryItemRow]:
        rows = real(ids, conn=conn)
        if not calls:  # the pre-check load: a concurrent reject commits right after it
            ops.update_memory_item(memory_id, status="rejected")
        calls.append(1)
        return rows

    monkeypatch.setattr(lifecycle_mod.ops, "get_memory_items", racing)
    with pytest.raises(PolicyViolation, match=r"approve\.not_pending"):
        env.lifecycle.approve(memory_id, REVIEWER, now=NOW)
    assert review_of(row_of(memory_id)["data"]["review_item_id"]).status == "pending"
    assert env.audits == []


def test_ut07_32_item_deleted_before_the_transaction(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-32 an item that vanishes before the run_write is MemoryNotFound."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item()
    real = ops.get_memory_items
    calls: list[int] = []

    def vanishing(ids: list[str], *, conn: Any = None) -> list[ops.MemoryItemRow]:
        calls.append(1)
        return real(ids, conn=conn) if len(calls) == 1 else []

    monkeypatch.setattr(lifecycle_mod.ops, "get_memory_items", vanishing)
    with pytest.raises(MemoryNotFound):
        env.lifecycle.approve(memory_id, REVIEWER, now=NOW)


def test_ut07_32_already_decided_review_item_is_left_alone(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-32 a linked review item no longer pending is not decided again; approve succeeds."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item()
    review_id = row_of(memory_id)["data"]["review_item_id"]
    shared.decide_review_item(review_id, "rejected", decided_by="system", note="x", now=NOW)
    env.audits.clear()
    assert env.lifecycle.approve(memory_id, REVIEWER, now=NOW).status == "active"
    assert review_of(review_id).decided_by == "system"
    assert env.audits == []


def test_ut07_32_unknown_review_item_is_skipped(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-32 a pending item without (or with a dangling) review item still activates."""
    env = make_lifecycle(tmp_path, monkeypatch)
    plain = seed_item(review=False)
    dangling = seed_item(review=False, data={"review_item_id": "rev_" + new_ulid()})
    assert env.lifecycle.approve(plain, REVIEWER, now=NOW).status == "active"
    assert env.lifecycle.approve(dangling, REVIEWER, now=NOW).status == "active"
    assert env.audits == []


def test_ut07_32_vector_sync_failure_is_logged_not_raised(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-32 LanceDB down after commit: memory.vector.sync_failed WARNING, item active."""
    env = make_lifecycle(tmp_path, monkeypatch)
    env.vectors.fail = True
    memory_id = seed_item()
    with capture_logs() as logs:
        item = env.lifecycle.approve(memory_id, REVIEWER, now=NOW)
    assert item.status == "active"
    (event,) = _events(logs, "memory.vector.sync_failed")
    assert (event["log_level"], event["op"], event["count"]) == ("warning", "approve", 1)


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        (("mem_bad", REVIEWER), {}),
        ((_MISSING, "B" * 32), {}),
        ((_MISSING, "system"), {}),
        ((_MISSING, REVIEWER), {"confidence": 1.5}),
        ((_MISSING, REVIEWER), {"confidence": -0.1}),
        ((_MISSING, REVIEWER), {"confidence": float("nan")}),
        ((_MISSING, REVIEWER), {"confidence": True}),
        ((_MISSING, REVIEWER), {"note": "x" * 501}),
        ((_MISSING, REVIEWER), {"note": 5}),
    ],
)
def test_ut07_32_invalid_input_raises_tool_input_error(
    ops_store: OpsStoreHandle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    """UT07-32 bad memory_id, user_ref, confidence or note: ToolInputError before any load."""
    env = make_lifecycle(tmp_path, monkeypatch)
    with pytest.raises(ToolInputError):
        env.lifecycle.approve(*args, now=NOW, **kwargs)


# ---------------------------------------------------------------- UT07-33 derived + conflicts


def test_ut07_33_correction_with_weight_change_derives_review_and_expires_conflicts(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-33 derived weight_change review item; only the active listed conflict expires."""
    env = make_lifecycle(tmp_path, monkeypatch)
    old_active = seed_item(status="active")
    old_rejected = seed_item(status="rejected")
    data: dict[str, Any] = {
        "statement": "use the other metric", "effective_date": "2026-09-01",
        "suggested_action": "weight_change", "conflicts_with": [old_active, old_rejected],
        "entities": [{"type": "service", "id": "svc_a"}],
    }  # fmt: skip
    memory_id = seed_item(kind="user_correction", data=data, content="weight MTTR lower")
    with capture_logs() as logs:
        env.lifecycle.approve(memory_id, REVIEWER, now=NOW)
    stored = row_of(memory_id)["data"]
    derived = review_of(stored["derived_review_item_id"])
    assert derived.kind == "weight_change"
    assert dict(derived.payload) == {
        "source_memory_id": memory_id, "statement": "weight MTTR lower",
        "entities": [{"type": "service", "id": "svc_a"}], "effective_date": "2026-09-01",
        "suggested_action": "weight_change",
    }  # fmt: skip
    assert derived.status == "pending"
    expired = row_of(old_active)
    assert expired["status"] == "expired"
    assert expired["data"]["superseded_by"] == memory_id
    assert expired["data"]["expired_reason"] == "superseded"
    assert row_of(old_rejected)["status"] == "rejected"
    assert ([old_active], "expired") in env.vectors.calls
    (event,) = _events(logs, "memory.item.approved")
    assert event["derived_review_item_id"] == stored["derived_review_item_id"]


def test_ut07_33_business_rule_mapping_suggestion(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-33 a business_rule with mapping_suggestion derives a mapping_suggestion item."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item(kind="business_rule", data={"suggested_action": "mapping_suggestion"})
    env.lifecycle.approve(memory_id, REVIEWER, now=NOW)
    derived = review_of(row_of(memory_id)["data"]["derived_review_item_id"])
    assert derived.kind == "mapping_suggestion"
    assert derived.payload["effective_date"] is None


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("user_correction", {"suggested_action": "none"}),
        ("glossary", {"suggested_action": "weight_change"}),
        ("user_correction", {"suggested_action": "weight_change",
                             "derived_review_item_id": "rev_" + "0" * 26}),
    ],
)  # fmt: skip
def test_ut07_33_no_derived_item_otherwise(
    ops_store: OpsStoreHandle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    data: dict[str, Any],
) -> None:
    """UT07-33 no suggestion, a kind without suggestions, or one already derived: none added."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item(kind=kind, data=data)
    before = len(review_rows())
    env.lifecycle.approve(memory_id, REVIEWER, now=NOW)
    assert len(review_rows()) == before
    assert row_of(memory_id)["data"].get("derived_review_item_id") == data.get(
        "derived_review_item_id"
    )


def test_ut07_33_malformed_conflict_list_is_ignored(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-33 non-id entries and a non-list conflicts_with supersede nothing."""
    env = make_lifecycle(tmp_path, monkeypatch)
    active = seed_item(status="active")
    first = seed_item(data={"conflicts_with": "not-a-list"})
    second = seed_item(data={"conflicts_with": [5, "mem_bad"]})
    env.lifecycle.approve(first, REVIEWER, now=NOW)
    env.lifecycle.approve(second, REVIEWER, now=NOW)
    assert row_of(active)["status"] == "active"
    assert all(status != "expired" for _, status in env.vectors.calls)


# ---------------------------------------------------------------- UT07-34 reject


def test_ut07_34_reject_rejects_item_and_review_item(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-34 pending → rejected; review item rejected by the user; the note is redacted."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item()
    review_id = row_of(memory_id)["data"]["review_item_id"]
    with capture_logs() as logs:
        assert (
            env.lifecycle.reject(memory_id, REVIEWER, f"  wrong, ask {PLANTED_EMAIL} ", now=NOW)
            is None
        )
    row = row_of(memory_id)
    assert row["status"] == "rejected"
    data = row["data"]
    assert (data["rejected_by"], data["rejected_at"]) == (REVIEWER, clock.format_utc(NOW))
    assert PLANTED_EMAIL not in data["rejection_note"]
    assert data["rejection_note"].startswith("wrong, ask ")
    review = review_of(review_id)
    assert (review.status, review.decided_by, review.note) == (
        "rejected", REVIEWER, data["rejection_note"],
    )  # fmt: skip
    assert [(e, a) for e, a, _ in env.audits] == [("review_decision", REVIEWER)]
    assert env.vectors.calls == [([memory_id], "rejected")]
    (event,) = _events(logs, "memory.item.rejected")
    assert (event["memory_id"], event["review_item_id"]) == (memory_id, review_id)
    assert PLANTED_EMAIL not in str(logs)


def test_ut07_34_repeat_and_non_pending(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-34 rejected again: no change; active: PolicyViolation; missing: MemoryNotFound."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item()
    env.lifecycle.reject(memory_id, REVIEWER, "no")
    env.lifecycle.reject(memory_id, "c" * 32, "again", now=NOW)
    assert row_of(memory_id)["data"]["rejected_by"] == REVIEWER
    active = seed_item(status="active")
    with pytest.raises(PolicyViolation, match=r"reject\.not_pending"):
        env.lifecycle.reject(active, REVIEWER, "no", now=NOW)
    with pytest.raises(MemoryNotFound):
        env.lifecycle.reject(_MISSING, REVIEWER, "no", now=NOW)


def test_ut07_34_racing_approval_is_seen_inside_the_transaction(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-34 an approval committed after the pre-check: reject raises, nothing changes."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item()
    real = ops.get_memory_items
    calls: list[int] = []

    def racing(ids: list[str], *, conn: Any = None) -> list[ops.MemoryItemRow]:
        rows = real(ids, conn=conn)
        if not calls:
            ops.update_memory_item(memory_id, status="active")
        calls.append(1)
        return rows

    monkeypatch.setattr(lifecycle_mod.ops, "get_memory_items", racing)
    with pytest.raises(PolicyViolation, match=r"reject\.not_pending"):
        env.lifecycle.reject(memory_id, REVIEWER, "no", now=NOW)
    assert env.vectors.calls == []


@pytest.mark.parametrize(
    ("memory_id", "user_ref", "note"),
    [
        ("mem_bad", REVIEWER, "no"),
        (_MISSING, "nobody", "no"),
        (_MISSING, REVIEWER, "   "),
        (_MISSING, REVIEWER, "x" * 1001),
        (_MISSING, REVIEWER, None),
    ],
)
def test_ut07_34_invalid_input_raises_tool_input_error(
    ops_store: OpsStoreHandle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_id: str,
    user_ref: str,
    note: Any,
) -> None:
    """UT07-34 bad ids, an empty or overlong note: ToolInputError."""
    env = make_lifecycle(tmp_path, monkeypatch)
    with pytest.raises(ToolInputError):
        env.lifecycle.reject(memory_id, user_ref, note, now=NOW)


# ---------------------------------------------------------------- UT07-36 expiry


def test_ut07_36_expire_sweeps_due_items_in_batches(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-36 due candidate/pending/active items expire with reason ttl; the rest stay."""
    env = make_lifecycle(tmp_path, monkeypatch)
    monkeypatch.setattr(lifecycle_mod, "_EXPIRE_BATCH", 2)
    past = NOW - timedelta(minutes=1)
    due = [
        seed_item(status=s, expires_at=past) for s in ("candidate", "pending_approval", "active")
    ]
    due.append(seed_item(status="active", expires_at=NOW))  # expires_at == now is due
    later = seed_item(status="active", expires_at=NOW + timedelta(seconds=1))
    gone = seed_item(status="rejected", expires_at=past)
    with capture_logs() as logs:
        assert env.lifecycle.expire(NOW) == 4
    for memory_id in due:
        row = row_of(memory_id)
        assert (row["status"], row["data"]["expired_reason"]) == ("expired", "ttl")
    assert (row_of(later)["status"], row_of(gone)["status"]) == ("active", "rejected")
    assert sorted(i for ids, _ in env.vectors.calls for i in ids) == sorted(due)
    assert {status for _, status in env.vectors.calls} == {"expired"}
    assert [len(ids) for ids, _ in env.vectors.calls] == [2, 2]
    (event,) = _events(logs, "memory.item.expired")
    assert (event["count"], event["reason"]) == (4, "ttl")
    assert env.lifecycle.expire(NOW) == 0  # idempotent


def test_ut07_36_expire_default_now_and_sync_failure(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-36 without `now` the clock is used; a LanceDB failure is logged, not raised."""
    env = make_lifecycle(tmp_path, monkeypatch)
    env.vectors.fail = True
    memory_id = seed_item(status="active", expires_at=clock.now() - timedelta(days=1))
    with capture_logs() as logs:
        assert env.lifecycle.expire() == 1
    assert row_of(memory_id)["status"] == "expired"
    (event,) = _events(logs, "memory.vector.sync_failed")
    assert (event["op"], event["count"]) == ("expire", 1)


def test_ut07_36_expire_item_sets_reason_and_superseded_by(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-36 expire_item: expired with reason and superseded_by; repeat changes nothing."""
    env = make_lifecycle(tmp_path, monkeypatch)
    newer = seed_item(status="active")
    memory_id = seed_item(status="active")
    with capture_logs() as logs:
        env.lifecycle.expire_item(memory_id, "replaced", superseded_by=newer)
    data = row_of(memory_id)["data"]
    assert row_of(memory_id)["status"] == "expired"
    assert (data["expired_reason"], data["superseded_by"]) == ("replaced", newer)
    assert env.vectors.calls == [([memory_id], "expired")]
    assert _events(logs, "memory.item.expired") == []  # the free-text reason is never logged
    env.lifecycle.expire_item(memory_id, "other")
    assert row_of(memory_id)["data"]["expired_reason"] == "replaced"
    assert len(env.vectors.calls) == 1
    plain = seed_item(status="pending_approval")
    env.lifecycle.expire_item(plain, "stale")
    assert "superseded_by" not in row_of(plain)["data"]


def test_ut07_36_expire_item_errors(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-36 missing → MemoryNotFound; bad reason or superseded_by → ToolInputError."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item(status="active")
    with pytest.raises(MemoryNotFound):
        env.lifecycle.expire_item(_MISSING, "gone")
    for reason, by in (("", None), ("x" * 201, None), ("ok", "mem_bad"), ("ok", _MISSING)):
        with pytest.raises(ToolInputError):
            env.lifecycle.expire_item(memory_id, reason, superseded_by=by)
    with pytest.raises(ToolInputError):
        env.lifecycle.expire_item("mem_bad", "gone")
    assert row_of(memory_id)["status"] == "active"


# ---------------------------------------------------------------- UT07-37 record_use


def test_ut07_37_record_use_counts_each_live_id_once(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-37 repeated, unknown and rejected ids: each live id counted once."""
    env = make_lifecycle(tmp_path, monkeypatch)
    active, pending = seed_item(status="active"), seed_item()
    rejected = seed_item(status="rejected")
    ids = [active, active, _MISSING, pending, rejected, pending]
    env.lifecycle.record_use(ids, "run_" + new_ulid(), now=NOW)
    for memory_id in (active, pending):
        row = row_of(memory_id)
        assert (row["use_count"], row["last_used_at"]) == (1, clock.format_utc(NOW))
    assert (row_of(rejected)["use_count"], row_of(rejected)["last_used_at"]) == (0, None)
    env.lifecycle.record_use([active], "run_x")
    assert row_of(active)["use_count"] == 2
    env.lifecycle.record_use([], "run_x", now=NOW)  # nothing rendered: no write
    assert env.vectors.calls == []


@pytest.mark.parametrize(
    "ids", [["mem_bad"], ["mem_" + new_ulid() for _ in range(201)], "mem_x", [5]]
)
def test_ut07_37_invalid_ids_raise_tool_input_error(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ids: Any
) -> None:
    """UT07-37 a malformed id, a non-list or more than 200 ids: ToolInputError."""
    env = make_lifecycle(tmp_path, monkeypatch)
    with pytest.raises(ToolInputError):
        env.lifecycle.record_use(ids, "run_x", now=NOW)


# ---------------------------------------------------------------- concurrent repeats


def _race(monkeypatch: pytest.MonkeyPatch, change: Any) -> None:
    """Run `change()` right after the pre-check load (a decision committing concurrently)."""
    real = ops.get_memory_items
    calls: list[int] = []

    def racing(ids: list[str], *, conn: Any = None) -> list[ops.MemoryItemRow]:
        rows = real(ids, conn=conn)
        calls.append(1)
        if len(calls) == 1:  # the racing call's own loads pass straight through
            change()
        return rows

    monkeypatch.setattr(lifecycle_mod.ops, "get_memory_items", racing)


def test_ut07_32_concurrent_approval_wins_returns_the_item(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-32 another approval commits first: this call changes nothing and returns it."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item()
    other = "c" * 32
    _race(monkeypatch, lambda: env.lifecycle.approve(memory_id, other, now=NOW))
    item = env.lifecycle.approve(memory_id, REVIEWER, now=NOW)
    assert (item.status, item.data["approved_by"]) == ("active", other)
    assert [a for _, a, _ in env.audits] == [other]


def test_ut07_34_concurrent_rejection_wins(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-34 another rejection commits first: this call changes nothing."""
    env = make_lifecycle(tmp_path, monkeypatch)
    memory_id = seed_item()
    other = "c" * 32
    _race(monkeypatch, lambda: env.lifecycle.reject(memory_id, other, "first", now=NOW))
    env.lifecycle.reject(memory_id, REVIEWER, "second", now=NOW)
    assert row_of(memory_id)["data"]["rejected_by"] == other
    assert [a for _, a, _ in env.audits] == [other]
