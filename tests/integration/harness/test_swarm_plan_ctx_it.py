"""IT06-35: `build_plan_context` over a real build and the real recording path (T06-15, U06-87).

A local build file holds `meta.dq_result` (two failing checks, one passing) and a stored
`score.portfolio` scenario. The reader is spec 05 `execute_recorded` bound to a real
`ToolContext` (the T06-11 pipeline-test pattern), so every read has a query id and an evidence
row. Memory returns a spec 07 `PriorContext` with recommendations of three prior runs; the
optimizer is an injected `portfolio_fn` that records its calls.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import JsonValue
from tests.support import tools_standin as sd
from tests.support.harness_fakes import FakeOps

from herness.core.resilience import ProcessState
from herness.core.types import MemoryRunContext, PriorContext, PriorRecommendation
from herness.harness import tools
from herness.harness.llm.settings import SqlSettings
from herness.harness.pipelines.settings import PipelinesConfig, resolve_knobs
from herness.harness.swarm.planner import build_plan_context
from herness.harness.tools import RecordedResult
from herness.harness.warehouse import open_warehouse
from herness.metrics.portfolio import PortfolioResult, PortfolioRow, Scenario
from herness.store.ops import RunRow

pytestmark = pytest.mark.integration

_ULID = "01J9ZQ4Y8M6V3K2N1P0R5T7W9"
_RUN = f"run_{_ULID}A"
_TASK = f"task_{_ULID}A"
_T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
_RENDERED = '<untrusted_data source="memory" record_id="m1">prior recs</untrusted_data>'


def _make_build(warehouse_dir: Path) -> None:
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(warehouse_dir / f"wh-{sd.BUILD_ID}.duckdb"))
    for schema in ("meta", "score"):
        con.execute(f"CREATE SCHEMA {schema}")
    con.execute(
        "CREATE TABLE meta.dq_result (check_name VARCHAR, severity VARCHAR, value DOUBLE,"
        " threshold DOUBLE, passed BOOLEAN, details JSON)"
    )
    con.executemany(
        "INSERT INTO meta.dq_result VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("orphan_links", "warn", 0.07, 0.05, False, '{"table": "enrich.link"}'),
            ("coverage", "error", 0.4, 0.9, False, '{"table": "core.incident"}'),
            ("freshness", "warn", 1.0, 2.0, True, "{}"),
        ],
    )
    con.execute(
        "CREATE TABLE score.portfolio (scenario VARCHAR, budget_usd DECIMAL(18,2),"
        " candidate_id VARCHAR, selected BOOLEAN, order_rank INTEGER,"
        " expected_impact_usd DECIMAL(18,2), solver_status VARCHAR, flags VARCHAR[],"
        " query_ids VARCHAR[])"
    )
    con.executemany(
        "INSERT INTO score.portfolio VALUES ('base', 1000, ?, ?, ?, ?, 'OPTIMAL', [], [])",
        [("e02", False, None, 10), ("e01", True, 1, 500), ("e03", True, 2, 300)],
    )
    con.close()


class _Recorded:
    """`RecordedReader` over `execute_recorded`; keeps every query id."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.qids: list[str] = []

    def __call__(self, sql: str, params: dict[str, JsonValue]) -> RecordedResult:
        result = tools.execute_recorded(self.ctx, sql, params)
        self.qids.append(result.query_id)
        return result


@pytest.fixture
def reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState
) -> Iterator[tuple[_Recorded, FakeOps]]:
    del reset_process_state
    sd.patch_config(monkeypatch)
    _make_build(tmp_path)
    handle = open_warehouse(sd.BUILD_ID, warehouse_dir=tmp_path, sql=SqlSettings())
    try:
        ops = FakeOps()
        yield _Recorded(sd.make_ctx(handle, ops)), ops
    finally:
        handle.close()


def _rec(rec_id: str, run_id: str) -> PriorRecommendation:
    return PriorRecommendation.model_validate(
        {
            "rec_id": rec_id, "run_id": run_id, "kind": "fund", "target_type": "candidate",
            "target_id": "e01", "summary": "fund e01", "numbers": [], "expected_metric": None,
            "confidence": 0.7, "decision": None, "decided_at": None, "effective_at": None,
            "outcome": None, "next_measurement_due": None,
        }
    )  # fmt: skip


class _Memory:
    def __init__(self) -> None:
        self.calls: list[MemoryRunContext] = []

    def prior_context(self, run_ctx: MemoryRunContext) -> PriorContext:
        self.calls.append(run_ctx)
        items = [_rec("rec_01", "run_old"), _rec("rec_02", "run_mid"), _rec("rec_03", "run_new"),
                 _rec("rec_04", "run_mid")]  # fmt: skip
        return PriorContext(items=items, rendered=_RENDERED, memory_ids=[], tally={})


def _solved(scenario: str) -> PortfolioResult:
    return PortfolioResult(
        scenario=scenario, budget_usd=Decimal(100), solver_status="OPTIMAL",
        rows=[PortfolioRow(candidate_id="e09", selected=True, order_rank=1,
                           expected_impact_usd=Decimal(42), flags=[])],
        selected=["e09"], total_effort_usd=Decimal(90), total_expected_impact_usd=Decimal(42),
        binding_constraints=[], query_ids=["q_" + "a" * 16], flags=[],
    )  # fmt: skip


def _run(kind: str, scenarios: list[object]) -> RunRow:
    request = {"kind": kind, "question": "What next?", "focus": None, "scenarios": scenarios}
    return RunRow(
        run_id=_RUN, kind=kind, depth="standard", profile="local", build_id=sd.BUILD_ID,
        status="planning", started_at=_T0, finished_at=None, token_usage={},
        cost_usd=Decimal(0), config_hash="h", meta={"request": request},
    )  # fmt: skip


def _build(run: RunRow, rd: _Recorded, memory: _Memory, calls: list[tuple[Any, dict[str, Any]]]):  # type: ignore[no-untyped-def]
    def portfolio_fn(entry: Any, **kw: Any) -> PortfolioResult:
        calls.append((entry, kw))
        return _solved(entry.name if isinstance(entry, Scenario) else str(entry))

    kind = "org_review" if run.kind == "org_review" else "funding_review"
    knobs = resolve_knobs(PipelinesConfig(), kind, "standard")
    return asyncio.run(
        build_plan_context(
            run, knobs, reader=rd, memory=memory, portfolio_fn=portfolio_fn,  # type: ignore[arg-type]
            unconfirmed_weights=["w_delivery"], pipelines_cfg=PipelinesConfig(),
            planner_task_id=_TASK,
        )
    )  # fmt: skip


def test_it06_35_context_fields_filled_with_query_ids(reader: tuple[_Recorded, FakeOps]) -> None:
    """IT06-35 DQ fails, prior recs of the two latest runs, the stored scenario first and the
    custom scenarios solved without persisting; every read carries its query id."""
    rd, ops = reader
    memory = _Memory()
    calls: list[tuple[Any, dict[str, Any]]] = []
    custom = {"name": "tight", "budget_usd": "100"}
    ctx = _build(_run("funding_review", ["base", custom, "unknown"]), rd, memory, calls)
    dq_qid, _names_qid, rows_qid = rd.qids
    assert [w["check_name"] for w in ctx.dq_warnings] == ["coverage", "orphan_links"]
    assert {w["query_id"] for w in ctx.dq_warnings} == {dq_qid}
    assert ctx.dq_warnings[0]["severity"] == "error"
    assert ctx.prior_context == _RENDERED
    assert [r["rec_id"] for r in ctx.prior_recs] == ["rec_02", "rec_03", "rec_04"]
    (mctx,) = memory.calls
    assert (mctx.role, mctx.task_id, mctx.run_kind, mctx.build_id) == (
        "planner",
        _TASK,
        "funding_review",
        sd.BUILD_ID,
    )
    portfolio = ctx.portfolio
    assert portfolio["scenario"] == "base"
    assert portfolio["query_ids"] == [rows_qid]
    assert [r["candidate_id"] for r in portfolio["rows"]] == ["e01", "e03", "e02"]
    assert portfolio["selected"] == ["e01", "e03"]
    assert [c["scenario"] for c in portfolio["custom"]] == ["tight", "unknown"]  # type: ignore[call-overload, index]
    assert [kw for _, kw in calls] == [
        {"persist": False, "build_id": sd.BUILD_ID, "run_id": _RUN}
    ] * 2
    assert isinstance(calls[0][0], Scenario)
    assert calls[1][0] == "unknown"
    assert ctx.unconfirmed_weights == ["w_delivery"]
    assert (ctx.question, ctx.focus, ctx.kind) == ("What next?", None, "funding_review")
    assert set(rd.qids) <= set(ops.evidence)  # every read recorded


def test_it06_35_custom_first_and_default_scenario(reader: tuple[_Recorded, FakeOps]) -> None:
    """IT06-35 a custom first entry defines the portfolio from the optimizer result; no
    scenarios fall back to `portfolio_scenario`; org runs read no portfolio."""
    rd, _ops = reader
    calls: list[tuple[Any, dict[str, Any]]] = []
    ctx = _build(_run("funding_review", ["missing"]), rd, _Memory(), calls)
    assert ctx.portfolio["scenario"] == "missing"
    assert ctx.portfolio["selected"] == ["e09"]
    assert ctx.portfolio["query_ids"] == ["q_" + "a" * 16]
    default = _build(_run("funding_review", []), rd, _Memory(), calls)
    assert default.portfolio["scenario"] == "base"  # PipelinesConfig default scenario
    assert len(calls) == 1
    n_reads = len(rd.qids)
    org = _build(_run("org_review", ["base"]), rd, _Memory(), calls)
    assert org.portfolio == {"scenario": "", "rows": [], "selected": [], "query_ids": [],
                             "custom": []}  # fmt: skip
    assert len(rd.qids) == n_reads + 1  # the DQ read only
