"""The Skeptic role and its output model (impl 05 U05-50, U05-51; design 05 §5.5).

The Skeptic tests one finding against the six checks and is also the role that judges claims
(R-37). The swarm (spec 06) turns a `SkepticOutput` into a `Challenge` by adding `round`,
`skeptic_task_id`, `votes` and `model`, so the shared fields carry the `Challenge` limits.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from herness.core.types import SKEPTIC_CHECKS, CheckResult
from herness.harness.roles.base import RoleSpec

__all__ = ["SKEPTIC", "SkepticOutput"]


class SkepticOutput(BaseModel):
    """The Skeptic's verdict on one finding (trust boundary: `extra="forbid"`, not strict)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_id: str = Field(pattern=r"^fnd_[0-9A-HJKMNP-TV-Z]{26}$")
    checks: list[CheckResult]
    verdict: Literal["uphold", "revise", "reject"]
    required_actions: list[Annotated[str, Field(max_length=400)]] = Field(default=[], max_length=10)

    @model_validator(mode="after")
    def _one_per_check(self) -> Self:
        if sorted(c.check for c in self.checks) != sorted(SKEPTIC_CHECKS):
            msg = "checks must hold exactly one CheckResult per SkepticCheck"
            raise ValueError(msg)
        if self.verdict == "revise" and not self.required_actions:
            msg = "a revise verdict needs at least one required action"
            raise ValueError(msg)
        return self


SKEPTIC: Final = RoleSpec(
    name="skeptic",
    specialty=None,
    prompt_files=("_common.md", "skeptic.md"),
    allowed_tools=frozenset(
        {
            "run_sql",
            "get_metric",
            "get_scores",
            "describe_table",
            "list_findings",
            "semantic_search",
            "get_cluster",
            "get_record",
            "recall_memory",
        }
    ),
    output_model=SkepticOutput,
    temperature=0.5,
    effort="high",
    thinking="auto",
    model_role="skeptic",
)
