"""Tests for herness.model.settings (U02-71 … U02-75)."""

from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from herness.model import settings as s

pytestmark = pytest.mark.unit

GOOD_MAPPINGS = """
enums:
  servicenow.incident_state:
    "1": open
    new: open
    in progress: in_progress
    cancelled: canceled
  jira.status_category_key:
    new: todo
    indeterminate: in_progress
    done: done
service_overrides:
  - service_id: servicenow:cmdb_ci_service:abc123
    team_id: servicenow:sys_user_group:g1
    aliases: [Payments, pay-api]
  - service_id: servicenow:cmdb_ci_service:def456
    jira_project: PAY
    jira_component: Checkout
    role: delivery
  - service_id: servicenow:cmdb_ci_service:abc123
    aliases: [payments]
custom_fields:
  servicenow:
    customer_impact_minutes: u_customer_impact_minutes
  jira:
    story_points: customfield_10016
"""


def _load(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


def _mappings(**changes: Any) -> dict[str, Any]:
    data = _load(GOOD_MAPPINGS)
    data.update(changes)
    return data


def test_ut02_53_good_mappings_sample_validates() -> None:
    """UT02-53 the YAML sample validates into frozen models with defaults filled."""
    cfg = s.MappingsConfig.model_validate(_load(GOOD_MAPPINGS))
    assert cfg.enums["servicenow.incident_state"]["1"] == "open"
    assert len(cfg.service_overrides) == 3
    assert cfg.service_overrides[0].role == "owner"
    assert cfg.service_overrides[1].role == "delivery"
    assert cfg.custom_fields.servicenow.customer_impact_minutes == "u_customer_impact_minutes"
    assert cfg.custom_fields.servicenow.acknowledged_at is None
    assert cfg.custom_fields.jira.story_points == "customfield_10016"
    with pytest.raises(ValidationError):
        cfg.enums = {}  # type: ignore[misc]
    empty = s.MappingsConfig()
    assert empty.enums == {}
    assert empty.service_overrides == []
    assert empty.custom_fields == s.CustomFieldsConfig()


def test_ut02_53_enum_domains_constant() -> None:
    """UT02-53 ENUM_DOMAINS holds the seven domains and their canonical sets."""
    assert set(s.ENUM_DOMAINS) == {
        "servicenow.incident_state",
        "servicenow.change_type",
        "servicenow.change_close_code",
        "monitoring.severity",
        "jira.issue_type",
        "jira.status_category",
        "jira.status_category_key",
    }
    assert s.ENUM_DOMAINS["servicenow.incident_state"] == frozenset(
        {"open", "in_progress", "on_hold", "resolved", "closed", "canceled"}
    )
    assert s.ENUM_DOMAINS["jira.issue_type"] == frozenset(
        {"initiative", "epic", "feature", "story", "bug", "task", "subtask"}
    )
    with pytest.raises(TypeError):
        s.ENUM_DOMAINS["x"] = frozenset()  # type: ignore[index]


def test_ut02_53_bad_domain_rejected() -> None:
    """UT02-53 an enum domain outside ENUM_DOMAINS is rejected, naming the domain."""
    with pytest.raises(ValidationError, match=r"servicenow\.nope"):
        s.MappingsConfig.model_validate(_mappings(enums={"servicenow.nope": {"a": "open"}}))


def test_ut02_53_bad_canonical_rejected() -> None:
    """UT02-53 a canonical value outside the domain set is rejected."""
    enums = {"monitoring.severity": {"sev1": "catastrophic"}}
    with pytest.raises(ValidationError, match=r"monitoring\.severity") as info:
        s.MappingsConfig.model_validate(_mappings(enums=enums))
    assert "sev1" not in str(info.value).split("[type=")[0]


@pytest.mark.parametrize("source", ["", "x" * 201])
def test_ut02_53_bad_source_value_length_rejected(source: str) -> None:
    """UT02-53 source values must be 1-200 characters."""
    with pytest.raises(ValidationError):
        s.MappingsConfig.model_validate(_mappings(enums={"jira.issue_type": {source: "bug"}}))


def test_ut02_53_case_conflicting_source_values_rejected() -> None:
    """UT02-53 source values equal after lower() must map to the same canonical value."""
    ok = {"jira.issue_type": {"Bug": "bug", "bug": "bug"}}
    assert s.MappingsConfig.model_validate(_mappings(enums=ok)).enums == ok
    bad = {"jira.issue_type": {"Bug": "bug", "bug": "task"}}
    with pytest.raises(ValidationError, match=r"jira\.issue_type"):
        s.MappingsConfig.model_validate(_mappings(enums=bad))


def test_ut02_53_conflicting_alias_rejected() -> None:
    """UT02-53 one alias (case-insensitive) on two different service_ids is rejected."""
    overrides = [
        {"service_id": "servicenow:cmdb_ci_service:a", "aliases": ["Payments"]},
        {"service_id": "servicenow:cmdb_ci_service:b", "aliases": ["PAYMENTS"]},
    ]
    with pytest.raises(ValidationError, match=r"service_overrides\[1\]\.aliases\[0\]") as info:
        s.MappingsConfig.model_validate(_mappings(service_overrides=overrides))
    assert "PAYMENTS" not in str(info.value).split("[type=")[0]


def test_ut02_53_bad_custom_field_name_rejected() -> None:
    """UT02-53 custom field names must full-match the snake_case column pattern (TH02-10)."""
    for bad in ('x" ; DROP TABLE core.incident; --', "CustomField", "1abc", "a" * 129, ""):
        with pytest.raises(ValidationError):
            s.MappingsConfig.model_validate(_mappings(custom_fields={"jira": {"team": bad}}))
    with pytest.raises(ValidationError):
        s.CustomFieldsConfig.model_validate({"servicenow": {"unknown": "u_x"}})
    ok = s.CustomFieldsConfig.model_validate({"jira": {"epic_link": "a" * 128}})
    assert ok.jira.epic_link == "a" * 128


def test_ut02_53_delivery_without_project_rejected() -> None:
    """UT02-53 role delivery requires jira_project."""
    entry = {"service_id": "servicenow:cmdb_ci_service:a", "team_id": "x:y:z", "role": "delivery"}
    with pytest.raises(ValidationError, match="delivery"):
        s.ServiceOverride.model_validate(entry)


def test_ut02_53_override_invariants() -> None:
    """UT02-53 ServiceOverride needs a target; jira_component needs jira_project."""
    sid = "servicenow:cmdb_ci_service:a"
    with pytest.raises(ValidationError, match="at least one"):
        s.ServiceOverride.model_validate({"service_id": sid})
    with pytest.raises(ValidationError, match="jira_component"):
        s.ServiceOverride.model_validate(
            {"service_id": sid, "team_id": "a:b:c", "jira_component": "C"}
        )
    ok = s.ServiceOverride.model_validate({"service_id": sid, "org_id": "servicenow:dept:9"})
    assert ok.org_id == "servicenow:dept:9"
    assert ok.aliases == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("service_id", "Servicenow:x:y"),
        ("service_id", "servicenow:x:has space"),
        ("service_id", "servicenow:x:" + "k" * 201),
        ("team_id", "no-colons"),
        ("org_id", "a:b:"),
        ("jira_project", "pay"),
        ("jira_project", "P" * 33),
        ("jira_component", ""),
        ("jira_component", "bad\x07bell"),
        ("jira_component", "c" * 256),
        ("role", "admin"),
        ("aliases", ["x"] * 21),
        ("aliases", [""]),
        ("aliases", ["tab\there"]),
        ("aliases", ["a" * 201]),
        ("extra_key", "x"),
    ],
)
def test_ut02_53_override_field_rules(field: str, value: object) -> None:
    """UT02-53 each ServiceOverride field pattern and bound is enforced."""
    entry: dict[str, object] = {
        "service_id": "servicenow:cmdb_ci_service:a",
        "jira_project": "PAY",
    }
    entry[field] = value
    with pytest.raises(ValidationError):
        s.ServiceOverride.model_validate(entry)


def test_ut02_53_override_count_bound() -> None:
    """UT02-53 service_overrides holds at most 10,000 entries."""
    entry = {"service_id": "servicenow:cmdb_ci_service:a", "jira_project": "PAY"}
    assert (
        len(
            s.MappingsConfig.model_validate(
                {"service_overrides": [entry] * 10_000}
            ).service_overrides
        )
        == 10_000
    )
    with pytest.raises(ValidationError):
        s.MappingsConfig.model_validate({"service_overrides": [entry] * 10_001})


def test_ut02_54_dq_defaults_and_warn_above_error() -> None:
    """UT02-54 DqSettings defaults; warn > error rejected; values bounded."""
    dq = s.DqSettings()
    assert dq.row_count_drop_max == 0.05
    assert dq.incident_service_null_warn == 0.30
    assert dq.incident_service_null_error == 0.60
    assert dq.work_item_service_null_warn == 0.40
    assert dq.cast_fail_warn == 0.005
    assert dq.future_timestamp_max == 0
    assert dq.resolved_before_opened_warn == 0.001
    assert dq.duplicate_key_max == 0
    assert dq.decision_coverage_min == 0.95
    assert dq.metric_daily_unmapped_warn == 0.05
    sample = _load("incident_service_null_warn: 0.7\nincident_service_null_error: 0.6\n")
    with pytest.raises(ValidationError, match="incident_service_null_warn"):
        s.DqSettings.model_validate(sample)
    equal = s.DqSettings.model_validate(_load("incident_service_null_warn: 0.6\n"))
    assert equal.incident_service_null_warn == 0.6
    for bad in (
        {"cast_fail_warn": 1.5},
        {"row_count_drop_max": -0.1},
        {"future_timestamp_max": -1},
        {"duplicate_key_max": 0.5},
        {"decision_coverage_min": float("nan")},
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            s.DqSettings.model_validate(bad)
    assert (
        s.DqSettings.model_validate({"cast_fail_warn": 1, "future_timestamp_max": 5}).cast_fail_warn
        == 1.0
    )


@pytest.mark.parametrize("limit", ["75%", "48GB", "100%", "1%", "99%", "1.5 GiB", "512MB", "8MiB"])
def test_ut02_54_memory_limit_accepted(limit: str) -> None:
    """UT02-54 memory_limit values of the pattern are accepted."""
    assert s.BuildSettings.model_validate({"memory_limit": limit}).memory_limit == limit


@pytest.mark.parametrize("limit", ["abc", "0%", "101%", "48 TB", "48gb", "", "75 %", "GB"])
def test_ut02_54_memory_limit_rejected(limit: str) -> None:
    """UT02-54 memory_limit values outside the pattern are rejected."""
    with pytest.raises(ValidationError):
        s.BuildSettings.model_validate({"memory_limit": limit})


def test_ut02_54_build_settings_defaults_and_bounds() -> None:
    """UT02-54 BuildSettings defaults and the keep_last, threads, class list bounds."""
    b = s.BuildSettings.model_validate(_load("keep_last: 5\nthreads: 8\n"))
    assert (b.keep_last, b.threads, b.memory_limit) == (5, 8, "75%")
    d = s.BuildSettings()
    assert d.keep_last == 3
    assert d.threads is None
    assert d.service_ci_classes == [
        "cmdb_ci_service",
        "cmdb_ci_service_business",
        "cmdb_ci_service_technical",
    ]
    for bad in (
        {"keep_last": 0},
        {"keep_last": 21},
        {"threads": 0},
        {"threads": 257},
        {"keep_last": "3"},
        {"threads": True},
        {"service_ci_classes": []},
        {"service_ci_classes": ["x"] * 51},
        {"service_ci_classes": ["Bad"]},
        {"service_ci_classes": ["a" * 81]},
    ):
        with pytest.raises(ValidationError):
            s.BuildSettings.model_validate(bad)
