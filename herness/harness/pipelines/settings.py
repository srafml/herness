"""Model of `config/pipelines.yaml` and per-run knob resolution (impl 06 §3.2, §9; U06-22-24).

Settings leaf (R-03, contract `settings modules are leaves`): imports only the standard library,
pydantic, `herness.core.types` and `herness.core.errors`.
"""

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from typing import Annotated, Any, Final, Literal, Self, get_args

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, ValidationError
from pydantic.functional_validators import field_validator, model_validator

from herness.core.errors import ConfigError
from herness.core.types import Depth, Specialty, TaskBudget

_DEPTHS: Final[frozenset[str]] = frozenset(get_args(Depth.__value__))
_STRUCTURAL: Final = frozenset({"analyst_budget", "k_samples", "org_specialties", "large_stage"})

type _Pos = Annotated[int, Field(ge=1)]
type _NonNeg = Annotated[int, Field(ge=0)]
type _Fraction = Annotated[float, Field(ge=0, le=1)]
type _NonNegFloat = Annotated[float, Field(ge=0)]
type _Count10 = Annotated[int, Field(ge=1, le=10)]
type _Small = Annotated[int, Field(ge=1, le=5)]
type _Sample = Annotated[int, Field(ge=1, le=9)]


def _all_depths[V](value: dict[Depth, V]) -> dict[Depth, V]:
    if set(value) != _DEPTHS:
        msg = "depth must have exactly the keys fast, standard, deep"
        raise ValueError(msg)
    return value


type _ByDepth[V] = Annotated[dict[Depth, V], AfterValidator(_all_depths)]


def _by_depth(fast: object, standard: object, deep: object) -> Any:  # noqa: ANN401 - pydantic Field
    return Field(
        default_factory=lambda: deepcopy({"fast": fast, "standard": standard, "deep": deep})
    )


class _Model(BaseModel):
    """Closed, strict and immutable; YAML scalars arrive as native types (U06-22)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DedupSettings(_Model):
    """`swarm.dedup`."""

    cosine: _Fraction = 0.85


class CoverageSettings(_Model):
    """`swarm.coverage`."""

    min_done_fraction: _Fraction = 0.8


class SkepticSettings(_Model):
    """`swarm.skeptic`: thresholds passed to the Skeptic prompt input."""

    min_sample: _Pos = 30
    single_record_share: _Fraction = 0.25
    min_weeks_seasonality: _Pos = 13


class CrosscheckSettings(_Model):
    """`swarm.crosscheck`: tolerances for SQL cross-validation."""

    rel_tol: _NonNegFloat = 0.005
    abs_tol_count: _NonNegFloat = 0.5
    abs_tol_ratio: _NonNegFloat = 0.001


class SwarmSettings(_Model):
    """`swarm`: scheduler, spawn, dedup, coverage and adversarial-layer settings (§9)."""

    max_task_attempts: _Count10 = 3
    oversubscribe: float = Field(default=1.5, ge=1.0, le=4.0)
    admit_fraction: _Fraction = 0.25
    aging_per_min: _Fraction = 0.01
    max_inflight_children_per_parent: _Count10 = 2
    max_inflight_per_entity: _Count10 = 2
    max_children_per_task: int = Field(default=3, ge=0, le=10)
    child_budget_factor: float = Field(default=0.5, gt=0, le=1)
    writer_reserve: float = Field(default=0.15, gt=0, lt=1)
    verifier_batch: int = Field(default=20, ge=1, le=200)
    dedup: DedupSettings = DedupSettings()
    coverage: CoverageSettings = CoverageSettings()
    skeptic: SkepticSettings = SkepticSettings()
    crosscheck: CrosscheckSettings = CrosscheckSettings()


class BudgetSettings(_Model):
    """Task step, token and wall-clock limits, within the `TaskBudget` bounds (U06-04)."""

    max_steps: int = Field(ge=1, le=200)
    max_tokens: int = Field(ge=1_000)
    wall_clock_s: int = Field(ge=1, le=86_400)

    def to_task_budget(self) -> TaskBudget:
        """The shared `TaskBudget` with these limits and no cost cap (U06-24 step 1)."""
        return TaskBudget(**self.model_dump(), max_cost_usd=Decimal(0))


class KSamples(_Model):
    """Skeptic samples per challenge: `default`, and `on_reject` after a reject (U06-23)."""

    default: _Sample
    on_reject: _Sample

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.on_reject < self.default:
            msg = "on_reject must be >= default"
            raise ValueError(msg)
        return self


class _DepthFields(BaseModel):
    """Knobs shared by `DepthConfig` and `DepthKnobs` (§9, U06-23)."""

    max_tasks_per_run: _Pos
    run_tokens: _Pos
    skeptic_top_n: _NonNeg
    skeptic_rounds: _Small
    k_samples: KSamples
    max_spawn_depth: int = Field(ge=0, le=2)
    plans_best_of: _Small
    crosscheck_ways: _Small
    writer_fix_passes: int = Field(ge=0, le=5)
    writer_max_findings: _Pos
    large_stage: bool = False


class DepthConfig(_DepthFields, _Model):
    """`depth.<fast|standard|deep>`: the knobs one depth mode sets (§9)."""

    analyst_budget: BudgetSettings

    @field_validator("k_samples", mode="before")
    @classmethod
    def _normalize_k(cls, value: object) -> object:
        """An int n → `KSamples(n, n)`; `{"reject": k}` → `KSamples(1, k)` (U06-23)."""
        if isinstance(value, int) and not isinstance(value, bool):
            return KSamples(default=value, on_reject=value)
        if isinstance(value, dict) and set(value) == {"reject"}:
            return KSamples(default=1, on_reject=value["reject"])
        msg = "k_samples must be an int or {reject: int}"
        raise ValueError(msg)


class DepthKnobs(_DepthFields):
    """Resolved knob set for one review run (U06-23), built by `resolve_knobs`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    analyst_budget: TaskBudget
    window_days: _Pos
    K_candidates: int = 0
    K_clusters: int = 0
    K_teams: int = 0
    M_must: _NonNeg
    H_wildcards: _NonNeg
    org_specialties: list[Specialty] = []


def _override_problem(key: str, value: object) -> str | None:
    """The TH06-15 allowlist check of one `budget_override` item (U06-24 step 3)."""
    if key not in DepthKnobs.model_fields or key in _STRUCTURAL:
        return f"unknown budget_override key: {key}"
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return f"budget_override {key} must be a non-negative int"
    return None


def _escalation_keys(value: dict[str, int]) -> dict[str, int]:
    for key, item in value.items():
        if (problem := _override_problem(key, item)) is not None:
            raise ValueError(problem)
    return value


class _PipelineConfig(_Model):
    window_days: _Pos

    def depth_keys(self, depth: Depth) -> dict[str, object]:
        """`window_days` plus every per-depth map of this pipeline taken at `depth`."""
        keys: dict[str, object] = {"window_days": self.window_days}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, dict):
                keys[name] = value[depth]
        return keys


class FundingPipelineConfig(_PipelineConfig):
    """`pipelines.funding_review` (§9, design 06 §5.11)."""

    window_days: _Pos = 365
    portfolio_scenario: str = Field(default="base", min_length=1, max_length=64)
    K_candidates: _ByDepth[_NonNeg] = _by_depth(10, 25, 60)
    K_clusters: _ByDepth[_NonNeg] = _by_depth(5, 10, 25)
    M_must: _ByDepth[_NonNeg] = _by_depth(5, 10, 20)
    H_wildcards: _ByDepth[_NonNeg] = _by_depth(0, 3, 8)


type _Specialties = Annotated[list[Specialty], Field(min_length=1)]


class OrgPipelineConfig(_PipelineConfig):
    """`pipelines.org_review` (§9, design 06 §5.11)."""

    window_days: _Pos = 180
    K_teams: _ByDepth[_NonNeg] = _by_depth(10, 25, 60)
    org_specialties: _ByDepth[_Specialties] = _by_depth(
        ["ops"], ["ops", "change"], ["ops", "change", "delivery"]
    )
    M_must: _ByDepth[_NonNeg] = _by_depth(5, 10, 20)
    H_wildcards: _ByDepth[_NonNeg] = _by_depth(0, 3, 8)


_ESCALATION: Final = {
    "max_tasks_per_run": 8, "skeptic_rounds": 1, "skeptic_top_n": 3, "run_tokens": 400_000
}  # fmt: skip


class ChatPipelineConfig(_Model):
    """`pipelines.chat`: turn budget and the `budget_override` used on escalation."""

    budget: BudgetSettings = BudgetSettings(max_steps=12, max_tokens=40_000, wall_clock_s=120)
    escalation: Annotated[dict[str, int], AfterValidator(_escalation_keys)] = Field(
        default_factory=lambda: dict(_ESCALATION)
    )


class PipelineSections(_Model):
    """`pipelines`: one section per pipeline kind."""

    funding_review: FundingPipelineConfig = FundingPipelineConfig()
    org_review: OrgPipelineConfig = OrgPipelineConfig()
    chat: ChatPipelineConfig = ChatPipelineConfig()


def _usd(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        msg = "must be a number"
        raise ValueError(msg)  # noqa: TRY004 - pydantic reports ValueError with the key path
    return Decimal(str(value))


class HybridConfig(_Model):
    """`hybrid`: evidence-pack and off-network cost caps (design 06 §5.12)."""

    max_input_tokens_per_call: int = Field(default=30_000, ge=10_000)
    max_cost_usd_per_run: Annotated[Decimal, BeforeValidator(_usd)] = Field(Decimal(15), ge=0)


def _budget(steps: int, tokens: int, wall: int) -> dict[str, int]:
    return {"max_steps": steps, "max_tokens": tokens, "wall_clock_s": wall}


_DEPTH_DEFAULTS: Final[dict[Depth, dict[str, object]]] = {
    "fast": {"max_tasks_per_run": 40, "run_tokens": 1_500_000, "skeptic_top_n": 5,
             "skeptic_rounds": 1, "k_samples": 1, "max_spawn_depth": 0, "plans_best_of": 1,
             "crosscheck_ways": 1, "writer_fix_passes": 1, "writer_max_findings": 25,
             "analyst_budget": _budget(12, 40_000, 300)},
    "standard": {"max_tasks_per_run": 150, "run_tokens": 6_000_000, "skeptic_top_n": 15,
                 "skeptic_rounds": 2, "k_samples": {"reject": 3}, "max_spawn_depth": 1,
                 "plans_best_of": 1, "crosscheck_ways": 2, "writer_fix_passes": 2,
                 "writer_max_findings": 60, "analyst_budget": _budget(20, 80_000, 600)},
    "deep": {"max_tasks_per_run": 600, "run_tokens": 30_000_000, "skeptic_top_n": 40,
             "skeptic_rounds": 3, "k_samples": 5, "max_spawn_depth": 2, "plans_best_of": 3,
             "crosscheck_ways": 3, "writer_fix_passes": 3, "writer_max_findings": 120,
             "analyst_budget": _budget(30, 150_000, 1_800), "large_stage": True},
}  # fmt: skip


class PipelinesConfig(_Model):
    """Root of `config/pipelines.yaml` (`cfg.pipelines`, U06-22); defaults = the shipped file.

    Violations raise `ValidationError` with the key path (spec 10 reports it as `ConfigError`).
    """

    version: Literal[1] = 1
    swarm: SwarmSettings = SwarmSettings()
    depth: _ByDepth[DepthConfig] = Field(
        default_factory=lambda: {
            d: DepthConfig.model_validate(v) for d, v in _DEPTH_DEFAULTS.items()
        }
    )
    pipelines: PipelineSections = PipelineSections()
    hybrid: HybridConfig = HybridConfig()


type ReviewKind = Literal["funding_review", "org_review"]


def resolve_knobs(
    cfg: PipelinesConfig,
    kind: ReviewKind,
    depth: Depth,
    *,
    override: Mapping[str, int] | None = None,
) -> DepthKnobs:
    """Build the `DepthKnobs` of one review run (U06-24); pure.

    Raises `ConfigError` for an override outside the int-knob allowlist (TH06-15), a value that is
    not a non-negative int, or resolved knobs that break a `DepthKnobs` bound.
    """
    base = cfg.depth[depth]
    knobs: dict[str, object] = dict(base)
    knobs["analyst_budget"] = base.analyst_budget.to_task_budget()
    section: _PipelineConfig = getattr(cfg.pipelines, kind)
    knobs.update(section.depth_keys(depth))
    for key, value in (override or {}).items():
        if (problem := _override_problem(key, value)) is not None:
            raise ConfigError(problem)
        knobs[key] = value
    try:
        return DepthKnobs.model_validate(knobs)
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
        msg = f"invalid {kind} {depth} knobs: {fields}"
        raise ConfigError(msg) from exc
