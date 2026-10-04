"""Plan context assembly of the planner step (impl 06 U06-87; design 06 §5.2 Inputs, Scenarios).

Private sibling of `herness.harness.swarm.planner`, which re-exports `build_plan_context`
(T06-15 split, §2 module map). Every warehouse read goes through the run's `RecordedReader`
(design 06 §3.7), so each one has a query id and an evidence row; cells are stored JSON-safe.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Final, cast

from pydantic import JsonValue

from herness.core.errors import ConfigError
from herness.core.types import MemoryRunContext, PriorRecommendation
from herness.harness.pipelines.base import PlanContext
from herness.harness.tools import json_safe
from herness.metrics.portfolio import PortfolioResult, Scenario

if TYPE_CHECKING:
    from herness.harness.memory import MemoryStore
    from herness.harness.pipelines.base import RecordedReader
    from herness.harness.pipelines.settings import DepthKnobs, PipelinesConfig
    from herness.store.ops import RunRow

__all__ = ["build_plan_context"]

DQ_SQL: Final = (
    "SELECT check_name, severity, value, threshold, details FROM meta.dq_result"
    " WHERE passed = false ORDER BY check_name"
)
SCENARIOS_SQL: Final = "SELECT DISTINCT scenario FROM score.portfolio"
PORTFOLIO_SQL: Final = (
    "SELECT * FROM score.portfolio WHERE scenario = $scenario"
    " ORDER BY order_rank NULLS LAST, candidate_id"
)
PRIOR_RUNS: Final = 2

type _Row = dict[str, JsonValue]
type _Entry = Scenario | str


def _read(reader: RecordedReader, sql: str, params: dict[str, JsonValue]) -> tuple[str, list[_Row]]:
    result = reader(sql, params)
    rows = [
        {col: json_safe(cell) for col, cell in zip(result.columns, row, strict=True)}
        for row in result.rows
    ]
    return result.query_id, rows


def _dq_warnings(reader: RecordedReader) -> list[_Row]:
    qid, rows = _read(reader, DQ_SQL, {})
    return [row | {"query_id": qid} for row in rows]


def _prior_recs(items: Sequence[PriorRecommendation]) -> list[_Row]:
    """Items of the two most recent distinct prior runs (by `rec_id`), as JSON."""
    runs: list[str] = []
    for item in sorted(items, key=lambda i: i.rec_id, reverse=True):
        if item.run_id not in runs:
            runs.append(item.run_id)
    recent = set(runs[:PRIOR_RUNS])
    return [cast("_Row", item.model_dump(mode="json")) for item in items if item.run_id in recent]


def _entries(run: RunRow, cfg: PipelinesConfig) -> list[_Entry]:
    request = run.meta.get("request")
    raw = request.get("scenarios") if isinstance(request, Mapping) else None
    entries: list[_Entry] = [
        value if isinstance(value, str) else Scenario.model_validate(value)
        for value in (raw if isinstance(raw, list) else [])
    ]
    return entries or [cfg.pipelines.funding_review.portfolio_scenario]


def _portfolio(
    run: RunRow,
    reader: RecordedReader,
    portfolio_fn: Callable[..., PortfolioResult],
    cfg: PipelinesConfig,
) -> dict[str, JsonValue]:
    """U06-87 step 3: stored scenarios read, the others solved without persisting."""
    portfolio: dict[str, JsonValue] = {
        "scenario": "", "rows": [], "selected": [], "query_ids": [], "custom": []
    }  # fmt: skip
    if run.kind != "funding_review":
        return portfolio
    _qid, names = _read(reader, SCENARIOS_SQL, {})
    stored = {str(row["scenario"]) for row in names}
    custom: list[JsonValue] = []
    for i, entry in enumerate(_entries(run, cfg)):
        if isinstance(entry, str) and entry in stored:
            qid, rows = _read(reader, PORTFOLIO_SQL, {"scenario": entry})
            first: dict[str, JsonValue] = {
                "scenario": entry, "rows": list(rows), "query_ids": [qid],
                "selected": [str(r["candidate_id"]) for r in rows if r.get("selected") is True],
            }  # fmt: skip
        else:
            res = portfolio_fn(entry, persist=False, build_id=run.build_id, run_id=run.run_id)
            dumped = cast("_Row", res.model_dump(mode="json"))
            custom.append(dumped)
            first = {
                "scenario": res.scenario, "rows": dumped["rows"],
                "selected": list(res.selected), "query_ids": list(res.query_ids),
            }  # fmt: skip
        if i == 0:
            portfolio.update(first)
    portfolio["custom"] = custom
    return portfolio


async def build_plan_context(  # noqa: PLR0913 - U06-87 signature
    run: RunRow,
    knobs: DepthKnobs,
    *,
    reader: RecordedReader,
    memory: MemoryStore,
    portfolio_fn: Callable[..., PortfolioResult],
    unconfirmed_weights: Sequence[str],
    pipelines_cfg: PipelinesConfig,
    planner_task_id: str,
) -> PlanContext:
    """Assemble the run's `PlanContext` (U06-87); blocking reads run in threads.

    `QueryError`, spec 04 `ConfigError` and `ModelUnavailable` from memory propagate.
    `unconfirmed_weights` is spec 04 `unconfirmed_blocks(cfg.weights, cfg.weights.blocks())`.
    """
    if run.build_id is None:
        msg = f"run {run.run_id} has no build_id"
        raise ConfigError(msg)
    dq = await asyncio.to_thread(_dq_warnings, reader)
    run_ctx = MemoryRunContext(
        run_id=run.run_id, run_kind=run.kind, role="planner", task_id=planner_task_id,
        build_id=run.build_id, profile=run.profile,
    )  # fmt: skip
    pc = await asyncio.to_thread(memory.prior_context, run_ctx)
    portfolio = await asyncio.to_thread(_portfolio, run, reader, portfolio_fn, pipelines_cfg)
    request = run.meta.get("request")
    req = request if isinstance(request, Mapping) else {}
    return PlanContext.model_validate(
        {
            "run_id": run.run_id, "kind": run.kind, "depth": run.depth, "profile": run.profile,
            "build_id": run.build_id, "question": req.get("question"), "focus": req.get("focus"),
            "knobs": knobs, "dq_warnings": dq, "unconfirmed_weights": list(unconfirmed_weights),
            "prior_context": pc.rendered, "prior_recs": _prior_recs(pc.items),
            "portfolio": portfolio,
        }
    )  # fmt: skip
