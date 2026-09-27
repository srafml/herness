"""The Writer role, its retrospective variant and its output model (impl 05 U05-50, U05-51).

Design 05 §5.5: the Writer drafts the report from verified findings; it has no
`propose_memory` (R-27). `WriterOutput` must publish the same JSON Schema as spec 06's
`ReportDraft.writer_schema()` up to `title` annotations and `$defs` names (UT05-93, D05-18), so
the docstrings below are the schema descriptions and match the spec 06 ones word for word.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from herness.core.types import Paragraph, RecommendationItem, Section
from herness.harness.roles.base import RoleSpec

__all__ = ["WRITER", "WRITER_RETROSPECTIVE", "WriterOutput", "WriterRecommendation"]

_SWARM_SET: Final = frozenset({"rank", "rec_id"})
_RECOMMENDATION_DOC: Final = (
    "A Writer-authored recommendation: every `RecommendationItem` field but `rank` and\n"
    "`rec_id`, which the swarm sets (U06-15, U06-19)."
)


def _writer_recommendation() -> type[BaseModel]:
    """`RecommendationItem` minus `rank` and `rec_id`, keeping its model validators."""
    fields: dict[str, Any] = {
        name: (info.annotation, info)
        for name, info in RecommendationItem.model_fields.items()
        if name not in _SWARM_SET
    }
    validators: dict[str, Any] = {
        name.lstrip("_"): model_validator(mode=dec.info.mode)(dec.func)
        for name, dec in RecommendationItem.__pydantic_decorators__.model_validators.items()
    }
    return create_model(
        "WriterRecommendation",
        __config__=ConfigDict(extra="forbid", frozen=True),
        __doc__=_RECOMMENDATION_DOC,
        __validators__=validators,
        **fields,
    )


if TYPE_CHECKING:
    WriterRecommendation = BaseModel
else:
    WriterRecommendation = _writer_recommendation()


class WriterOutput(BaseModel):
    """The Writer's output contract: only the model-authored draft fields (U06-19, D06-06)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=200)
    sections: list[Section]
    recommendations: list[WriterRecommendation]
    caveats: list[Annotated[str, Field(max_length=600)]]
    prior_outcomes_commentary: Paragraph | None


WRITER: Final = RoleSpec(
    name="writer",
    specialty=None,
    prompt_files=("_common.md", "writer.md"),
    allowed_tools=frozenset({"list_findings", "get_scores", "get_metric", "recall_memory"}),
    output_model=WriterOutput,
    temperature=0.4,
    effort="high",
    thinking="auto",
    model_role="writer",
)
WRITER_RETROSPECTIVE: Final = dataclasses.replace(
    WRITER, prompt_files=(*WRITER.prompt_files, "writer_retrospective.md")
)
