"""Tests for herness.metrics._solver (U04-72 … U04-75)."""

import dataclasses
import random
from decimal import Decimal

import pytest

from herness.core.errors import ConfigError
from herness.metrics._solver import (
    PortfolioCandidate,
    SolveOutcome,
    binding_constraints,
    order_selected,
    solve_portfolio,
)
from herness.metrics.settings import SolverConfig

pytestmark = pytest.mark.unit

SOLVER = SolverConfig(
    num_workers=1, random_seed=0, max_deterministic_time=20, max_time_in_seconds=60
)


def _cand(  # noqa: PLR0913 - test builder with a default for every field
    cid: str,
    effort: str | None = "100",
    impact: str = "1000",
    *,
    team: str | None = None,
    points: float | None = None,
    priority: float | None = None,
    descendants: tuple[str, ...] = (),
    blockers: tuple[str, ...] = (),
    noncandidate_blockers: int = 0,
) -> PortfolioCandidate:
    return PortfolioCandidate(
        candidate_id=cid,
        candidate_type="work_item",
        team_id=team,
        effort_cost_usd=None if effort is None else Decimal(effort),
        expected_impact_usd=Decimal(impact),
        priority=priority,
        points=points,
        descendants=descendants,
        blockers=blockers,
        noncandidate_blockers=noncandidate_blockers,
    )


def _solve(
    candidates: list[PortfolioCandidate],
    budget: str,
    *,
    mandatory: frozenset[str] = frozenset(),
    excluded: frozenset[str] = frozenset(),
    capacities: dict[str, float] | None = None,
    solver: SolverConfig = SOLVER,
) -> SolveOutcome:
    return solve_portfolio(
        candidates=candidates,
        budget_usd=Decimal(budget),
        mandatory=mandatory,
        excluded=excluded,
        capacities=capacities,
        capacity_default=1.0,
        horizon_quarters=2,
        solver=solver,
    )


# --- UT04-101: small instance -----------------------------------------------------------------

SMALL = [
    _cand("a", "100.10", "500.90", team="t1", points=1.5),
    _cand("b", "200", "800", team="t1", points=1.0),
    _cand("c", "300", "900", team="t2", points=None),
    _cand("d", None, "5000"),
    _cand("e", "60", "5000", team="t2", points=0.0, blockers=("d",)),
    _cand("f", "50", "100", noncandidate_blockers=2),
    _cand("p", "1", "100", team="t2", points=0.1, descendants=("b",)),
    _cand("x", "1", "99999", team="t2", points=0.0),
]
MANDATORY = frozenset({"f"})
EXCLUDED = frozenset({"x", "zz"})
CAPS = {"t1": 1.0}


def test_ut04_101_small_instance_constraints_hold() -> None:
    """UT04-101 optimum under budget, capacity, blocker, parent, mandatory and excluded rules."""
    outcome = _solve(
        list(reversed(SMALL)), "500.99", mandatory=MANDATORY, excluded=EXCLUDED, capacities=CAPS
    )
    assert outcome.status == "OPTIMAL"
    assert outcome.selected == frozenset({"a", "c", "f", "p"})
    assert outcome.flags == ("unknown_excluded",)
    assert dict(outcome.row_flags) == {
        "a": (),
        "b": (),
        "c": ("no_points",),
        "d": ("no_estimate",),
        "e": ("blocked_by_unestimated",),
        "f": ("blocked_by_noncandidate", "no_team"),
        "p": (),
        "x": (),
    }
    assert outcome.wall_time_s >= 0


def test_ut04_101_solve_is_deterministic() -> None:
    """UT04-101 same inputs give the same outcome apart from wall time."""
    kwargs = {"mandatory": MANDATORY, "excluded": EXCLUDED, "capacities": CAPS}
    first = _solve(SMALL, "500", **kwargs)  # type: ignore[arg-type]
    second = _solve(list(reversed(SMALL)), "500", **kwargs)  # type: ignore[arg-type]
    assert dataclasses.replace(first, wall_time_s=0.0) == dataclasses.replace(
        second, wall_time_s=0.0
    )


def test_ut04_101_capacity_not_enforced_without_capacities() -> None:
    """UT04-101 capacities None: no capacity terms, no team or points flags."""
    outcome = _solve(SMALL, "500", excluded=frozenset({"x"}))
    assert outcome.status == "OPTIMAL"
    assert outcome.selected == frozenset({"b", "c"})
    assert outcome.row_flags["c"] == ()
    assert outcome.row_flags["f"] == ("blocked_by_noncandidate",)
    assert outcome.flags == ()


def test_ut04_101_binding_constraint_names() -> None:
    """UT04-101 budget, capacity:<team> and mandatory name what stopped selection."""
    names = binding_constraints(
        candidates=SMALL,
        selected=frozenset({"a", "c", "f", "p"}),
        budget_usd=Decimal("500.99"),
        mandatory=MANDATORY,
        excluded=EXCLUDED,
        capacities=CAPS,
        capacity_default=1.0,
        horizon_quarters=2,
    )
    assert names == ["budget", "capacity:t1", "mandatory"]


def test_ut04_101_binding_constraints_none_when_room_left() -> None:
    """UT04-101 nothing binds when every remaining candidate still fits."""
    names = binding_constraints(
        candidates=[_cand("a", "10", team="t1", points=1.0), _cand("b", "10", team="t1")],
        selected=frozenset({"a"}),
        budget_usd=Decimal("100"),
        mandatory=frozenset(),
        excluded=frozenset(),
        capacities={},
        capacity_default=5.0,
        horizon_quarters=1,
    )
    assert names == []


def test_ut04_101_binding_ignores_excluded_and_unmodeled() -> None:
    """UT04-101 excluded and effort-less candidates never make the budget bind."""
    names = binding_constraints(
        candidates=[_cand("a", "90"), _cand("b", "50"), _cand("c", None)],
        selected=frozenset({"a"}),
        budget_usd=Decimal("100"),
        mandatory=frozenset(),
        excluded=frozenset({"b"}),
        capacities=None,
        capacity_default=1.0,
        horizon_quarters=1,
    )
    assert names == []


def test_ut04_101_model_invalid_on_overflow() -> None:
    """UT04-101 an objective that overflows int64 maps to MODEL_INVALID with no selection."""
    huge = str(5 * 10**18)
    outcome = _solve([_cand("a", "1", huge), _cand("b", "1", huge)], "10")
    assert outcome.status == "MODEL_INVALID"
    assert outcome.selected == frozenset()


def test_ut04_101_candidate_is_frozen() -> None:
    """UT04-101 PortfolioCandidate is immutable."""
    cand = _cand("a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cand.candidate_id = "b"  # type: ignore[misc]


# --- UT04-102: ordering -----------------------------------------------------------------------


def test_ut04_102_chain_is_topological() -> None:
    """UT04-102 blockers come first even when their priority is lower."""
    ranks, had_cycle = order_selected(
        [
            _cand("c3", priority=9.0, blockers=("c2",)),
            _cand("c2", priority=5.0, blockers=("c1",)),
            _cand("c1", priority=1.0),
            _cand("z", priority=None),
            _cand("y", priority=5.0),
        ]
    )
    assert had_cycle is False
    assert ranks == {"y": 1, "c1": 2, "c2": 3, "c3": 4, "z": 5}


def test_ut04_102_cycle_flagged_and_broken() -> None:
    """UT04-102 a cycle is broken at its smallest key and reported."""
    ranks, had_cycle = order_selected(
        [
            _cand("a", priority=1.0, blockers=("b",)),
            _cand("b", priority=2.0, blockers=("a",)),
            _cand("d", priority=9.0, blockers=("b", "gone")),
        ]
    )
    assert had_cycle is True
    assert ranks == {"b": 1, "d": 2, "a": 3}


def test_ut04_102_cycle_break_respects_upstream_cycle() -> None:
    """UT04-102 a node downstream of a cycle is not forced before the cycle."""
    ranks, had_cycle = order_selected(
        [
            _cand("a", priority=1.0, blockers=("b",)),
            _cand("b", priority=1.0, blockers=("a",)),
            _cand("x", priority=9.0, blockers=("b",)),
        ]
    )
    assert had_cycle is True
    assert ranks == {"a": 1, "b": 2, "x": 3}


def test_ut04_102_empty() -> None:
    """UT04-102 no selection gives no ranks."""
    assert order_selected([]) == ({}, False)


# --- UT04-104: mandatory preconditions --------------------------------------------------------


def test_ut04_104_mandatory_without_effort() -> None:
    """UT04-104 a mandatory candidate with no effort estimate is a ConfigError."""
    with pytest.raises(ConfigError, match="mandatory candidate a has no effort estimate"):
        _solve([_cand("a", None)], "100", mandatory=frozenset({"a"}))


def test_ut04_104_unknown_mandatory() -> None:
    """UT04-104 an unknown mandatory ID is a ConfigError."""
    with pytest.raises(ConfigError, match="unknown mandatory candidate q"):
        _solve([_cand("a")], "100", mandatory=frozenset({"q"}))


def test_ut04_104_mandatory_and_excluded_overlap() -> None:
    """UT04-104 an ID both mandatory and excluded is a ConfigError."""
    with pytest.raises(ConfigError, match="candidate a is both mandatory and excluded"):
        _solve([_cand("a")], "100", mandatory=frozenset({"a"}), excluded=frozenset({"a"}))


# --- UT04-105: mandatory above budget ---------------------------------------------------------


def test_ut04_105_mandatory_above_budget_is_infeasible() -> None:
    """UT04-105 mandatory effort above the budget gives INFEASIBLE and no selection."""
    outcome = _solve([_cand("a", "500"), _cand("b", "10")], "100", mandatory=frozenset({"a"}))
    assert outcome.status == "INFEASIBLE"
    assert outcome.selected == frozenset()
    assert "no_solution_found" not in outcome.flags


# --- UT04-106: tiny deterministic limit -------------------------------------------------------


def _knapsack(n: int) -> tuple[list[PortfolioCandidate], str]:
    rng = random.Random(7)
    items = []
    total = 0
    for i in range(n):
        weight = rng.randint(10_000, 1_000_000)
        total += weight
        items.append(_cand(f"k{i:05d}", str(weight), str(weight + rng.randint(0, 1000))))
    return items, str(total // 2)


def _tiny_limit() -> SolverConfig:
    return SolverConfig(
        num_workers=1, random_seed=0, max_deterministic_time=0.1, max_time_in_seconds=60
    )


def test_ut04_106_tiny_deterministic_limit_is_feasible() -> None:
    """UT04-106 a hard instance under a tiny deterministic limit is FEASIBLE, not proven."""
    items, budget = _knapsack(300)
    outcome = _solve(items, budget, solver=_tiny_limit())
    assert outcome.status == "FEASIBLE"
    assert outcome.flags == ("not_proven_optimal",)
    assert outcome.selected


def test_ut04_106_no_solution_maps_to_infeasible() -> None:
    """UT04-106 UNKNOWN (no solution in the limit) maps to INFEASIBLE + no_solution_found."""
    items, budget = _knapsack(3000)
    outcome = _solve(items, budget, solver=_tiny_limit())
    assert outcome.status == "INFEASIBLE"
    assert outcome.flags == ("no_solution_found",)
    assert outcome.selected == frozenset()
