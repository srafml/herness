"""Org review pipeline (impl 06 U06-73, U06-74; design 06 §5.2 org_review generator).

Every read goes through the run's `RecordedReader` (spec 05 U05-35 named params: the U06-74 `?`
placeholders are written `$ids`, `$k`, `$focus_ids`), so each one has a citable `query_id`.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Final

from pydantic import JsonValue

from herness.core.ids import IdKind, new_id
from herness.core.types import (
    EntityScope,
    Finding,
    RankedEntity,
    RecommendationDraft,
    ReportDraft,
    RunKind,
    ScopeEntityType,
    Specialty,
    TaskInputs,
    TaskSpec,
)
from herness.harness.findings import compute_dedup_key
from herness.harness.pipelines.base import (
    PlanContext,
    RecordedReader,
    build_ranked_entities,
    default_challenge_priority,
    to_recommendation_drafts,
)
from herness.harness.pipelines.settings import DepthKnobs

__all__ = ["OrgReviewPipeline"]

type _Row = dict[str, object]
# (scope entity type, entity ids, specialty, objective)
type _Spec = tuple[ScopeEntityType, list[str], Specialty, str]

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
    "retrospective": "Review prior recommendations and their measured outcomes for runs {ids}:"
    " compare expected and actual values in the outcome rows.",
}
_DQ_TABLES: Final = ("score.org", "score.action_lever", "core.incident", "core.change",
                     "core.work_item")  # fmt: skip
_OUTLINE: Final = ["executive_summary", "recommendations", "org_scorecards", "actions",
                   "retrospective", "risks_and_caveats", "method"]  # fmt: skip
_ANALYST_TOOLS: Final = (
    "describe_table", "get_cluster", "get_metric", "get_record", "get_scores", "list_findings",
    "list_tables", "post_finding", "propose_memory", "recall_memory", "request_subtask",
    "run_sql", "semantic_search",
)  # fmt: skip
_NOTES_MAX: Final = 1_500


# T06-15: replace with herness.harness.swarm.planner.base_priority
def _base_priority(rank: int | None, must_cover: bool) -> float:
    """U06-91: `(100 - rank if rank is not None else 0) + (50 if must_cover else 0)`."""
    return float((100 - rank if rank is not None else 0) + (50 if must_cover else 0))


# T06-13: replace with herness.harness.swarm.routing.default_tools
def _default_tools(*, child_depth: int, knobs: DepthKnobs) -> list[str]:
    """U06-101 analyst (non-crosscheck) tools; `request_subtask` only below the spawn cap."""
    drop = "request_subtask" if child_depth >= knobs.max_spawn_depth else ""
    return [name for name in _ANALYST_TOOLS if name != drop]


def _teams_query(ctx: PlanContext) -> tuple[str, dict[str, object]]:
    """U06-74 step 1 SQL: `LIMIT K_teams`, or with `focus` no limit and the ids filtered."""
    if ctx.focus is not None:
        focus = " AND entity_id IN (SELECT unnest($focus_ids))"
        return _TEAMS_SQL.format(focus=focus, limit=""), {"focus_ids": list(ctx.focus.entity_ids)}
    return _TEAMS_SQL.format(focus="", limit=" LIMIT $k"), {"k": ctx.knobs.K_teams}


# --- "as funding" helpers (U06-71, U06-72), free of org specifics so T06-11 can lift them ---


def _read(reader: RecordedReader, sql: str, params: dict[str, object]) -> tuple[str, list[_Row]]:
    """One recorded read: its `query_id` and the rows as column dicts."""
    result = reader(sql, params)
    return result.query_id, [dict(zip(result.columns, row, strict=True)) for row in result.rows]


def _str_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list | tuple) else []


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _retro_run_ids(prior_recs: Sequence[Mapping[str, JsonValue]]) -> list[str]:
    """Distinct prior `run_id`s of rows with an outcome, first-seen order, at most 2."""
    runs = _unique(str(r["run_id"]) for r in prior_recs if r.get("outcome") is not None)
    return runs[:2]


def _prior_lines(prior_recs: Sequence[Mapping[str, JsonValue]], key: str,
                 ids: Sequence[str]) -> list[str]:  # fmt: skip
    """`"prior rec <rec_id>: decision <decision or none>, outcome <verdict or none>"` lines."""
    lines = []
    for row in prior_recs:
        if str(row.get(key)) in ids:
            outcome = row.get("outcome")
            verdict = outcome.get("verdict") if isinstance(outcome, dict) else None
            lines.append(
                f"prior rec {row.get('rec_id')}: decision {row.get('decision') or 'none'},"
                f" outcome {verdict or 'none'}"
            )
    return lines


def _dq_matches(ctx: PlanContext, needles: Sequence[str]) -> list[str]:
    """Check names whose `details` JSON text contains an entity id or one of the tables."""
    names = [
        w["check_name"]
        for w in ctx.dq_warnings
        if any(n in json.dumps(w["details"], ensure_ascii=False) for n in needles)
    ]
    return _unique(names)


def _challenge_summary(f: Finding) -> dict[str, object] | None:
    """Last verdict and the `note` of each `concern`/`fail` check of the last challenge."""
    if not f.challenge:
        return None
    last = f.challenge[-1]
    notes = [c.note for c in last.checks if c.result in {"concern", "fail"}]
    return {"verdict": last.verdict, "notes": notes}


_FINDING_KEYS: Final = {"finding_id", "entity_type", "entity_id", "claim", "numbers",
                        "confidence", "query_ids"}  # fmt: skip


def _finding_row(f: Finding) -> dict[str, object]:
    """One writer `findings` item (U06-71)."""
    row: dict[str, object] = f.model_dump(mode="json", include=_FINDING_KEYS)
    return row | {"challenge_summary": _challenge_summary(f)}


class OrgReviewPipeline:
    """Org review pipeline (U06-73, `kind = "org_review"`); constructed per run, caches reads."""

    kind: RunKind = "org_review"

    def __init__(self, reader: RecordedReader, *, window_end: date) -> None:
        self._reader = reader
        self.window_end = window_end  # kept for the U06-73 signature; org reads no window
        self._teams: list[tuple[str, int]] | None = None
        self._teams_qid = ""
        self._must_ranks: list[tuple[str, int]] = []
        self._score_rows: dict[tuple[str, str], dict[str, object]] = {}

    def _selected(self, ctx: PlanContext) -> list[tuple[str, int]]:
        """U06-74 step 1, read once per run: `(team_id, min rank)` in rank order."""
        if self._teams is None:
            qid, rows = _read(self._reader, *_teams_query(ctx))
            self._teams_qid = qid
            self._teams = [(str(r["entity_id"]), int(str(r["rank"]))) for r in rows]
            for team_id, rank in self._teams:
                self._score_rows["team", team_id] = {"entity_id": team_id, "rank": rank}
        return self._teams

    def must_cover(self, ctx: PlanContext) -> set[str]:
        """Top `M_must` selected teams as `"team:<id>"` plus the retrospective runs."""
        self._must_ranks = self._selected(ctx)[: ctx.knobs.M_must]
        runs = _retro_run_ids(ctx.prior_recs)
        return {f"team:{t}" for t, _ in self._must_ranks} | {f"run:{r}" for r in runs}

    def deterministic_tasks(self, ctx: PlanContext) -> list[TaskSpec]:
        """Deterministic org task plan (U06-74, design 06 §5.2)."""
        teams = self._selected(ctx)
        must = self.must_cover(ctx)
        tasks: list[TaskSpec] = []
        if teams:
            ids = [t for t, _ in teams]
            lever_qid, lever_rows = _read(self._reader, _LEVERS_SQL, {"ids": ids})
            rollup_qid, rollups = _read(self._reader, _ROLLUP_SQL, {"ids": ids})
            for team_id, rank in teams:
                tasks += self._team_tasks(ctx, team_id, rank, must, (lever_qid, lever_rows))
            ranks = dict(teams)
            for row in rollups:
                tasks.append(self._rollup_task(ctx, row, ranks, rollup_qid))
        runs = _retro_run_ids(ctx.prior_recs)
        if runs:
            lines = _prior_lines(ctx.prior_recs, "run_id", runs)
            text = _OBJECTIVES["retrospective"].format(ids=", ".join(runs))
            tasks.append(self._task(ctx, ("run", runs, "retrospective", text), [], lines, None))
        return tasks

    def _team_tasks(self, ctx: PlanContext, team_id: str, rank: int, must: set[str],
                    levers: tuple[str, list[_Row]]) -> list[TaskSpec]:  # fmt: skip
        lever_qid, rows = levers
        mine = sorted(
            (r for r in rows if r["entity_id"] == team_id),
            key=lambda r: (-Decimal(str(r["delta_usd"])), str(r["metric"])),
        )
        qids = [self._teams_qid]
        lines = _prior_lines(ctx.prior_recs, "target_id", [team_id])
        if mine:
            qids += [lever_qid, *(q for r in mine for q in _str_list(r["query_ids"]))]
            lines.insert(0, f"Top action levers: {', '.join(str(r['metric']) for r in mine)}.")
        ranked = (rank, f"team:{team_id}" in must)
        tasks = []
        for s in ctx.knobs.org_specialties:
            spec: _Spec = ("team", [team_id], s, _OBJECTIVES[s].format(id=team_id))
            tasks.append(self._task(ctx, spec, qids, lines, ranked))
        return tasks

    def _rollup_task(self, ctx: PlanContext, row: _Row, ranks: Mapping[str, int],
                     rollup_qid: str) -> TaskSpec:  # fmt: skip
        org_id, team_ids = str(row["org_id"]), _str_list(row["team_ids"])
        self._score_rows["org", org_id] = {"org_id": org_id, "n": row["n"], "team_ids": team_ids}
        lines = _prior_lines(ctx.prior_recs, "target_id", [org_id])
        rank = min(ranks[t] for t in team_ids)
        spec: _Spec = ("org", [org_id], "org", _OBJECTIVES["org"].format(id=org_id))
        return self._task(ctx, spec, [rollup_qid, self._teams_qid], lines, (rank, False))

    def _task(
        self,
        ctx: PlanContext,
        spec: _Spec,
        query_ids: list[str],
        lines: list[str],
        ranked: tuple[int, bool] | None,
    ) -> TaskSpec:
        """U06-72 step 7 fields; `ranked` is `(rank, must_cover)`, None for the retrospective."""
        entity_type, ids, specialty, objective = spec
        scope = EntityScope(entity_type=entity_type, entity_ids=ids)
        rank, must_cover = ranked if ranked is not None else (None, True)
        inputs = TaskInputs(
            query_ids=_unique(query_ids),
            dq_warnings=_dq_matches(ctx, [*ids, *_DQ_TABLES])[:100],
            notes="\n".join(lines)[:_NOTES_MAX] or None,
        )
        return TaskSpec(
            task_id=new_id(IdKind.TASK), run_id=ctx.run_id, role="analyst", specialty=specialty,
            objective=objective, scope=scope, inputs=inputs,
            tools=_default_tools(child_depth=0, knobs=ctx.knobs), budget=ctx.knobs.analyst_budget,
            priority=_base_priority(rank, must_cover), model_role="analyst", must_cover=must_cover,
            dedup_key=compute_dedup_key("analyst", specialty, scope, objective),
        )  # fmt: skip

    def planner_input(self, ctx: PlanContext, tasks: list[TaskSpec]) -> dict[str, object]:
        """Planner input as for funding (U06-71)."""
        deterministic = [
            t.model_dump(include={"dedup_key", "specialty", "objective"})
            | t.scope.model_dump(include={"entity_type", "entity_ids"})
            | {"score_row": self._score_rows.get((t.scope.entity_type, t.scope.entity_ids[0]))}
            for t in tasks
        ]
        return {
            "kind": self.kind,
            "question": ctx.question,
            "deterministic": deterministic,
            "dq_warnings": ctx.dq_warnings,
            "unconfirmed_weights": ctx.unconfirmed_weights,
            "prior_context": ctx.prior_context,
            "H_wildcards": ctx.knobs.H_wildcards,
        }

    def challenge_priority(self, f: Finding, ctx: PlanContext) -> float:
        """Skeptic selection score: U06-68."""
        return default_challenge_priority(f)

    def writer_input(self, ctx: PlanContext, verified: list[Finding]) -> dict[str, object]:
        """Writer input as for funding (U06-71) with the org outline and the teams' levers."""
        ids = [t for t, _ in self._selected(ctx)]
        levers: list[_Row] = []
        if ids:
            qid, rows = _read(self._reader, _WRITER_LEVERS_SQL, {"ids": ids})
            levers = [row | {"query_id": qid} for row in rows]
        return {
            "kind": self.kind,
            "question": ctx.question,
            "outline": list(_OUTLINE),
            "findings": [_finding_row(f) for f in verified],
            "portfolio": ctx.portfolio,
            "dq_warnings": ctx.dq_warnings,
            "unconfirmed_weights": ctx.unconfirmed_weights,
            "prior_context": ctx.prior_context,
            "prior_recs": ctx.prior_recs,
            "levers": levers,
        }

    def ranked_entities(self, draft: ReportDraft) -> list[RankedEntity]:
        """`build_ranked_entities(draft, "team", cached must-cover ranks)` (U06-69)."""
        return build_ranked_entities(draft, "team", self._must_ranks)

    def recommendation_drafts(
        self, draft: ReportDraft, findings: dict[str, Finding]
    ) -> list[RecommendationDraft]:
        """U06-70."""
        return to_recommendation_drafts(draft, findings)
