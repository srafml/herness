"""Security end-to-end integration tests (impl 10 IT10-05, IT10-10; card T10-21, TH10-07).

IT10-05 approves a ``review_item`` through impl 02's ``decide_review_item`` on the fixture ops
store and reads the audit file. IT10-10 is the sentinel run of ``tests.support.secret_leak``
(fixture connector sync, one chat turn with ``FakeLLMClient``); its grep is ST10-14 in
``tests/security/test_st10_secret_leak.py``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.support.secret_leak import SECRET_NAME, SENTINEL_KEY, SENTINELS, USER_REF, LeakRun
from tests.support.sync_env import init_sync_config

from herness.core import secrets
from herness.core.audit import verify_chain
from herness.store.errors import ReviewItemConflict
from herness.store.ops import create_review_item, decide_review_item, get_review_item

pytestmark = pytest.mark.integration

_NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.UTC)
_ZERO_HASH = "0" * 64
_AUDIT_KEYS = {"ts", "audit_id", "event", "actor", "fields", "config_hash", "prev_hash"}


def _audit_lines(logs: Path) -> list[bytes]:
    return [
        line
        for path in sorted(logs.glob("audit-*.jsonl"))
        for line in path.read_bytes().split(b"\n")
        if line
    ]


@pytest.fixture
def logs(ops_store: OpsStoreHandle, tmp_path: Path) -> Path:
    """The fixture ops store with a loaded config whose ``paths.logs`` is under ``tmp_path``."""
    cfg = init_sync_config(tmp_path)
    assert cfg.paths.data == ops_store.data_root
    return Path(cfg.paths.logs)


def test_it10_05_approval_writes_exactly_one_review_decision_line(logs: Path) -> None:
    """IT10-05 approving a review_item through decide_review_item: one review_decision line."""
    item_id = create_review_item("label_check", {"question": "q1", "record_id": "r1"}, now=_NOW)
    assert _audit_lines(logs) == []  # creating the item audits nothing
    item = decide_review_item(item_id, "approved", decided_by=USER_REF, now=_NOW)
    assert (item.status, get_review_item(item_id).status) == ("approved", "approved")

    (raw,) = _audit_lines(logs)
    line = json.loads(raw)
    assert set(line) == _AUDIT_KEYS
    assert (line["event"], line["actor"]) == ("review_decision", USER_REF)
    assert line["fields"] == {
        "item_id": item_id,
        "kind": "label_check",
        "status": "approved",
        "decided_by": USER_REF,
        "note_len": 0,
    }
    assert line["prev_hash"] == _ZERO_HASH  # the first line of the chain
    assert line["audit_id"].startswith("aud_")
    report = verify_chain(logs)
    assert (report.ok, report.lines, report.first_break) == (True, 1, None)


def test_it10_05_second_decision_is_refused_and_chain_links(logs: Path) -> None:
    """IT10-05 a repeated decision is refused without a line; the next item's line chains."""
    first = create_review_item("mapping_suggestion", {"field": "category"}, now=_NOW)
    decide_review_item(first, "approved", decided_by=USER_REF, now=_NOW)
    with pytest.raises(ReviewItemConflict):
        decide_review_item(first, "approved", decided_by=USER_REF, now=_NOW)
    assert len(_audit_lines(logs)) == 1

    second = create_review_item("mapping_suggestion", {"field": "team"}, now=_NOW)
    decide_review_item(second, "rejected", decided_by=USER_REF, note="dup", now=_NOW)
    one, two = _audit_lines(logs)
    decisions = [json.loads(one), json.loads(two)]
    assert [d["fields"]["item_id"] for d in decisions] == [first, second]
    assert [d["event"] for d in decisions] == ["review_decision"] * 2
    assert decisions[1]["fields"]["note_len"] == 3
    assert decisions[1]["prev_hash"] == hashlib.sha256(one).hexdigest()
    assert verify_chain(logs).ok


# ------------------------------------------------------------------ IT10-10 sentinel run


def test_it10_10_sentinel_run_completes_end_to_end(leak_run: LeakRun) -> None:
    """IT10-10 connector sync, one chat turn (FakeLLMClient and the adapter on ScriptRouter),
    review approval: every stage ran and the sentinels really entered the pipeline."""
    run = leak_run
    assert run.synced_rows == 1
    for sentinel in SENTINELS:  # positive control: the raw lake zone holds the planted text
        assert sentinel in run.lake_text
    assert SENTINEL_KEY in secrets.known_values()  # resolved at the point of use
    assert len(run.answers) == 2
    assert run.answers[0] == run.answers[1]
    assert run.answers[0].startswith("Ticket TCK-1 reports a printer outage")
    assert len(run.fake_requests) == 2
    assert len(run.wire_bodies) == 2
    tool_result = run.fake_requests[1].messages[-1].parts[0]
    content: Any = getattr(tool_result, "content", "")
    assert "Printer outage" in content  # the record reached the model, redacted
    assert json.loads(run.wire_bodies[1])["messages"][-1]["content"] == content


def test_it10_10_audit_trail_of_the_run(leak_run: LeakRun) -> None:
    """IT10-10 the run's audit file: secret_set by name, config_change, one review_decision."""
    logs = leak_run.data_root / "logs"
    lines = [json.loads(raw) for raw in _audit_lines(logs)]
    events = [line["event"] for line in lines]
    assert events.count("review_decision") == 1
    assert events.count("config_change") == 1
    (secret_set,) = [line for line in lines if line["event"] == "admin_action"]
    assert secret_set["fields"] == {"action": "secret_set", "target": SECRET_NAME}
    assert verify_chain(logs).ok
