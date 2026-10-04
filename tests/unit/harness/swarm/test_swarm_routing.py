"""UT06-64, UT06-68: role prompt names, default tool lists and role budgets (U06-97, U06-101)."""

from __future__ import annotations

from typing import get_args

import pytest

from herness.core.errors import ConfigError
from herness.core.types import Role, Specialty, TaskBudget
from herness.harness.budget import new_phase_budgets
from herness.harness.pipelines.settings import DepthKnobs, PipelinesConfig, resolve_knobs
from herness.harness.roles.analyst import analyst_role
from herness.harness.roles.planner import PLANNER
from herness.harness.swarm.routing import default_tools, role_budget, role_prompt_name

pytestmark = pytest.mark.unit

_SPECIALTIES: tuple[Specialty, ...] = get_args(Specialty.__value__)
_ROLES: tuple[Role, ...] = get_args(Role.__value__)
_ANALYST = [
    "describe_table", "get_cluster", "get_metric", "get_record", "get_scores", "list_findings",
    "list_tables", "post_finding", "propose_memory", "recall_memory", "request_subtask", "run_sql",
    "semantic_search",
]  # fmt: skip


def _knobs(depth: str) -> DepthKnobs:
    return resolve_knobs(PipelinesConfig(), "funding_review", depth)  # type: ignore[arg-type]


@pytest.mark.parametrize("specialty", _SPECIALTIES)
def test_ut06_64_analyst_maps_to_specialty_prompt(specialty: Specialty) -> None:
    """UT06-64 analyst + specialty -> analyst_<specialty>, a real spec 05 RoleSpec name."""
    name = role_prompt_name("analyst", specialty)
    assert name == f"analyst_{specialty}"
    assert analyst_role(specialty).name == name


@pytest.mark.parametrize("role", [r for r in _ROLES if r != "analyst"])
@pytest.mark.parametrize("specialty", ["general", "ops"])
def test_ut06_64_other_roles_map_one_to_one(role: Role, specialty: Specialty) -> None:
    """UT06-64 planner, judge, skeptic, writer, chat, verifier map to their own name."""
    assert role_prompt_name(role, specialty) == role


def test_ut06_64_planner_name_matches_role_spec() -> None:
    """UT06-64 the planner mapping names the spec 05 planner RoleSpec."""
    assert role_prompt_name("planner", "general") == PLANNER.name


def test_ut06_68_fast_analyst_has_no_request_subtask() -> None:
    """UT06-68 fast mode (max_spawn_depth 0): analyst tools lack request_subtask."""
    tools = default_tools("analyst", "general", "fast", child_depth=0, knobs=_knobs("fast"))
    assert "request_subtask" not in tools
    assert tools == sorted(tools) == [t for t in _ANALYST if t != "request_subtask"]


@pytest.mark.parametrize(("depth", "child_depth", "spawn"), [
    ("standard", 0, True), ("standard", 1, False), ("deep", 1, True), ("deep", 2, False),
])  # fmt: skip
def test_ut06_68_request_subtask_below_max_spawn_depth(
    depth: str, child_depth: int, spawn: bool
) -> None:
    """UT06-68 request_subtask only while child_depth < max_spawn_depth."""
    tools = default_tools("analyst", "ops", depth, child_depth=child_depth, knobs=_knobs(depth))
    assert ("request_subtask" in tools) is spawn
    assert tools == sorted(tools)


def test_ut06_68_revision_task_has_no_request_subtask() -> None:
    """UT06-68 revision tasks use child_depth = max_spawn_depth: no request_subtask."""
    knobs = _knobs("deep")
    tools = default_tools("analyst", "ops", "deep", child_depth=knobs.max_spawn_depth, knobs=knobs)
    assert "request_subtask" not in tools
    assert "post_finding" in tools


def test_ut06_68_role_lists() -> None:
    """UT06-68 crosscheck, skeptic, writer, planner, judge, verifier lists (sorted, U06-101)."""
    knobs = _knobs("deep")

    def tools(role: Role, specialty: Specialty = "general") -> list[str]:
        return default_tools(role, specialty, "deep", child_depth=0, knobs=knobs)

    assert tools("analyst", "crosscheck") == [
        "describe_table", "get_metric", "get_scores", "list_tables", "post_finding", "run_sql",
    ]  # fmt: skip
    assert tools("skeptic") == [
        "describe_table", "get_metric", "get_scores", "list_findings", "recall_memory", "run_sql",
        "semantic_search",
    ]  # fmt: skip
    assert tools("writer") == ["get_metric", "get_scores", "list_findings", "recall_memory"]
    assert "propose_memory" not in tools("writer")
    assert tools("planner") == sorted(PLANNER.allowed_tools)
    assert tools("judge") == tools("verifier") == []
    assert set(tools("analyst", "ops")) <= analyst_role("ops").allowed_tools
    with pytest.raises(ConfigError, match="no default tools"):
        tools("chat")


def test_ut06_68_role_budgets() -> None:
    """UT06-68 planner/skeptic/analyst share the analyst budget; judge and verifier fixed."""
    knobs = _knobs("standard")
    for role in ("planner", "skeptic", "analyst"):
        assert role_budget(role, knobs) == knobs.analyst_budget  # type: ignore[arg-type]
    assert role_budget("judge", knobs) == TaskBudget(
        max_steps=2, max_tokens=20_000, wall_clock_s=300
    )
    verifier = role_budget("verifier", knobs)
    assert (verifier.max_steps, verifier.wall_clock_s) == (1, 900)
    assert verifier.max_tokens == 1_000  # TaskBudget floor (spec note: U06-101 says 1)
    with pytest.raises(ConfigError, match="no default budget"):
        role_budget("chat", knobs)


def test_ut06_68_writer_budget_is_writer_ledger() -> None:
    """UT06-68 writer max_tokens = the writer ledger cap; 4x analyst wall clock."""
    knobs = _knobs("standard")
    _analysis, writer = new_phase_budgets(
        run_id="run_01J9ZZZZZZZZZZZZZZZZZZZZZZ",
        run_tokens=knobs.run_tokens,
        writer_reserve=0.15,
        cost_cap=None,
        cost_cap_raises=True,
    )
    budget = role_budget("writer", knobs, writer_tokens=writer.tokens_cap)
    assert budget.max_tokens == writer.tokens_cap
    assert budget.max_steps == knobs.analyst_budget.max_steps
    assert budget.wall_clock_s == 4 * knobs.analyst_budget.wall_clock_s


def test_ut06_68_writer_budget_needs_tokens_and_caps_wall_clock() -> None:
    """UT06-68 writer_tokens < 1 -> ConfigError; below the TaskBudget floor -> ConfigError."""
    knobs = _knobs("standard")
    with pytest.raises(ConfigError, match="writer_tokens must be >= 1"):
        role_budget("writer", knobs)
    with pytest.raises(ConfigError, match="token minimum"):
        role_budget("writer", knobs, writer_tokens=10)
    long = knobs.model_copy(
        update={"analyst_budget": TaskBudget(max_steps=5, max_tokens=5_000, wall_clock_s=50_000)}
    )
    assert role_budget("writer", long, writer_tokens=5_000).wall_clock_s == 86_400


@pytest.mark.parametrize("depth", ["fast", "standard", "deep"])
@pytest.mark.parametrize("child_depth", [0, 1, 2])
def test_ut06_68_pipeline_stand_in_matches_default_tools(depth: str, child_depth: int) -> None:
    """UT06-68 the pipelines' analyst tool stand-in (rank 3 may not import the swarm, §2) gives
    the same list as `default_tools` for every non-crosscheck specialty."""
    from herness.harness.pipelines import _review_common as common  # noqa: PLC0415 - test only

    knobs = _knobs(depth)
    for specialty in (s for s in _SPECIALTIES if s != "crosscheck"):
        args = ("analyst", specialty, depth)
        assert common.default_tools(*args, child_depth=child_depth, knobs=knobs) == (
            default_tools(*args, child_depth=child_depth, knobs=knobs)  # type: ignore[arg-type]
        )
