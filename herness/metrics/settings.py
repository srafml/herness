"""Section models for config/metrics.yaml and config/weights.yaml, and the weight gate.

Imports only the standard library, pydantic, herness.core.types and herness.core.errors
(R-03), so the spec 10 config loader can import it without DuckDB, Jinja or sqlglot.
"""

import dataclasses
import datetime
import re
import types
import zoneinfo
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Annotated, Any, Final, Literal, Self, get_args

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from herness.core.errors import ConfigError

# fmt: off
Period = Literal["week", "month", "quarter", "t12w", "t12m"]
EntityType = Literal["service", "team", "org", "work_item", "cluster"]
Unit = Literal["count", "usd", "pct", "ratio", "hours", "minutes", "seconds", "days", "score",
               "rank", "other"]
Better = Literal["higher", "lower"]
Aggregation = Literal["count", "sum", "mean", "median", "ratio", "snapshot"]
Domain = Literal["ops", "change", "delivery", "monitoring", "cost"]
UsdModel = Literal["mttr", "repeat", "reopen", "reassign", "sla", "cfr", "noise"]
FilterKey = Literal["priority", "service_id", "team_id", "org_id", "cluster_id", "work_item_type",
                    "severity", "change_type"]
Source = Literal["incident", "change", "event", "work_item", "metric_daily"]
# fmt: on

_CONFIG_HASH = r"^cfg_[0-9a-f]{16}$"
_DECIMAL_TEXT = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_WEIGHT_VALUE = r"^(true|false|-?[0-9]+(\.[0-9]+)?)$"
_PRIORITIES: Final = frozenset(range(1, 6))
_MAX_CENT_EXPONENT: Final = -2


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


def _rule(test: Callable[[Any], bool], msg: str) -> AfterValidator:
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


_UNIQUE = _rule(lambda v: len(set(v)) == len(v), "items must be unique")
Money = Annotated[Decimal, BeforeValidator(_to_decimal)]
Fraction = Annotated[float, Field(ge=0, le=1)]
Positive = Annotated[float, Field(gt=0)]
_PriorityMap = Annotated[
    dict[int, Annotated[float, Field(ge=0, le=10)]],
    _rule(lambda v: set(v) == _PRIORITIES, "keys must be exactly 1..5"),
]

_WindowKey = Literal["week", "month", "quarter"]
_Windows = Annotated[
    dict[_WindowKey, Annotated[int, Field(ge=1, le=520)]],
    _rule(lambda v: set(v) == set(get_args(_WindowKey)), "windows needs week, month and quarter"),
]

_TierWeight = Annotated[float, Field(gt=0, le=1)]

_Templates = Annotated[
    dict[UsdModel, Annotated[str, Field(min_length=1, max_length=500)]],
    _rule(lambda v: set(v) == set(get_args(UsdModel)), "templates needs every usd_model"),
]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class MetricDef(_Model):
    """One catalog entry (design 04 §3.1, §4.1)."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    description: str = Field(min_length=1, max_length=300)
    domain: Domain
    grains: Annotated[list[EntityType], Field(min_length=1, max_length=5), _UNIQUE]
    unit: Unit
    better: Better
    aggregation: Aggregation
    min_sample_size: int = Field(ge=1, le=100_000)
    owner: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    estimate: bool
    uses_weights: list[str]
    usd_model: UsdModel | None
    filters: Annotated[list[FilterKey], _UNIQUE]
    enabled: bool
    requires_columns: list[Annotated[str, Field(pattern=r"^core\.[a-z_]+\.[a-z_]+$")]]
    sql: str = Field(min_length=1, max_length=20_000)


class MetricsDefaults(_Model):
    """`metrics.yaml: defaults` (design 04 §4.1)."""

    min_sample_size: int = Field(default=10, ge=1, le=100_000)
    windows: _Windows = {"week": 26, "month": 24, "quarter": 8}
    exclude_incident_states: list[str] = ["canceled"]
    exclude_close_codes: list[str] = ["Duplicate", "Cancelled", "Not an incident"]
    max_resolve_days: int = Field(default=365, ge=1, le=3650)
    cluster_min_membership: Fraction = 0.5
    change_link_min_score: Fraction = 0.7
    repeat_window_days: int = Field(default=30, ge=1, le=365)
    noise_severities: Annotated[
        list[Literal["critical", "major", "minor", "warning", "info"]], Field(min_length=1)
    ] = ["critical", "major", "minor", "warning"]
    failure_outcomes: Annotated[
        list[Literal["unsuccessful", "backed_out", "successful_with_issues"]], Field(min_length=1)
    ] = ["unsuccessful", "backed_out"]
    compute_timeout_s: float = Field(default=30.0, ge=1, le=300)


class TierWeights(_Model):
    cluster_weight: _TierWeight = 0.8
    root_cause_weight: _TierWeight = 0.6
    service_weight: _TierWeight = 0.3


class FundingScoring(_Model):
    tier_weights: TierWeights = Field(default_factory=TierWeights)
    min_direct_links: int = Field(default=1, ge=1, le=100)
    window_days: int = Field(default=365, ge=30, le=1095)


class OrgScoring(_Model):
    min_peer_group: int = Field(default=5, ge=2, le=100)
    trend_weight: float = Field(default=0.5, ge=0, le=5)
    min_weight_coverage: Fraction = 0.5
    metrics: dict[str, Positive] = Field(min_length=1, max_length=40)


class PeerGroupScoring(_Model):
    min_service_peers: int = Field(default=3, ge=1, le=100)


class LeverScoring(_Model):
    top_entities: int = Field(default=50, ge=1, le=10_000)
    templates: _Templates


class ScoringConfig(_Model):
    """`metrics.yaml: scoring` (design 04 §7.1); catalog cross-checks run in U04-26."""

    as_of: datetime.date | None = None
    funding: FundingScoring = Field(default_factory=FundingScoring)
    org: OrgScoring
    peer_group: PeerGroupScoring = Field(default_factory=PeerGroupScoring)
    levers: LeverScoring


class MetricsCatalogConfig(_Model):
    """Root model of config/metrics.yaml (spec 10 root field `metrics`)."""

    version: Literal[1]
    defaults: MetricsDefaults
    metrics: list[MetricDef] = Field(min_length=1, max_length=200)
    scoring: ScoringConfig

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [metric.name for metric in self.metrics]
        for name in sorted({n for n in names if names.count(n) > 1}):
            msg = f"duplicate metric {name}"
            raise ValueError(msg)
        return self


class WeightBlock(_Model):
    """A weights.yaml block; `unconfirmed` is gated by `check_weight_confirmations`."""

    unconfirmed: bool = True


class CostPerDowntimeHour(WeightBlock):
    by_criticality: Annotated[
        dict[int, Annotated[Money, Field(ge=0)]],
        _rule(lambda v: set(v) <= {1, 2, 3, 4}, "criticality keys must be within 1..4"),
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
    outage_fraction: dict[int, Fraction]
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
        tuple[Positive, Positive],
        BeforeValidator(lambda v: tuple(v) if isinstance(v, list) else v),
        _rule(lambda v: v[0] <= v[1], "clip needs min <= max"),
    ]
    portfolio: dict[str, Positive]
    org: dict[str, Positive]


class ExpectedReduction(WeightBlock):
    epic: Fraction
    feature: Fraction
    initiative: Fraction
    cluster_fix: Fraction
    overrides: dict[str, Fraction]


class ClusterFix(WeightBlock):
    effort_hours: Positive
    min_incidents_12m: int = Field(ge=1)
    min_annual_pain_usd: Annotated[Money, Field(ge=0)]
    max_linked_share: Fraction


class TeamCapacity(WeightBlock):
    default: Positive
    teams: dict[str, Positive]


class ScenarioConfig(_Model):
    name: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    budget_usd: Annotated[Money, Field(gt=0, le=Decimal("1e12"))]


class SolverConfig(_Model):
    num_workers: Literal[1]
    random_seed: int = Field(ge=0)
    max_deterministic_time: float = Field(ge=0.1, le=600)
    max_time_in_seconds: float = Field(ge=1, le=600)


def _scenario_names_ok(scenarios: list[ScenarioConfig]) -> bool:
    names = [scenario.name for scenario in scenarios]
    return len(set(names)) == len(names) and "unconstrained" not in names


class PortfolioConfig(_Model):
    horizon_quarters: int = Field(ge=1, le=8)
    scenarios: Annotated[
        list[ScenarioConfig],
        Field(max_length=20),
        _rule(_scenario_names_ok, "scenario names must be unique and not unconstrained"),
    ]
    mandatory: list[str]
    excluded: list[str]
    enforce_team_capacity: bool
    solver: SolverConfig

    @model_validator(mode="after")
    def _disjoint(self) -> Self:
        if set(self.mandatory) & set(self.excluded):
            msg = "mandatory and excluded overlap"
            raise ValueError(msg)
        return self


class WeightsConfig(_Model):
    """Root model of config/weights.yaml (design 04 §7.2; spec 10 root field `weights`)."""

    version: Literal[1]
    business_timezone: Annotated[
        str, Field(min_length=1, max_length=64), _rule(_is_zone, "unknown IANA time zone")
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


WEIGHT_BLOCKS: Final[tuple[str, ...]] = tuple(
    name
    for name, field in WeightsConfig.model_fields.items()
    if isinstance(field.annotation, type) and issubclass(field.annotation, WeightBlock)
)


def _union(*parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(name for part in parts for name in part))


_ENGINEER: Final = ("cost_per_engineer_hour",)
_INCIDENT: Final = ("cost_per_downtime_hour", "priority_impact_multiplier", "impact_fallback")
_INCIDENT_COST: Final = (*_INCIDENT, "toil", *_ENGINEER)
_TOIL: Final = ("toil", *_ENGINEER)
_BACKOUT: Final = ("change", *_ENGINEER)
_FUNDING_OWN: Final = ("hours_per_story_point", "strategic_weights", "expected_reduction")
_FUNDING: Final = _union(_INCIDENT_COST, _TOIL, _BACKOUT, _FUNDING_OWN, ("cluster_fix",))
# fmt: off
WEIGHT_USES: Final[Mapping[str, tuple[str, ...]]] = types.MappingProxyType({
    "incident_cost": _INCIDENT_COST, "toil": _TOIL, "impact": ("impact_fallback",),
    "noise_cost": _TOIL, "backout_cost": _BACKOUT, "funding": _FUNDING,
    "lever_mttr": _INCIDENT_COST, "lever_repeat": _INCIDENT_COST, "lever_reopen": _INCIDENT_COST,
    "lever_reassign": _TOIL, "lever_sla": ("sla_penalty_usd",),
    "lever_cfr": _union(_INCIDENT_COST, _BACKOUT), "lever_noise": _TOIL,
    "portfolio": _union(_FUNDING, ("team_capacity_points_per_quarter",)),
})
# fmt: on


def unconfirmed_blocks(weights: WeightsConfig, blocks: Iterable[str]) -> list[str]:
    """Return the named blocks whose `unconfirmed` flag is set, sorted and unique."""
    by_name = weights.blocks()
    out: set[str] = set()
    for name in blocks:
        if name not in by_name:
            msg = f"unknown weight block {name}"
            raise ConfigError(msg)
        if by_name[name].unconfirmed:
            out.add(name)
    return sorted(out)


class WeightChange(_Model):
    """One changed weight value; values are numbers or booleans only, never text."""

    path: str = Field(pattern=r"^weights\.[a-z_]+(\.[A-Za-z0-9_:\-]+){0,3}$")
    old: str | None = Field(max_length=32, pattern=_WEIGHT_VALUE)
    new: str | None = Field(max_length=32, pattern=_WEIGHT_VALUE)


class WeightChangePayload(_Model):
    """Schema of `review_item.payload` for kind `weight_change`; no free-text field."""

    blocks: Annotated[
        list[str],
        Field(min_length=1, max_length=20),
        _rule(lambda v: set(v) <= set(WEIGHT_BLOCKS), "unknown weight block"),
    ]
    proposed_config_hash: str | None = Field(default=None, pattern=_CONFIG_HASH)
    changes: list[WeightChange] = Field(default_factory=list, max_length=100)
    origin: Literal["operator", "memory"]
    memory_id: str | None = Field(default=None, pattern=r"^mem_[0-9A-HJKMNP-TV-Z]{26}$")

    @model_validator(mode="after")
    def _memory_origin(self) -> Self:
        if (self.origin == "memory") != (self.memory_id is not None):
            msg = "memory_id is required exactly when origin is memory"
            raise ValueError(msg)
        return self


@dataclasses.dataclass(frozen=True, slots=True)
class WeightIssue:
    """Issue returned by the weight gate; the owner validator maps it onto ConfigIssue."""

    severity: Literal["error", "warn"]
    path: str
    message: str

    def __post_init__(self) -> None:
        if (
            self.severity not in ("error", "warn")
            or not 1 <= len(self.path) <= 200  # noqa: PLR2004 - U04-82 bound
            or not 1 <= len(self.message) <= 300  # noqa: PLR2004 - U04-82 bound
        ):
            msg = "bad weight issue"
            raise ConfigError(msg)


def check_weight_confirmations(
    previous: WeightsConfig | None,
    current: WeightsConfig,
    new_config_hash: str,
    approved: Sequence[WeightChangePayload],
    /,
) -> list[WeightIssue]:
    """Refuse blocks newly confirmed without an approved weight_change for this config hash."""
    if not re.fullmatch(_CONFIG_HASH, new_config_hash):
        msg = "new_config_hash is not a config hash"
        raise ConfigError(msg)
    allowed = {b for p in approved if p.proposed_config_hash == new_config_hash for b in p.blocks}
    before = None if previous is None else previous.blocks()
    message = "confirming {} needs an approved weight_change review item for " + new_config_hash
    return [
        WeightIssue("error", f"weights.{name}.unconfirmed", message.format(name))
        for name, block in current.blocks().items()
        if not block.unconfirmed
        and (before is None or before[name].unconfirmed)
        and name not in allowed
    ]
