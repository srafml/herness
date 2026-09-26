"""Tests of U08-70: arbiter_decide and ArbiterDecision."""

import dataclasses
from datetime import UTC, datetime

import pytest

from herness.core import jobs
from herness.core.jobs.arbiter import ArbiterDecision, arbiter_decide
from herness.core.jobs.windows import ActiveWindow
from herness.core.resilience.settings import ResilienceConfig
from herness.core.types import GpuClass

pytestmark = pytest.mark.unit

T0 = datetime(2026, 1, 6, 8, tzinfo=UTC)
T1 = datetime(2026, 1, 6, 19, tzinfo=UTC)


@pytest.fixture
def win(cfg_default: ResilienceConfig) -> dict[str, ActiveWindow]:
    """The design 08 §7 default windows by name, as active windows."""
    return {spec.name: ActiveWindow(spec, T0, T1) for spec in cfg_default.schedule.windows}


# (loaded, window, claimable, requested) -> (action, target, reason)
CASES: list[tuple[GpuClass, str, dict[GpuClass, int], GpuClass | None, tuple[str, str, str]]] = [
    # step 1: external requests
    ("decider", "deep", {}, "large", ("swap", "large", "requested")),
    ("reasoning", "chat", {"reasoning": 3}, "large", ("idle", "reasoning", "request_not_allowed")),
    ("reasoning", "chat", {"reasoning": 3}, "none", ("swap", "none", "requested")),
    ("none", "reviews", {}, "decider", ("swap", "decider", "requested")),
    ("reasoning", "chat", {"reasoning": 1}, "reasoning", ("keep", "reasoning", "loaded_claimable")),
    # step 2: loaded class allowed with claimable jobs
    (
        "decider",
        "reviews",
        {"decider": 2, "reasoning": 5},
        None,
        ("keep", "decider", "loaded_claimable"),
    ),
    # step 2a: GPU_SLOT_KINDS jobs (key none) keep the loaded class, no swap
    (
        "reasoning",
        "enrichment",
        {"none": 1, "decider": 4},
        None,
        ("keep", "reasoning", "slot_kind_claimable"),
    ),
    ("none", "chat", {"none": 1}, None, ("keep", "none", "slot_kind_claimable")),
    ("large", "morning_prep", {"none": 2}, None, ("keep", "large", "slot_kind_claimable")),
    # step 3: first allowed class (preference order) with claimable jobs
    ("large", "reviews", {"decider": 1, "reasoning": 1}, None, ("swap", "decider", "claimable")),
    ("large", "reviews", {"reasoning": 1}, None, ("swap", "reasoning", "claimable")),
    ("decider", "deep", {"large": 1}, None, ("swap", "large", "claimable")),
    ("none", "enrichment", {"decider": 1, "reasoning": 9}, None, ("swap", "decider", "claimable")),
    # claimable of a class the window does not allow is ignored
    ("decider", "chat", {"decider": 5}, None, ("swap", "reasoning", "preload")),
    # step 4: preload
    ("none", "morning_prep", {}, None, ("swap", "reasoning", "preload")),
    # step 5: idle on the loaded class
    ("reasoning", "chat", {"reasoning": 0}, None, ("idle", "reasoning", "no_work")),
    ("decider", "reviews", {"none": 0}, None, ("idle", "decider", "no_work")),
    ("large", "deep", {}, None, ("idle", "large", "no_work")),
    ("none", "enrichment", {}, None, ("idle", "none", "no_work")),
]


@pytest.mark.parametrize(("loaded", "window", "claimable", "requested", "expected"), CASES)
def test_ut08_78_decision_table(
    win: dict[str, ActiveWindow],
    loaded: GpuClass,
    window: str,
    claimable: dict[GpuClass, int],
    requested: GpuClass | None,
    expected: tuple[str, str, str],
) -> None:
    """UT08-78 loaded x window x claimable x requested gives the U08-70 decision."""
    decision = arbiter_decide(loaded, win[window], claimable, requested=requested)
    assert (decision.action, decision.target, decision.reason) == expected


def test_ut08_78_slot_kind_jobs_never_swap(win: dict[str, ActiveWindow]) -> None:
    """UT08-78 claimable["none"] > 0 gives keep without a swap in every window and class."""
    loaded_classes: tuple[GpuClass, ...] = ("none", "reasoning", "decider", "large")
    for window in win.values():
        for loaded in loaded_classes:
            decision = arbiter_decide(loaded, window, {"none": 1})
            assert decision == ArbiterDecision("keep", loaded, "slot_kind_claimable")


def test_ut08_78_decision_is_frozen_and_exported(win: dict[str, ActiveWindow]) -> None:
    """UT08-78 ArbiterDecision is frozen; both names resolve from herness.core.jobs."""
    decision = jobs.arbiter_decide("none", win["chat"], {})
    assert decision == ArbiterDecision("swap", "reasoning", "preload")
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.action = "keep"  # type: ignore[misc]
    assert jobs.ArbiterDecision is ArbiterDecision
    assert jobs.ActiveWindow is ActiveWindow
