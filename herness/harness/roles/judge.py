"""The Judge role and its output model (impl 05 U05-50, U05-51; design 05 §5.5).

The Judge scores planner proposals in deep mode; it has no tools. Its schema is enforced by
`complete_validated` (spec 08) with `JudgeOutput` as the output model.
"""

from __future__ import annotations

from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from herness.harness.roles.base import RoleSpec

__all__ = ["JUDGE", "JudgeOutput"]


class JudgeOutput(BaseModel):
    """The Judge's choice among the proposals (trust boundary: `extra="forbid"`, not strict)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    choice: int = Field(ge=0)
    scores: list[Annotated[float, Field(ge=0, le=5)]] = Field(min_length=1, max_length=10)
    reasons: list[str] = Field(max_length=10)

    @model_validator(mode="after")
    def _choice_in_range(self) -> Self:
        if self.choice >= len(self.scores):
            msg = "choice must index one of the scores"
            raise ValueError(msg)
        return self


JUDGE: Final = RoleSpec(
    name="judge",
    specialty=None,
    prompt_files=("_common.md", "judge.md"),
    allowed_tools=frozenset(),
    output_model=JudgeOutput,
    temperature=0.0,
    effort="medium",
    thinking="off",
    model_role="judge",
)
