"""Section models for config/metrics.yaml and config/weights.yaml, and the weight gate.

Imports only the standard library, pydantic, herness.core.types, herness.core.errors and its
private sibling herness.metrics._weights_settings (R-03), so the spec 10 config loader can
import it without DuckDB, Jinja or sqlglot. The weights.yaml models live in that sibling to
keep both modules inside their line budgets; they are re-exported here.
"""

import dataclasses
import datetime
import re
import types
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Final, Literal, Self, get_args

from pydantic import Field, model_validator

from herness.core.errors import ConfigError
from herness.metrics._weights_settings import (
    WEIGHT_BLOCKS,
    Fraction,
    Model,
    PortfolioConfig,
    Positive,
    ScenarioConfig,
    SolverConfig,
    WeightsConfig,
    rule,
)

__all__ = [
    "WEIGHT_USES",
    "Aggregation",
    "Better",
    "Domain",
    "EntityType",
    "FilterKey",
    "FundingScoring",
    "LeverScoring",
    "MetricDef",
    "MetricsCatalogConfig",
    "MetricsDefaults",
    "OrgScoring",
    "PeerGroupScoring",
    "Period",
    "PortfolioConfig",
    "ScenarioConfig",
    "ScoringConfig",
    "SolverConfig",
    "Source",
    "TierWeights",
    "Unit",
    "UsdModel",
    "WeightChange",
    "WeightChangePayload",
    "WeightIssue",
    "WeightsConfig",
    "check_weight_confirmations",
    "unconfirmed_blocks",
]

Period = Literal["week", "month", "quarter", "t12w", "t12m"]
EntityType = Literal["service", "team", "org", "work_item", "cluster"]
Unit = Literal[
    "count", "usd", "pct", "ratio", "hours", "minutes", "seconds", "days", "score", "rank", "other"
]
Better = Literal["higher", "lower"]
Aggregation = Literal["count", "sum", "mean", "median", "ratio", "snapshot"]
Domain = Literal["ops", "change", "delivery", "monitoring", "cost"]
UsdModel = Literal["mttr", "repeat", "reopen", "reassign", "sla", "cfr", "noise"]
FilterKey = Literal[
    "priority",
    "service_id",
    "team_id",
    "org_id",
    "cluster_id",
    "work_item_type",
    "severity",
    "change_type",
]
Source = Literal["incident", "change", "event", "work_item", "metric_daily"]

_CONFIG_HASH = r"^cfg_[0-9a-f]{16}$"
_WEIGHT_VALUE = r"^(true|false|-?[0-9]+(\.[0-9]+)?)$"
_UNIQUE = rule(lambda v: len(set(v)) == len(v), "items must be unique")
_WindowKey = Literal["week", "month", "quarter"]
_Windows = Annotated[
    Mapping[_WindowKey, Annotated[int, Field(ge=1, le=520)]],
    rule(lambda v: set(v) == set(get_args(_WindowKey)), "windows needs week, month and quarter"),
]
_DEFAULT_WINDOWS: Final[Mapping[_WindowKey, int]] = types.MappingProxyType(
    {"week": 26, "month": 24, "quarter": 8}
)
_TierWeight = Annotated[float, Field(gt=0, le=1)]
_Templates = Annotated[
    Mapping[UsdModel, Annotated[str, Field(min_length=1, max_length=500)]],
    rule(lambda v: set(v) == set(get_args(UsdModel)), "templates needs every usd_model"),
]


class MetricDef(Model):
    """One catalog entry (design 04 §3.1, §4.1)."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    description: str = Field(min_length=1, max_length=300)
    domain: Domain
    grains: Annotated[tuple[EntityType, ...], Field(min_length=1, max_length=5), _UNIQUE]
    unit: Unit
    better: Better
    aggregation: Aggregation
    min_sample_size: int = Field(ge=1, le=100_000)
    owner: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    estimate: bool
    uses_weights: tuple[str, ...]
    usd_model: UsdModel | None
    filters: Annotated[tuple[FilterKey, ...], _UNIQUE]
    enabled: bool
    requires_columns: tuple[Annotated[str, Field(pattern=r"^core\.[a-z_]+\.[a-z_]+$")], ...]
    sql: str = Field(min_length=1, max_length=20_000)


class MetricsDefaults(Model):
    """`metrics.yaml: defaults` (design 04 §4.1)."""

    min_sample_size: int = Field(default=10, ge=1, le=100_000)
    windows: _Windows = Field(default_factory=lambda: _DEFAULT_WINDOWS)
    exclude_incident_states: tuple[str, ...] = ("canceled",)
    exclude_close_codes: tuple[str, ...] = ("Duplicate", "Cancelled", "Not an incident")
    max_resolve_days: int = Field(default=365, ge=1, le=3650)
    cluster_min_membership: Fraction = 0.5
    change_link_min_score: Fraction = 0.7
    repeat_window_days: int = Field(default=30, ge=1, le=365)
    noise_severities: Annotated[
        tuple[Literal["critical", "major", "minor", "warning", "info"], ...], Field(min_length=1)
    ] = ("critical", "major", "minor", "warning")
    failure_outcomes: Annotated[
        tuple[Literal["unsuccessful", "backed_out", "successful_with_issues"], ...],
        Field(min_length=1),
    ] = ("unsuccessful", "backed_out")
    compute_timeout_s: float = Field(default=30.0, ge=1, le=300)


class TierWeights(Model):
    """`scoring.funding.tier_weights` (design 04 §5.5)."""

    cluster_weight: _TierWeight = 0.8
    root_cause_weight: _TierWeight = 0.6
    service_weight: _TierWeight = 0.3


class FundingScoring(Model):
    """`scoring.funding` (design 04 §7.1)."""

    tier_weights: TierWeights = Field(default_factory=TierWeights)
    min_direct_links: int = Field(default=1, ge=1, le=100)
    window_days: int = Field(default=365, ge=30, le=1095)


class OrgScoring(Model):
    """`scoring.org` (design 04 §5.8, §7.1)."""

    min_peer_group: int = Field(default=5, ge=2, le=100)
    trend_weight: float = Field(default=0.5, ge=0, le=5)
    min_weight_coverage: Fraction = 0.5
    metrics: Mapping[str, Positive] = Field(min_length=1, max_length=40)


class PeerGroupScoring(Model):
    """`scoring.peer_group` (design 04 §5.8.1)."""

    min_service_peers: int = Field(default=3, ge=1, le=100)


class LeverScoring(Model):
    """`scoring.levers` (design 04 §5.9); placeholder checks run in U04-26."""

    top_entities: int = Field(default=50, ge=1, le=10_000)
    templates: _Templates


class ScoringConfig(Model):
    """`metrics.yaml: scoring` (design 04 §7.1); catalog cross-checks run in U04-26."""

    as_of: datetime.date | None = None
    funding: FundingScoring = Field(default_factory=FundingScoring)
    org: OrgScoring
    peer_group: PeerGroupScoring = Field(default_factory=PeerGroupScoring)
    levers: LeverScoring


class MetricsCatalogConfig(Model):
    """Root model of config/metrics.yaml (spec 10 root field `metrics`)."""

    version: Literal[1]
    defaults: MetricsDefaults
    metrics: tuple[MetricDef, ...] = Field(min_length=1, max_length=200)
    scoring: ScoringConfig

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [metric.name for metric in self.metrics]
        for name in sorted({n for n in names if names.count(n) > 1}):
            msg = f"duplicate metric {name}"
            raise ValueError(msg)
        return self


def _union(*parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(name for part in parts for name in part))


_ENGINEER: Final = ("cost_per_engineer_hour",)
_INCIDENT: Final = ("cost_per_downtime_hour", "priority_impact_multiplier", "impact_fallback")
_INCIDENT_COST: Final = (*_INCIDENT, "toil", *_ENGINEER)
_TOIL: Final = ("toil", *_ENGINEER)
_BACKOUT: Final = ("change", *_ENGINEER)
_FUNDING_OWN: Final = ("hours_per_story_point", "strategic_weights", "expected_reduction")
_FUNDING: Final = _union(_INCIDENT_COST, _TOIL, _BACKOUT, _FUNDING_OWN, ("cluster_fix",))

WEIGHT_USES: Final[Mapping[str, tuple[str, ...]]] = types.MappingProxyType(
    {
        "incident_cost": _INCIDENT_COST,
        "toil": _TOIL,
        "impact": ("impact_fallback",),
        "noise_cost": _TOIL,
        "backout_cost": _BACKOUT,
        "funding": _FUNDING,
        "lever_mttr": _INCIDENT_COST,
        "lever_repeat": _INCIDENT_COST,
        "lever_reopen": _INCIDENT_COST,
        "lever_reassign": _TOIL,
        "lever_sla": ("sla_penalty_usd",),
        "lever_cfr": _union(_INCIDENT_COST, _BACKOUT),
        "lever_noise": _TOIL,
        "portfolio": _union(_FUNDING, ("team_capacity_points_per_quarter",)),
    }
)


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


class WeightChange(Model):
    """One changed weight value; values are numbers or booleans only, never text."""

    path: str = Field(pattern=r"^weights\.[a-z_]+(\.[A-Za-z0-9_:\-]+){0,3}$")
    old: str | None = Field(max_length=32, pattern=_WEIGHT_VALUE)
    new: str | None = Field(max_length=32, pattern=_WEIGHT_VALUE)


class WeightChangePayload(Model):
    """Schema of `review_item.payload` for kind `weight_change`; no free-text field."""

    blocks: Annotated[
        tuple[str, ...],
        Field(min_length=1, max_length=20),
        rule(lambda v: set(v) <= set(WEIGHT_BLOCKS), "unknown weight block"),
    ]
    proposed_config_hash: str | None = Field(default=None, pattern=_CONFIG_HASH)
    changes: tuple[WeightChange, ...] = Field(default=(), max_length=100)
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
