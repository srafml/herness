"""Property tests for herness.metrics._solver (PT04-08 … PT04-10), checked against brute force."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from herness.metrics._solver import PortfolioCandidate, order_selected, solve_portfolio
from herness.metrics.settings import SolverConfig

pytestmark = pytest.mark.unit

SOLVER = SolverConfig(
    num_workers=1, random_seed=0, max_deterministic_time=20, max_time_in_seconds=60
)
IDS = [f"c{i:02d}" for i in range(15)]
SLOW_OK = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])


@dataclass(frozen=True)
class Instance:
    candidates: list[PortfolioCandidate]
    budget: Decimal
    mandatory: frozenset[str]
    excluded: frozenset[str]
    capacities: Mapping[str, float] | None
    capacity_default: float
    horizon: int


def _links(pool: list[str]) -> st.SearchStrategy[tuple[str, ...]]:
    """Mostly no links, so that a good share of instances selects something."""
    some = st.sets(st.sampled_from(pool), min_size=1, max_size=2).map(lambda s: tuple(sorted(s)))
    return st.one_of(st.just(()), st.just(()), some)


@st.composite
def _candidate(draw: st.DrawFn, cid: str, others: list[str]) -> PortfolioCandidate:
    effort = draw(st.none() | st.decimals(0, 400, places=2))
    return PortfolioCandidate(
        candidate_id=cid,
        candidate_type=draw(st.sampled_from(["work_item", "cluster_fix"])),
        team_id=draw(st.sampled_from([None, "t1", "t2"])),
        effort_cost_usd=effort,
        expected_impact_usd=draw(st.decimals(0, 1000, places=2) | st.decimals(1, 1000, places=2)),
        priority=draw(st.none() | st.sampled_from([0.0, 1.0, 2.5, 7.0])),
        points=draw(st.none() | st.sampled_from([0.0, 0.5, 1.0, 1.25, 2.0])),
        descendants=draw(_links(others)),
        blockers=draw(_links([*others, "zz"])),
        noncandidate_blockers=draw(st.integers(0, 1)),
    )


@st.composite
def instances(draw: st.DrawFn) -> Instance:
    n = draw(st.integers(1, 15))
    ids = IDS[:n]
    cands = [draw(_candidate(cid, [o for o in ids if o != cid] or ["zz"])) for cid in ids]
    modeled = [c.candidate_id for c in cands if c.effort_cost_usd is not None]
    use_mandatory = modeled and draw(st.integers(0, 3)) == 0
    mandatory = draw(st.sets(st.sampled_from(modeled), max_size=2)) if use_mandatory else set()
    rest = [cid for cid in [*ids, "unknown"] if cid not in mandatory]
    excluded = draw(st.sets(st.sampled_from(rest), max_size=3))
    caps = draw(st.none() | st.dictionaries(st.just("t1"), st.sampled_from([0.5, 1.0, 2.0])))
    return Instance(
        candidates=cands,
        budget=draw(st.decimals(0, 2000, places=2) | st.decimals(300, 3000, places=2)),
        mandatory=frozenset(mandatory),
        excluded=frozenset(excluded),
        capacities=caps,
        capacity_default=draw(st.sampled_from([0.5, 1.0, 3.0])),
        horizon=draw(st.integers(1, 2)),
    )


def _solve(inst: Instance) -> tuple[str, frozenset[str]]:
    outcome = solve_portfolio(
        candidates=inst.candidates,
        budget_usd=inst.budget,
        mandatory=inst.mandatory,
        excluded=inst.excluded,
        capacities=inst.capacities,
        capacity_default=inst.capacity_default,
        horizon_quarters=inst.horizon,
        solver=SOLVER,
    )
    return outcome.status, outcome.selected


def _capacity_violations(inst: Instance, picked: list[PortfolioCandidate]) -> list[str]:
    if inst.capacities is None:
        return []
    out = []
    for team in ("t1", "t2"):
        cap = inst.capacities.get(team, inst.capacity_default)
        used = sum(math.ceil((c.points or 0) * 100) for c in picked if c.team_id == team)
        if used > math.floor(cap * inst.horizon * 100):
            out.append(f"capacity:{team}")
    return out


def _violations(inst: Instance, chosen: frozenset[str]) -> list[str]:
    """Independent check of every design 04 §5.10 constraint on a chosen set."""
    by_id = {c.candidate_id: c for c in inst.candidates}
    picked = [by_id[cid] for cid in sorted(chosen)]
    out: list[str] = []
    if any(c.effort_cost_usd is None for c in picked):
        out.append("unmodeled")
    elif sum(math.ceil(c.effort_cost_usd or 0) for c in picked) > math.floor(inst.budget):
        out.append("budget")
    out.extend(_capacity_violations(inst, picked))
    if not inst.mandatory <= chosen:
        out.append("mandatory")
    if inst.excluded & chosen:
        out.append("excluded")
    for c in picked:
        if any(b not in chosen for b in c.blockers):
            out.append(f"blocker:{c.candidate_id}")
        if any(d in chosen for d in c.descendants):
            out.append(f"descendant:{c.candidate_id}")
    return out


def _objective(inst: Instance, chosen: frozenset[str]) -> int:
    return sum(
        math.floor(c.expected_impact_usd) for c in inst.candidates if c.candidate_id in chosen
    )


def _brute_force(inst: Instance) -> int | None:
    modeled = [c.candidate_id for c in inst.candidates if c.effort_cost_usd is not None]
    best: int | None = None
    for mask in range(1 << len(modeled)):
        chosen = frozenset(cid for i, cid in enumerate(modeled) if mask >> i & 1)
        if not _violations(inst, chosen):
            value = _objective(inst, chosen)
            best = value if best is None else max(best, value)
    return best


@SLOW_OK
@given(instances())
def test_pt04_09_objective_equals_brute_force(inst: Instance) -> None:
    """PT04-09 random instances of at most 15 candidates reach the brute-force optimum."""
    status, selected = _solve(inst)
    best = _brute_force(inst)
    if best is None:
        assert status == "INFEASIBLE"
        assert selected == frozenset()
    else:
        assert status == "OPTIMAL"
        assert _objective(inst, selected) == best


@SLOW_OK
@given(instances())
def test_pt04_10_constraints_hold(inst: Instance) -> None:
    """PT04-10 budget, capacity, mandatory, excluded, parent/descendant and blockers hold."""
    status, selected = _solve(inst)
    if status in {"OPTIMAL", "FEASIBLE"}:
        assert _violations(inst, selected) == []
    else:
        assert selected == frozenset()


def _reach(edges: set[tuple[str, str]], nodes: list[str]) -> dict[str, set[str]]:
    reach = {n: {b for a, b in edges if a == n} for n in nodes}
    changed = True
    while changed:
        changed = False
        for n in nodes:
            extra = set().union(*(reach[m] for m in reach[n])) - reach[n]
            if extra:
                reach[n] |= extra
                changed = True
    return reach


@given(st.integers(0, 12).flatmap(lambda n: st.tuples(*(_candidate(i, IDS) for i in IDS[:n]))))
def test_pt04_08_order_respects_blocks_outside_cycles(cands: tuple[PortfolioCandidate]) -> None:
    """PT04-08 order_selected respects every blocks edge outside cycles."""
    ranks, had_cycle = order_selected(list(cands))
    ids = [c.candidate_id for c in cands]
    assert sorted(ranks.values()) == list(range(1, len(ids) + 1))
    edges = {(a, c.candidate_id) for c in cands for a in c.blockers if a in ranks}
    reach = _reach(edges, ids)
    cyclic = any(n in reach[n] for n in ids)
    assert had_cycle == cyclic
    for a, b in edges:
        if not (b in reach[a] and a in reach[b]):
            assert ranks[a] < ranks[b]
