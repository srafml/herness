"""Role mapping, tool lists and budgets per role (impl 06 U06-97, U06-101; design 06 §4.1).

This module holds the pure part of the swarm routing: `role_prompt_name` maps a task to its
spec 05 `RoleSpec.name`, `default_tools` gives the least-privilege tool list of a role (spec 05
§5.5 allow-lists, TH06-02) and `role_budget` its `TaskBudget` (§13 O06-01). Model routing, hooks
and tool context construction (T06-13) are added below them.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from pydantic import ValidationError

from herness.core.errors import ConfigError
from herness.core.types import Depth, Role, Specialty, TaskBudget
from herness.harness.pipelines.settings import DepthKnobs

__all__ = ["default_tools", "role_budget", "role_prompt_name"]

SPAWN_TOOL: Final = "request_subtask"
_ANALYST: Final = frozenset(
    {
        "describe_table", "get_cluster", "get_metric", "get_record", "get_scores",
        "list_findings", "list_tables", "post_finding", "propose_memory", "recall_memory",
        SPAWN_TOOL, "run_sql", "semantic_search",
    }
)  # fmt: skip
_CROSSCHECK: Final = frozenset(
    {"describe_table", "get_metric", "get_scores", "list_tables", "post_finding", "run_sql"}
)
# Roles whose tool list does not depend on the specialty or the spawn depth.
_FIXED: Final[MappingProxyType[str, frozenset[str]]] = MappingProxyType(
    {
        "skeptic": frozenset(
            {"describe_table", "get_metric", "get_scores", "list_findings", "recall_memory"}
            | {"run_sql", "semantic_search"}
        ),
        "writer": frozenset({"get_metric", "get_scores", "list_findings", "recall_memory"}),
        "planner": frozenset(
            {"describe_table", "get_metric", "get_scores", "list_tables", "recall_memory"}
        ),
        "judge": frozenset(),
        "verifier": frozenset(),
    }
)
_JUDGE_BUDGET: Final = TaskBudget(max_steps=2, max_tokens=20_000, wall_clock_s=300)
# U06-101 names max_tokens=1; the in-tree TaskBudget (U06-04) floors max_tokens at 1,000, and a
# verifier task makes no model call, so its token limit is that floor (spec note in the report).
_VERIFIER_BUDGET: Final = TaskBudget(max_steps=1, max_tokens=1_000, wall_clock_s=900)
_MAX_WALL_CLOCK_S: Final = 86_400


def role_prompt_name(role: Role, specialty: Specialty) -> str:
    """Spec 05 `RoleSpec.name` of a task: `analyst_<specialty>`, other roles 1:1 (U06-97)."""
    return f"analyst_{specialty}" if role == "analyst" else role


def default_tools(
    role: Role, specialty: Specialty, depth_mode: Depth, *, child_depth: int, knobs: DepthKnobs
) -> list[str]:
    """Sorted tool names of one task of `role` (U06-101, TH06-02 least privilege).

    `request_subtask` is dropped when `child_depth >= knobs.max_spawn_depth` (fast mode has
    `max_spawn_depth = 0`, so `depth_mode` acts through `knobs` only). Chat tools depend on the
    chat mode (`CHAT_TOOLS`, design 06 §5.13), so `chat` raises `ConfigError`.
    """
    del depth_mode
    if role == "analyst":
        if specialty == "crosscheck":
            return sorted(_CROSSCHECK)
        drop = {SPAWN_TOOL} if child_depth >= knobs.max_spawn_depth else set()
        return sorted(_ANALYST - drop)
    tools = _FIXED.get(role)
    if tools is None:
        msg = f"role {role} has no default tools"
        raise ConfigError(msg)
    return sorted(tools)


def role_budget(role: Role, knobs: DepthKnobs, *, writer_tokens: int = 0) -> TaskBudget:
    """The `TaskBudget` of one task of `role` (U06-101, §13 O06-01); pure.

    The writer's token limit is the writer ledger (`writer_tokens` ≥ 1, else `ConfigError`); its
    wall clock is 4 times the analyst's, capped at the `TaskBudget` bound of one day.
    """
    analyst = knobs.analyst_budget
    if role in {"planner", "skeptic", "analyst"}:
        return analyst
    if role == "judge":
        return _JUDGE_BUDGET
    if role == "verifier":
        return _VERIFIER_BUDGET
    if role != "writer":
        msg = f"role {role} has no default budget"
        raise ConfigError(msg)
    if writer_tokens < 1:
        msg = "writer_tokens must be >= 1 for the writer budget"
        raise ConfigError(msg)
    wall = min(4 * analyst.wall_clock_s, _MAX_WALL_CLOCK_S)
    try:
        return TaskBudget(max_steps=analyst.max_steps, max_tokens=writer_tokens, wall_clock_s=wall)
    except ValidationError as exc:
        msg = "writer_tokens is below the TaskBudget token minimum"
        raise ConfigError(msg) from exc
