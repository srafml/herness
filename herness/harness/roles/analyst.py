"""The Analyst roles and their output model (impl 05 U05-50, U05-51; design 05 §5.5).

One `RoleSpec` per specialty, named `analyst_<specialty>` with prompt `analyst_<specialty>.md`;
all share the model role `analyst`. Finding ids come from the task buffer, not the output.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from herness.core.errors import ConfigError
from herness.harness.roles.base import RoleSpec

__all__ = ["ANALYST_SPECIALTIES", "AnalystOutput", "analyst_role"]

ANALYST_SPECIALTIES: Final = (
    "ops",
    "change",
    "delivery",
    "org",
    "crosscheck",
    "retrospective",
    "general",
)
_ANALYST_TOOLS: Final = frozenset(
    {
        "list_tables",
        "describe_table",
        "run_sql",
        "get_metric",
        "get_scores",
        "get_cluster",
        "get_record",
        "semantic_search",
        "recall_memory",
        "propose_memory",
        "post_finding",
        "list_findings",
        "request_subtask",
    }
)


class AnalystOutput(BaseModel):
    """An Analyst's final answer (trust boundary: `extra="forbid"`, not strict)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(max_length=3_000)
    unknowns: list[str] = Field(max_length=50)
    suggested_followups: list[str] = Field(max_length=20)


_ROLES: Final = MappingProxyType(
    {
        s: RoleSpec(
            name=f"analyst_{s}",
            specialty=s,
            prompt_files=("_common.md", f"analyst_{s}.md"),
            allowed_tools=_ANALYST_TOOLS,
            output_model=AnalystOutput,
            temperature=0.2,
            effort="medium",
            thinking="auto",
            model_role="analyst",
        )
        for s in ANALYST_SPECIALTIES
    }
)


def analyst_role(specialty: str) -> RoleSpec:
    """The Analyst `RoleSpec` of one specialty; `ConfigError` for an unknown one."""
    role = _ROLES.get(specialty)
    if role is None:
        msg = f"unknown analyst specialty {specialty[:64]}"
        raise ConfigError(msg)
    return role
