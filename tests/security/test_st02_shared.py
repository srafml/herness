"""Security tests for herness.store.ops.shared (ST02-06, TH02-06: unaudited decisions;
ST02-07, TH02-07: oversized payloads and notes)."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from herness.core.errors import ConfigError, FatalError, SchemaViolation
from herness.store.ops import core, shared
from herness.store.ops.migrate import migrate
from herness.store.ops.shared import create_review_item, decide_review_item, get_review_item

pytestmark = pytest.mark.unit

USER = "ab" * 16  # a 32-hex user_ref (spec 09)
T0 = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=datetime.UTC)
T1 = T0 + datetime.timedelta(minutes=1)


@pytest.fixture
def store(ops_store: Path) -> Path:
    migrate()
    return ops_store


def _raise_audit(event: str, actor: str, **fields: Any) -> None:
    msg = f"audit write failed: {event}"
    raise FatalError(msg)


def test_st02_06_audit_failure_rolls_back_the_decision(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST02-06 with `audit` patched to raise, a decision leaves the item `pending`."""
    monkeypatch.setattr(shared, "audit", _raise_audit)
    item_id = create_review_item("label_check", {"question": "q1"}, now=T0)
    with pytest.raises(FatalError):
        decide_review_item(item_id, "approved", decided_by=USER, now=T1)
    item = get_review_item(item_id)
    assert (item.status, item.decided_by, item.decided_at) == ("pending", None, None)


def test_st02_06_audit_failure_rolls_back_the_callers_transaction(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST02-06 with `conn`, the audit error propagates and the caller's write rolls back."""
    monkeypatch.setattr(shared, "audit", _raise_audit)
    item_id = create_review_item("memory_write", {"memory_id": "mem_1"}, now=T0)

    def approve(conn: sqlite3.Connection) -> None:
        decide_review_item(item_id, "approved", decided_by=USER, now=T1, conn=conn)

    with pytest.raises(FatalError):
        core.run_write(approve, op="memory_approve")
    assert get_review_item(item_id).status == "pending"


def test_st02_07_oversized_payload_and_note_rejected(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST02-07 70 KiB payload → SchemaViolation; 3,000-char note → ConfigError; CHECK too."""
    monkeypatch.setattr(shared, "audit", lambda *a, **k: None)
    with pytest.raises(SchemaViolation, match="exceeds"):
        create_review_item("label_check", {"blob": "x" * 70 * 1024}, now=T0)
    item_id = create_review_item("label_check", {"question": "q1"}, now=T0)
    with pytest.raises(ConfigError):
        decide_review_item(item_id, "approved", decided_by=USER, note="n" * 3000, now=T1)
    assert get_review_item(item_id).status == "pending"
    # A direct insert that bypasses dump_json still meets the SQLite CHECK caps.
    ts = "2026-09-26T10:00:00.000000Z"
    oversize = '{"blob":"' + "x" * 70 * 1024 + '"}'
    insert = (
        "INSERT INTO review_item (item_id, kind, payload, status, created_at, decided_by,"
        " decided_at, note) VALUES (?, 'label_check', ?, ?, ?, ?, ?, ?)"
    )
    cases = [
        ("rev_A", oversize, "pending", ts, None, None, None),
        ("rev_B", "{}", "approved", ts, "system", ts, "n" * 3000),
    ]
    for params in cases:
        with pytest.raises(SchemaViolation, match="CHECK constraint failed"):
            core.run_write(lambda conn, p=params: conn.execute(insert, p), op="direct_insert")
