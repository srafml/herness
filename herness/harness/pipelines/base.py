"""Review pipeline contract, plan context and shared helpers (impl 06 U06-66 … U06-70, U06-75).

`Pipeline` is the contract `get_pipeline` resolves at runtime (design 06 §3.4); `PlanContext`
carries everything one pipeline needs to plan and write a run. The shared helpers are pure:
`default_challenge_priority` (§5.7) and `build_ranked_entities`/`to_recommendation_drafts`
(§4.4, spec 07 §3.2, R-30). `get_pipeline` lazily imports `herness.harness.pipelines.funding_review`
or `.org_review` (§3.7), so importing this module imports neither.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from importlib import import_module
from typing import Final, Literal, Protocol, TypedDict, runtime_checkable

from pydantic import BaseModel, ConfigDict, JsonValue, with_config

from herness.core.errors import ConfigError, ReportContractError
from herness.core.types import (
    Depth,
    EntityScope,
    Finding,
    RankedEntity,
    RecommendationDraft,
    ReportDraft,
    RunKind,
    TaskSpec,
)
from herness.harness.findings import impact_usd
from herness.harness.pipelines.settings import DepthKnobs
from herness.harness.tools import RecordedResult

__all__ = [
    "Pipeline",
    "PlanContext",
    "RecordedReader",
    "build_ranked_entities",
    "default_challenge_priority",
    "get_pipeline",
    "to_recommendation_drafts",
]


# --- reader contract (design 06 §3.7) ---------------------------------------------------------


# `execute_recorded(ctx, sql, params, guard=True)` bound to the planner task's `ToolContext`
# (design 06 §3.7); every call gets a `query_id` and an `evidence` row.
type RecordedReader = Callable[[str, dict[str, JsonValue]], RecordedResult]


# --- plan context (U06-67) --------------------------------------------------------------------


@with_config(extra="forbid")
class _DqWarning(TypedDict):
    """One `dq_warnings` row surfaced to a pipeline."""

    check_name: str
    severity: str
    value: JsonValue
    threshold: JsonValue
    details: JsonValue
    query_id: str


@with_config(extra="forbid")
class _Portfolio(TypedDict):
    """The `portfolio` field of `PlanContext`."""

    scenario: str
    rows: list[dict[str, JsonValue]]
    selected: list[str]
    query_ids: list[str]
    custom: list[dict[str, JsonValue]]


class PlanContext(BaseModel):
    """Everything a pipeline needs to plan and write a run (U06-67, design 06 §3.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    run_id: str
    kind: RunKind
    depth: Depth
    profile: str
    build_id: str
    question: str | None
    focus: EntityScope | None
    knobs: DepthKnobs
    dq_warnings: list[_DqWarning]
    unconfirmed_weights: list[str]
    prior_context: str
    prior_recs: list[dict[str, JsonValue]]
    portfolio: _Portfolio


# --- pipeline contract (U06-66) -----------------------------------------------------------------


@runtime_checkable
class Pipeline(Protocol):
    """Review pipeline contract (design 06 §3.4).

    Implementations are constructed per run by `get_pipeline` and may cache per-run reads.
    `must_cover(ctx)` is always called before `ranked_entities`.
    """

    kind: RunKind

    def deterministic_tasks(self, ctx: PlanContext) -> list[TaskSpec]: ...
    def must_cover(self, ctx: PlanContext) -> set[str]: ...
    def planner_input(self, ctx: PlanContext, tasks: list[TaskSpec]) -> dict[str, object]: ...
    def challenge_priority(self, f: Finding, ctx: PlanContext) -> float: ...
    def writer_input(self, ctx: PlanContext, verified: list[Finding]) -> dict[str, object]: ...
    def ranked_entities(self, draft: ReportDraft) -> list[RankedEntity]: ...

    def recommendation_drafts(
        self, draft: ReportDraft, findings: dict[str, Finding]
    ) -> list[RecommendationDraft]: ...


# --- shared helpers (U06-68 … U06-70) -----------------------------------------------------------


def default_challenge_priority(f: Finding, /) -> float:
    """Default Skeptic selection score (U06-68, design 06 §5.7); pure."""
    return math.log10(1 + float(impact_usd(f))) * f.confidence


def build_ranked_entities(
    draft: ReportDraft,
    entity_type: Literal["candidate", "team"],
    remaining: Sequence[tuple[str, int]],
) -> list[RankedEntity]:
    """Derive `ReportDraft.ranked_entities` (U06-69, design 06 §4.4); pure.

    Recommendation targets rank first, in the order they first appear; the `remaining`
    must-cover entities not already listed follow, sorted by rank then id.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for rec in draft.recommendations:
        if rec.target_id not in seen:
            seen.add(rec.target_id)
            ordered.append(rec.target_id)
    rank_by_id = dict(remaining)
    leftovers = sorted(
        (entity_id for entity_id in rank_by_id if entity_id not in seen),
        key=lambda entity_id: (rank_by_id[entity_id], entity_id),
    )
    ordered.extend(leftovers)
    return [
        RankedEntity(rank=rank, entity_type=entity_type, entity_id=entity_id)
        for rank, entity_id in enumerate(ordered, start=1)
    ]


def to_recommendation_drafts(
    draft: ReportDraft, findings: Mapping[str, Finding]
) -> list[RecommendationDraft]:
    """Map draft recommendations to spec 07 `RecommendationDraft` (U06-70, spec 07 §3.2, R-30).

    Raises `ReportContractError` when a recommendation cites a finding that is missing from
    `findings` or not `verified`. `summary` keeps its markers (spec 07 resolves the values).
    Pure otherwise.
    """
    drafts: list[RecommendationDraft] = []
    for item in draft.recommendations:
        for finding_id in item.finding_ids:
            finding = findings.get(finding_id)
            if finding is None or finding.status != "verified":
                msg = f"recommendation cites unverified finding: {finding_id}"
                raise ReportContractError(msg)
        drafts.append(
            RecommendationDraft.model_validate(
                {
                    "rank": item.rank,
                    "kind": item.kind,
                    "target_type": item.target_type,
                    "target_id": item.target_id,
                    "summary": item.summary,
                    "numbers": item.numbers,
                    "expected_metric": item.expected_metric,
                    "expected_delta_ref": item.expected_delta_ref,
                    "expected_usd_ref": item.expected_usd_ref,
                    "finding_ids": item.finding_ids,
                }
            )
        )
    return drafts


# --- factory (U06-75) ---------------------------------------------------------------------------

_PIPELINE_MODULES: Final[Mapping[str, tuple[str, str]]] = {
    "funding_review": ("herness.harness.pipelines.funding_review", "FundingReviewPipeline"),
    "org_review": ("herness.harness.pipelines.org_review", "OrgReviewPipeline"),
}


def get_pipeline(
    kind: Literal["funding_review", "org_review"], reader: RecordedReader, *, window_end: date
) -> Pipeline:
    """Pipeline factory (U06-75): lazy import of the implementing module.

    `"chat"` or any other value raises `ConfigError("no review pipeline for kind <kind>")`.
    """
    target = _PIPELINE_MODULES.get(kind)
    if target is None:
        msg = f"no review pipeline for kind {kind}"
        raise ConfigError(msg)
    module_name, class_name = target
    cls = getattr(import_module(module_name), class_name)
    pipeline: Pipeline = cls(reader, window_end=window_end)
    return pipeline
