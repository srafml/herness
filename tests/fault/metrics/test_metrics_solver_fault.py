"""Fault tests for herness.metrics._solver (FT04-04, TH04-08)."""

import random
from decimal import Decimal

import pytest

from herness.metrics._solver import PortfolioCandidate, solve_portfolio
from herness.metrics.settings import SolverConfig

pytestmark = pytest.mark.fault


def _instance(n: int) -> list[PortfolioCandidate]:
    rng = random.Random(4)
    ids = [f"k{i:05d}" for i in range(n)]
    out = []
    for i, cid in enumerate(ids):
        weight = rng.randint(10_000, 1_000_000)
        parents = rng.sample(ids[:i], k=min(i, 1)) if rng.random() < 0.2 else []
        out.append(
            PortfolioCandidate(
                candidate_id=cid,
                candidate_type="work_item",
                team_id=f"t{rng.randint(0, 20)}",
                effort_cost_usd=Decimal(weight),
                expected_impact_usd=Decimal(weight + rng.randint(0, 5000)),
                priority=rng.random(),
                points=rng.choice([0.5, 1.0, 2.0, 3.0, 5.0, 8.0]),
                descendants=tuple(rng.sample(ids, k=2)) if rng.random() < 0.1 else (),
                blockers=tuple(parents),
                noncandidate_blockers=0,
            )
        )
    return out


def test_ft04_04_wall_limit_on_5k_candidates() -> None:
    """FT04-04 a 1 s wall limit on 5k candidates returns FEASIBLE or a flagged no-solution."""
    items = _instance(5000)
    budget = sum((c.effort_cost_usd or Decimal(0)) for c in items) / 3
    solver = SolverConfig(
        num_workers=1, random_seed=0, max_deterministic_time=600, max_time_in_seconds=1
    )
    outcome = solve_portfolio(
        candidates=items,
        budget_usd=budget,
        mandatory=frozenset(),
        excluded=frozenset(),
        capacities={},
        capacity_default=40.0,
        horizon_quarters=2,
        solver=solver,
    )
    assert outcome.wall_time_s < 5
    limit_flags = {"no_solution_found", "wall_clock_limit"} & set(outcome.flags)
    assert outcome.status == "FEASIBLE" or limit_flags
    if outcome.status != "FEASIBLE":
        assert outcome.selected == frozenset()
