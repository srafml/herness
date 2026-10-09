"""Writer-output, plan-context, pipeline and task-row factories for the T06-19 draft tests."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from tests.unit.harness.swarm._swarm_findings import RUN_ID, T0, fid, knobs, qid, tid

from herness.core.types import (
    EntityScope,
    Finding,
    RankedEntity,
    ReportDraft,
    TaskBudget,
    TaskSpec,
    VerificationResult,
)
from herness.harness.findings import compute_dedup_key
from herness.harness.pipelines.base import PlanContext, build_ranked_entities
from herness.store.ops.runs import TaskRow

BUILD_ID = "20260925-101500-ABCDEF"


class StubPipeline:
    """Priority = confidence; ranked entities from the recommendation targets (candidates)."""

    kind = "funding_review"

    def __init__(self, must: set[str] | None = None) -> None:
        self.must = set(must or set())

    def challenge_priority(self, f: Finding, ctx: object) -> float:
        del ctx
        return f.confidence

    def must_cover(self, ctx: object) -> set[str]:
        del ctx
        return set(self.must)

    def ranked_entities(self, draft: ReportDraft) -> list[RankedEntity]:
        return build_ranked_entities(draft, "candidate", [])


def plan_ctx(
    *, dq: bool = False, unconfirmed: bool = False, portfolio_qids: list[str] | None = None
) -> PlanContext:
    dq_rows: list[dict[str, Any]] = [
        {"check_name": "fresh", "severity": "warn", "value": 1, "threshold": 0, "details": None,
         "query_id": qid(90)}
    ] if dq else []  # fmt: skip
    return PlanContext.model_validate(
        {
            "run_id": RUN_ID, "kind": "funding_review", "depth": "standard", "profile": "local",
            "build_id": BUILD_ID, "question": None, "focus": None, "knobs": knobs("standard"),
            "dq_warnings": dq_rows, "unconfirmed_weights": ["w1"] if unconfirmed else [],
            "prior_context": "", "prior_recs": [],
            "portfolio": {"scenario": "", "rows": [], "selected": [],
                          "query_ids": portfolio_qids or [], "custom": [{"x": 1}]},
        }
    )  # fmt: skip


def vresult(passed: bool = True, wheres: tuple[str, ...] = ()) -> VerificationResult:
    """A verification result whose items at `wheres` fail (an uncited numeral each)."""
    items = [
        {"where": w, "passed": False, "checks": [],
         "uncited": [{"text": "2", "start": 0, "end": 1}],
         "unknown_markers": [], "bad_refs": [], "unverified_findings": []}
        for w in wheres
    ]  # fmt: skip
    return VerificationResult.model_validate(
        {"build_id": BUILD_ID, "passed": passed and not wheres, "items": items, "n_numbers": 0,
         "n_failed": 0, "verified_at": T0, "duration_ms": 1}
    )  # fmt: skip


def nref(
    n: int, *, q: int = 1, unit: str = "count", column: str = "incidents", value: Any = None
) -> dict[str, Any]:
    v = value if value is not None else ("1200.50" if unit == "usd" else 7)
    return {"id": f"n{n}", "value": v, "unit": unit, "query_id": qid(q), "column": column,
            "row_key": None}  # fmt: skip


def para(
    text: str = "Team t1 had [[n1]] incidents", *, q: int = 1, fids: list[str] | None = None
) -> dict[str, Any]:
    return {"text": text, "numbers": [nref(1, q=q)], "finding_ids": fids or [fid(1)]}


def fund_rec(target: str = "c1", **over: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "kind": "fund", "target_type": "candidate", "target_id": target,
        "headline": "Fund the cache rewrite", "summary": "It saves [[n1]] a year",
        "numbers": [nref(1, q=11, unit="usd", column="expected_impact_usd"),
                    nref(2, q=12, unit="usd", column="effort_cost_usd"),
                    nref(3, q=12, unit="score", column="confidence", value=0.7)],
        "expected_usd_ref": "n1", "effort_usd_ref": "n2", "confidence_ref": "n3",
        "finding_ids": [fid(1)], "query_ids": [],
    }  # fmt: skip
    return rec | over


def org_rec(target: str = "t1", **over: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "kind": "org_action", "target_type": "team", "target_id": target,
        "headline": "Cut MTTR", "summary": "Saves [[n1]]",
        "numbers": [nref(1, q=21, unit="usd", column="delta_usd", value="900.00"),
                    nref(2, q=21, unit="usd", column="delta_usd", value="300.00")],
        "expected_metric": "mttr", "expected_usd_ref": "n1",
        "action_levers": [
            {"entity_type": "team", "entity_id": target, "metric": "mttr", "delta_usd_ref": "n1"},
            {"entity_type": "team", "entity_id": target, "metric": "reopen", "delta_usd_ref": "n2"},
        ],
        "finding_ids": [fid(1)], "query_ids": [],
    }  # fmt: skip
    return rec | over


def writer_out(**over: Any) -> BaseModel:
    data: dict[str, Any] = {
        "title": "Funding review for Q3 2026",
        "sections": [{"id": "executive_summary", "title": "Summary", "paragraphs": [para()]}],
        "recommendations": [fund_rec()], "caveats": ["Data covers ninety days"],
        "prior_outcomes_commentary": None,
    }  # fmt: skip
    return ReportDraft.writer_output_model().model_validate(data | over)


# The spec is always an analyst spec (skeptic specs need a target); `role` sets the row role.
def task_row(  # noqa: PLR0913 - test factory with keyword defaults
    i: int,
    *,
    role: str = "analyst",
    status: str = "done",
    entity: tuple[str, str] = ("candidate", "c1"),
    must_cover: bool = False,
    specialty: str = "general",
    round_no: int = 0,
    result: dict[str, Any] | None = None,
    last_error: dict[str, Any] | None = None,
) -> TaskRow:
    scope = EntityScope(entity_type=entity[0], entity_ids=[entity[1]])  # type: ignore[arg-type]
    spec = TaskSpec(
        task_id=tid(i), run_id=RUN_ID, role="analyst", specialty=specialty,  # type: ignore[arg-type]
        objective=f"objective {i}", scope=scope, tools=[],
        budget=TaskBudget(max_steps=5, max_tokens=5_000, wall_clock_s=60), model_role="analyst",
        must_cover=must_cover, round=round_no,
        dedup_key=compute_dedup_key("analyst", specialty, scope, f"objective {i}"),  # type: ignore[arg-type]
    )  # fmt: skip
    res = result if result is not None else {"partial": False, "finding_ids": [f"x{i}"]}
    return TaskRow(
        task_id=spec.task_id, run_id=RUN_ID, parent_task_id=None, role=role, spec=spec,
        status=status, attempts=1, last_error=last_error, checkpoint=None,
        result=res if status == "done" else None, created_at=T0, updated_at=T0,
    )  # fmt: skip
