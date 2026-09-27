"""Helpers shared by the review pipelines (impl 06 U06-71 … U06-74; private to `pipelines`).

The funding pipeline (U06-71, U06-72) and the org pipeline ("as funding", U06-73, U06-74) plan
analyst tasks, build planner and writer inputs and read through the run's `RecordedReader` the
same way; this module holds that common part. `base_priority` and `default_tools` are stand-ins
for the swarm functions of T06-15 and T06-13.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from pydantic import JsonValue

from herness.core.ids import IdKind, new_id
from herness.core.types import (
    Depth,
    EntityScope,
    Finding,
    Role,
    RunKind,
    ScopeEntityType,
    Specialty,
    TaskInputs,
    TaskSpec,
)
from herness.harness.findings import compute_dedup_key
from herness.harness.pipelines.base import PlanContext, RecordedReader
from herness.harness.pipelines.settings import DepthKnobs

type Row = dict[str, object]
# (scope entity type, entity ids, specialty, objective)
type AnalystSpec = tuple[ScopeEntityType, list[str], Specialty, str]

RETRO_OBJECTIVE: Final = (
    "Review prior recommendations and their measured outcomes for runs {ids}: compare expected"
    " and actual values in the outcome rows."
)
NOTES_MAX: Final = 1_500
_DQ_MAX: Final = 100
_ANALYST_TOOLS: Final = (
    "describe_table", "get_cluster", "get_metric", "get_record", "get_scores", "list_findings",
    "list_tables", "post_finding", "propose_memory", "recall_memory", "request_subtask",
    "run_sql", "semantic_search",
)  # fmt: skip
_FINDING_KEYS: Final = {"finding_id", "entity_type", "entity_id", "claim", "numbers",
                        "confidence", "query_ids"}  # fmt: skip


# T06-15: replace with herness.harness.swarm.planner.base_priority
def base_priority(rank: int | None, must_cover: bool) -> float:
    """U06-91: `(100 - rank if rank is not None else 0) + (50 if must_cover else 0)`."""
    return float((100 - rank if rank is not None else 0) + (50 if must_cover else 0))


# T06-13: replace with herness.harness.swarm.routing.default_tools
def default_tools(
    role: Role, specialty: Specialty, depth_mode: Depth, *, child_depth: int, knobs: DepthKnobs
) -> list[str]:
    """U06-101 tools of a non-crosscheck analyst (the only role the pipelines plan).

    `request_subtask` is dropped when `child_depth >= knobs.max_spawn_depth`; `depth_mode` acts
    through `knobs` only. Any other role or the crosscheck specialty raises `ValueError`.
    """
    del depth_mode
    if role != "analyst" or specialty == "crosscheck":
        msg = "pipeline tool stand-in covers the non-crosscheck analyst only"
        raise ValueError(msg)
    drop = "request_subtask" if child_depth >= knobs.max_spawn_depth else ""
    return [name for name in _ANALYST_TOOLS if name != drop]


def read_rows(
    reader: RecordedReader, sql: str, params: dict[str, JsonValue]
) -> tuple[str, list[Row]]:
    """One recorded read: its `query_id` and the rows as column dicts."""
    result = reader(sql, params)
    return result.query_id, [dict(zip(result.columns, row, strict=True)) for row in result.rows]


def str_list(value: object) -> list[str]:
    """A list or tuple cell as strings; anything else (NULL) as `[]`."""
    return [str(v) for v in value] if isinstance(value, list | tuple) else []


def unique(values: Iterable[str]) -> list[str]:
    """First-seen order without duplicates."""
    return list(dict.fromkeys(values))


def retro_run_ids(prior_recs: Sequence[Mapping[str, JsonValue]]) -> list[str]:
    """Distinct prior `run_id`s of rows with an outcome, first-seen order, at most 2."""
    runs = unique(str(r["run_id"]) for r in prior_recs if r.get("outcome") is not None)
    return runs[:2]


def prior_lines(prior_recs: Sequence[Mapping[str, JsonValue]], key: str,
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


def dq_matches(ctx: PlanContext, needles: Sequence[str]) -> list[str]:
    """Check names whose `details` JSON text contains an entity id or one of the tables."""
    names = [
        w["check_name"]
        for w in ctx.dq_warnings
        if any(n in json.dumps(w["details"], ensure_ascii=False) for n in needles)
    ]
    return unique(names)[:_DQ_MAX]  # TaskInputs.dq_warnings holds at most 100 names (U06-03)


def challenge_summary(f: Finding) -> dict[str, object] | None:
    """Last verdict and the `note` of each `concern`/`fail` check of the last challenge."""
    if not f.challenge:
        return None
    last = f.challenge[-1]
    notes = [c.note for c in last.checks if c.result in {"concern", "fail"}]
    return {"verdict": last.verdict, "notes": notes}


def finding_row(f: Finding) -> dict[str, object]:
    """One writer `findings` item (U06-71)."""
    row: dict[str, object] = f.model_dump(mode="json", include=_FINDING_KEYS)
    return row | {"challenge_summary": challenge_summary(f)}


def task_inputs(query_ids: Iterable[str], dq: list[str], lines: Sequence[str],
                candidate_ids: Sequence[str] = ()) -> TaskInputs:  # fmt: skip
    """`TaskInputs` with unique query ids and the notes cut to 1,500 characters (U06-72)."""
    return TaskInputs(
        query_ids=unique(query_ids),
        candidate_ids=list(candidate_ids),
        dq_warnings=dq,
        notes="\n".join(lines)[:NOTES_MAX] or None,
    )


def analyst_task(ctx: PlanContext, spec: AnalystSpec, inputs: TaskInputs,
                 ranked: tuple[int, bool] | None) -> TaskSpec:  # fmt: skip
    """U06-72 step 7 fields; `ranked` is `(rank, must_cover)`, None for the retrospective."""
    entity_type, ids, specialty, objective = spec
    scope = EntityScope(entity_type=entity_type, entity_ids=ids)
    rank, must_cover = ranked if ranked is not None else (None, True)
    tools = default_tools("analyst", specialty, ctx.depth, child_depth=0, knobs=ctx.knobs)
    return TaskSpec(
        task_id=new_id(IdKind.TASK), run_id=ctx.run_id, role="analyst", specialty=specialty,
        objective=objective, scope=scope, inputs=inputs, tools=tools,
        budget=ctx.knobs.analyst_budget, priority=base_priority(rank, must_cover),
        model_role="analyst", must_cover=must_cover,
        dedup_key=compute_dedup_key("analyst", specialty, scope, objective),
    )  # fmt: skip


def retro_task(ctx: PlanContext, dq_tables: Sequence[str]) -> TaskSpec | None:
    """The retrospective task (U06-72 step 5), or None without a prior rec with an outcome."""
    runs = retro_run_ids(ctx.prior_recs)
    if not runs:
        return None
    lines = prior_lines(ctx.prior_recs, "run_id", runs)
    inputs = task_inputs([], dq_matches(ctx, [*runs, *dq_tables]), lines)
    text = RETRO_OBJECTIVE.format(ids=", ".join(runs))
    return analyst_task(ctx, ("run", runs, "retrospective", text), inputs, None)


def planner_input(kind: RunKind, ctx: PlanContext, tasks: Sequence[TaskSpec],
                  score_rows: Mapping[tuple[str, str], Row]) -> dict[str, object]:  # fmt: skip
    """Planner input (U06-71): each deterministic task with the score row of its first entity."""
    deterministic = [
        t.model_dump(include={"dedup_key", "specialty", "objective"})
        | t.scope.model_dump(include={"entity_type", "entity_ids"})
        | {"score_row": score_rows.get((t.scope.entity_type, t.scope.entity_ids[0]))}
        for t in tasks
    ]
    return {
        "kind": kind,
        "question": ctx.question,
        "deterministic": deterministic,
        "dq_warnings": ctx.dq_warnings,
        "unconfirmed_weights": ctx.unconfirmed_weights,
        "prior_context": ctx.prior_context,
        "H_wildcards": ctx.knobs.H_wildcards,
    }


def writer_input(kind: RunKind, ctx: PlanContext, outline: Sequence[str],
                 verified: Sequence[Finding]) -> dict[str, object]:  # fmt: skip
    """Writer input (U06-71) with the pipeline's `outline`."""
    return {
        "kind": kind,
        "question": ctx.question,
        "outline": list(outline),
        "findings": [finding_row(f) for f in verified],
        "portfolio": ctx.portfolio,
        "dq_warnings": ctx.dq_warnings,
        "unconfirmed_weights": ctx.unconfirmed_weights,
        "prior_context": ctx.prior_context,
        "prior_recs": ctx.prior_recs,
    }
