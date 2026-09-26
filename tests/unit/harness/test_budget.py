"""Tests for herness.harness.budget (U06-25..U06-28): RunBudget ledger and phase split."""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from herness.core.errors import BudgetExceeded, ConfigError, SchemaViolation
from herness.core.types import BudgetLedger
from herness.harness.budget import RunBudget, new_phase_budgets

pytestmark = pytest.mark.unit


def _ledger(cap: int = 100, cost: Decimal | None = None, *, raises: bool = True) -> RunBudget:
    return RunBudget("analysis", cap, cost, cost_cap_raises=raises, run_id="r1")


# --- UT06-14 -----------------------------------------------------------------------------


def test_ut06_14_second_charge_crosses_cap_and_raises() -> None:
    """UT06-14 cap 100: charge 60 then 50 raises; totals 110; exhausted is sticky."""
    b = _ledger(100)
    b.charge(30, 30, Decimal("0.10"))
    assert b.snapshot()["exhausted"] is False
    with pytest.raises(BudgetExceeded, match="run_id=r1 phase=analysis"):
        b.charge(25, 25, Decimal("0.20"))
    snap = b.snapshot()
    assert snap["tokens_used"] == 110
    assert snap["tokens_in"] == 55
    assert snap["tokens_out"] == 55
    assert snap["tokens_remaining"] == 0
    assert snap["calls"] == 2
    assert snap["cost_used"] == "0.30"
    assert b.exhausted
    with pytest.raises(BudgetExceeded):
        b.charge(0, 0, Decimal(0))
    assert b.exhausted
    assert b.snapshot()["calls"] == 3


def test_ut06_14_ledger_satisfies_budget_ledger_protocol() -> None:
    """UT06-14 RunBudget implements the spec 05 BudgetLedger protocol."""
    ledger: BudgetLedger = _ledger()
    assert callable(ledger.charge)
    assert callable(ledger.snapshot)


@pytest.mark.parametrize(("cap", "cost"), [(0, None), (-1, None), (1, Decimal("-0.01"))])
def test_ut06_14_invalid_caps_raise_config_error(cap: int, cost: Decimal | None) -> None:
    """UT06-14 tokens_cap < 1 or a negative cost cap is a ConfigError."""
    with pytest.raises(ConfigError):
        _ledger(cap, cost)


@pytest.mark.parametrize(("t_in", "t_out", "cost"), [(-1, 0, "0"), (0, -1, "0"), (0, 0, "-1")])
def test_ut06_14_negative_charge_is_config_error(t_in: int, t_out: int, cost: str) -> None:
    """UT06-14 a negative amount raises ConfigError("negative charge") and records nothing."""
    b = _ledger()
    with pytest.raises(ConfigError, match="negative charge"):
        b.charge(t_in, t_out, Decimal(cost))
    assert b.snapshot()["calls"] == 0


# --- UT06-15 -----------------------------------------------------------------------------


def test_ut06_15_flag_mode_cost_cap_does_not_raise() -> None:
    """UT06-15 cost cap 1 with cost_cap_raises=False: cost 2 sets cost_cap_reached only."""
    b = _ledger(1000, Decimal(1), raises=False)
    b.charge(1, 1, Decimal(2))
    assert b.cost_cap_reached
    assert not b.exhausted
    snap = b.snapshot()
    assert snap["cost_cap"] == "1"
    assert snap["cost_remaining"] == "0"
    b.charge(1, 1, Decimal(0))
    assert b.cost_cap_reached


def test_ut06_15_raising_cost_cap_exhausts() -> None:
    """UT06-15 with cost_cap_raises=True reaching the cost cap exhausts and raises."""
    b = _ledger(1000, Decimal(1))
    with pytest.raises(BudgetExceeded):
        b.charge(1, 1, Decimal(1))
    assert b.cost_cap_reached
    assert b.exhausted


def test_ut06_15_no_cost_cap_snapshot_nulls() -> None:
    """UT06-15 without a cost cap cost_cap and cost_remaining are null."""
    snap = _ledger().snapshot()
    assert snap["cost_cap"] is None
    assert snap["cost_remaining"] is None
    assert snap["cost_cap_reached"] is False


# --- UT06-16 -----------------------------------------------------------------------------


def test_ut06_16_snapshot_restore_round_trip() -> None:
    """UT06-16 snapshot of a charged ledger restores equal counters on a new ledger."""
    a = _ledger(100, Decimal(5), raises=False)
    a.charge(10, 20, Decimal("1.25"))
    a.charge(5, 5, Decimal("0.75"))
    snap = a.snapshot()
    assert set(snap) == {
        "name", "tokens_cap", "tokens_in", "tokens_out", "tokens_used", "tokens_remaining",
        "cost_cap", "cost_used", "cost_remaining", "calls", "exhausted", "cost_cap_reached",
    }  # fmt: skip
    b = _ledger(100, Decimal(5), raises=False)
    b.restore(snap)
    assert b.snapshot() == snap
    assert b.cost_used == Decimal("2.00")


def test_ut06_16_restore_recomputes_exhausted_against_current_cap() -> None:
    """UT06-16 caps stay the current knobs; exhausted is recomputed against them."""
    a = _ledger(40)
    with pytest.raises(BudgetExceeded):
        a.charge(30, 20, Decimal(0))
    bigger = _ledger(100)
    bigger.restore(a.snapshot())
    assert not bigger.exhausted
    assert bigger.snapshot()["tokens_cap"] == 100
    assert bigger.snapshot()["tokens_used"] == 50
    smaller = _ledger(50)
    smaller.restore({**bigger.snapshot(), "exhausted": False})
    assert smaller.exhausted


def test_ut06_16_restore_recomputes_cost_cap_reached() -> None:
    """UT06-16 cost_cap_reached follows the current cost cap after restore."""
    a = _ledger(1000, Decimal(1), raises=False)
    a.charge(1, 1, Decimal(2))
    b = _ledger(1000, Decimal(10), raises=False)
    b.restore(a.snapshot())
    assert not b.cost_cap_reached
    c = _ledger(1000, Decimal(1))
    c.restore(a.snapshot())
    assert c.cost_cap_reached
    assert c.exhausted


def test_ut06_16_wrong_name_is_schema_violation() -> None:
    """UT06-16 a snapshot from another phase is a SchemaViolation."""
    snap = RunBudget("writer", 10, run_id="r1").snapshot()
    with pytest.raises(SchemaViolation, match="budget snapshot invalid: run_id=r1"):
        _ledger().restore(snap)


@pytest.mark.parametrize("key", ["tokens_in", "tokens_out", "cost_used", "calls", "name"])
def test_ut06_16_missing_key_is_schema_violation(key: str) -> None:
    """UT06-16 a snapshot missing a key is a SchemaViolation."""
    snap = dict(_ledger().snapshot())
    del snap[key]
    with pytest.raises(SchemaViolation):
        _ledger().restore(snap)


@pytest.mark.parametrize(
    ("key", "value"),
    [("tokens_in", -1), ("tokens_out", "x"), ("calls", True), ("cost_used", "abc"),
     ("cost_used", "-1"), ("cost_used", 3), ("exhausted", "no"), ("cost_cap_reached", 1)],
)  # fmt: skip
def test_ut06_16_malformed_value_is_schema_violation(key: str, value: object) -> None:
    """UT06-16 a snapshot with a malformed counter or flag is a SchemaViolation."""
    snap = {**_ledger().snapshot(), key: value}
    b = _ledger()
    with pytest.raises(SchemaViolation):
        b.restore(snap)
    assert b.snapshot()["calls"] == 0


# --- UT06-17 -----------------------------------------------------------------------------


def test_ut06_17_split_six_million() -> None:
    """UT06-17 6,000,000 tokens, reserve 0.15, cost 15 split to 5.1M/0.9M and 12.75/2.25."""
    a, w = new_phase_budgets(
        run_id="r1", run_tokens=6_000_000, writer_reserve=0.15,
        cost_cap=Decimal(15), cost_cap_raises=False,
    )  # fmt: skip
    sa, sw = a.snapshot(), w.snapshot()
    assert (sa["name"], sw["name"]) == ("analysis", "writer")
    assert (sa["tokens_cap"], sw["tokens_cap"]) == (5_100_000, 900_000)
    assert (sa["cost_cap"], sw["cost_cap"]) == ("12.75", "2.25")


def test_ut06_17_split_rounds_analysis_down() -> None:
    """UT06-17 analysis tokens floor and cost ROUND_DOWN to 0.01; writer takes the rest."""
    a, w = new_phase_budgets(
        run_id="r1", run_tokens=1001, writer_reserve=0.3,
        cost_cap=Decimal("1.00"), cost_cap_raises=True,
    )  # fmt: skip
    assert (a.snapshot()["tokens_cap"], w.snapshot()["tokens_cap"]) == (700, 301)
    assert (a.snapshot()["cost_cap"], w.snapshot()["cost_cap"]) == ("0.70", "0.30")
    a2, w2 = new_phase_budgets(
        run_id="r1", run_tokens=10, writer_reserve=0.5, cost_cap=None, cost_cap_raises=True
    )
    assert a2.snapshot()["cost_cap"] is None
    assert w2.snapshot()["cost_cap"] is None


@pytest.mark.parametrize("reserve", [0.0, 1.0, -0.1, 1.5])
def test_ut06_17_reserve_out_of_range_is_config_error(reserve: float) -> None:
    """UT06-17 writer_reserve outside (0, 1) is a ConfigError."""
    with pytest.raises(ConfigError):
        new_phase_budgets(
            run_id="r1", run_tokens=100, writer_reserve=reserve,
            cost_cap=None, cost_cap_raises=True,
        )  # fmt: skip


# --- PT06-05 -----------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    cap=st.integers(min_value=1, max_value=5_000),
    charges=st.lists(
        st.lists(st.tuples(st.integers(0, 200), st.integers(0, 200)), max_size=12),
        min_size=8, max_size=8,
    ),
)  # fmt: skip
def test_pt06_05_concurrent_charges_total_and_raise_iff_cap(
    cap: int, charges: list[list[tuple[int, int]]]
) -> None:
    """PT06-05 8 threads: totals equal the sum; a charge raises iff the cap is reached."""
    b = RunBudget("analysis", cap, run_id="pt")
    barrier = threading.Barrier(8)
    raised: list[int] = []
    passed: list[int] = []
    lock = threading.Lock()

    def worker(items: list[tuple[int, int]]) -> None:
        barrier.wait()
        for t_in, t_out in items:
            try:
                b.charge(t_in, t_out, Decimal("0.01"))
            except BudgetExceeded:
                with lock:
                    raised.append(t_in + t_out)
            else:
                with lock:
                    passed.append(t_in + t_out)

    threads = [threading.Thread(target=worker, args=(c,)) for c in charges]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = sum(i + o for items in charges for i, o in items)
    n_calls = sum(len(items) for items in charges)
    snap = b.snapshot()
    assert snap["tokens_used"] == total
    assert snap["calls"] == n_calls
    assert Decimal(str(snap["cost_used"])) == Decimal("0.01") * n_calls
    assert b.exhausted == (total >= cap)
    assert bool(raised) == (total >= cap)
    # A charge passes iff the running total after it stays below the cap (lock order).
    assert sum(passed) < cap
