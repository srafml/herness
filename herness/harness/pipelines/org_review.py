"""Org review pipeline (impl 06 U06-73, U06-74; design 06 §5.2 org_review generator).

Every read goes through the run's `RecordedReader` (spec 05 U05-35 named params: the U06-74 `?`
placeholders are written `$ids`, `$k`, `$focus_ids`), so each one has a citable `query_id`.
"""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Final

from pydantic import JsonValue

from herness.core.types import (
    Finding,
    RankedEntity,
    RecommendationDraft,
    ReportDraft,
    RunKind,
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

__all__ = ["OrgReviewPipeline"]

_TEAMS_SQL: Final = (
    "SELECT entity_id, min(rank) AS rank FROM score.org WHERE entity_type = 'team'{focus}"
    " GROUP BY entity_id ORDER BY rank, entity_id{limit}"
)
_LEVERS_SQL: Final = (
    "SELECT entity_id, metric, delta_usd, query_ids FROM score.action_lever"
    " WHERE entity_type = 'team' AND entity_id IN (SELECT unnest($ids))"
    " QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY delta_usd DESC, metric) <= 3"
)
# `team_ids` is added to the U06-74 select: the rollup priority needs each org's selected teams.
_ROLLUP_SQL: Final = (
    "SELECT org_id, count(*) AS n, list(team_id ORDER BY team_id) AS team_ids FROM core.team"
    " WHERE team_id IN (SELECT unnest($ids)) GROUP BY org_id HAVING count(*) >= 2 ORDER BY org_id"
)
_WRITER_LEVERS_SQL: Final = (
    "SELECT entity_id, metric, target_kind, current_value, target_value, delta_usd, query_ids"
    " FROM score.action_lever WHERE entity_type = 'team' AND entity_id IN (SELECT unnest($ids))"
)
_OBJECTIVES: Final[Mapping[str, str]] = {
    "ops": "Review team {id} operations: incident volume, MTTR, reopen and SLA metrics against"
    " peers in score.org.",
    "change": "Review team {id} change health: failure rate and change-caused incidents in"
    " metrics.change_fact.",
    "delivery": "Review team {id} delivery flow: cycle time and unplanned work in"
    " metrics.work_item_fact.",
    "org": "Roll up team findings for org {id}: compare its teams in score.org and"
    " metrics.metric_value.",
}
_DQ_TABLES: Final = ("score.org", "score.action_lever", "core.incident", "core.change",
                     "core.work_item")  # fmt: skip
_OUTLINE: Final = ["executive_summary", "recommendations", "org_scorecards", "actions",
                   "retrospective", "risks_and_caveats", "method"]  # fmt: skip


def _teams_query(ctx: PlanContext) -> tuple[str, dict[str, JsonValue]]:
    """U06-74 step 1 SQL: `LIMIT K_teams`, or with `focus` no limit and the ids filtered."""
    if ctx.focus is not None:
        focus = " AND entity_id IN (SELECT unnest($focus_ids))"
        return _TEAMS_SQL.format(focus=focus, limit=""), {"focus_ids": list(ctx.focus.entity_ids)}
    return _TEAMS_SQL.format(focus="", limit=" LIMIT $k"), {"k": ctx.knobs.K_teams}


class OrgReviewPipeline:
    """Org review pipeline (U06-73, `kind = "org_review"`); constructed per run, caches reads."""

    kind: RunKind = "org_review"

    def __init__(self, reader: RecordedReader, *, window_end: date) -> None:
        self._reader = reader
        self.window_end = window_end  # kept for the U06-73 signature; org reads no window
        self._teams: list[tuple[str, int]] | None = None
        self._teams_qid = ""
        self._must_ranks: list[tuple[str, int]] = []
        self._score_rows: dict[tuple[str, str], Row] = {}

    def _selected(self, ctx: PlanContext) -> list[tuple[str, int]]:
        """U06-74 step 1, read once per run: `(team_id, min rank)` in rank order."""
        if self._teams is None:
            qid, rows = common.read_rows(self._reader, *_teams_query(ctx))
            self._teams_qid = qid
            self._teams = [(str(r["entity_id"]), int(str(r["rank"]))) for r in rows]
            for team_id, rank in self._teams:
                self._score_rows["team", team_id] = {"entity_id": team_id, "rank": rank}
        return self._teams

    def must_cover(self, ctx: PlanContext) -> set[str]:
        """Top `M_must` selected teams as `"team:<id>"` plus the retrospective runs."""
        self._must_ranks = self._selected(ctx)[: ctx.knobs.M_must]
        runs = common.retro_run_ids(ctx.prior_recs)
        return {f"team:{t}" for t, _ in self._must_ranks} | {f"run:{r}" for r in runs}

    def deterministic_tasks(self, ctx: PlanContext) -> list[TaskSpec]:
        """Deterministic org task plan (U06-74, design 06 §5.2)."""
        teams = self._selected(ctx)
        must = self.must_cover(ctx)
        tasks: list[TaskSpec] = []
        if teams:
            ids: list[JsonValue] = [t for t, _ in teams]
            lever_qid, lever_rows = common.read_rows(self._reader, _LEVERS_SQL, {"ids": ids})
            rollup_qid, rollups = common.read_rows(self._reader, _ROLLUP_SQL, {"ids": ids})
            for team_id, rank in teams:
                tasks += self._team_tasks(ctx, team_id, rank, must, (lever_qid, lever_rows))
            ranks = dict(teams)
            for row in rollups:
                tasks.append(self._rollup_task(ctx, row, ranks, rollup_qid))
        retro = common.retro_task(ctx, _DQ_TABLES)
        if retro is not None:
            tasks.append(retro)
        return tasks

    def _team_tasks(self, ctx: PlanContext, team_id: str, rank: int, must: set[str],
                    levers: tuple[str, list[Row]]) -> list[TaskSpec]:  # fmt: skip
        lever_qid, rows = levers
        mine = sorted(
            (r for r in rows if r["entity_id"] == team_id),
            key=lambda r: (-Decimal(str(r["delta_usd"])), str(r["metric"])),
        )
        qids = [self._teams_qid]
        lines = common.prior_lines(ctx.prior_recs, "target_id", [team_id])
        if mine:
            qids += [lever_qid, *(q for r in mine for q in common.str_list(r["query_ids"]))]
            lines.insert(0, f"Top action levers: {', '.join(str(r['metric']) for r in mine)}.")
        ranked = (rank, f"team:{team_id}" in must)
        tasks = []
        for s in ctx.knobs.org_specialties:
            spec: AnalystSpec = ("team", [team_id], s, _OBJECTIVES[s].format(id=team_id))
            tasks.append(self._task(ctx, spec, qids, lines, ranked))
        return tasks

    def _rollup_task(self, ctx: PlanContext, row: Row, ranks: Mapping[str, int],
                     rollup_qid: str) -> TaskSpec:  # fmt: skip
        org_id, team_ids = str(row["org_id"]), common.str_list(row["team_ids"])
        self._score_rows["org", org_id] = {"org_id": org_id, "n": row["n"], "team_ids": team_ids}
        lines = common.prior_lines(ctx.prior_recs, "target_id", [org_id])
        rank = min(ranks[t] for t in team_ids)
        spec: AnalystSpec = ("org", [org_id], "org", _OBJECTIVES["org"].format(id=org_id))
        return self._task(ctx, spec, [rollup_qid, self._teams_qid], lines, (rank, False))

    def _task(self, ctx: PlanContext, spec: AnalystSpec, query_ids: list[str], lines: list[str],
              ranked: tuple[int, bool]) -> TaskSpec:  # fmt: skip
        """U06-74 step 6: U06-72 fields with the org DQ tables."""
        dq = common.dq_matches(ctx, [*spec[1], *_DQ_TABLES])
        return common.analyst_task(ctx, spec, common.task_inputs(query_ids, dq, lines), ranked)

    def planner_input(self, ctx: PlanContext, tasks: list[TaskSpec]) -> dict[str, object]:
        """Planner input as for funding (U06-71)."""
        return common.planner_input(self.kind, ctx, tasks, self._score_rows)

    def challenge_priority(self, f: Finding, ctx: PlanContext) -> float:
        """Skeptic selection score: U06-68."""
        return default_challenge_priority(f)

    def writer_input(self, ctx: PlanContext, verified: list[Finding]) -> dict[str, object]:
        """Writer input as for funding (U06-71) with the org outline and the teams' levers."""
        ids: list[JsonValue] = [t for t, _ in self._selected(ctx)]
        levers: list[Row] = []
        if ids:
            qid, rows = common.read_rows(self._reader, _WRITER_LEVERS_SQL, {"ids": ids})
            levers = [row | {"query_id": qid} for row in rows]
        return common.writer_input(self.kind, ctx, _OUTLINE, verified) | {"levers": levers}

    def ranked_entities(self, draft: ReportDraft) -> list[RankedEntity]:
        """`build_ranked_entities(draft, "team", cached must-cover ranks)` (U06-69)."""
        return build_ranked_entities(draft, "team", self._must_ranks)

    def recommendation_drafts(
        self, draft: ReportDraft, findings: dict[str, Finding]
    ) -> list[RecommendationDraft]:
        """U06-70."""
        return to_recommendation_drafts(draft, findings)
