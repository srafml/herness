"""IT07-05 (F07-07, F07-08): the outcome job on planted MTTR series, end to end through the
facade and the spec 08 queue (T07-24).

The recommendation is written by the real spec 07 `MemoryStore.write_recommendations` (a done
prior run with a verified finding citing recorded evidence), accepted through
`MemoryStore.decide`, which enqueues the two `outcome_measure` jobs (U07-82); the due job is
claimed and run with `run_inline`, the handler `register_memory_components` registered. The
warehouse is the planted `metrics_tiny` series of `_outcome_env` (weekly incidents, 20 % MTTR
improvement from the effective week; real spec 04 facts, `compute_metric`, `peer_group`).
Unlike the T07-18 row in `tests/integration/harness/test_memory_outcome_it.py` (raw-SQL rec and
decision, handler called directly), nothing of the closed loop is seeded with raw SQL.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.integration.memory._it07_env import Mem, memory_store
from tests.support.dispatch_standin import use_test_config
from tests.support.metrics_tiny import BUILD_ID
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._outcome_env import (
    EFFECTIVE,
    METRIC,
    flat,
    improves,
    make_env,
    outcomes,
    plant,
    summaries,
)

from herness.core import redact as r
from herness.core import time as clock
from herness.core.ids import IdKind, new_id, query_id
from herness.core.jobs import enqueue, run_inline
from herness.core.jobs.ports import bind_jobs_backend
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.types import Evidence, Finding, NumberRef, RecommendationDraft
from herness.harness.memory import outcome, register_memory_components
from herness.harness.tools import ToolRegistry
from herness.store.ops import core, record_evidence, run_write
from herness.store.ops.findings import insert_finding
from herness.store.ops.jobs import SqliteJobsBackend
from herness.store.ops.resilience import SqliteResilienceBackend
from herness.store.ops.runs import RunRow, insert_run

pytestmark = pytest.mark.integration

PEERS = ("svc_p1", "svc_p2", "svc_p3")
USER = "e" * 32
SQL = "SELECT avg(mttr_hours) AS mttr FROM metrics.service_week WHERE service_id = $s"


def _accepted(mem: Mem, target: str) -> str:
    """A done prior run, its verified finding on `target`, the facade's rec, accepted."""
    at = EFFECTIVE - timedelta(days=3)
    run_id = new_id(IdKind.RUN)
    row = RunRow(run_id=run_id, kind="org_review", depth="standard", profile="local",
                 build_id=BUILD_ID, status="done", started_at=at, finished_at=at, token_usage={},
                 cost_usd=Decimal(0), config_hash="h", meta={})  # fmt: skip
    run_write(lambda c: insert_run(c, row), op="test_seed")
    params = {"s": target}
    ev = Evidence(query_id=query_id(SQL, params, BUILD_ID), run_id=run_id, build_id=BUILD_ID,
                  sql=SQL, params=params, result_hash="0" * 64, row_count=1,
                  result_sample=[{"mttr": 10.0}], executed_at=at, duration_ms=1)  # fmt: skip
    assert record_evidence(ev)
    n1 = NumberRef(id="n1", value=10.0, unit="hours", query_id=ev.query_id, column="mttr",
                   row_key=None)  # fmt: skip
    finding = Finding(finding_id=new_id(IdKind.FINDING), run_id=run_id, task_id=new_id(IdKind.TASK),
                      author_role="analyst", claim="Service MTTR was [[n1]] hours",
                      entity_type="service", entity_id=target, numbers=[n1],
                      query_ids=[ev.query_id], confidence=0.7, status="verified",
                      created_at=at)  # fmt: skip
    run_write(lambda c: insert_finding(c, finding), op="test_seed")
    draft = RecommendationDraft(
        rank=1, kind="org_action", target_type="service", target_id=target,
        summary="Fix the slow restores of the service", numbers=[n1], expected_metric=METRIC,
        expected_delta_ref=None, expected_usd_ref=None, finding_ids=[finding.finding_id],
    )  # fmt: skip
    [rec_id] = mem.store.write_recommendations(run_id, [draft])
    mem.store.decide(rec_id, "accepted", "planned work", USER, effective_at=EFFECTIVE)
    return rec_id


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, con: Any) -> Mem:
    """The facade, its handlers registered; the outcome seam reads the planted warehouse."""
    use_test_config(tmp_path / "cfg", (f"paths.data={(tmp_path / 'data').as_posix()}",))
    bind_jobs_backend(SqliteJobsBackend())
    bind_ops_backend(SqliteResilienceBackend())
    planted = make_env(tmp_path / "plant", monkeypatch, con)  # clock.now = NOW; catalog; facts
    mem = memory_store(tmp_path, tmp_path / "data")
    monkeypatch.setattr(r._State, "redactor", mem.env.redactor)  # the job payload scan's key
    register_memory_components(ToolRegistry(), mem.store)
    outcome.configure_outcome(dataclasses.replace(
        mem.store.outcome_deps, config_hash=planted.deps.config_hash,
        open_current=planted.deps.open_current, load_catalog=planted.deps.load_catalog,
    ))  # fmt: skip
    return mem


def _jobs(rec_id: str) -> dict[int, str]:
    """The queued `outcome_measure` jobs of `rec_id` by measurement."""
    rows = core.read_all("SELECT job_id, payload FROM job WHERE kind = 'outcome_measure'", ())
    found = {}
    for row in rows:
        payload = core.load_json(row["payload"], field="payload")
        if payload.get("rec_id") == rec_id:
            found[int(payload["measurement"])] = str(row["job_id"])
    return found


def _sweep() -> Any:
    return run_inline(enqueue("outcome_measure", {"sweep": True}, "none"))


def test_it07_05_planted_improvement_flat_peers_paid_off_rerun_adds_no_row(
    ops_store: OpsStoreHandle, reset_process_state: ProcessState, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """IT07-05 a planted 20 % MTTR improvement after `effective_at` with flat peers: the
    queued measurement is `paid_off`; rerunning the job (a sweep) adds no row."""
    del ops_store, reset_process_state
    mem = _setup(tmp_path, monkeypatch, plant({"svc_t": improves()} | dict.fromkeys(PEERS, flat)))
    rec_id = _accepted(mem, "svc_t")
    jobs = _jobs(rec_id)
    assert sorted(jobs) == [1, 2]  # F07-07: both measurements enqueued by the decision

    first = run_inline(jobs[1])

    assert first.status == "done"
    assert first.result == {"measured": 1, "skipped": 0, "verdicts": {"paid_off": 1}}
    [row] = outcomes()
    assert (row["rec_id"], row["measurement"], row["verdict"]) == (rec_id, 1, "paid_off")
    assert row["details"]["rel"] == pytest.approx(0.2, abs=0.01)
    rerun = _sweep()
    assert rerun.result == {"measured": 0, "skipped": 0, "verdicts": {}}
    assert len(outcomes()) == 1  # no new row
    assert len(summaries()) == 1
    [second] = core.read_all("SELECT scheduled_for FROM job WHERE job_id = ?", (jobs[2],))
    assert clock.now() < clock.parse_utc(str(second["scheduled_for"]))  # not due yet


def test_it07_05_same_improvement_in_all_peers_no_effect_rerun_adds_no_row(
    ops_store: OpsStoreHandle, reset_process_state: ProcessState, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """IT07-05 the same 20 % improvement in every peer: the peer-adjusted effect is zero,
    the queued measurement is `no_effect`; rerunning the job adds no row."""
    del ops_store, reset_process_state
    hours = {"svc_t": improves()} | dict.fromkeys(PEERS, improves())
    mem = _setup(tmp_path, monkeypatch, plant(hours))
    rec_id = _accepted(mem, "svc_t")

    first = run_inline(_jobs(rec_id)[1])

    assert first.result == {"measured": 1, "skipped": 0, "verdicts": {"no_effect": 1}}
    [row] = outcomes()
    assert (row["rec_id"], row["verdict"]) == (rec_id, "no_effect")
    assert row["details"]["method"] == "did_peer_median"
    rerun = _sweep()
    assert rerun.result == {"measured": 0, "skipped": 0, "verdicts": {}}
    assert len(outcomes()) == 1
    [summary] = summaries()
    assert summary["data"]["rec_id"] == rec_id
