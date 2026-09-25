"""07-owned shared memory types (impl 07 U07-01 … U07-10; design 07 §3.2; R-01, R-30).

Re-exported from `herness.core.types`. Data types only (R-75); the memory behaviour lives
in `herness.harness.memory`, whose `types.py` re-exports these names.
"""

import types
from collections.abc import Mapping
from datetime import date
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from herness.core.errors import ToolInputError
from herness.core.types.harness import NumberRef, ToolContext

# Same patterns as MEMORY_ID_RE, QUERY_ID_RE, FINDING_ID_RE in herness.harness.memory.types.
_ULID = "[0-9A-HJKMNP-TV-Z]{26}"
_MEMORY_ID = Annotated[str, Field(pattern=rf"^mem_{_ULID}$")]
_QUERY_ID = Annotated[str, Field(pattern=r"^q_[0-9a-f]{16}$")]
_FINDING_ID = Annotated[str, Field(pattern=rf"^fnd_{_ULID}$")]
_MARKER = Annotated[str, Field(pattern=r"^n[0-9]+$")]
_UNIT = Annotated[float, Field(ge=0, le=1)]
_SHORT_CONTENT_MAX: Final = 2_000
_COMPONENT_KEYS: Final = frozenset({"sim", "kw", "ent", "rec", "conf", "final"})
_TALLY_KEYS: Final = ("accepted", "paid_off", "no_effect", "worse", "inconclusive", "pending")
_OUTCOME_KEYS: Final = frozenset(
    {"outcome_id", "measurement", "verdict", "baseline", "actual", "delta", "rel", "query_id"}
)

Layer = Literal["episodic", "semantic", "procedural"]
Kind = Literal[
    "run_summary", "outcome_summary", "decision_note", "glossary", "business_rule", "mapping",
    "insight", "user_correction", "sql_template", "qa_pair", "analysis_recipe",
]  # fmt: skip
Status = Literal["candidate", "pending_approval", "active", "expired", "rejected"]
# fmt: off
KIND_LAYER: Final[Mapping[Kind, Layer]] = types.MappingProxyType({
    "run_summary": "episodic", "outcome_summary": "episodic", "decision_note": "episodic",
    "glossary": "semantic", "business_rule": "semantic", "mapping": "semantic",
    "insight": "semantic", "user_correction": "semantic", "sql_template": "procedural",
    "qa_pair": "procedural", "analysis_recipe": "procedural",
})
# fmt: on


def _unique_numbers(numbers: list[NumberRef]) -> list[NumberRef]:
    if len({n.id for n in numbers}) != len(numbers):
        msg = "numbers ids must be unique"
        raise ValueError(msg)
    return numbers


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Provenance(_Strict):
    """Who wrote a memory item, from which run, task, session and evidence (U07-02)."""

    author_type: Literal["agent", "human", "system"]
    author_role: Annotated[str, Field(max_length=40)] | None
    author_ref: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None
    run_id: Annotated[str, Field(pattern=rf"^run_{_ULID}$")] | None
    task_id: Annotated[str, Field(pattern=rf"^task_{_ULID}$")] | None
    query_ids: list[_QUERY_ID] = Field(default=[], max_length=20)
    finding_ids: list[_FINDING_ID] = Field(default=[], max_length=20)
    session_id: Annotated[str, Field(max_length=64)] | None = None
    source_message_id: Annotated[str, Field(max_length=64)] | None = None
    build_id: Annotated[str, Field(pattern=r"^\d{8}-\d{6}-[0-9A-Z]{6}$")] | None = None
    via: Literal["tool", "pipeline", "chat", "cli", "dashboard", "outcome_job", "promotion"]

    @model_validator(mode="after")
    def _author_invariants(self) -> Self:
        if self.author_type == "human" and self.author_ref is None:
            msg = "author_ref is required for a human author"
            raise ValueError(msg)
        if self.author_type == "agent":
            for name in ("author_role", "run_id"):
                if getattr(self, name) is None:
                    msg = f"{name} is required for an agent author"
                    raise ValueError(msg)
        return self


class MemoryItem(_Frozen):
    """One hydrated `memory_item` row (U07-03); lax because it comes from SQLite text."""

    memory_id: _MEMORY_ID
    layer: Layer
    kind: Kind
    content: str = Field(max_length=8_000)
    data: dict[str, JsonValue]
    provenance: Provenance
    confidence: _UNIT
    status: Status
    created_at: AwareDatetime
    expires_at: AwareDatetime | None
    last_used_at: AwareDatetime | None
    use_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _kind_rules(self) -> Self:
        if KIND_LAYER[self.kind] != self.layer:
            msg = f"layer {self.layer} does not match kind {self.kind}"
            raise ValueError(msg)
        if self.kind not in ("sql_template", "qa_pair") and len(self.content) > _SHORT_CONTENT_MAX:
            msg = f"content longer than 2000 chars for kind {self.kind}"
            raise ValueError(msg)
        return self


class MemoryProposal(_Strict):
    """Input to `MemoryStore.propose` (U07-04); kind-specific limits are checked at write."""

    layer: Layer
    kind: Kind
    content: str = Field(min_length=1, max_length=8_000)
    data: dict[str, JsonValue] = {}
    numbers: Annotated[list[NumberRef], Field(max_length=20)] = []
    confidence: float = Field(ge=0, le=1)
    expires_at: AwareDatetime | None = None
    provenance: Provenance

    _numbers = field_validator("numbers")(_unique_numbers)


class RecallHit(_Frozen):
    """One recall result with its score breakdown (U07-05)."""

    item: MemoryItem
    score: _UNIT
    components: dict[Literal["sim", "kw", "ent", "rec", "conf", "final"], float]
    unconfirmed: bool

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if set(self.components) != _COMPONENT_KEYS or self.components["final"] != self.score:
            msg = "components need all six keys and final == score"
            raise ValueError(msg)
        if self.unconfirmed != (self.item.status == "pending_approval"):
            msg = "unconfirmed must equal item.status == pending_approval"
            raise ValueError(msg)
        return self


class MemoryRunContext(_Frozen):
    """Run identity for memory calls (U07-06); built from the tool context and run row."""

    run_id: str
    run_kind: str
    role: str
    task_id: str | None
    build_id: str
    profile: str
    session_id: str | None = None
    user_ref: str | None = None

    @classmethod
    def from_tool_ctx(cls, ctx: ToolContext, *, run_meta: Mapping[str, JsonValue]) -> Self:
        """Copy identity from `ctx`; `run_meta` holds `run.kind` and the chat run meta."""
        kind = run_meta.get("kind")
        if not isinstance(kind, str):
            msg = f"run kind unknown for {ctx.run_id}"
            raise ToolInputError(msg)
        session_id, user_ref = run_meta.get("session_id"), run_meta.get("user_ref")
        return cls(
            run_id=ctx.run_id, run_kind=kind, role=ctx.role, task_id=ctx.task_id,
            build_id=ctx.build_id, profile=ctx.profile,
            session_id=session_id if isinstance(session_id, str) else None,
            user_ref=user_ref if isinstance(user_ref, str) else None,
        )  # fmt: skip


class RecommendationDraft(_Strict):
    """One recommendation handed to `write_recommendations` (U07-07; binding on 06, R-30)."""

    rank: int = Field(ge=1)
    kind: Literal["fund", "org_action"]
    target_type: Literal["service", "team", "org", "work_item"]
    target_id: str = Field(max_length=200)
    summary: str = Field(min_length=1, max_length=400)
    numbers: Annotated[list[NumberRef], Field(max_length=20)]
    expected_metric: str | None
    expected_delta_ref: _MARKER | None
    expected_usd_ref: _MARKER | None
    finding_ids: list[_FINDING_ID] = Field(min_length=1, max_length=50)

    _numbers = field_validator("numbers")(_unique_numbers)


class PriorRecommendation(_Frozen):
    """A prior recommendation with its decision and latest outcome (U07-08)."""

    rec_id: str
    run_id: str
    kind: str
    target_type: str
    target_id: str
    summary: str
    numbers: list[NumberRef]
    expected_metric: str | None
    confidence: _UNIT
    decision: Literal["accepted", "rejected", "deferred"] | None
    decided_at: AwareDatetime | None
    effective_at: AwareDatetime | None
    outcome: dict[str, JsonValue] | None
    next_measurement_due: date | None

    @field_validator("outcome")
    @classmethod
    def _outcome_keys(cls, value: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        if value is not None and set(value) != _OUTCOME_KEYS:
            msg = f"outcome needs exactly the keys {sorted(_OUTCOME_KEYS)}"
            raise ValueError(msg)
        return value


class PriorContext(_Frozen):
    """Output of `prior_context` (U07-09; `MemoryStore.prior_context` returns it, R-30)."""

    items: list[PriorRecommendation]
    rendered: str
    memory_ids: list[str]
    tally: dict[str, int]

    @field_validator("tally")
    @classmethod
    def _fill_tally(cls, value: dict[str, int]) -> dict[str, int]:
        unknown = set(value) - set(_TALLY_KEYS)
        if unknown:
            msg = f"unknown tally keys {sorted(unknown)}"
            raise ValueError(msg)
        return {key: value.get(key, 0) for key in _TALLY_KEYS}


class SimilarOutcome(_Frozen):
    """One similar past recommendation and its verdict (U07-10)."""

    rec_id: str
    sim: float
    verdict: Literal["paid_off", "no_effect", "worse", "inconclusive"]
    outcome_query_id: str


class ConfidenceAdjustment(_Frozen):
    """Result of the outcome feedback into confidence (U07-10; design 07 §5.10)."""

    confidence: float = Field(ge=0.05, le=0.95)
    base: _UNIT
    delta: float = Field(ge=-0.25, le=0.15)
    similar: list[SimilarOutcome] = Field(max_length=20)
