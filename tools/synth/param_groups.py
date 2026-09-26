"""Parameter groups of `SynthParams` with their embedded defaults (U11-02).

Split out of `tools.synth.params` for the module line budget. Values follow design §5.1.3,
§5.1.4 and §5.1.6, delta DD11-05 and the generator units of impl 11 §3.1. Probabilities
in each distribution sum to 1 +/- 1e-9; rates lie in [0, 1].
"""

import math
from typing import Annotated, ClassVar, Final, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from herness.core import time as clock
from herness.core.errors import ConfigError

_SUM_TOLERANCE: Final = 1e-9


def _check_distribution[K](value: dict[K, float]) -> dict[K, float]:
    total = math.fsum(value.values())
    if abs(total - 1.0) > _SUM_TOLERANCE:
        msg = f"probabilities sum to {total:.12g}, expected 1"
        raise ValueError(msg)
    return value


def _check_priorities[V](value: dict[int, V]) -> dict[int, V]:
    if set(value) != {1, 2, 3, 4, 5}:
        msg = "keys must be the priorities 1..5"
        raise ValueError(msg)
    return value


def check_zone(value: str) -> str:
    """Return `value` when it names an IANA time zone, else raise `ValueError`."""
    try:
        clock.zone(value)
    except ConfigError:
        msg = "unknown IANA time zone"
        raise ValueError(msg) from None
    return value


Rate = Annotated[float, Field(ge=0.0, le=1.0)]
Positive = Annotated[float, Field(gt=0.0)]
Dist = Annotated[dict[str, Rate], AfterValidator(_check_distribution)]
IntDist = Annotated[dict[int, Rate], AfterValidator(_check_distribution)]
PriorityDist = Annotated[IntDist, AfterValidator(_check_priorities)]
PerPriority = Annotated[dict[int, Positive], AfterValidator(_check_priorities)]
MonthDay = Annotated[str, Field(pattern=r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")]

# Hour weights in business_timezone (U11-07): 2.2 for 10-15, 0.3 for 0-6 and 20-23, else 1.0.
_HOUR_CURVE: Final = (0.3,) * 7 + (1.0,) * 3 + (2.2,) * 6 + (1.0,) * 4 + (0.3,) * 4
_HOLIDAYS: Final = ("01-01", "01-15", "02-19", "05-27", "07-04")
_HOLIDAYS_H2: Final = ("09-02", "10-14", "11-11", "11-28", "12-25")


class _Group(BaseModel):
    """A parameter group; `_ordered` lists (low, high) field pairs that must not invert."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    _ordered: ClassVar[tuple[tuple[str, str], ...]] = ()

    @model_validator(mode="after")
    def _check_ordered(self) -> Self:
        for low, high in self._ordered:
            if getattr(self, low) > getattr(self, high):
                msg = f"{low} must not exceed {high}"
                raise ValueError(msg)
        return self


class OrgParams(_Group):
    _ordered = (("teams_per_org_min", "teams_per_org_max"),)
    teams_per_org_mean: Positive = 12.5
    teams_per_org_min: int = Field(default=6, ge=1)
    teams_per_org_max: int = Field(default=20, ge=1)
    criticality: IntDist = {1: 0.10, 2: 0.25, 3: 0.40, 4: 0.25}


class IncidentParams(_Group):
    pareto_alpha: Positive = 1.2
    top_service_share: Rate = 0.05
    top_share_target: Rate = 0.40
    criticality1_weight: Positive = 1.5


class PriorityParams(_Group):
    shares: PriorityDist = {1: 0.01, 2: 0.06, 3: 0.38, 4: 0.50, 5: 0.05}
    criticality1_high_multiplier: Positive = 2.0  # on P1/P2; the difference comes from P4


class ArrivalParams(_Group):
    saturday_factor: Rate = 0.35
    sunday_factor: Rate = 0.30
    hour_curve: tuple[Positive, ...] = Field(default=_HOUR_CURVE, min_length=24, max_length=24)
    annual_amplitude: Rate = 0.1
    holidays: tuple[MonthDay, ...] = _HOLIDAYS + _HOLIDAYS_H2
    holiday_factor: Rate = 0.5


class AckParams(_Group):
    share: Rate = 0.85
    median_minutes: PerPriority = {1: 5.0, 2: 15.0, 3: 45.0, 4: 90.0, 5: 180.0}
    sigma: Positive = 0.9


class MttrParams(_Group):
    median_hours: PerPriority = {1: 3.0, 2: 8.0, 3: 30.0, 4: 72.0, 5: 240.0}
    sigma: Positive = 0.9
    team_multiplier_sigma: Positive = 0.2


class ReassignParams(_Group):
    base_mean: float = Field(default=0.6, ge=0.0)
    extra_mean: float = Field(default=0.8, ge=0.0)
    extra_threshold: Positive = 1.3  # team MTTR multiplier above which extra_mean applies
    reopen_rate: Rate = 0.04
    reopen_rate_p4_p5: Rate = 0.06


class SlaParams(_Group):
    limit_hours: PerPriority = {1: 4.0, 2: 12.0, 3: 72.0, 4: 168.0, 5: 720.0}


class ImpactParams(_Group):
    _ordered = (("min_factor", "max_factor"),)
    share: Rate = 0.70  # of P1/P2 incidents
    min_factor: Rate = 0.3
    max_factor: Rate = 1.0


class ChangeParams(_Group):
    types: Dist = {"standard": 0.60, "normal": 0.35, "emergency": 0.05}
    close_codes: Dist = {
        "successful": 0.90,
        "successful_with_issues": 0.05,
        "unsuccessful": 0.03,
        "backed_out": 0.02,
    }
    emergency_failure_multiplier: Positive = 3.0


class ProblemParams(_Group):
    incidents_per_problem: int = Field(default=80, ge=1)
    known_error_rate: Rate = 0.40


class EventParams(_Group):
    severities: Dist = {
        "critical": 0.05,
        "major": 0.15,
        "minor": 0.30,
        "warning": 0.35,
        "info": 0.15,
    }
    near_incident_share: Rate = 0.50  # DD11-05
    near_incident_ref_share: Rate = 0.90  # DD11-05


class MetricDailyParams(_Group):
    _ordered = (("availability_min", "availability_max"),)
    availability_min: float = Field(default=99.5, ge=0.0, le=100.0)
    availability_max: float = Field(default=99.99, ge=0.0, le=100.0)


class JiraParams(_Group):
    # stories/bugs/tasks share 0.80, split 0.60/0.25/0.15 (U11-08)
    issue_types: Dist = {
        "initiative": 0.01,
        "epic": 0.05,
        "feature": 0.14,
        "story": 0.48,
        "bug": 0.20,
        "task": 0.12,
    }
    story_points: IntDist = {1: 0.15, 2: 0.25, 3: 0.25, 5: 0.20, 8: 0.10, 13: 0.05}
    cycle_time_median_days: Positive = 6.0
    cycle_time_sigma: Positive = 0.8
    reenter_in_progress_rate: Rate = 0.12
    carry_over_rate: Rate = 0.15
    ticket_mention_rate: Rate = 0.03


class TextParams(_Group):
    typo_rate: Rate = 0.01
    casing_noise_rate: Rate = 0.05
    spanish_share: Rate = 0.05
    slot_variation: Rate = 0.20


class PiiParams(_Group):
    _ordered = (("spans_min", "spans_max"),)
    incident_share: Rate = 0.03
    jira_share: Rate = 0.01
    spans_min: int = Field(default=1, ge=1)
    spans_max: int = Field(default=3, ge=1)
    person_names: int = Field(default=500, ge=1)


class DirtyRates(_Group):
    bad_timestamp: Rate = 0.002
    future_ts: Rate = 0.0002
    resolved_before_opened: Rate = 0.0005
    missing_service: Rate = 0.08
    missing_component: Rate = 0.25
    duplicate_rows: Rate = 0.01
    later_versions: Rate = 0.05
    tombstones: Rate = 0.003
    unknown_enum: Rate = 0.001
    heavy_multiplier: Positive = 5.0  # heavy = multiplier x default, capped at 1.0 (U11-16)
