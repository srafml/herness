"""Pure adversarial-layer rules (impl 06 U06-104 … U06-107, U06-110 … U06-112; design 06 §5.7).

Skeptic selection, the deterministic verdict rule (a `reject` needs a failing check with a
query, TH06-07), the self-consistency vote, Skeptic and revision task specs, cross-check task
planning for key numbers, SQL path independence and the agreement tolerance. Every function is
pure; the run driver (`challenge.py`, T06-17) performs the writes.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final, Literal

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import SqlglotError

from herness.core.types import Challenge, EntityScope, Finding, NumberRef, TaskInputs, TaskSpec
from herness.harness.findings import compute_dedup_key
from herness.harness.swarm.routing import default_tools, role_budget

if TYPE_CHECKING:
    from herness.harness.pipelines.settings import DepthKnobs
    from herness.store.ops.runs import RunRow

__all__ = [
    "build_revision_task",
    "build_skeptic_task",
    "crosscheck_agree",
    "is_independent_sql",
    "majority_verdict",
    "normalize_verdict",
    "plan_crosschecks",
    "select_for_challenge",
]

type Verdict = Literal["uphold", "revise", "reject"]

_STRICTNESS: Final[dict[str, int]] = {"uphold": 0, "revise": 1, "reject": 2}
_DEFAULT_ACTION: Final = "Re-examine the claim with an independent query."
_OBJECTIVE_MAX: Final = 2_000
_RATIO_UNITS: Final = frozenset({"ratio", "pct"})
# Revisions and crosschecks have no run row; `default_tools` reads the depth through `knobs`
# only (U06-101), so this placeholder never changes a tool list.
_DEPTH_MODE: Final = "standard"
# sqlglot's "falling back to Command" warning quotes the SQL text; never log SQL text.
logging.getLogger("sqlglot").addFilter(lambda r: "unsupported syntax" not in r.getMessage())


def _entity_key(f: Finding) -> str:
    return f"{f.entity_type}:{f.entity_id}"


# --- selection and verdicts (U06-104 … U06-106) ------------------------------------------------


def select_for_challenge(
    findings: Sequence[Finding],
    *,
    score: Callable[[Finding], float],
    top_n: int,
    must_cover: set[str],
    retrospective_task_ids: set[str],
    excluded_task_ids: set[str],
) -> list[tuple[Finding, float]]:
    """Findings for the first Skeptic round with their scores, in `(-score, id)` order (U06-104).

    Selected = the first `top_n` | the first finding of every must-cover entity | every finding
    of a retrospective task; findings of `excluded_task_ids` (crosscheck tasks) never count.
    """
    scored = [(f, score(f)) for f in findings if f.task_id not in excluded_task_ids]
    scored.sort(key=lambda pair: (-pair[1], pair[0].finding_id))
    picked = {f.finding_id for f, _ in scored[: max(top_n, 0)]}
    covered: set[str] = set()
    for f, _ in scored:
        key = _entity_key(f)
        if key in must_cover and key not in covered:
            covered.add(key)
            picked.add(f.finding_id)
        if f.task_id in retrospective_task_ids:
            picked.add(f.finding_id)
    return [(f, s) for f, s in scored if f.finding_id in picked]


def normalize_verdict(ch: Challenge) -> Challenge:
    """Downgrade an unevidenced `reject` to `revise` (U06-105, TH06-07); pure.

    A `reject` stands only when some check has `result == "fail"` with `query_ids`.
    """
    if ch.verdict != "reject" or any(c.result == "fail" and c.query_ids for c in ch.checks):
        return ch
    actions = list(ch.required_actions)
    if not actions:
        names = [c.check for c in ch.checks if c.result in {"concern", "fail"}]
        actions = [f"Address these checks: {', '.join(names)}" if names else _DEFAULT_ACTION]
    return ch.model_copy(update={"verdict": "revise", "required_actions": actions})


def majority_verdict(verdicts: Sequence[Verdict]) -> tuple[str, dict[str, int]]:
    """The winning verdict and the tallies of the verdicts seen (U06-106); pure.

    The highest count wins; ties go to the stricter verdict (`reject` > `revise` > `uphold`).
    """
    if not verdicts:
        msg = "majority_verdict needs at least one verdict"
        raise ValueError(msg)
    tally = Counter(verdicts)
    winner = max(tally, key=lambda v: (tally[v], _STRICTNESS[v]))
    return winner, {v: tally[v] for v in sorted(tally, key=_STRICTNESS.__getitem__)}


# --- task builders (U06-107) ------------------------------------------------------------------


def build_skeptic_task(  # noqa: PLR0913 - U06-107 signature
    f: Finding,
    *,
    run: RunRow,
    round_no: int,
    final_round: bool,
    score: float,
    knobs: DepthKnobs,
    new_task_id: Callable[[], str],
) -> TaskSpec:
    """The Skeptic task challenging `f` in round `round_no` (U06-107).

    The final round of a deep or hybrid run uses model role `skeptic_final`.
    """
    scope = EntityScope(entity_type=f.entity_type, entity_ids=[f.entity_id])
    objective = (
        f"Challenge finding {f.finding_id} (round {round_no}): "
        "run the six checks and return a verdict."
    )
    final = final_round and (run.depth == "deep" or run.profile == "hybrid")
    return TaskSpec(
        task_id=new_task_id(),
        run_id=run.run_id,
        role="skeptic",
        objective=objective,
        scope=scope,
        inputs=TaskInputs(finding_ids=[f.finding_id], query_ids=list(f.query_ids)),
        tools=default_tools("skeptic", "general", _DEPTH_MODE, child_depth=0, knobs=knobs),
        budget=role_budget("skeptic", knobs),
        round=round_no,
        priority=score,
        k_samples=knobs.k_samples.default,
        model_role="skeptic_final" if final else "skeptic",
        dedup_key=compute_dedup_key("skeptic", "general", scope, objective),
    )


def build_revision_task(
    f: Finding,
    ch: Challenge,
    *,
    skeptic: TaskSpec,
    author: TaskSpec,
    knobs: DepthKnobs,
    new_task_id: Callable[[], str],
) -> TaskSpec:
    """The analyst task revising `f` after a `revise` verdict `ch` (U06-107).

    It keeps the author's specialty and scope, runs one round after the Skeptic and cannot
    spawn (`child_depth = max_spawn_depth`).
    """
    objective = (
        f"Revise finding {f.finding_id}: {f.claim} Required actions: "
        + "; ".join(ch.required_actions)
    )[:_OBJECTIVE_MAX]
    tools = default_tools(
        "analyst", author.specialty, _DEPTH_MODE, child_depth=knobs.max_spawn_depth, knobs=knobs
    )
    return TaskSpec(
        task_id=new_task_id(),
        run_id=skeptic.run_id,
        role="analyst",
        specialty=author.specialty,
        objective=objective,
        scope=author.scope,
        inputs=TaskInputs(finding_ids=[f.finding_id], query_ids=list(f.query_ids)),
        tools=tools,
        budget=knobs.analyst_budget,
        depth=0,
        parent_task_id=skeptic.task_id,
        priority=skeptic.priority,
        model_role="analyst",
        dedup_key=compute_dedup_key("analyst", author.specialty, author.scope, objective),
        revision_of=f.finding_id,
        round=skeptic.round + 1,
    )


# --- cross-check planning (U06-110) -----------------------------------------------------------


def _number_order(n: NumberRef) -> int:
    return int(n.id[1:])


def _key_numbers(
    f: Finding, *, deep: bool, must_cover: set[str], selected: set[str]
) -> list[NumberRef]:
    """Key numbers of `f` (U06-110 step 2), in number-id order."""
    if deep and f.finding_id in selected:
        numbers = list(f.numbers)
    elif deep or _entity_key(f) in must_cover:
        numbers = [n for n in f.numbers if n.unit == "usd"]
    else:
        numbers = []
    return sorted(numbers, key=_number_order)


def _row_text(n: NumberRef) -> str:
    if not n.row_key:
        return "single row"
    return ", ".join(f"{k}={v}" for k, v in sorted(n.row_key.items()))


def _crosscheck_task(
    f: Finding, n: NumberRef, way: int, run: RunRow, knobs: DepthKnobs, task_id: str
) -> TaskSpec:
    scope = EntityScope(entity_type=f.entity_type, entity_ids=[f.entity_id])
    objective = (
        f"Compute independently: {n.column} for {f.entity_type} {f.entity_id}, unit {n.unit}, "
        f"period the pipeline window, row {_row_text(n)}. Use a different table or aggregation "
        f"path. Post one finding whose first number is this value (way {way})."
    )[:_OBJECTIVE_MAX]
    return TaskSpec(
        task_id=task_id,
        run_id=run.run_id,
        role="analyst",
        specialty="crosscheck",
        objective=objective,
        scope=scope,
        inputs=TaskInputs(finding_ids=[f.finding_id], notes=f"target_number={n.id}"),
        tools=default_tools("analyst", "crosscheck", _DEPTH_MODE, child_depth=0, knobs=knobs),
        budget=knobs.analyst_budget,
        priority=0.0,
        model_role="analyst",
        dedup_key=compute_dedup_key("analyst", "crosscheck", scope, objective),
    )


def plan_crosschecks(  # noqa: PLR0913 - U06-110 signature
    findings: Sequence[Finding],
    *,
    run: RunRow,
    knobs: DepthKnobs,
    must_cover: set[str],
    selected_ids: set[str],
    task_count: int,
    new_task_id: Callable[[], str],
) -> tuple[list[TaskSpec], list[str]]:
    """Cross-check tasks (`ways - 1` per key number) and the finding ids skipped for the cap.

    Standard: `usd` numbers of must-cover findings; deep: every `usd` number plus every number
    of `selected_ids`. Tasks carry only the finding id and the target number id, never the
    original query or SQL (U06-110). Planning stops at `knobs.max_tasks_per_run`.
    """
    ways = knobs.crosscheck_ways
    if ways <= 1:
        return [], []
    deep = run.depth == "deep"
    tasks: list[TaskSpec] = []
    skipped: list[str] = []
    for f in sorted(findings, key=lambda x: x.finding_id):
        numbers = _key_numbers(f, deep=deep, must_cover=must_cover, selected=selected_ids)
        plan = [(n, way) for n in numbers for way in range(1, ways)]
        for n, way in plan:
            if task_count + len(tasks) >= knobs.max_tasks_per_run:
                skipped.append(f.finding_id)
                break
            tasks.append(_crosscheck_task(f, n, way, run, knobs, new_task_id()))
    return tasks, skipped


# --- SQL independence and agreement (U06-111, U06-112) ----------------------------------------

type _Path = tuple[frozenset[str], list[tuple[str, list[str]]], list[str]]


def _columns(node: exp.Expr) -> list[str]:
    return sorted(c.name for c in node.find_all(exp.Column))


def _sql_path(sql: str) -> _Path | None:
    """Tables, aggregation signature and group-by columns of `sql`; None when unparsable."""
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except SqlglotError:
        return None
    if isinstance(tree, exp.Command):
        return None
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    tables = frozenset(
        f"{t.db}.{t.name}" if t.db else t.name
        for t in tree.find_all(exp.Table)
        if t.db or t.name not in ctes
    )
    aggs = sorted((a.sql_name().lower(), _columns(a)) for a in tree.find_all(exp.AggFunc))
    groups = sorted(name for g in tree.find_all(exp.Group) for name in _columns(g))
    return tables, aggs, groups


def is_independent_sql(sql_a: str, sql_b: str) -> bool:
    """Whether `sql_b` took a different path than `sql_a` (U06-111); pure.

    Independent when the qualified tables (CTE names excluded), the aggregation signatures or
    the group-by columns differ. An unparsable query is never independent.
    """
    a = _sql_path(sql_a)
    b = _sql_path(sql_b)
    if a is None or b is None:
        return False
    return a != b


def crosscheck_agree(
    a: float, b: float, unit: str, *, rel_tol: float, abs_tol_count: float, abs_tol_ratio: float
) -> bool:
    """Agreement of an original and a cross-check value (U06-112); pure.

    `ratio` and `pct` compare absolutely with `abs_tol_ratio`; every other unit within
    `max(rel_tol * |a|, abs_tol_count)`.
    """
    if unit in _RATIO_UNITS:
        return abs(a - b) <= abs_tol_ratio
    return abs(a - b) <= max(rel_tol * abs(a), abs_tol_count)
