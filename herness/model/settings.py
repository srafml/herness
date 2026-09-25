"""Config section models of impl 02 (U02-71 … U02-75).

``MappingsConfig`` is the ``mappings.yaml`` section; ``DqSettings`` and ``BuildSettings``
are the top-level ``sources.yaml`` sections ``dq`` and ``build`` (siblings of impl 01's
connector sections). The root config of impl 10 composes them (R-03). This module imports
only the standard library and pydantic (contract ``model-settings-light``). Values reach SQL
only as Arrow data or through the identifier and number filters (TH02-10). Validation
messages name domains, fields and positions, never the offending value.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

_FIRST_CONTROL: Final = 32
_DEL: Final = 127
_C1_END: Final = 159


def _no_control_chars(value: str) -> str:
    if any(ord(ch) < _FIRST_CONTROL or _DEL <= ord(ch) <= _C1_END for ch in value):
        msg = "must not contain control characters"
        raise ValueError(msg)
    return value


# record-ID shape ``<source>:<entity>:<key>`` (herness.core.ids, restated: no ids import, R-03)
_RecordId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*:[a-z0-9_]+:[^\s]{1,200}$")]
_JiraProject = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,31}$")]
_Component = Annotated[
    str, StringConstraints(min_length=1, max_length=255), AfterValidator(_no_control_chars)
]
_Alias = Annotated[
    str, StringConstraints(min_length=1, max_length=200), AfterValidator(_no_control_chars)
]
_Column = Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]{0,127}$")]
_Fraction = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
_Count = Annotated[int, Field(ge=0)]
_CiClass = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,79}$")]
_MEMORY_LIMIT_RE: Final = r"^([1-9][0-9]?%|100%|[0-9]+(\.[0-9]+)?\s?(GB|MB|GiB|MiB))$"

_MAX_ALIASES: Final = 20
_MAX_OVERRIDES: Final = 10_000
_SOURCE_VALUE_MAX: Final = 200

ENUM_DOMAINS: Final[Mapping[str, frozenset[str]]] = types.MappingProxyType(
    {
        "servicenow.incident_state": frozenset(
            {"open", "in_progress", "on_hold", "resolved", "closed", "canceled"}
        ),
        "servicenow.change_type": frozenset({"standard", "normal", "emergency"}),
        "servicenow.change_close_code": frozenset(
            {"successful", "successful_with_issues", "unsuccessful", "backed_out", "canceled"}
        ),
        "monitoring.severity": frozenset({"critical", "major", "minor", "warning", "info"}),
        "jira.issue_type": frozenset(
            {"initiative", "epic", "feature", "story", "bug", "task", "subtask"}
        ),
        "jira.status_category": frozenset({"todo", "in_progress", "done"}),
        "jira.status_category_key": frozenset({"todo", "in_progress", "done"}),
    }
)

_MODEL_CONFIG: Final = ConfigDict(extra="forbid", strict=True, frozen=True)


class ServiceOverride(BaseModel):
    """One ``mappings.yaml: service_overrides`` entry (first precedence in ``core.service_map``)."""

    model_config = _MODEL_CONFIG

    service_id: _RecordId
    team_id: _RecordId | None = None
    jira_project: _JiraProject | None = None
    jira_component: _Component | None = None
    org_id: _RecordId | None = None
    role: Literal["owner", "support", "delivery"] = "owner"
    aliases: Annotated[list[_Alias], Field(max_length=_MAX_ALIASES)] = []

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        if not (self.team_id or self.jira_project or self.org_id or self.aliases):
            msg = "at least one of team_id, jira_project, org_id, aliases must be set"
            raise ValueError(msg)
        if self.jira_component is not None and self.jira_project is None:
            msg = "jira_component requires jira_project"
            raise ValueError(msg)
        if self.role == "delivery" and self.jira_project is None:
            msg = "role delivery requires jira_project"
            raise ValueError(msg)
        return self


class ServiceNowCustomFields(BaseModel):
    """Raw lake column names of ServiceNow custom fields."""

    model_config = _MODEL_CONFIG

    customer_impact_minutes: _Column | None = None
    acknowledged_at: _Column | None = None


class JiraCustomFields(BaseModel):
    """Raw lake column names of Jira custom fields."""

    model_config = _MODEL_CONFIG

    story_points: _Column | None = None
    team: _Column | None = None
    estimate_cost_usd: _Column | None = None
    epic_link: _Column | None = None


class CustomFieldsConfig(BaseModel):
    """Raw lake column names of source custom fields (design 02 §8)."""

    model_config = _MODEL_CONFIG

    servicenow: ServiceNowCustomFields = ServiceNowCustomFields()
    jira: JiraCustomFields = JiraCustomFields()


def _check_domain(domain: str, values: Mapping[str, str]) -> None:
    allowed = ENUM_DOMAINS.get(domain)
    if allowed is None:
        msg = f"enums: unknown domain {domain}"
        raise ValueError(msg)
    seen: dict[str, str] = {}
    for position, (source, canonical) in enumerate(values.items()):
        if not 1 <= len(source) <= _SOURCE_VALUE_MAX:
            msg = f"enums.{domain}[{position}]: source value must be 1-200 characters"
            raise ValueError(msg)
        if canonical not in allowed:
            msg = f"enums.{domain}[{position}]: canonical value not in the domain"
            raise ValueError(msg)
        if seen.setdefault(source.lower(), canonical) != canonical:
            msg = f"enums.{domain}[{position}]: case-variant source values map differently"
            raise ValueError(msg)


def _check_aliases(overrides: list[ServiceOverride]) -> None:
    owner: dict[str, str] = {}
    for index, override in enumerate(overrides):
        for position, alias in enumerate(override.aliases):
            if owner.setdefault(alias.lower(), override.service_id) != override.service_id:
                msg = (
                    f"service_overrides[{index}].aliases[{position}]: "
                    "alias already maps to another service_id"
                )
                raise ValueError(msg)


class MappingsConfig(BaseModel):
    """The ``mappings.yaml`` section (spec 10 §5.1, owner 02)."""

    model_config = _MODEL_CONFIG

    enums: dict[str, dict[str, str]] = {}
    service_overrides: Annotated[list[ServiceOverride], Field(max_length=_MAX_OVERRIDES)] = []
    custom_fields: CustomFieldsConfig = CustomFieldsConfig()

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        for domain, values in self.enums.items():
            _check_domain(domain, values)
        _check_aliases(self.service_overrides)
        return self


class DqSettings(BaseModel):
    """``sources.yaml: dq`` thresholds (design 02 §4.8)."""

    model_config = _MODEL_CONFIG

    row_count_drop_max: _Fraction = 0.05
    incident_service_null_warn: _Fraction = 0.30
    incident_service_null_error: _Fraction = 0.60
    work_item_service_null_warn: _Fraction = 0.40
    cast_fail_warn: _Fraction = 0.005
    future_timestamp_max: _Count = 0
    resolved_before_opened_warn: _Fraction = 0.001
    duplicate_key_max: _Count = 0
    decision_coverage_min: _Fraction = 0.95
    metric_daily_unmapped_warn: _Fraction = 0.05

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.incident_service_null_warn > self.incident_service_null_error:
            msg = "incident_service_null_warn must be <= incident_service_null_error"
            raise ValueError(msg)
        return self


class BuildSettings(BaseModel):
    """``sources.yaml: build`` (design 02 §8)."""

    model_config = _MODEL_CONFIG

    keep_last: Annotated[int, Field(ge=1, le=20)] = 3
    memory_limit: Annotated[str, StringConstraints(pattern=_MEMORY_LIMIT_RE)] = "75%"
    threads: Annotated[int, Field(ge=1, le=256)] | None = None
    service_ci_classes: Annotated[list[_CiClass], Field(min_length=1, max_length=50)] = [
        "cmdb_ci_service",
        "cmdb_ci_service_business",
        "cmdb_ci_service_technical",
    ]
