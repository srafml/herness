"""Module-local memory types and error (impl 07 U07-11 … U07-17), the memory ID patterns
(impl 07 §3 "IDs") and re-exports of the 07 shared types of `herness.core.types` (§3.1)."""

# fmt: off
import re  # noqa: I001 - compact re-export layout keeps the 150-line budget
from datetime import datetime
from pathlib import Path
from typing import Annotated, ClassVar, Final, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, NonNegativeInt, model_validator

from herness.core.errors import NotFound
from herness.core.types import (
    KIND_LAYER, ConfidenceAdjustment, Kind, Layer, MemoryItem, MemoryProposal, MemoryRunContext,
    PriorContext, PriorRecommendation, Provenance, RecallHit, RecommendationDraft,
    SimilarOutcome, Status,
)
# fmt: on

MEMORY_ID_RE: Final = re.compile(r"^mem_[0-9A-HJKMNP-TV-Z]{26}$")
REC_ID_RE: Final = re.compile(r"^rec_[0-9A-HJKMNP-TV-Z]{26}$")
QUERY_ID_RE: Final = re.compile(r"^q_[0-9a-f]{16}$")
FINDING_ID_RE: Final = re.compile(r"^fnd_[0-9A-HJKMNP-TV-Z]{26}$")

type _Flag = Literal["redacted", "instruction_like", "unverified_numbers", "conflict",
                    "embedding_pending"]  # fmt: skip
type NotFoundKind = Literal["memory_item", "recommendation", "session", "review_item", "run"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecallFilters(_Frozen):
    """Filters for `recall` (U07-11); `include_pending_for` is set only by the chat tool."""

    kinds: list[Kind] | None = Field(default=None, max_length=11)
    entity_type: Literal["service", "team", "org", "work_item", "cluster"] | None = None
    entity_ids: list[Annotated[str, Field(max_length=200)]] = Field(default=[], max_length=50)
    min_confidence: float = Field(default=0.0, ge=0, le=1)
    include_pending_for: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    created_after: AwareDatetime | None = None

    @model_validator(mode="after")
    def _unique_kinds(self) -> Self:
        if self.kinds is not None and len(set(self.kinds)) != len(self.kinds):
            msg = "kinds must be unique"
            raise ValueError(msg)
        return self


class ProposeResult(_Frozen):
    """Result of `propose` (U07-12); a merge reports the surviving item as `memory_id`."""

    memory_id: str
    status: Status
    review_item_id: str | None
    merged_into: str | None
    flags: list[_Flag]

    @model_validator(mode="after")
    def _merge_rule(self) -> Self:
        if self.merged_into is not None and self.memory_id != self.merged_into:
            msg = "memory_id must equal merged_into after a merge"
            raise ValueError(msg)
        return self


class ChatTurn(_Frozen):
    """One stored chat message (U07-13)."""

    message_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    query_ids: list[str]


class SessionContext(_Frozen):
    """Output of `session_load` (U07-13); messages oldest first."""

    session_id: str
    summary: str | None
    messages: list[ChatTurn]
    memory_ids: list[str]


class PromotionReport(_Frozen):
    """Result of `promote_procedural` (U07-14)."""

    run_id: str
    queries_seen: NonNegativeInt
    skipped_unparsable: NonNegativeInt
    templates_created: NonNegativeInt
    templates_updated: NonNegativeInt
    qa_pairs_created: NonNegativeInt
    promoted: list[str]
    expired: list[str]
    already_processed: bool


class ExportReport(_Frozen):
    """Result of `export_lora` (U07-15)."""

    export_id: str
    out_dir: Path
    train_count: NonNegativeInt
    val_count: NonNegativeInt
    excluded_golden: NonNegativeInt
    excluded_low_pass_lb: NonNegativeInt
    templates: NonNegativeInt
    config_hash: str
    manifest_sha256: str


class ContextStats(_Frozen):
    """Token pressure snapshot (U07-16; design 07 §3.4)."""

    tokens: int
    exact: bool
    budget: int
    soft: int
    hard: int
    target: int

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not 0 < self.target < self.soft < self.hard < self.budget:
            msg = "thresholds must satisfy 0 < target < soft < hard < budget"
            raise ValueError(msg)
        return self


class MemoryNotFound(NotFound):
    """A referenced memory item, recommendation, session, review item or run is missing."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("kind", "ident")

    def __init__(self, kind: NotFoundKind, ident: str) -> None:
        super().__init__(f"{kind} not found: {ident}", details={"kind": kind, "ident": ident})
        self.kind, self.ident = kind, ident


__all__ = [
    "FINDING_ID_RE", "KIND_LAYER", "MEMORY_ID_RE", "QUERY_ID_RE", "REC_ID_RE", "ChatTurn",
    "ConfidenceAdjustment", "ContextStats", "ExportReport", "Kind", "Layer", "MemoryItem",
    "MemoryNotFound", "MemoryProposal", "MemoryRunContext", "PriorContext", "PriorRecommendation",
    "PromotionReport", "ProposeResult", "Provenance", "RecallFilters", "RecallHit",
    "RecommendationDraft", "SessionContext", "SimilarOutcome", "Status",
]  # fmt: skip
