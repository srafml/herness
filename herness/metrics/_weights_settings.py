"""Shared model primitives and the config/weights.yaml models (U04-19), re-exported by settings.

Private sibling of herness.metrics.settings; the same import rule applies (R-03): only the
standard library, pydantic, herness.core.types and herness.core.errors.
"""

import re
import types
import zoneinfo
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# Decimal text keeps any precision (an operator can quote an exact amount); YAML floats are
# limited to cents because binary floats cannot carry more digits reliably.
_DECIMAL_TEXT = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_MAX_CENT_EXPONENT: Final = -2
_PRIORITIES: Final = frozenset(range(1, 6))


def _to_decimal(value: object) -> object:
    """YAML has no decimal type: accept ints, floats with at most 2 decimals and decimal text."""
    if isinstance(value, float):
        number = Decimal(repr(value))
        exponent = number.as_tuple().exponent
        if not isinstance(exponent, int) or exponent < _MAX_CENT_EXPONENT:
            msg = "float amounts need at most 2 decimals; quote the value as a string"
            raise ValueError(msg)
        return number
    if type(value) is int or (isinstance(value, str) and _DECIMAL_TEXT.fullmatch(value)):
        return Decimal(value)
    return value


def rule(test: Callable[[Any], bool], msg: str) -> AfterValidator:
    """An after-validator that raises ValueError(msg) when `test(value)` is false."""

    def check(value: object) -> object:
        if not test(value):
            raise ValueError(msg)
        return value

    return AfterValidator(check)


def _is_zone(name: str) -> bool:
    try:
        zoneinfo.ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        return False
    return True


Money = Annotated[Decimal, BeforeValidator(_to_decimal)]
Fraction = Annotated[float, Field(ge=0, le=1)]
Positive = Annotated[float, Field(gt=0)]
_PriorityMap = Annotated[
    Mapping[int, Annotated[float, Field(ge=0, le=10)]],
    rule(lambda v: set(v) == _PRIORITIES, "keys must be exactly 1..5"),
]


class Model(BaseModel):
    """Frozen, strict, closed section model whose containers are read-only too.

    YAML lists arrive as tuples and mappings are wrapped in MappingProxyType, so a loaded
    config cannot be changed after its config_hash is computed (TH04-05).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, validate_default=True)

    @field_validator("*", mode="before")
    @classmethod
    def _lists_to_tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("*", mode="after")
    @classmethod
    def _read_only_maps(cls, value: object) -> object:
        return types.MappingProxyType(value) if isinstance(value, dict) else value


class WeightBlock(Model):
    """A weights.yaml block; `unconfirmed` is gated by `check_weight_confirmations`."""

    unconfirmed: bool = True


class CostPerDowntimeHour(WeightBlock):
    by_criticality: Annotated[
        Mapping[int, Annotated[Money, Field(ge=0)]],
        rule(lambda v: set(v) <= {1, 2, 3, 4}, "criticality keys must be within 1..4"),
    ]
    default: Annotated[Money, Field(ge=0)]


class PositiveAmount(WeightBlock):
    value: Annotated[Money, Field(gt=0)]


class SlaPenalty(WeightBlock):
    value: Annotated[Money, Field(ge=0)]


class PriorityImpactMultiplier(WeightBlock):
    values: _PriorityMap


class ImpactFallback(WeightBlock):
    enabled: bool
    max_priority: int = Field(ge=1, le=5)
    outage_fraction: Mapping[int, Fraction]
    cap_hours: Positive

    @model_validator(mode="after")
    def _fractions_cover_priorities(self) -> Self:
        if not set(range(1, self.max_priority + 1)) <= set(self.outage_fraction):
            msg = "outage_fraction needs a key for every priority up to max_priority"
            raise ValueError(msg)
        return self


class Toil(WeightBlock):
    effort_factor: _PriorityMap
    business_share: Fraction
    max_hours_per_incident: Positive
    triage_minutes_per_alert: float = Field(ge=0)
    reassignment_hours: float = Field(ge=0)
    reopen_rework_factor: float = Field(ge=0)


class ChangeWeights(WeightBlock):
    backout_effort_hours: float = Field(ge=0)


class StrategicWeights(WeightBlock):
    default: Positive
    clip: Annotated[
        tuple[Positive, Positive], rule(lambda v: v[0] <= v[1], "clip needs min <= max")
    ]
    portfolio: Mapping[str, Positive]
    org: Mapping[str, Positive]


class ExpectedReduction(WeightBlock):
    epic: Fraction
    feature: Fraction
    initiative: Fraction
    cluster_fix: Fraction
    overrides: Mapping[str, Fraction]


class ClusterFix(WeightBlock):
    effort_hours: Positive
    min_incidents_12m: int = Field(ge=1)
    min_annual_pain_usd: Annotated[Money, Field(ge=0)]
    max_linked_share: Fraction


class TeamCapacity(WeightBlock):
    default: Positive
    teams: Mapping[str, Positive]


class ScenarioConfig(Model):
    """A named portfolio budget scenario."""

    name: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    budget_usd: Annotated[Money, Field(gt=0, le=Decimal("1e12"))]


class SolverConfig(Model):
    """CP-SAT limits; one worker keeps the solve deterministic."""

    num_workers: Literal[1]
    random_seed: int = Field(ge=0)
    max_deterministic_time: float = Field(ge=0.1, le=600)
    max_time_in_seconds: float = Field(ge=1, le=600)


def _scenario_names_ok(scenarios: tuple[ScenarioConfig, ...]) -> bool:
    names = [scenario.name for scenario in scenarios]
    return len(set(names)) == len(names) and "unconstrained" not in names


class PortfolioConfig(Model):
    """`weights.yaml: portfolio` (design 04 §7.2, §5.10)."""

    horizon_quarters: int = Field(ge=1, le=8)
    scenarios: Annotated[
        tuple[ScenarioConfig, ...],
        Field(max_length=20),
        rule(_scenario_names_ok, "scenario names must be unique and not unconstrained"),
    ]
    mandatory: tuple[str, ...]
    excluded: tuple[str, ...]
    enforce_team_capacity: bool
    solver: SolverConfig

    @model_validator(mode="after")
    def _disjoint(self) -> Self:
        if set(self.mandatory) & set(self.excluded):
            msg = "mandatory and excluded overlap"
            raise ValueError(msg)
        return self


class WeightsConfig(Model):
    """Root model of config/weights.yaml (design 04 §7.2; spec 10 root field `weights`)."""

    version: Literal[1]
    business_timezone: Annotated[
        str, Field(min_length=1, max_length=64), rule(_is_zone, "unknown IANA time zone")
    ]
    cost_per_downtime_hour: CostPerDowntimeHour
    cost_per_engineer_hour: PositiveAmount
    hours_per_story_point: PositiveAmount
    priority_impact_multiplier: PriorityImpactMultiplier
    impact_fallback: ImpactFallback
    toil: Toil
    change: ChangeWeights
    sla_penalty_usd: SlaPenalty
    strategic_weights: StrategicWeights
    expected_reduction: ExpectedReduction
    cluster_fix: ClusterFix
    team_capacity_points_per_quarter: TeamCapacity
    portfolio: PortfolioConfig

    def blocks(self) -> dict[str, WeightBlock]:
        """The weight blocks by name, in file order."""
        return {name: getattr(self, name) for name in WEIGHT_BLOCKS}


# The 12 blocks, derived from the fields so blocks(), payload checks and WEIGHT_USES agree.
WEIGHT_BLOCKS: Final[tuple[str, ...]] = tuple(
    name
    for name, field in WeightsConfig.model_fields.items()
    if isinstance(field.annotation, type) and issubclass(field.annotation, WeightBlock)
)
