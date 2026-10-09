"""Integration test of privacy deletion F10-13 (impl 10 IT10-06; U10-99, R-54, R-57).

Every store is real and planted under one data root (``privacy_env``; spec 11 ``tiny_build``
is not in the tree): raw lake, enrichment vectors, decision cache, labels, a promoted
``CURRENT`` build, memory items, vectors and FTS rows, ``evidence``, ``finding`` and traces.
The rebuild is a fake impl 08 job completion.
"""

from __future__ import annotations

import json

import pytest
from tests.support.privacy_env import (  # noqa: F401 - fixture import
    H_OTHER,
    H_RID,
    NEW_BUILD,
    OLD_BUILD,
    OTHER,
    RID,
    PrivacyEnv,
    privacy_env,
)
from tests.unit.enrich._purge_support import QSV

from herness.enrich.cache import DecisionCache
from herness.store import warehouse

pytestmark = pytest.mark.integration


def _audits(env: PrivacyEnv) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for file in sorted((env.data / "logs").glob("audit-*.jsonl")):
        for line in file.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("event") == "admin_action":
                found.append(record["fields"])
    return found


def test_it10_06_privacy_delete_end_to_end(
    privacy_env: PrivacyEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IT10-06 the record is absent from every store (memory included, R-54) and from the
    next build; the sync after step 1 drops it, the next sync does not re-ingest it, and lake
    pass 2 removes the row a stale sync committed late (steps 3 and 3b repeat)."""
    env = privacy_env
    assert RID in env.lake_ids()
    seen = env.delete_end_to_end(monkeypatch)

    assert (seen["stale"], seen["fresh"], seen["late"]) == (0, 1, 0)
    assert seen["before_pass1"] == 2  # the planted row plus the stale sync's commit
    assert seen["first"].result == {"waiting_for": seen["first"].result["waiting_for"]}
    req = env.request()
    assert seen["last"].result == {"request_id": req.request_id}
    assert req.status == "done"
    assert {k: v["status"] for k, v in req.steps.items()} == dict.fromkeys(
        ["1", "2", "3", "3b", "4", "5", "6", "7"], "done"
    )
    assert req.steps["2"]["counts"]["rows_removed"] == 2
    assert req.steps["3b"]["counts"] == {"memory_items": 2}
    assert req.steps["7"]["counts"]["rows_removed"] == 1  # the late row
    assert req.steps["7"]["counts"]["memory_items"] == 0  # 3b repeated, nothing left
    assert req.steps["4"]["counts"] == {"evidence_rows": 1, "finding_rows": 1, "trace_files": 1}

    # lake: only the other record; the next sync (deletion set reloaded) does not re-ingest
    assert set(env.lake_ids()) == {OTHER}
    assert env.sync([(RID, "again", 9)], filt=env.filter()) == 1
    assert set(env.lake_ids()) == {OTHER}
    # enrichment: vectors (every version), decision cache, labels
    assert env.enrich.vector_ids() == [(OTHER, H_OTHER)]
    assert RID not in env.enrich.ids_in_every_version()
    assert DecisionCache(env.enrich.paths, QSV).dataset() is not None
    assert env.enrich.cache_hashes() == [H_OTHER]
    assert H_RID not in env.enrich.cache_hashes()
    assert env.enrich.label_rows("teacher") == [(OTHER, H_OTHER)]
    assert env.enrich.label_rows("human") == []
    # memory (R-54): items, FTS rows and vectors
    assert env.memory_ids() == env.kept
    assert env.fts("zephyrquokka") == []
    assert env.vectors.vectors(env.cited) == {}
    # ops rows and traces
    assert RID not in env.evidence_sample()
    assert OTHER in env.evidence_sample()
    assert RID not in env.finding_numbers()
    assert sorted(p.name for p in (env.data / "traces").iterdir()) == ["run_bbbb.jsonl"]
    # warehouse: the old build is gone and the next build does not hold the record
    assert warehouse.read_current() == NEW_BUILD
    assert not warehouse.build_path(OLD_BUILD).exists()
    assert env.build_record_ids(NEW_BUILD) == [OTHER]
    # audit: one admin_action naming only the record_id
    assert _audits(env) == [{"action": "privacy_delete", "target": RID}]
