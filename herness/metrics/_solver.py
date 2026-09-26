"""Pure CP-SAT portfolio model, ordering and binding constraints (design 04 §5.10).

No I/O. The model is integer: whole dollars (effort up, impact and budget down) and
hundredths of story points (points up, capacity down). One worker, a fixed seed and
deterministic plus wall-clock limits keep it reproducible and bounded (TH04-08).
"""

import heapq
import math
import types
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from ortools.sat.python import cp_model

from herness.core.errors import ConfigError
from herness.metrics.settings import SolverConfig

SolverStatus = Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "MODEL_INVALID"]
WALL_LIMIT_MARGIN_S = 0.5
_STATUS: dict[cp_model.CpSolverStatus, tuple[SolverStatus, tuple[str, ...]]] = {
    cp_model.OPTIMAL: ("OPTIMAL", ()),
    cp_model.FEASIBLE: ("FEASIBLE", ("not_proven_optimal",)),
    cp_model.INFEASIBLE: ("INFEASIBLE", ()),
    cp_model.MODEL_INVALID: ("MODEL_INVALID", ()),
    cp_model.UNKNOWN: ("INFEASIBLE", ("no_solution_found",)),  # DD04-10
}


@dataclass(frozen=True, slots=True)
class PortfolioCandidate:
    """One optimizer input row (U04-79)."""

    candidate_id: str
    candidate_type: str
    team_id: str | None
    effort_cost_usd: Decimal | None
    expected_impact_usd: Decimal
    priority: float | None
    points: float | None
    descendants: tuple[str, ...]
    blockers: tuple[str, ...]
    noncandidate_blockers: int


@dataclass(frozen=True, slots=True)
class SolveOutcome:
    """Solver result; `selected` is empty unless the status is OPTIMAL or FEASIBLE."""

    status: SolverStatus
    selected: frozenset[str]
    row_flags: Mapping[str, tuple[str, ...]]
    flags: tuple[str, ...]
    wall_time_s: float


def _effort_units(cand: PortfolioCandidate) -> int:
    """Whole dollars of effort, rounded up; 0 when there is no estimate."""
    return 0 if cand.effort_cost_usd is None else math.ceil(cand.effort_cost_usd)


def _points_units(cand: PortfolioCandidate) -> int:
    """Hundredths of a story point, rounded up; NULL points count 0."""
    return 0 if cand.points is None else math.ceil(Decimal(repr(cand.points)) * 100)


def _capacity_units(capacity: float, horizon_quarters: int) -> int:
    """Hundredths of a point a team can deliver over the horizon, rounded down."""
    return math.floor(Decimal(repr(capacity)) * horizon_quarters * 100)


def _check_preconditions(
    by_id: Mapping[str, PortfolioCandidate], mandatory: frozenset[str], excluded: frozenset[str]
) -> None:
    for cid in sorted(mandatory & excluded):
        msg = f"candidate {cid} is both mandatory and excluded"
        raise ConfigError(msg)
    for cid in sorted(mandatory):
        cand = by_id.get(cid)
        if cand is None:
            msg = f"unknown mandatory candidate {cid}"
            raise ConfigError(msg)
        if cand.effort_cost_usd is None:
            msg = f"mandatory candidate {cid} has no effort estimate"
            raise ConfigError(msg)


def _row_flags(
    cand: PortfolioCandidate, modeled: Collection[str], capacity: bool
) -> tuple[str, ...]:
    flags: list[str] = []
    if cand.effort_cost_usd is None:
        flags.append("no_estimate")
    else:
        if any(blocker not in modeled for blocker in cand.blockers):
            flags.append("blocked_by_unestimated")
        if capacity and cand.team_id is None:
            flags.append("no_team")
        elif capacity and cand.points is None:
            flags.append("no_points")
    if cand.noncandidate_blockers > 0:
        flags.append("blocked_by_noncandidate")
    return tuple(sorted(flags))


def _add_capacity(
    model: cp_model.CpModel,
    x: Mapping[str, cp_model.IntVar],
    modeled: Sequence[PortfolioCandidate],
    capacities: Mapping[str, float],
    capacity_units: tuple[float, int],
) -> None:
    """Per team: points of the selected (hundredths) within capacity over the horizon."""
    default, horizon = capacity_units
    for team in sorted({c.team_id for c in modeled if c.team_id is not None}):
        members = [c for c in modeled if c.team_id == team]
        used = cp_model.LinearExpr.weighted_sum(
            [x[c.candidate_id] for c in members], [_points_units(c) for c in members]
        )
        model.add(used <= _capacity_units(capacities.get(team, default), horizon))


def _add_links(
    model: cp_model.CpModel,
    x: Mapping[str, cp_model.IntVar],
    modeled: Sequence[PortfolioCandidate],
) -> None:
    """Blockers first (x_B ≤ x_A, or x_B = 0 when A is not modeled); parent xor descendant."""
    for cand in modeled:
        own = x[cand.candidate_id]
        for blocker in cand.blockers:
            model.add(own <= x[blocker] if blocker in x else own == 0)
        for child in cand.descendants:
            if child in x:
                model.add(own + x[child] <= 1)


def solve_portfolio(  # noqa: PLR0913 - keyword-only signature fixed by U04-73
    *,
    candidates: Sequence[PortfolioCandidate],
    budget_usd: Decimal,
    mandatory: frozenset[str],
    excluded: frozenset[str],
    capacities: Mapping[str, float] | None,
    capacity_default: float,
    horizon_quarters: int,
    solver: SolverConfig,
) -> SolveOutcome:
    """Build and solve the CP-SAT portfolio model (U04-73); raises `ConfigError`."""
    ordered = sorted(candidates, key=lambda c: c.candidate_id)
    by_id = {c.candidate_id: c for c in ordered}
    _check_preconditions(by_id, mandatory, excluded)
    modeled = [c for c in ordered if c.effort_cost_usd is not None]
    model = cp_model.CpModel()
    x = {c.candidate_id: model.new_bool_var(f"x{i}") for i, c in enumerate(modeled)}
    chosen, impact = list(x.values()), [math.floor(c.expected_impact_usd) for c in modeled]
    model.maximize(cp_model.LinearExpr.weighted_sum(chosen, impact))
    effort = cp_model.LinearExpr.weighted_sum(chosen, [_effort_units(c) for c in modeled])
    model.add(effort <= math.floor(budget_usd))
    if capacities is not None:
        _add_capacity(model, x, modeled, capacities, (capacity_default, horizon_quarters))
    for cid in sorted(mandatory):
        model.add(x[cid] == 1)
    for cid in sorted(excluded & x.keys()):
        model.add(x[cid] == 0)
    _add_links(model, x, modeled)

    cp_solver = cp_model.CpSolver()
    params = cp_solver.parameters
    params.num_workers = solver.num_workers
    params.random_seed = solver.random_seed
    params.max_deterministic_time = solver.max_deterministic_time
    params.max_time_in_seconds = solver.max_time_in_seconds
    params.log_search_progress = False
    status, status_flags = _STATUS[cp_solver.solve(model)]
    wall = cp_solver.wall_time
    flags = set(status_flags)
    if excluded - by_id.keys():
        flags.add("unknown_excluded")
    if wall >= solver.max_time_in_seconds - WALL_LIMIT_MARGIN_S:
        flags.add("wall_clock_limit")
    solved = status in {"OPTIMAL", "FEASIBLE"}
    selected = frozenset(cid for cid, var in x.items() if solved and cp_solver.boolean_value(var))
    row_flags = {c.candidate_id: _row_flags(c, x, capacities is not None) for c in ordered}
    return SolveOutcome(
        status=status,
        selected=selected,
        row_flags=types.MappingProxyType(row_flags),
        flags=tuple(sorted(flags)),
        wall_time_s=wall,
    )


def _components(succ: Mapping[str, set[str]]) -> dict[str, str]:
    """Strongly connected component (named by its root) of every node; iterative Tarjan."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    comp: dict[str, str] = {}
    stack: list[str] = []
    at: dict[str, int] = {}
    work: list[tuple[str, Iterator[str]]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        at[node] = len(stack)
        stack.append(node)
        work.append((node, iter(sorted(succ[node]))))

    for root in sorted(succ):
        if root not in index:
            visit(root)
        while work:
            node, edges = work[-1]
            nxt = next(edges, None)
            if nxt is None:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    comp.update(dict.fromkeys(stack[at[node] :], node))
                    del stack[at[node] :]
            elif nxt not in index:
                visit(nxt)
            elif nxt not in comp:
                low[node] = min(low[node], index[nxt])
    return comp


def order_selected(selected: Sequence[PortfolioCandidate], /) -> tuple[dict[str, int], bool]:
    """Ranks 1..n, topological over blocks links, ties `priority DESC, candidate_id` (U04-74).

    A cycle is broken (`had_cycle`) at the smallest-key node whose unranked blockers all
    share its cycle, so blocks edges outside cycles hold."""
    by_id = {c.candidate_id: c for c in selected}
    succ: dict[str, set[str]] = {cid: set() for cid in by_id}
    preds: dict[str, set[str]] = {cid: set() for cid in by_id}
    for cand in by_id.values():
        for blocker in cand.blockers:
            if blocker in by_id:
                succ[blocker].add(cand.candidate_id)
                preds[cand.candidate_id].add(blocker)

    def key(cid: str) -> tuple[float, str]:
        priority = by_id[cid].priority
        return (math.inf if priority is None else -priority, cid)

    comp = _components(succ)
    waiting = {cid: len(p) for cid, p in preds.items()}
    heap = [key(cid) for cid, n in waiting.items() if n == 0]
    heapq.heapify(heap)
    ranks: dict[str, int] = {}
    had_cycle = False
    while len(ranks) < len(by_id):
        if heap:
            cid = heapq.heappop(heap)[1]
        else:
            had_cycle = True
            cid = min(
                key(n)
                for n in by_id
                if n not in ranks and all(p in ranks or comp[p] == comp[n] for p in preds[n])
            )[1]
        ranks[cid] = len(ranks) + 1
        for nxt in succ[cid]:
            waiting[nxt] -= 1
            if waiting[nxt] == 0 and nxt not in ranks:
                heapq.heappush(heap, key(nxt))
    return ranks, had_cycle


def binding_constraints(  # noqa: PLR0913 - keyword-only signature fixed by U04-75
    *,
    candidates: Sequence[PortfolioCandidate],
    selected: Collection[str],
    budget_usd: Decimal,
    mandatory: frozenset[str],
    excluded: frozenset[str],
    capacities: Mapping[str, float] | None,
    capacity_default: float,
    horizon_quarters: int,
) -> list[str]:
    """Constraints that stopped further selection, sorted (U04-75)."""
    chosen = [c for c in candidates if c.candidate_id in selected]
    taken = set(selected) | excluded
    rest = [c for c in candidates if c.candidate_id not in taken and c.effort_cost_usd is not None]
    names: list[str] = []
    left = math.floor(budget_usd) - sum(_effort_units(c) for c in chosen)
    if rest and left < min(_effort_units(c) for c in rest):
        names.append("budget")
    if capacities is not None:
        for team in sorted({c.team_id for c in rest if c.team_id is not None}):
            cap = _capacity_units(capacities.get(team, capacity_default), horizon_quarters)
            room = cap - sum(_points_units(c) for c in chosen if c.team_id == team)
            if room < min(_points_units(c) for c in rest if c.team_id == team):
                names.append(f"capacity:{team}")
    if mandatory:
        names.append("mandatory")
    return sorted(names)
