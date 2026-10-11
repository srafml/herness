"""Shared set-up of the impl 07 integration acceptance rows (T07-24; IT07-01 … IT07-10).

Not a test module; no side effects at import. `memory_store` builds the real spec 07
`MemoryStore` facade (U07-97) on the test's migrated ops store and LanceDB in tmp, with the
deterministic hash-seeded fake embedder and the test redactor of the memory unit helpers
(`tests.unit.harness.memory._write_env`), as IT06-37 and IT11-23 do. `pin_tiny` copies the
`tiny_build` warehouse and `CURRENT` under the test's data root and pins the T06-22 run-driver
set-up (`drv`) to its build id (the `tests.integration.eval._eval_it_env` pattern).
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.unit.harness.memory._write_env import PATTERNS, Env, FakeEmbed, make_writer

from herness.harness.memory import MemoryStore
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder
from herness.store.ops import core, record_evidence

if TYPE_CHECKING:
    import duckdb
    import pytest
    from tests.integration.harness._swarm_run_env import Drv
    from tests.support.builds import BuildHandle

    from herness.core.types import Evidence
    from herness.harness.swarm.lifecycle import RunResult
    from herness.harness.swarm.run import Swarm

__all__ = [
    "Mem", "memory_store", "pin_build", "pin_tiny", "real_prior_context", "record_portfolio",
    "record_query", "run_review", "run_summaries", "swarm_for",
]  # fmt: skip


@dataclass
class Mem:
    """The real facade and the fakes under it (embedder calls, vectors, redactor)."""

    store: MemoryStore
    env: Env

    @property
    def embed(self) -> FakeEmbed:
        return self.env.embed


def memory_store(
    tmp_path: Path, data_root: Path, *, allowed: Sequence[str] = (r"(INC|CHG|PRB)\d+",),
    cfg: MemoryConfig | None = None, llms: Any = None,
) -> Mem:  # fmt: skip
    """The U07-97 facade on `core.connection` (the test's ops store) and LanceDB in tmp."""
    cfg = cfg or MemoryConfig(injection_patterns=PATTERNS)
    env = make_writer(tmp_path / "memory", cfg=cfg)
    store = MemoryStore(
        cfg, conn_factory=core.connection, vectors=env.vectors,
        embedder=Embedder(env.embed, model_name="bge-m3"), redactor=env.redactor, llms=llms,
        allowed_numeral_patterns=allowed, data_root=data_root,
    )  # fmt: skip
    return Mem(store, env)


def record_portfolio() -> None:
    """The portfolio evidence row the scripted Writer's recommendation cites (IT06-37)."""
    from tests.integration.harness import _swarm_run_env as e  # noqa: PLC0415

    portfolio = e._evidence("SELECT 1 AS expected_impact_usd", {}, {"expected_impact_usd": 1200})
    assert record_evidence(portfolio.model_copy(update={"query_id": e.PORTFOLIO_QID}))


def swarm_for(drv: Drv, memory: object, **deps: Any) -> Swarm:
    """A `Swarm` over the driver's deps with `memory` in place of the fake; closed by `drv`."""
    import dataclasses  # noqa: PLC0415

    from herness.harness.swarm.run import Swarm  # noqa: PLC0415

    swarm = Swarm(drv.hcfg.pipelines, drv.llms, memory,  # type: ignore[arg-type]
                  deps=dataclasses.replace(drv.deps, **deps) if deps else drv.deps)  # fmt: skip
    drv.swarms.append(swarm)
    return swarm


def run_review(drv: Drv, memory: object, kind: str = "funding_review",
               depth: str = "standard", **request: Any) -> RunResult:  # fmt: skip
    """One review run to its end through the real driver; the result."""
    swarm = swarm_for(drv, memory)
    return asyncio.run(swarm.start(drv.request(kind=kind, depth=depth, **request)))


def run_summaries(run_id: str) -> list[dict[str, Any]]:
    """The `run_summary` items of `run_id` (provenance.run_id)."""
    rows = core.read_all(
        "SELECT memory_id, content, data FROM memory_item WHERE kind = 'run_summary'"
        " AND json_extract(provenance, '$.run_id') = ?", (run_id,),
    )  # fmt: skip
    return [dict(r) for r in rows]


def record_query(con: duckdb.DuckDBPyConnection, sql: str, params: dict[str, Any],
                 build_id: str) -> Evidence:  # fmt: skip
    """Run `sql` on the build and record its evidence row with the real result hash and the
    full result as sample (what the spec 05 recording path stores; the Verifier re-runs it)."""
    from herness.core import time as clock  # noqa: PLC0415
    from herness.core.ids import query_id  # noqa: PLC0415
    from herness.core.types import Evidence  # noqa: PLC0415
    from herness.metrics.evidence import result_hash  # noqa: PLC0415

    cur = con.cursor()
    try:
        cur.execute(sql, params)
        types = [(str(d[0]), str(d[1])) for d in cur.description or ()]
        rows = cur.fetchall()
    finally:
        cur.close()
    sample = [{name: _json_cell(v) for (name, _), v in zip(types, row, strict=True)}
              for row in rows]  # fmt: skip
    ev = Evidence(
        query_id=query_id(sql, params, build_id), run_id=None, build_id=build_id, sql=sql,
        params=params, result_hash=result_hash(types, rows), row_count=len(rows),
        result_sample=sample, executed_at=clock.now(), duration_ms=1,
    )  # fmt: skip
    assert record_evidence(ev)
    return ev


def _json_cell(value: object) -> object:
    from decimal import Decimal  # noqa: PLC0415

    return str(value) if isinstance(value, Decimal) else value


def pin_build(drv: Drv, build_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the driver to `build_id` (whose warehouse is `CURRENT` under the data root) and
    record the driver's evidence rows again for it."""
    from tests.integration.harness import _swarm_run_env as e  # noqa: PLC0415
    from tests.unit.harness._blackboard_env import FakeWarehouse  # noqa: PLC0415

    monkeypatch.setattr(e, "BUILD_ID", build_id)
    drv.pool.warehouse = FakeWarehouse(build_id)
    for team in ("t1", "t2"):
        assert record_evidence(e._evidence(e.INCIDENT_SQL, {"team": team}, {"incidents": 7}))
    sample = {"effort_cost_usd": 300, "confidence": 0.7}
    assert record_evidence(e._evidence(e.FUNDING_SQL, {"candidate": "c1"}, sample))


def pin_tiny(drv: Drv, data_root: Path, build: BuildHandle, monkeypatch: pytest.MonkeyPatch
             ) -> Path:  # fmt: skip
    """Copy the tiny warehouse and `CURRENT` under `data_root`, pin the driver to its id."""
    wh = data_root / "warehouse"
    wh.mkdir(exist_ok=True)
    copy = wh / build.warehouse.name
    shutil.copyfile(build.warehouse, copy)
    (wh / "CURRENT").write_text(build.build_id, encoding="ascii")
    pin_build(drv, build.build_id, monkeypatch)
    return copy


def real_prior_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """The driver's plan context with the real `MemoryStore.prior_context` and `_prior_recs`
    (U07-81 via `_plan_ctx`), the real U06-71 planner and Writer inputs (IT11-23 pattern)."""
    from tests.integration.harness import _swarm_run_env as e  # noqa: PLC0415

    from herness.core.types import MemoryRunContext  # noqa: PLC0415
    from herness.harness.pipelines import _review_common  # noqa: PLC0415
    from herness.harness.swarm import _plan_ctx, _run_steps  # noqa: PLC0415

    async def plan_context(run: Any, knobs: Any, **kw: Any) -> Any:
        del knobs
        run_ctx = MemoryRunContext(
            run_id=run.run_id, run_kind=run.kind, role="planner",
            task_id=kw["planner_task_id"], build_id=run.build_id, profile=run.profile,
        )  # fmt: skip
        pc = await asyncio.to_thread(kw["memory"].prior_context, run_ctx)
        return e.plan_ctx_for(run).model_copy(
            update={"prior_context": pc.rendered, "prior_recs": _plan_ctx._prior_recs(pc.items)}
        )

    def planner_input(self: Any, ctx: Any, tasks: Any) -> dict[str, object]:
        return _review_common.planner_input(self.kind, ctx, tasks, {})

    def writer_input(self: Any, ctx: Any, verified: list[Any]) -> dict[str, object]:
        return _review_common.writer_input(self.kind, ctx, ["executive_summary"], verified)

    monkeypatch.setattr(_run_steps, "build_plan_context", plan_context)
    monkeypatch.setattr(e.StubPipeline, "planner_input", planner_input)
    monkeypatch.setattr(e.StubPipeline, "writer_input", writer_input)
