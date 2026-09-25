"""06-owned task, finding, challenge and checkpoint-state types (impl 06 U06-01 … U06-12, U06-140).

Design 06 §4.1 … §4.3. Marker, numeral, evidence, entity and PII checks run in U06-53.
"""

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Annotated, Final, Literal, Self, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue
from pydantic.functional_validators import AfterValidator, model_validator
from pydantic_core import ValidationError

from herness.core.errors import SchemaViolation
from herness.core.types.harness import Budgets, NumberRef, VerificationResult

_RUN_ID: Final = r"^run_[0-9A-HJKMNP-TV-Z]{26}$"
_TASK_ID: Final = r"^task_[0-9A-HJKMNP-TV-Z]{26}$"
_FINDING_ID: Final = r"^fnd_[0-9A-HJKMNP-TV-Z]{26}$"
_QUERY_ID: Final = r"^q_[0-9a-f]{16}$"
_DEDUP_KEY: Final = r"^[0-9a-f]{16}$"
_NUMBER_ID: Final = r"^n[0-9]+$"
_NAME: Final = r"^[a-z_]{1,40}$"

type RunKind = Literal["funding_review", "org_review", "chat"]
type Depth = Literal["fast", "standard", "deep"]
type Role = Literal["planner", "judge", "analyst", "skeptic", "verifier", "writer", "chat"]
type Specialty = Literal[
    "ops", "change", "delivery", "org", "crosscheck", "retrospective", "general"
]  # fmt: skip
type ScopeEntityType = Literal[
    "service", "team", "org", "work_item", "cluster", "candidate", "run"
]  # fmt: skip
type SkepticCheck = Literal[
    "confounding", "seasonality", "mis_mapping", "small_sample", "double_counting",
    "survivorship",
]  # fmt: skip
SKEPTIC_CHECKS: Final[tuple[SkepticCheck, ...]] = get_args(SkepticCheck.__value__)
type RejectReason = Literal[
    "skeptic_reject", "verifier_fail", "crosscheck_disagree", "withdrawn", "revision_dead"
]  # fmt: skip
type FindingStatus = Literal["proposed", "challenged", "verified", "rejected", "revised", "merged"]
type Banner = Literal[
    "dq_warnings", "unconfirmed_weights", "partial_run", "hybrid_fallback", "budget_exhausted"
]  # fmt: skip
type SectionId = Literal[
    "executive_summary", "recommendations", "portfolio", "org_scorecards", "actions",
    "retrospective", "risks_and_caveats", "method",
]  # fmt: skip

_REJECT_REASONS: Final = frozenset(get_args(RejectReason.__value__))


def _unique[T](values: list[T]) -> list[T]:
    if len(set(values)) != len(values):
        msg = "items must be unique"
        raise ValueError(msg)
    return values


def _reject_reason(value: str) -> str:
    reason, sep, detail = value.partition(":")
    if reason not in _REJECT_REASONS or (sep and not detail):
        msg = "reason must be a RejectReason or '<RejectReason>:<detail>'"
        raise ValueError(msg)
    return value


type _FindingId = Annotated[str, Field(pattern=_FINDING_ID)]
type _QueryId = Annotated[str, Field(pattern=_QUERY_ID)]
type _EntityId = Annotated[str, Field(min_length=1, max_length=200)]
type _UtcDatetime = Annotated[AwareDatetime, AfterValidator(lambda v: v.astimezone(UTC))]
_UNIQUE: Final = AfterValidator(_unique)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)  # JSON input, impl 06 §3


class EntityScope(_Model):
    """Entities one task is about (U06-02); `entity_ids` keeps input order."""

    entity_type: ScopeEntityType
    entity_ids: Annotated[list[_EntityId], Field(min_length=1, max_length=50), _UNIQUE]
    period_start: date | None = None
    period_end: date | None = None

    @model_validator(mode="after")
    def _period_order(self) -> Self:
        start, end = self.period_start, self.period_end
        if start is not None and end is not None and start > end:
            msg = "period_start is after period_end"
            raise ValueError(msg)
        return self


class TaskInputs(_Model):
    """Evidence and framing a task starts from (U06-03)."""

    finding_ids: list[_FindingId] = Field(default=[], max_length=200)
    query_ids: list[_QueryId] = Field(default=[], max_length=200)
    candidate_ids: list[str] = Field(default=[], max_length=200)
    memory_ids: list[str] = Field(default=[], max_length=200)
    dq_warnings: list[str] = Field(default=[], max_length=100)
    notes: str | None = Field(default=None, max_length=1_500)


class TaskBudget(_Model):
    """Per-task limits (U06-04), within spec 05 `Budgets` bounds; sole conversion to it (R-23)."""

    max_steps: int = Field(ge=1, le=200)
    max_tokens: int = Field(ge=1_000)
    max_cost_usd: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    wall_clock_s: int = Field(ge=1, le=86_400)

    def to_budgets(self, now: datetime) -> Budgets:
        """Spec 05 `Budgets` with `deadline = now + wall_clock_s` (R-22); naive `now` fails."""
        if now.tzinfo is None or now.utcoffset() is None:
            msg = "naive datetime"
            raise SchemaViolation(msg)
        return Budgets(**self.model_dump(), deadline=now + timedelta(seconds=self.wall_clock_s))

    def scaled(self, factor: float) -> "TaskBudget":
        """Limits times `factor` (0 < factor <= 1), never below their minimums; cost rounds down."""
        if not 0 < factor <= 1:
            msg = "factor must be in (0, 1]"
            raise ValueError(msg)
        cost = self.max_cost_usd * Decimal(str(factor))
        return TaskBudget(
            max_steps=max(1, math.floor(self.max_steps * factor)),
            max_tokens=max(1_000, math.floor(self.max_tokens * factor)),
            max_cost_usd=cost.quantize(Decimal("0.01"), ROUND_DOWN),
            wall_clock_s=max(1, math.floor(self.wall_clock_s * factor)),
        )


class TaskSpec(_Model):
    """One swarm task, stored as `task.spec` (U06-05, design 06 §4.1)."""

    task_id: str = Field(pattern=_TASK_ID)
    run_id: str = Field(pattern=_RUN_ID)
    role: Role
    specialty: Specialty = "general"
    objective: str = Field(min_length=1, max_length=2_000)
    scope: EntityScope
    inputs: TaskInputs = TaskInputs()
    tools: Annotated[list[Annotated[str, Field(pattern=_NAME)]], _UNIQUE]
    budget: TaskBudget
    depth: int = Field(default=0, ge=0, le=2)
    parent_task_id: str | None = Field(default=None, pattern=_TASK_ID)
    priority: float = Field(default=0.0, allow_inf_nan=False)
    model_role: str = Field(pattern=_NAME)
    must_cover: bool = False
    dedup_key: str = Field(pattern=_DEDUP_KEY)
    revision_of: str | None = Field(default=None, pattern=_FINDING_ID)
    round: int = Field(default=0, ge=0, le=10)
    k_samples: int = Field(default=1, ge=1, le=9)

    @model_validator(mode="after")
    def _role_invariants(self) -> Self:
        if self.revision_of is not None and (self.role != "analyst" or self.round < 1):
            msg = "revision_of requires role 'analyst' and round >= 1"
            raise ValueError(msg)
        if self.role == "skeptic" and (self.round < 1 or len(self.inputs.finding_ids) != 1):
            msg = "a skeptic task needs round >= 1 and exactly one input finding"
            raise ValueError(msg)
        return self


class PlannedTask(_Model):
    """One Planner-proposed task (U06-06, R-28); it has no budget, tool, priority or id field."""

    dedup_key: str | None = Field(pattern=_DEDUP_KEY)
    specialty: Specialty
    objective: str = Field(min_length=1, max_length=2_000)
    entity_type: ScopeEntityType
    entity_ids: list[_EntityId] = Field(min_length=1, max_length=50)
    notes: str | None = Field(max_length=1_500)


class CheckResult(_Model):
    """One Skeptic check outcome (U06-09); concern or fail cites at least one query."""

    check: SkepticCheck
    result: Literal["pass", "concern", "fail", "n_a"]
    note: str = Field(max_length=400)
    numbers: list[NumberRef] = Field(default=[], max_length=10)
    query_ids: list[_QueryId] = []

    @model_validator(mode="after")
    def _cited(self) -> Self:
        if self.result in {"concern", "fail"} and not self.query_ids:
            msg = f"a {self.result} result needs at least one query_id"
            raise ValueError(msg)
        return self


class Challenge(_Model):
    """Skeptic output and challenge history record (U06-10); defaults filled by the swarm."""

    finding_id: str = Field(pattern=_FINDING_ID)
    round: int = Field(default=0, ge=0, le=10)
    skeptic_task_id: str = ""
    checks: list[CheckResult]
    verdict: Literal["uphold", "revise", "reject"]
    required_actions: list[Annotated[str, Field(max_length=400)]] = Field(default=[], max_length=10)
    votes: dict[str, int] | None = None
    model: str = ""

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if sorted(check.check for check in self.checks) != sorted(SKEPTIC_CHECKS):
            msg = "checks must hold exactly one result per SkepticCheck"
            raise ValueError(msg)
        if self.verdict == "revise" and not self.required_actions:
            msg = "a revise verdict needs at least one required action"
            raise ValueError(msg)
        return self


class CrossCheck(_Model):
    """Independent recomputations of one number (U06-11); the first query is the original."""

    number_id: str = Field(pattern=_NUMBER_ID)
    query_ids: Annotated[list[_QueryId], _UNIQUE]
    values: list[Annotated[float, Field(allow_inf_nan=False)]]
    agreed: bool

    @model_validator(mode="after")
    def _aligned(self) -> Self:
        if len(self.values) != len(self.query_ids):
            msg = "values and query_ids must have the same length"
            raise ValueError(msg)
        return self


class VerificationRecord(_Model):
    """Gate 1 record stored in `finding.verification` (U06-12)."""

    gate: Literal[1]
    result: VerificationResult
    cross_checks: list[CrossCheck] = []
    reason: Annotated[str, Field(max_length=200), AfterValidator(_reject_reason)] | None = None


class Finding(_Model):
    """One blackboard entry, 1:1 with ops `finding` (U06-07, design 06 §4.2)."""

    finding_id: str = Field(pattern=_FINDING_ID)
    run_id: str = Field(pattern=_RUN_ID)
    task_id: str = Field(pattern=_TASK_ID)
    author_role: Role
    claim: str = Field(min_length=1, max_length=1_500)
    entity_type: ScopeEntityType
    entity_id: _EntityId
    numbers: list[NumberRef] = Field(min_length=1, max_length=20)
    query_ids: Annotated[list[_QueryId], Field(min_length=1, max_length=50), _UNIQUE]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    status: FindingStatus = "proposed"
    challenge: list[Challenge] = []
    verification: VerificationRecord | None = None
    supersedes: str | None = Field(default=None, pattern=_FINDING_ID)
    merged_into: str | None = Field(default=None, pattern=_FINDING_ID)
    created_at: _UtcDatetime

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not {n.query_id for n in self.numbers} <= set(self.query_ids):
            msg = "query_ids must include every number's query_id"
            raise ValueError(msg)
        if len({n.id for n in self.numbers}) != len(self.numbers):
            msg = "number ids must be unique"
            raise ValueError(msg)
        if (self.status == "merged") != (self.merged_into is not None):
            msg = "status 'merged' requires merged_into, and merged_into requires 'merged'"
            raise ValueError(msg)
        return self


class SwarmTaskState(_Model):
    """The `state` key of the task checkpoint envelope (U06-140, R-21)."""

    phase: str = Field(min_length=1)
    pending_findings: Annotated[list[_FindingId], Field(max_length=200), _UNIQUE] = []
    proposals: list[dict[str, JsonValue]] | None = Field(default=None, max_length=5)
    pseudonyms: dict[str, dict[str, str]] | None = None

    @classmethod
    def from_envelope(cls, checkpoint: Mapping[str, object], *, task_id: str) -> Self | None:
        """The stored `state` key, or None when absent or null; invalid → `SchemaViolation`."""
        state = checkpoint.get("state")
        if state is None:
            return None
        try:
            return cls.model_validate(state)
        except ValidationError as exc:
            msg = f"task state invalid: task_id={task_id}"
            raise SchemaViolation(msg, task_id=task_id) from exc
