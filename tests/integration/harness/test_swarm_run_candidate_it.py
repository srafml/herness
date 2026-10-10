"""IT06-37: a funding run with a candidate-targeted recommendation reaches `done` (T06-22b).

The T06-22 run-driver setup (`drv`: real swarm steps, scripted agents, planted evidence), with
the real spec 07 `MemoryStore` in place of the fake: the record step (`run_record_step`) hands
the funding pipeline's drafts, whose `target_type` is `candidate`, to the real
`write_recommendations`, which validates and persists them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.unit.harness._blackboard_env import test_redactor
from tests.unit.harness.memory._write_env import PATTERNS, make_writer

from herness.core.types import ReportDraft
from herness.harness.memory import MemoryStore
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder
from herness.harness.swarm.run import Swarm
from herness.store import ops
from herness.store.ops import core, get_run, record_evidence

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
__all__ = ["drv", "test_redactor"]


def _real_memory(drv_: Drv, tmp_path: Path) -> MemoryStore:
    env = make_writer(tmp_path)
    return MemoryStore(
        MemoryConfig(injection_patterns=PATTERNS),
        conn_factory=core.connection,
        vectors=env.vectors,
        embedder=Embedder(env.embed, model_name="bge-m3"),
        redactor=env.redactor,
        llms=None,
        allowed_numeral_patterns=drv_.hcfg.app.reports.allowed_numeral_patterns,
        data_root=Path(drv_.hcfg.paths.data),
    )


def test_it06_37_candidate_recommendation_records_to_done(drv: Drv, tmp_path: Path) -> None:
    """IT06-37 a funding run whose Writer recommends candidate `c1` passes the real record step
    and `MemoryStore.write_recommendations` to `done`; the `rec_id` is filled in the draft and
    the persisted recommendation row targets the candidate."""
    portfolio = e._evidence("SELECT 1 AS expected_impact_usd", {}, {"expected_impact_usd": 1200})
    assert record_evidence(portfolio.model_copy(update={"query_id": e.PORTFOLIO_QID}))
    memory = _real_memory(drv, tmp_path)
    swarm = Swarm(drv.hcfg.pipelines, drv.llms, memory, deps=drv.deps)
    drv.swarms.append(swarm)
    res = asyncio.run(swarm.start(drv.request(depth="standard")))
    assert res.status == "done"
    run = get_run(res.run_id)
    assert run is not None
    assert run.status == "done"
    rows = ops.run_recommendations(res.run_id)
    assert [(r["kind"], r["target_type"], r["target_id"]) for r in rows] == [
        ("fund", "candidate", "c1")
    ]
    draft = ReportDraft.model_validate_json(Path(str(res.draft_path)).read_bytes())
    assert [r.rec_id for r in draft.recommendations] == [rows[0]["rec_id"]]
