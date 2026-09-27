"""Funding review pipeline (impl 06 U06-71, U06-72; design 06 §5.2 funding_review generator).

Every read goes through the run's `RecordedReader` (spec 05 U05-35 named params: the U06-72 `?`
placeholders are written `$k`, `$focus_ids`, `$ids`, `$since`), so each one has a citable
`query_id` and an `evidence` row. The module touches no warehouse, store or model directly.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from pydantic import JsonValue

from herness.core.types import (
    Finding,
    RankedEntity,
    RecommendationDraft,
    ReportDraft,
    RunKind,
    Specialty,
    TaskSpec,
)
from herness.harness.pipelines import _review_common as common
from herness.harness.pipelines._review_common import AnalystSpec, Row
from herness.harness.pipelines.base import (
    PlanContext,
    RecordedReader,
    build_ranked_entities,
    default_challenge_priority,
    to_recommendation_drafts,
)

__all__ = ["FundingReviewPipeline"]

_SELECT_SQL: Final = (
    "SELECT candidate_id, candidate_type, rank, query_ids FROM score.funding"
    " WHERE candidate_type {types}{focus} ORDER BY rank, candidate_id{limit}"
)
_CANDIDATE_TYPES: Final = "IN ('epic','feature','initiative')"
_CLUSTER_TYPES: Final = "= 'cluster_fix'"
_CLUSTER_FIX: Final = "cluster_fix"
_INCIDENTS_SQL: Final = (
    "SELECT a.candidate_id, count(*) AS n FROM score.funding_attribution a"
    " JOIN core.incident i ON i.record_id = a.record_id"
    " WHERE a.candidate_id IN (SELECT unnest($ids)) AND a.record_kind = 'incident'"
    " AND i.opened_at >= CAST($since AS DATE) GROUP BY a.candidate_id"
)
_SHARE_SQL: Final = (
    "SELECT a.candidate_id, avg(CASE WHEN l.incident_id IS NULL THEN 0.0 ELSE 1.0 END) AS share"
    " FROM (SELECT DISTINCT candidate_id, record_id FROM score.funding_attribution"
    " WHERE candidate_id IN (SELECT unnest($ids)) AND record_kind = 'incident') a"
    " LEFT JOIN (SELECT DISTINCT incident_id FROM enrich.incident_change_link) l"
    " ON l.incident_id = a.record_id GROUP BY a.candidate_id"
)
# keyed by (is cluster fix, specialty)
_OBJECTIVES: Final[dict[tuple[bool, Specialty], str]] = {
    (False, "delivery"): "Build the funding case for candidate {id} ({type}): check addressable"
    " pain, effort and confidence drivers in score.funding and score.funding_attribution.",
    (False, "ops"): "Assess the operational pain behind candidate {id}: incidents, MTTR and toil"
    " in metrics.incident_fact for its attributed records.",
    (True, "ops"): "Assess recurring incident cluster fix {id}: volume, trend and cost in"
    " metrics.incident_fact and enrich.cluster_member.",
    (True, "change"): "Check change-caused incidents for cluster fix {id} using"
    " enrich.incident_change_link and metrics.change_fact.",
}
_CANDIDATE_DQ: Final = ("score.funding", "score.funding_attribution", "core.work_item",
                        "core.incident")  # fmt: skip
_CLUSTER_DQ: Final = ("score.funding", "enrich.cluster", "enrich.cluster_member",
                      "core.incident", "enrich.incident_change_link")  # fmt: skip
_OUTLINE: Final = ["executive_summary", "recommendations", "portfolio", "retrospective",
                   "risks_and_caveats", "method"]  # fmt: skip
_TOP_CLUSTERS: Final = 3
_CHANGE_SHARE_MIN: Final = 0.20


@dataclass(frozen=True, slots=True)
class _Pick:
    """One selected `score.funding` row."""

    candidate_id: str
    candidate_type: str
    rank: int
    query_ids: list[str]

    @property
    def is_cluster(self) -> bool:
        return self.candidate_type == _CLUSTER_FIX


@dataclass(frozen=True, slots=True)
class _Selection:
    """One selection read: its `query_id` and the picks in rank order."""

    query_id: str
    picks: list[_Pick]

    @property
    def ids(self) -> list[JsonValue]:
        return [p.candidate_id for p in self.picks]


def _select_query(ctx: PlanContext, types: str, k: int) -> tuple[str, dict[str, JsonValue]]:
    """U06-72 steps 1-2: `LIMIT k`, or with `focus` no limit and the candidate focus ids."""
    if ctx.focus is not None:
        focus = ctx.focus
        ids: list[JsonValue] = list(focus.entity_ids) if focus.entity_type == "candidate" else []
        sql = _SELECT_SQL.format(
            types=types, focus=" AND candidate_id IN (SELECT unnest($focus_ids))", limit=""
        )
        return sql, {"focus_ids": ids}
    return _SELECT_SQL.format(types=types, focus="", limit=" LIMIT $k"), {"k": k}


class FundingReviewPipeline:
    """Funding review pipeline (U06-71, `kind = "funding_review"`); per run, caches reads."""

    kind: RunKind = "funding_review"

    def __init__(self, reader: RecordedReader, *, window_end: date) -> None:
        self._reader = reader
        self.window_end = window_end
        self._selection: tuple[_Selection, _Selection] | None = None
        self._must_ranks: list[tuple[str, int]] = []
        self._score_rows: dict[tuple[str, str], Row] = {}

    def _read_selection(self, ctx: PlanContext, types: str, k: int) -> _Selection:
        qid, rows = common.read_rows(self._reader, *_select_query(ctx, types, k))
        picks = [
            _Pick(str(r["candidate_id"]), str(r["candidate_type"]), int(str(r["rank"])),
                  common.str_list(r["query_ids"]))
            for r in rows
        ]  # fmt: skip
        for p in picks:
            self._score_rows["candidate", p.candidate_id] = {
                "candidate_id": p.candidate_id, "candidate_type": p.candidate_type,
                "rank": p.rank, "query_ids": p.query_ids,
            }  # fmt: skip
        return _Selection(qid, picks)

    def _selected(self, ctx: PlanContext) -> tuple[_Selection, _Selection]:
        """U06-72 steps 1-2, read once per run: candidates, then cluster fixes."""
        if self._selection is None:
            knobs = ctx.knobs
            self._selection = (
                self._read_selection(ctx, _CANDIDATE_TYPES, knobs.K_candidates),
                self._read_selection(ctx, _CLUSTER_TYPES, knobs.K_clusters),
            )
        return self._selection

    def must_cover(self, ctx: PlanContext) -> set[str]:
        """Top `M_must` candidates, the portfolio selection and the top 3 cluster fixes."""
        candidates, clusters = self._selected(ctx)
        top = [*candidates.picks[: ctx.knobs.M_must], *clusters.picks[:_TOP_CLUSTERS]]
        selected = [str(c) for c in ctx.portfolio["selected"]]
        known = {p.candidate_id: p.rank for p in [*candidates.picks, *clusters.picks]}
        ranks = {p.candidate_id: p.rank for p in top}
        ranks |= {c: known[c] for c in selected if c in known}
        self._must_ranks = list(ranks.items())
        runs = common.retro_run_ids(ctx.prior_recs)
        ids = [*(p.candidate_id for p in top), *selected]
        return {f"candidate:{c}" for c in ids} | {f"run:{r}" for r in runs}

    def deterministic_tasks(self, ctx: PlanContext) -> list[TaskSpec]:
        """Deterministic funding task plan (U06-72, design 06 §5.2)."""
        candidates, clusters = self._selected(ctx)
        must = self.must_cover(ctx)
        incidents = self._incident_counts(ctx, candidates)
        shares = self._change_shares(ctx, clusters)
        tasks: list[TaskSpec] = []
        for p in candidates.picks:
            with_ops = incidents.get(p.candidate_id, 0) > 0
            specialties: list[Specialty] = ["delivery", "ops"] if with_ops else ["delivery"]
            tasks += [self._task(ctx, p, s, candidates.query_id, must) for s in specialties]
        for p in clusters.picks:
            with_change = shares.get(p.candidate_id, 0.0) > _CHANGE_SHARE_MIN
            specialties = ["ops", "change"] if with_change else ["ops"]
            tasks += [self._task(ctx, p, s, clusters.query_id, must) for s in specialties]
        retro = common.retro_task(ctx, _CANDIDATE_DQ)
        if retro is not None:
            tasks.append(retro)
        return tasks

    def _incident_counts(self, ctx: PlanContext, candidates: _Selection) -> dict[str, int]:
        """U06-72 step 3 (standard, deep): attributed incidents opened within the window."""
        if ctx.depth == "fast" or not candidates.picks:
            return {}
        since = self.window_end - timedelta(days=ctx.knobs.window_days)
        params: dict[str, JsonValue] = {"ids": candidates.ids, "since": since.isoformat()}
        _, rows = common.read_rows(self._reader, _INCIDENTS_SQL, params)
        return {str(r["candidate_id"]): int(str(r["n"])) for r in rows}

    def _change_shares(self, ctx: PlanContext, clusters: _Selection) -> dict[str, float]:
        """U06-72 step 4 (deep): share of each cluster's incidents with a change link."""
        if ctx.depth != "deep" or not clusters.picks:
            return {}
        _, rows = common.read_rows(self._reader, _SHARE_SQL, {"ids": clusters.ids})
        return {str(r["candidate_id"]): float(str(r["share"])) for r in rows}

    def _task(self, ctx: PlanContext, pick: _Pick, specialty: Specialty, selection_qid: str,
              must: set[str]) -> TaskSpec:  # fmt: skip
        """U06-72 steps 6-7 for one candidate or cluster-fix task."""
        cid = pick.candidate_id
        text = _OBJECTIVES[pick.is_cluster, specialty].format(id=cid, type=pick.candidate_type)
        tables = _CLUSTER_DQ if pick.is_cluster else _CANDIDATE_DQ
        inputs = common.task_inputs(
            [*pick.query_ids, selection_qid],
            common.dq_matches(ctx, [cid, *tables]),
            common.prior_lines(ctx.prior_recs, "target_id", [cid]),
            [cid],
        )
        spec: AnalystSpec = ("candidate", [cid], specialty, text)
        return common.analyst_task(ctx, spec, inputs, (pick.rank, f"candidate:{cid}" in must))

    def planner_input(self, ctx: PlanContext, tasks: list[TaskSpec]) -> dict[str, object]:
        """Planner input (U06-71): deterministic tasks with their `score.funding` rows."""
        return common.planner_input(self.kind, ctx, tasks, self._score_rows)

    def challenge_priority(self, f: Finding, ctx: PlanContext) -> float:
        """Skeptic selection score: U06-68."""
        return default_challenge_priority(f)

    def writer_input(self, ctx: PlanContext, verified: list[Finding]) -> dict[str, object]:
        """Writer input (U06-71) with the funding outline."""
        return common.writer_input(self.kind, ctx, _OUTLINE, verified)

    def ranked_entities(self, draft: ReportDraft) -> list[RankedEntity]:
        """`build_ranked_entities(draft, "candidate", cached must-cover ranks)` (U06-69)."""
        return build_ranked_entities(draft, "candidate", self._must_ranks)

    def recommendation_drafts(
        self, draft: ReportDraft, findings: dict[str, Finding]
    ) -> list[RecommendationDraft]:
        """U06-70."""
        return to_recommendation_drafts(draft, findings)
