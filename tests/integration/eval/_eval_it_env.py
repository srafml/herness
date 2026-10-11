"""Integration environment of the eval runner (T11-30: IT11-20, IT11-21, IT11-22).

Reviews run through the real swarm driver of the T06-22 run-driver setup (`drv`: real run
steps, Blackboard, findings, drafts, run rows; scripted agents and fakes for the Verifier,
renderer and warehouse pool, as in IT06-37), pinned to the `tiny_build` warehouse (seed 7
tiny, built through the real pipeline). The warehouse file and `CURRENT` are copied under
the test's data root, the truth manifest beside it. The scripted agents' SQL is rebound to
statements that run on the tiny build, and their evidence is recorded for its build id, so
`count_unsupported` re-runs every cited number and finds it supported. Chat answers come
from `AnswerChat` citing the same evidence. Golden content and `tests/eval/mock_scripts`
belong to T11-32: suites are written per test.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.support.builds import BuildHandle
from tests.support.ops_store import OpsStoreHandle
from tests.unit.eval._golden_fixtures import write_suite
from tests.unit.eval._runner_env import AnswerChat, FakeCtx, settings
from tests.unit.harness._blackboard_env import FakeWarehouse

from herness.core import time as clock
from herness.eval import runner
from herness.eval.runner import EvalDeps
from herness.store.ops import record_evidence

__all__ = ["EvalEnv", "eval_env", "question"]

INCIDENT_SQL = "SELECT 7 AS incidents FROM core.team WHERE $team IS NOT NULL LIMIT 1"
FUNDING_SQL = (
    "SELECT CAST(300.00 AS DECIMAL(10, 2)) AS effort_cost_usd, CAST(0.7 AS DOUBLE) AS confidence"
    " FROM core.team WHERE $candidate IS NOT NULL LIMIT 1"
)
REFERENCE_SQL = "SELECT 7 AS value FROM core.team LIMIT 1"
CHAT_TEXT = "Team t1 had [[n1]] incidents."


def question(qid: str, pipeline: str = "chat", **fields: Any) -> dict[str, Any]:
    """A question graded by `REFERENCE_SQL` (7, exact) and a mention of team `t1`."""
    raw: dict[str, Any] = {
        "id": qid, "pipeline": pipeline, "question": f"How many incidents had team t1 ({qid})?",
        "expected": {
            "numeric": {"reference_sql": REFERENCE_SQL, "unit": "count", "tolerance": {"abs": 0}},
            "must_mention": ["t1"],
        },
    }  # fmt: skip
    raw.update(fields)
    return raw


@dataclass
class EvalEnv:
    """One eval setup on the tiny build: data root, warehouse, swarm driver and chat."""

    data_root: Path
    build: BuildHandle
    drv: Drv
    chat: AnswerChat
    con: duckdb.DuckDBPyConnection
    tmp: Path

    def suite(self, questions: list[dict[str, Any]]) -> Path:
        return write_suite(self.tmp / "suite.yaml", questions)

    def deps(self, ctx: Any) -> EvalDeps:
        build_id = self.build.build_id
        swarm = self.drv.swarm(current_build=lambda: build_id, build_exists=lambda b: b == build_id)
        return EvalDeps(
            warehouse_for=lambda b: self.con, swarm_for=lambda profile: swarm,
            chat_for=lambda profile: self.chat,  # type: ignore[arg-type,return-value]
            judge_for=lambda: None, memory=None, job=ctx, clock=clock.now,
            data_root=self.data_root, settings=settings(), repo_root=self.tmp,
        )  # fmt: skip

    def handle(self, payload: dict[str, Any]) -> tuple[FakeCtx, Any]:
        """Bind the deps factory, run `handle_eval` in-process (the `--inline` path, R-45)."""
        ctx = FakeCtx(payload)
        runner.set_deps_factory(self.deps)
        return ctx, runner.handle_eval(ctx)  # type: ignore[arg-type]

    def out_dir(self, run_id: object) -> Path:
        return self.data_root / "reports" / "eval" / str(run_id)


@pytest.fixture
def eval_env(
    drv: Drv,
    ops_store: OpsStoreHandle,
    tiny_build: BuildHandle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[EvalEnv]:
    data_root = ops_store.data_root
    wh = data_root / "warehouse"
    wh.mkdir(exist_ok=True)
    copy = wh / tiny_build.warehouse.name
    shutil.copyfile(tiny_build.warehouse, copy)
    (wh / "CURRENT").write_text(tiny_build.build_id, encoding="ascii")
    shutil.copytree(tiny_build.root / "truth", data_root.parent / "truth")
    monkeypatch.setattr(e, "BUILD_ID", tiny_build.build_id)
    drv.pool.warehouse = FakeWarehouse(tiny_build.build_id)  # the agents' tool context
    monkeypatch.setattr(e, "INCIDENT_SQL", INCIDENT_SQL)
    monkeypatch.setattr(e, "FUNDING_SQL", FUNDING_SQL)
    for team in ("t1", "t2"):
        assert record_evidence(e._evidence(INCIDENT_SQL, {"team": team}, {"incidents": 7}))
    sample = {"effort_cost_usd": "300.00", "confidence": 0.7}
    assert record_evidence(e._evidence(FUNDING_SQL, {"candidate": "c1"}, sample))
    monkeypatch.setattr(runner._Binding, "factory", None)
    chat = AnswerChat(value=7, query_id=e.incident_qid("t1"), column="incidents", text=CHAT_TEXT)
    con = duckdb.connect(str(copy), read_only=True)
    try:
        yield EvalEnv(data_root, tiny_build, drv, chat, con, tmp_path)
    finally:
        con.close()
