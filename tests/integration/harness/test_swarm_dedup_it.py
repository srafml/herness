"""IT06-27 (U06-103): `run_dedup` over a real Blackboard with the embedding model raising.

Two similar `proposed` findings citing the same query are posted to a migrated tmp ops store;
`embed_query` raises `ModelUnavailable`, so dedup falls back to `query_set` mode, logs
`harness.dedup.degraded` (WARNING) and merges the lower-scored finding into the other.
A second run is idempotent (merged findings are no longer `proposed`).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import numpy as np
import pytest
import structlog
from tests.unit.harness import _blackboard_env as env_mod
from tests.unit.harness._blackboard_env import BbEnv, args

from herness.core.errors import ModelUnavailable
from herness.harness.pipelines.settings import PipelinesConfig
from herness.harness.swarm import dedup
from herness.store.ops import get_findings
from herness.store.ops.runs import get_run

if TYPE_CHECKING:
    from herness.harness.swarm.routing import RunEnv

pytestmark = pytest.mark.integration

bb_env = env_mod.bb_env  # fixtures
test_redactor = env_mod.test_redactor


def test_it06_27_embedding_failure_falls_back_to_query_set(
    bb_env: BbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-27 embedding stub raising → `query_set` fallback logged; duplicates merged."""

    def raising(text: str) -> np.ndarray:
        msg = "embedding model unavailable"
        raise ModelUnavailable(msg)

    monkeypatch.setattr(dedup, "embed_query", raising)
    ctx = bb_env.ctx()
    keep = bb_env.bb.post(ctx, **args(bb_env.ops_qid, confidence=0.9))
    dup = bb_env.bb.post(ctx, **args(bb_env.ops_qid, claim="Team t1 saw [[n1]] incidents"))
    run = get_run(bb_env.run_id)
    assert run is not None
    assert run.depth == "standard"
    env = cast("RunEnv", SimpleNamespace(run=run, bb=bb_env.bb, cfg=PipelinesConfig()))
    with structlog.testing.capture_logs() as logs:
        merged = asyncio.run(dedup.run_dedup(env))
    assert merged == 1
    degraded = [e for e in logs if e["event"] == "harness.dedup.degraded"]
    assert len(degraded) == 1
    assert degraded[0]["log_level"] == "warning"
    assert degraded[0]["run_id"] == bb_env.run_id
    done = [e for e in logs if e["event"] == "harness.dedup.completed"]
    assert (done[0]["mode"], done[0]["merged"]) == ("query_set", 1)
    rows = get_findings([keep, dup])
    assert rows[keep].status == "proposed"
    assert (rows[dup].status, rows[dup].merged_into) == ("merged", keep)
    assert asyncio.run(dedup.run_dedup(env)) == 0
