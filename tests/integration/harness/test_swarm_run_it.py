"""IT06-32, IT06-01/02 (driver level), IT06-03, IT06-15, IT06-17..IT06-21, ST06-13: the run driver.

Every swarm step runs for real over the migrated ops store, the Blackboard writer and the spec
08 task helpers; only `run_agent` is scripted per role, and the Verifier, memory, renderer and
pipeline are fakes (`_swarm_run_env`).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import ALL_STATUSES, Drv

from herness.core.types import ReportDraft
from herness.harness.swarm.lifecycle import RunResult
from herness.store.ops import get_run, select_tasks

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
test_redactor = e.env_mod.test_redactor


def _changes(logs: list[dict[str, Any]], run_id: str) -> list[str]:
    seq = [ev for ev in logs if ev["event"] == "harness.run.status_changed"]
    seq = [ev for ev in seq if ev["run_id"] == run_id]
    return [seq[0]["from"], *(ev["to"] for ev in seq)] if seq else []


def _draft(d: Drv, res: RunResult) -> ReportDraft:
    assert res.draft_path is not None
    return ReportDraft.model_validate_json(open(res.draft_path, "rb").read())


def test_it06_32_standard_run_status_sequence(drv: Drv) -> None:
    """IT06-32 full standard run: `harness.run.status_changed` events walk design 06 §5.1."""
    swarm = drv.swarm()
    with structlog.testing.capture_logs() as logs:
        res = asyncio.run(swarm.start(drv.request(depth="standard")))
    assert res.status == "done"
    assert _changes(logs, res.run_id) == list(ALL_STATUSES)
    run = get_run(res.run_id)
    assert run is not None
    assert run.finished_at is not None
    assert set(run.token_usage) == {"analysis", "writer", "by_role"}
    assert drv.renders == [(res.run_id, list(drv.hcfg.app.reports.formats))]
    draft = _draft(drv, res)
    assert draft.coverage.publishable is True
    assert [r.rec_id for r in draft.recommendations] == drv.memory.recs[res.run_id]
    assert all(t.status == "done" for t in select_tasks(res.run_id))
    finished = [ev for ev in logs if ev["event"] == "harness.run.finished"]
    assert finished[0]["status"] == "done"
    assert ("herness_harness_runs_total", {"kind": "funding_review", "status": "done"}) in (
        drv.metrics.counters
    )
