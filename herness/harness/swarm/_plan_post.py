"""Planner post-processing: the TH06-04 boundary of the plan (impl 06 U06-89; design 06 §5.2).

Private sibling of `herness.harness.swarm.planner`, which re-exports `postprocess_plan` and
`PlanDecision` (T06-15 split, §2 module map). A model-proposed task can only add notes to a
deterministic task or become a capped analyst hypothesis whose budget, tools, priority and
model role are set here; `PlannedTask` has no field for any of them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final, Literal

from herness.core.types import (
    Depth,
    EntityScope,
    PlannedTask,
    ScopeEntityType,
    TaskInputs,
    TaskSpec,
)
from herness.harness.findings import compute_dedup_key
from herness.harness.swarm.routing import default_tools

if TYPE_CHECKING:
    from herness.harness.pipelines.settings import DepthKnobs

__all__ = ["NOTES_MAX", "PlanDecision", "postprocess_plan"]

NOTES_MAX: Final = 400  # notes a planned item may add to a deterministic task (TH06-04)
_TABLE_RE: Final = re.compile(r"\b(core|enrich|metrics|score|meta)\.[a-z_]+\b", re.IGNORECASE)

type PlanAction = Literal["notes_applied", "kept", "dropped"]


@dataclass(frozen=True, slots=True)
class PlanDecision:
    """What post-processing did with one planned item: the `plan_decision` trace (D06-01).

    Structured values only, never model text (TH06-09); `reason` is the drop reason or "".
    """

    action: PlanAction
    reason: str
    dedup_key: str | None
    specialty: str
    entity_type: str
    n_entities: int


@dataclass(slots=True)
class _Post:
    """Mutable state of one `postprocess_plan` call."""

    tasks: list[TaskSpec]
    by_key: dict[str, int]
    framed: set[str]
    covered: set[tuple[str, str, str]]
    keys: set[str]
    kept: list[TaskSpec]


def _triples(entity_type: str, ids: Sequence[str], specialty: str) -> set[tuple[str, str, str]]:
    return {(entity_type, entity_id, specialty) for entity_id in ids}


def _names_metric(objective: str, metric_names: frozenset[str]) -> bool:
    """A whole-word metric name (case-insensitive) or a `<schema>.<table>` reference."""
    if _TABLE_RE.search(objective):
        return True
    return any(
        re.search(rf"(?<!\w){re.escape(name)}(?!\w)", objective, re.IGNORECASE)
        for name in metric_names
    )


def _drop_reason(
    item: PlannedTask,
    key: str,
    st: _Post,
    h_wildcards: int,
    entity_missing: Callable[[ScopeEntityType, list[str]], set[str]],
    metric_names: frozenset[str],
) -> str | None:
    if len(st.kept) == h_wildcards:
        return "over_limit"
    if entity_missing(item.entity_type, list(item.entity_ids)):
        return "unknown_entity"
    if not _names_metric(item.objective, metric_names):
        return "no_metric_or_table"
    if _triples(item.entity_type, item.entity_ids, item.specialty) & st.covered:
        return "already_covered"
    if key in st.keys:
        return "duplicate_key"
    return None


def postprocess_plan(  # noqa: PLR0913 - U06-89 signature
    deterministic: list[TaskSpec],
    planned: Sequence[PlannedTask],
    *,
    knobs: DepthKnobs,
    depth_mode: Depth,
    run_id: str,
    entity_missing: Callable[[ScopeEntityType, list[str]], set[str]],
    metric_names: frozenset[str],
    new_task_id: Callable[[], str],
) -> tuple[list[TaskSpec], list[PlanDecision]]:
    """Deterministic tasks (maybe with new notes) then at most `H_wildcards` hypotheses (U06-89).

    TH06-04: a planned item matching a deterministic `dedup_key` only sets its notes (<= 400
    characters); a hypothesis gets the analyst budget, default tools, a priority below every
    deterministic task and `must_cover = False`. Pure given the callables.
    """
    st = _Post(
        tasks=list(deterministic),
        by_key={t.dedup_key: i for i, t in enumerate(deterministic)},
        framed=set(),
        covered=set().union(
            *(_triples(t.scope.entity_type, t.scope.entity_ids, t.specialty) for t in deterministic)
        ),
        keys={t.dedup_key for t in deterministic},
        kept=[],
    )
    priority = min((t.priority for t in deterministic), default=0.0) - 1
    decisions: list[PlanDecision] = []
    for item in planned:
        decide = partial(
            PlanDecision,
            specialty=item.specialty,
            entity_type=item.entity_type,
            n_entities=len(item.entity_ids),
        )
        if item.dedup_key in st.by_key and item.dedup_key not in st.framed:
            _frame(st, item.dedup_key, item.notes)
            decisions.append(decide("notes_applied", "", item.dedup_key))
            continue
        scope = EntityScope(entity_type=item.entity_type, entity_ids=list(item.entity_ids))
        key = compute_dedup_key("analyst", item.specialty, scope, item.objective)
        reason = _drop_reason(item, key, st, knobs.H_wildcards, entity_missing, metric_names)
        if reason is not None:
            decisions.append(decide("dropped", reason, key))
            continue
        tools = default_tools("analyst", item.specialty, depth_mode, child_depth=0, knobs=knobs)
        task = TaskSpec(
            task_id=new_task_id(), run_id=run_id, role="analyst", specialty=item.specialty,
            objective=item.objective, scope=scope, inputs=TaskInputs(notes=item.notes),
            tools=tools, budget=knobs.analyst_budget, depth=0, priority=priority,
            model_role="analyst", must_cover=False, dedup_key=key,
        )  # fmt: skip
        st.kept.append(task)
        st.keys.add(key)
        st.covered |= _triples(item.entity_type, item.entity_ids, item.specialty)
        decisions.append(decide("kept", "", key))
    return [*st.tasks, *st.kept], decisions


def _frame(st: _Post, dedup_key: str, notes: str | None) -> None:
    """Mark a deterministic task framed; non-empty notes replace its `inputs.notes`."""
    st.framed.add(dedup_key)
    if not notes:
        return
    i = st.by_key[dedup_key]
    task = st.tasks[i]
    inputs = task.inputs.model_copy(update={"notes": notes[:NOTES_MAX]})
    st.tasks[i] = task.model_copy(update={"inputs": inputs})
