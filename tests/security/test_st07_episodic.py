"""Security test ST07-12 for decisions (impl 07 §11.5; U07-82, TH07-12, T07-16).

Approvals (U07-51) and recommendation decisions (U07-82) are attributed: `approved_by` /
`decided_by` are stored and the audit chain holds a line naming the actor. The
`recommendation_decision` line is written by spec 09's action before it calls `decide`
(U07-82 security notes); this test replays that order against the real audit file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.support.sync_env import init_sync_config
from tests.unit.harness.memory._episodic_env import USER, make_deps, rec_row, seed_recs, seed_run
from tests.unit.harness.memory._lifecycle_env import REVIEWER, make_lifecycle, row_of, seed_item
from tests.unit.harness.memory._write_env import NOW

from herness.core.audit import audit
from herness.harness.memory.episodic import decide
from herness.store.ops import core

pytestmark = pytest.mark.integration

_REASON = "budget agreed in the planning review"


def _audit_lines(logs: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted(logs.glob("audit-*.jsonl"))
        for line in path.read_bytes().split(b"\n")
        if line
    ]


def test_st07_12_approve_and_decide_record_actor_and_audit(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST07-12 approve and decide: approved_by / decided_by stored, audit lines present."""
    logs = Path(init_sync_config(tmp_path).paths.logs)
    env = make_lifecycle(tmp_path, monkeypatch, real_audit=True)
    memory_id = seed_item()
    env.lifecycle.approve(memory_id, REVIEWER, now=NOW)
    assert row_of(memory_id)["data"]["approved_by"] == REVIEWER

    (rec_id,) = seed_recs([rec_row(seed_run(), NOW, expected_metric=None)])
    deps = make_deps(env.writer)
    before = len(_audit_lines(logs))
    decide(rec_id, "deferred", _REASON, USER, deps=deps, now=NOW)
    assert len(_audit_lines(logs)) == before  # decide leaves the audit line to spec 09
    # spec 09's action order (U09 decide_recommendation): audit, then decide
    audit("recommendation_decision", USER, rec_id=rec_id, decision="accepted", decided_by=USER)
    decide(rec_id, "accepted", _REASON, USER, deps=deps, now=NOW)

    rows = core.read_all("SELECT decision, decided_by FROM decision_log ORDER BY rowid")
    assert [tuple(r) for r in rows] == [("deferred", USER), ("accepted", USER)]
    notes = core.read_all("SELECT provenance FROM memory_item WHERE kind = 'decision_note'")
    authors = {json.loads(r["provenance"])["author_ref"] for r in notes}
    assert authors == {USER}
    lines = _audit_lines(logs)
    review = [d for d in lines if d["event"] == "review_decision"]
    assert [(d["actor"], d["fields"]["decided_by"]) for d in review] == [(REVIEWER, REVIEWER)]
    recs = [d for d in lines if d["event"] == "recommendation_decision"]
    assert [(d["actor"], d["fields"]) for d in recs] == [
        (USER, {"rec_id": rec_id, "decision": "accepted", "decided_by": USER})
    ]
    assert _REASON not in "".join(json.dumps(d) for d in lines)
