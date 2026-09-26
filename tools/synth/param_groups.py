"""Parameter groups of `SynthParams` with their embedded defaults (U11-02).

Split out of `tools.synth.params` for the module line budget. Values follow design §5.1.3,
§5.1.4 and §5.1.6, delta DD11-05 and the generator units of impl 11 §3.1. Probabilities
in each distribution sum to 1 +/- 1e-9; rates lie in [0, 1].
"""

import math
from typing import Annotated, Any, ClassVar, Final, Self

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from herness.core import time as clock
from herness.core.errors import ConfigError

_SUM_TOLERANCE: Final = 1e-9


def _check_distribution[K](value: dict[K, float]) -> dict[K, float]:
    total = math.fsum(value.values())
    if abs(total - 1.0) > _SUM_TOLERANCE:
        msg = f"probabilities sum to {total:.12g}, expected 1"
        raise ValueError(msg)
    return value


def _keys(*allowed: object) -> AfterValidator:
    """Validator pinning a map's key set to exactly `allowed`."""
    names = ", ".join(str(k) for k in allowed)

    def check[M: dict[Any, Any]](value: M) -> M:
        if set(value) != set(allowed):
            msg = f"keys must be exactly {names}"
            raise ValueError(msg)
        return value

    return AfterValidator(check)


def check_zone(value: str) -> str:
    """Return `value` when it names an IANA time zone, else raise `ValueError`."""
    try:
        clock.zone(value)
    except ConfigError:
        msg = "unknown IANA time zone"
        raise ValueError(msg) from None
    return value


def _reject_bool(value: object) -> object:
    if isinstance(value, bool):
        msg = "must be a number, not a boolean"
        raise ValueError(msg)  # noqa: TRY004 - pydantic needs ValueError to report the path
    return value


_NOT_BOOL: Final = BeforeValidator(_reject_bool)
# Finite numbers (no inf or nan); booleans are rejected. Numeric strings stay accepted
# because YAML 1.1 reads JSON exponents such as `1e-09` as strings.
Rate = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False), _NOT_BOOL]
Positive = Annotated[float, Field(gt=0.0, allow_inf_nan=False), _NOT_BOOL]
NonNegative = Annotated[float, Field(ge=0.0, allow_inf_nan=False), _NOT_BOOL]
Percent = Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False), _NOT_BOOL]
Count = Annotated[int, Field(ge=1, strict=True)]
Dist = Annotated[dict[str, Rate], AfterValidator(_check_distribution)]
IntDist = Annotated[dict[int, Rate], AfterValidator(_check_distribution)]
_PRIORITIES: Final = _keys(1, 2, 3, 4, 5)
PriorityDist = Annotated[IntDist, _PRIORITIES]
PerPriority = Annotated[dict[int, Positive], _PRIORITIES]
_CLOSE_CODES: Final = _keys("successful", "successful_with_issues", "unsuccessful", "backed_out")
_SEVERITIES: Final = _keys("critical", "major", "minor", "warning", "info")
_ISSUE_TYPES: Final = _keys("initiative", "epic", "feature", "story", "bug", "task")
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
    teams_per_org_min: Count = 6
    teams_per_org_max: Count = 20
    criticality: Annotated[IntDist, _keys(1, 2, 3, 4)] = {1: 0.10, 2: 0.25, 3: 0.40, 4: 0.25}


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
    base_mean: NonNegative = 0.6
    extra_mean: NonNegative = 0.8
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
    types: Annotated[Dist, _keys("standard", "normal", "emergency")] = {
        "standard": 0.60,
        "normal": 0.35,
        "emergency": 0.05,
    }
    close_codes: Annotated[Dist, _CLOSE_CODES] = {
        "successful": 0.90,
        "successful_with_issues": 0.05,
        "unsuccessful": 0.03,
        "backed_out": 0.02,
    }
    emergency_failure_multiplier: Positive = 3.0


class ProblemParams(_Group):
    incidents_per_problem: Count = 80
    known_error_rate: Rate = 0.40


class EventParams(_Group):
    severities: Annotated[Dist, _SEVERITIES] = {
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
    availability_min: Percent = 99.5
    availability_max: Percent = 99.99


class JiraParams(_Group):
    # stories/bugs/tasks share 0.80, split 0.60/0.25/0.15 (U11-08)
    issue_types: Annotated[Dist, _ISSUE_TYPES] = {
        "initiative": 0.01,
        "epic": 0.05,
        "feature": 0.14,
        "story": 0.48,
        "bug": 0.20,
        "task": 0.12,
    }
    story_points: Annotated[IntDist, _keys(1, 2, 3, 5, 8, 13)] = {
        1: 0.15,
        2: 0.25,
        3: 0.25,
        5: 0.20,
        8: 0.10,
        13: 0.05,
    }
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
    spans_min: Count = 1
    spans_max: Count = 3
    person_names: Count = 500


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
