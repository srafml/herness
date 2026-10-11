"""IT07-02 (F07-06): idempotent recommendations across a crash in the record step (T07-24).

A funding review runs through the real swarm driver of the T06-22 set-up (`drv`: real run
steps, Blackboard, findings and drafts; scripted agents) with the real spec 07 `MemoryStore`.
The record step's first `write_recommendations` call commits and the process then "crashes"
before the draft is filled (a `StoreBusy` raised after the commit: a `RetryableError`, so
the driver leaves the run in `recording` for spec 08 to resume). The resumed run calls
`write_recommendations` a second time for the same `run_id`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.integration.memory._it07_env import (
    memory_store,
    record_portfolio,
    run_summaries,
    swarm_for,
)
from tests.unit.harness._blackboard_env import test_redactor

from herness.core.errors import StoreBusy
from herness.core.types import ReportDraft
from herness.harness.memory import MemoryStore
from herness.store import ops
from herness.store.ops import get_run, select_runs

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
__all__ = ["drv", "test_redactor"]


class CrashAfterCommit:
    """The real facade; the first `write_recommendations` commits, then raises (a crash)."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store
        self.returned: list[list[str]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def write_recommendations(self, run_id: str, drafts: list[Any]) -> list[str]:
        ids = self._store.write_recommendations(run_id, drafts)
        self.returned.append(ids)
        if len(self.returned) == 1:
            msg = "simulated crash after the recommendations commit"
            raise StoreBusy(msg)
        return ids


def test_it07_02_crash_between_calls_one_set_of_rows_same_ids_one_summary(
    drv: Drv, tmp_path: Path
) -> None:
    """IT07-02 `write_recommendations` called twice for one run (a crash between the calls):
    one set of rows, the same `rec_id`s returned both times, one `run_summary`."""
    record_portfolio()
    memory = CrashAfterCommit(memory_store(tmp_path, Path(drv.hcfg.paths.data)).store)
    swarm = swarm_for(drv, memory)
    with pytest.raises(StoreBusy, match="simulated crash"):
        asyncio.run(swarm.start(drv.request(depth="standard")))
    [run] = select_runs(statuses={"recording"})
    first = ops.run_recommendations(run.run_id)
    with structlog.testing.capture_logs() as logs:
        res = asyncio.run(swarm_for(drv, memory).resume(run.run_id))

    assert res.status == "done"
    stored = get_run(run.run_id)
    assert stored is not None
    assert stored.status == "done"
    [ids_1, ids_2] = memory.returned  # called twice for the run
    assert ids_1 == ids_2  # identical rec_ids both times
    rows = ops.run_recommendations(run.run_id)
    assert rows == first  # one set of rows: the resume wrote nothing
    assert sorted(r["rec_id"] for r in rows) == sorted(ids_1)
    assert [(r["kind"], r["target_type"], r["target_id"]) for r in rows] == [
        ("fund", "candidate", "c1")
    ]
    [summary] = run_summaries(run.run_id)  # one run_summary
    assert ids_1[0] in str(summary["data"])
    written = [x for x in logs if x["event"] == "memory.recommendations.written"]
    assert [(x["run_id"], x["reused"]) for x in written] == [(run.run_id, True)]
    draft = ReportDraft.model_validate_json(Path(str(res.draft_path)).read_bytes())
    assert [r.rec_id for r in draft.recommendations] == ids_1
