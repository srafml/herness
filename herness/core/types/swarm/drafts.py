"""06-owned report draft and chat types (impl 06 U06-13 … U06-21, design 06 §4.4, §4.5)."""

from typing import Annotated, Any, Final, Literal, Self, TypedDict

from pydantic import BaseModel, Field, JsonValue, TypeAdapter, model_validator, with_config

from herness.core.types.harness import NumberRef, VerificationResult
from herness.core.types.jobs import ChatMode
from herness.core.types.swarm.tasks import (
    _NUMBER_ID,
    _RUN_ID,
    _UNIQUE,
    Banner,
    Depth,
    RunKind,
    SectionId,
    _EntityId,
    _FindingId,
    _Model,
    _QueryId,
)

_JOB_ID: Final = r"^job_[0-9A-HJKMNP-TV-Z]{26}$"
_MEMORY_ID: Final = r"^mem_[0-9A-HJKMNP-TV-Z]{26}$"
_REC_ID: Final = r"^rec_[0-9A-HJKMNP-TV-Z]{26}$"

type _NumberId = Annotated[str, Field(pattern=_NUMBER_ID)]
type _Title = Annotated[str, Field(min_length=1, max_length=200)]
type _Caveat = Annotated[str, Field(max_length=600)]


@with_config(extra="forbid")
class _ActionLever(TypedDict):
    """One `action_levers` entry; a plain dict with exactly these keys."""

    entity_type: str
    entity_id: str
    metric: str
    delta_usd_ref: str


@with_config(extra="forbid")
class _Removed(TypedDict):
    """One `removed` entry: an item dropped by gate 2."""

    where: str
    reason: str


@with_config(extra="forbid")
class _DeadTask(TypedDict):
    """One `dead_tasks` entry."""

    task_id: str
    role: str
    objective: str
    last_error: str | None


class Paragraph(_Model):
    """One report paragraph with its numbers (U06-13); numbers need a finding."""

    text: str = Field(min_length=1, max_length=4_000)
    numbers: list[NumberRef] = Field(max_length=40)
    finding_ids: list[_FindingId]

    @model_validator(mode="after")
    def _cited(self) -> Self:
        if self.numbers and not self.finding_ids:
            msg = "a paragraph with numbers needs at least one of finding_ids"
            raise ValueError(msg)
        if len({n.id for n in self.numbers}) != len(self.numbers):
            msg = "number ids must be unique"
            raise ValueError(msg)
        return self


class Section(_Model):
    """One report outline slot (U06-14)."""

    id: SectionId
    title: _Title
    paragraphs: list[Paragraph] = Field(max_length=50)


class _WriterRecommendation(_Model):
    """A Writer-authored recommendation: every `RecommendationItem` field but `rank` and
    `rec_id`, which the swarm sets (U06-15, U06-19)."""

    kind: Literal["fund", "org_action"]
    target_type: str = Field(min_length=1, max_length=40)
    target_id: str = Field(min_length=1, max_length=200)
    headline: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=400)  # R-30
    numbers: list[NumberRef] = Field(min_length=1, max_length=20)
    expected_metric: str | None = None
    expected_delta_ref: _NumberId | None = None
    expected_usd_ref: _NumberId | None = None
    confidence_ref: _NumberId | None = None
    effort_usd_ref: _NumberId | None = None
    action_levers: list[_ActionLever] = Field(default=[], max_length=10)
    finding_ids: list[_FindingId] = Field(min_length=1)
    query_ids: list[_QueryId]

    @model_validator(mode="after")
    def _refs_resolve(self) -> Self:
        known = {n.id for n in self.numbers}
        refs = [
            self.expected_delta_ref, self.expected_usd_ref, self.confidence_ref,
            self.effort_usd_ref, *(lever["delta_usd_ref"] for lever in self.action_levers),
        ]  # fmt: skip
        dangling = sorted({ref for ref in refs if ref is not None} - known)
        if dangling:
            msg = f"refs name no number: {', '.join(dangling)}"
            raise ValueError(msg)
        return self


class RecommendationItem(_WriterRecommendation):
    """One ranked recommendation (U06-15, design 06 §4.4)."""

    rank: int = Field(ge=1)
    rec_id: str | None = Field(default=None, pattern=_REC_ID)


class RankedEntity(_Model):
    """Ranked target for eval grading (U06-16)."""

    rank: int = Field(ge=1)
    entity_type: Literal["candidate", "team", "service", "org"]
    entity_id: _EntityId


class Coverage(_Model):
    """Coverage counts and the publishability verdict (U06-17)."""

    planned_tasks: int = Field(ge=0)
    done_tasks: int = Field(ge=0)
    dead_tasks: int = Field(ge=0)
    must_cover_total: int = Field(ge=0)
    must_cover_done: int = Field(ge=0)
    verified_findings: int = Field(ge=0)
    rejected_findings: int = Field(ge=0)
    publishable: bool


class _WriterOutput(_Model):
    """The Writer's output contract: only the model-authored draft fields (U06-19, D06-06)."""

    title: _Title
    sections: list[Section]
    recommendations: list[_WriterRecommendation]
    caveats: list[_Caveat]
    prior_outcomes_commentary: Paragraph | None


class ReportDraft(_Model):
    """The report hand-off stored at `data/reports/<run_id>/draft.json` (U06-18, R-49)."""

    schema_version: Literal["1"] = "1"
    mode: Literal["full", "findings_only"] = "full"
    run_id: str = Field(pattern=_RUN_ID)
    kind: RunKind
    depth: Depth
    profile: str = Field(min_length=1)
    build_id: str = Field(min_length=1)
    title: _Title
    sections: list[Section]
    recommendations: list[RecommendationItem]
    ranked_entities: list[RankedEntity]
    caveats: list[_Caveat]
    prior_outcomes_commentary: Paragraph | None
    portfolio_custom: list[dict[str, JsonValue]] = Field(default=[])
    banners: Annotated[list[Banner], _UNIQUE]
    flags: dict[str, list[str]]
    contested: list[_FindingId]
    removed: list[_Removed]
    coverage: Coverage
    dead_tasks: list[_DeadTask]
    query_ids: list[_QueryId]
    verification: VerificationResult

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if [r.rank for r in self.recommendations] != list(range(1, len(self.recommendations) + 1)):
            msg = "recommendation rank must equal position + 1"
            raise ValueError(msg)
        section_ids = [s.id for s in self.sections]
        if len(set(section_ids)) != len(section_ids):
            msg = "section ids must be unique"
            raise ValueError(msg)
        if not set(self.flags) <= {"partial_coverage", "notes"}:
            msg = "flags keys must be partial_coverage or notes"
            raise ValueError(msg)
        if self.mode == "findings_only" and (
            self.recommendations or section_ids != ["executive_summary"]
        ):
            msg = "findings_only holds no recommendations and only the executive_summary section"
            raise ValueError(msg)
        return self

    @classmethod
    def writer_output_model(cls) -> type[BaseModel]:
        """The Writer output class; the same class object on every call (U06-19)."""
        return _WriterOutput

    @classmethod
    def writer_schema(cls) -> dict[str, Any]:
        """JSON Schema of the Writer output; every record object is closed (U06-19)."""
        return _WriterOutput.model_json_schema()


class ChatAnswer(_Model):
    """Chat role output (U06-20, design 06 §4.5)."""

    text: str = Field(min_length=1, max_length=8_000)
    numbers: list[NumberRef] = Field(max_length=40)
    query_ids: list[_QueryId]
    unknowns: list[str] = Field(default=[], max_length=20)
    followups: list[str] = Field(default=[], max_length=10)


class ModeEvent(_Model):
    """The chat mode chosen for the turn (U06-21)."""

    type: Literal["mode"] = "mode"
    mode: ChatMode
    message: str


class TokenEvent(_Model):
    """A chunk of the answer text (U06-21)."""

    type: Literal["token"] = "token"
    text: str


class ToolEvent(_Model):
    """One tool call of the chat turn (U06-21)."""

    type: Literal["tool"] = "tool"
    name: str
    query_id: _QueryId | None
    ok: bool


class EvidenceEvent(_Model):
    """One recorded query the answer may cite (U06-21)."""

    type: Literal["evidence"] = "evidence"
    query_id: _QueryId


class VerificationEvent(_Model):
    """Chat verification outcome (U06-21)."""

    type: Literal["verification"] = "verification"
    result: VerificationResult
    status: Literal["verified", "partial", "unverified"]
    removed_claims: list[str]


class EscalatedEvent(_Model):
    """The question was escalated to a mini swarm (U06-21)."""

    type: Literal["escalated"] = "escalated"
    run_id: str = Field(pattern=_RUN_ID)
    job_id: str = Field(pattern=_JOB_ID)


class FinalEvent(_Model):
    """The verified answer of the turn (U06-21)."""

    type: Literal["final"] = "final"
    answer: ChatAnswer
    run_id: str = Field(pattern=_RUN_ID)


class ErrorEvent(_Model):
    """The turn failed; no secret or ticket text in `message` or `hint` (U06-21)."""

    type: Literal["error"] = "error"
    error_type: str
    message: str
    hint: str | None


class CorrectionCapturedEvent(_Model):
    """A chat correction was captured after the answer (U06-21, R-32, D06-32)."""

    type: Literal["correction_captured"] = "correction_captured"
    memory_id: str = Field(pattern=_MEMORY_ID)


ChatEvent = Annotated[
    ModeEvent | TokenEvent | ToolEvent | EvidenceEvent | VerificationEvent | EscalatedEvent
    | FinalEvent | ErrorEvent | CorrectionCapturedEvent,
    Field(discriminator="type"),
]  # fmt: skip
CHAT_EVENT_ADAPTER: Final[TypeAdapter[ChatEvent]] = TypeAdapter(ChatEvent)
