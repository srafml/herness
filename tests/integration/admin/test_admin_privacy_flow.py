"""Integration test of privacy deletion F10-13 (impl 10 IT10-06; U10-99, R-54, R-57).

Every store is real and planted under one data root (``privacy_env``; spec 11 ``tiny_build``
is not in the tree): raw lake, enrichment vectors, decision cache, labels, a promoted
``CURRENT`` build, memory items, vectors and FTS rows, ``evidence``, ``finding`` and traces.
The rebuild is a fake impl 08 job completion.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

import pytest
from tests.support.privacy_env import (  # noqa: F401 - fixture import
    BASE,
    H_OTHER,
    H_RID,
    LATER,
    NEW_BUILD,
    NOW,
    OLD_BUILD,
    OTHER,
    OTHER_DESC,
    RID,
    PrivacyEnv,
    privacy_env,
)
from tests.unit.enrich._purge_support import QSV

from herness.core.errors import StoreBusy
from herness.core.jobs.ports import require_jobs_backend
from herness.enrich.cache import DecisionCache
from herness.store import warehouse
from herness.store.ops import core

pytestmark = pytest.mark.integration

RETIRED_BUILD = "20260930-120000-ABCDEF"  # retired before the deletion


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


def _set_run(status: str, build: str) -> None:
    """A review run pinned to ``build`` (non-terminal runs pin their build, U02-105)."""
    sql = (
        "INSERT INTO run (run_id, kind, depth, profile, config_hash, build_id, status, started_at)"
        " VALUES ('run_pinned', 'org_review', 'fast', 'local', ?, ?, ?, ?)"
        " ON CONFLICT (run_id) DO UPDATE SET status = excluded.status"
    )
    stamp = "2026-10-02T08:00:00.000000Z"
    params = ("cfg_" + "0" * 16, build, status, stamp)
    core.run_write(lambda c: c.execute(sql, params), op="test_pin_run")


@pytest.mark.parametrize("leftover", ["pinned promoted build", "pinned retired build"])
def test_it10_06_leftover_build_holding_the_record_fails_step5_closed(
    privacy_env: PrivacyEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    leftover: str,
) -> None:
    """IT10-06 / review I1 the real cleanup_builds keeps a pre-deletion build file that still
    holds the record (pinned by a running run; a retired build pinned the same way) and does
    not report it as deferred. Step 5 is recorded failed (StoreBusy, retried by impl 08) with
    the leftover counted, stays failed on a retry while the file remains, and is done only once
    no older build file remains."""
    env = privacy_env
    target = OLD_BUILD
    if leftover == "pinned retired build":
        target = RETIRED_BUILD
        env.make_build(RETIRED_BUILD, [(RID, "retired copy", H_RID)], status="retired")
    assert RID in env.build_record_ids(target)
    with pytest.raises(StoreBusy, match="older warehouse builds remain"):
        env.delete_end_to_end(monkeypatch, during_wait=lambda: _set_run("running", target))
    req = env.request()
    assert (req.status, req.steps["5"]["status"], req.steps["5"]["error"]) == (
        "running",
        "failed",
        "StoreBusy",
    )
    assert req.steps["5"]["counts"]["builds_left"] == 1
    assert "6" not in req.steps
    assert RID in env.build_record_ids(target)  # the reason step 5 is not done
    with pytest.raises(StoreBusy):  # the impl 08 retry while the file remains
        env.run(env.continuation(), now=LATER)
    assert env.request().steps["5"]["status"] == "failed"
    _set_run("done", target)
    env.run(env.continuation(), now=LATER)
    req = env.request()
    assert (req.status, req.steps["5"]["status"]) == ("done", "done")
    assert req.steps["5"]["counts"]["builds_left"] == 0
    assert not warehouse.build_path(target).exists()
    assert not warehouse.build_path(OLD_BUILD).exists()


FAILED_BUILD = "20261002-091000-ABCDEF"  # the first privacy rebuild, after the step-5 time
SECOND_BUILD = "20261002-100000-ABCDEF"  # the re-enqueued rebuild, promoted


def _finish_job(job_id: str, status: str) -> None:
    """Fake impl 08 end of a ``build_pipeline`` job."""
    stamp = "2026-10-02T10:20:00.000000Z"
    sql = "UPDATE job SET status = ?, started_at = ?, finished_at = ? WHERE job_id = ?"
    core.run_write(lambda c: c.execute(sql, (status, stamp, stamp, job_id)), op="test_finish_job")


def _latest_continuation() -> dict[str, Any]:
    rows = require_jobs_backend().list_jobs(status="queued", kind="maintenance", limit=20)
    found = [dict(r.payload) for r in rows if "request_id" in r.payload]
    return max(found, key=lambda p: int(p["wait"]))


def test_it10_06_failed_rebuild_then_promoted_rebuild_completes_step5(
    privacy_env: PrivacyEnv,  # noqa: F811
) -> None:
    """IT10-06 / review I4 the first privacy rebuild fails (its build file stays: cleanup_builds
    keeps the newest failed build) and the re-enqueued one is promoted. The step-5 time stays
    the original one across the re-enqueue, so the failed build, started after it, is no
    leftover: step 5 ends ``done`` and the request completes. The pre-deletion build is gone."""
    env = privacy_env
    first = env.run(dict(BASE))
    build_job = str(first.result["waiting_for"])
    env.make_build(FAILED_BUILD, [(OTHER, OTHER_DESC, H_OTHER)], status="failed")
    _finish_job(build_job, "failed")
    cont = _latest_continuation()
    again = env.run(cont, now=NOW + datetime.timedelta(minutes=31))
    second_job = str(again.result["waiting_for"])
    assert second_job != build_job
    cont2 = _latest_continuation()
    env.make_build(SECOND_BUILD, [(OTHER, OTHER_DESC, H_OTHER)])  # promoted, CURRENT
    _finish_job(second_job, "done")
    env.run(cont2, now=NOW + datetime.timedelta(minutes=91))

    req = env.request()
    assert (req.status, req.steps["5"]["status"]) == ("done", "done")
    assert req.steps["5"]["counts"]["builds_left"] == 0
    assert (cont2["rebuilds"], cont2["rebuild_at"]) == (1, cont["rebuild_at"])
    assert warehouse.read_current() == SECOND_BUILD
    assert not warehouse.build_path(OLD_BUILD).exists()
    assert env.build_record_ids(FAILED_BUILD) == [OTHER]  # kept, and it never held the record
