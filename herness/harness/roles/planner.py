"""The Planner role and its output model (impl 05 U05-50, U05-51; design 05 §5.5).

The Planner only proposes `PlannedTask` items; the swarm (spec 06) expands each one into a
full `TaskSpec` (R-28). It never runs anything itself.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from herness.core.types import PlannedTask
from herness.harness.roles.base import RoleSpec

__all__ = ["PLANNER", "PlannerOutput"]


class PlannerOutput(BaseModel):
    """The Planner's final answer (trust boundary: `extra="forbid"`, not strict)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tasks: list[PlannedTask] = Field(min_length=1, max_length=200)
    rationale: str = Field(max_length=4_000)
    unknowns: list[str] = Field(max_length=50)


PLANNER: Final = RoleSpec(
    name="planner",
    specialty=None,
    prompt_files=("_common.md", "planner.md"),
    allowed_tools=frozenset(
        {"list_tables", "describe_table", "get_scores", "get_metric", "recall_memory"}
    ),
    output_model=PlannerOutput,
    temperature=0.2,
    effort="high",
    thinking="auto",
    model_role="planner",
)
