"""Tests for `SourcesSection`, `SourcesConfig` and `allowed_hosts` (U01-14, U01-15, T01-02)."""

import copy
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from tests.unit.connectors import _settings_data as d

from herness.connectors import settings as s
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

# Design 01 §7 example with the `[...]` lists filled. Additions required by later rulings:
# `version: 1` (U01-14), `hosts` on snowflake and dataverse (R-06, U01-11, U01-12) and a
# GUID in place of the `<guid>` placeholder (U01-01). `backfill.start` is unquoted so YAML
# yields a date: strict mode rejects the quoted string (T01-01 UT01-05; loader is T10-03).
EXAMPLE = """
version: 1
sources:
  servicenow:
    enabled: true
    base_url: https://acme.service-now.com
    auth: {method: oauth_client_credentials, credentials: "secret:servicenow_oauth"}
    page_size: 1000
    overlap_minutes: 60
    max_concurrency: 4
    schedule: "*/30 * * * *"
    reconcile: {schedule: "0 3 * * SUN", max_delete_pct: 2.0}
    backfill: {start: 2023-09-01, slice_days: 30}
    entities:
      incident:
        fields: [number, opened_at, resolved_at, closed_at, priority, state, business_service,
                 cmdb_ci, assignment_group, reassignment_count, reopen_count, short_description,
                 description, close_notes, close_code, problem_id, caused_by, made_sla,
                 business_duration, u_impact_minutes]
        window_hours: 24
        backfill: {slice_days: 7}
      change_request: {fields: [number, opened_at, closed_at, state, type, risk]}
      cmdb_ci: {fields: [name, sys_class_name, operational_status],
                classes: [cmdb_ci_appl, cmdb_ci_service, cmdb_ci_service_auto]}
  jira:
    enabled: true
    flavor: cloud
    base_url: https://acme.atlassian.net
    auth: {method: api_token, credentials: "secret:jira_api_token"}
    page_size: 100
    overlap_minutes: 60
    max_concurrency: 2
    schedule: "15 * * * *"
    jql_scope: "project in (PAY, OPS, PLAT)"
    fetch_remote_links: true
    entities: {issue: {}}
  monitoring:
    enabled: true
    overlap_minutes: 120
    schedule: "0 2 * * *"
    adapters:
      datadog:
        enabled: true
        base_url: https://api.datadoghq.eu
        auth: {method: api_and_app_key, credentials: "secret:datadog_keys"}
        page_size: 1000
        event_query: "source:monitor status:(error OR warning)"
        metric_queries:
          - {name: error_rate, query: "avg:trace.http.request.errors{*} by {service}",
             agg: avg, unit: ratio}
      prometheus:
        enabled: false
        base_url: https://mimir.acme.local/prometheus
        tenant: ops
        auth: {method: bearer, credentials: "secret:mimir_read"}
        metric_queries:
          - {name: request_count, query: "sum by (service)(increase(http_requests_total[1d]))",
             unit: count}
  snowflake:
    enabled: false
    account: acme-xy12345
    hosts: [acme-xy12345.snowflakecomputing.com]
    auth: {method: key_pair, credentials: "secret:snowflake_svc"}
    warehouse: HERNESS_XS
    role: HERNESS_READER
    statement_timeout_s: 900
    max_scan_gb: 50
    entities:
      cost_center: {table: FINANCE.PUBLIC.COST_CENTER, key_field: CC_ID, updated_field: UPDATED_AT,
                    columns: [CC_ID, NAME, ORG_UNIT, UPDATED_AT]}
  dataverse:
    enabled: false
    base_url: https://acme.crm.dynamics.com
    hosts: [login.microsoftonline.com]
    auth: {method: msal_client_credentials, tenant_id: "0f8fad5b-d9cb-469f-a165-70867728950e",
           credentials: "secret:dataverse_app"}
    page_size: 5000
    entities:
      msdyn_project: {entityset: msdyn_projects, key_field: msdyn_projectid,
                      updated_field: modifiedon,
                      select: [msdyn_subject, msdyn_projectmanager, msdyn_scheduledstart,
                               modifiedon]}
  files:
    enabled: true
    inbox: data/inbox
    schedule: "*/10 * * * *"
    entities:
      team_roster: {pattern: "*.xlsx", sheet: Teams, key_field: [team_code], mode: snapshot}
      legacy_incidents: {pattern: "*.csv", key_field: [ticket_id], updated_field: last_modified,
                         mode: delta}
"""


def _config(**sources: dict[str, Any]) -> s.SourcesConfig:
    return s.SourcesConfig.model_validate({"version": 1, "sources": sources})


# UT01-02 ----------------------------------------------------------------------


def test_ut01_02_design_example_validates() -> None:
    """UT01-02 the design 01 §7 example YAML (lists filled) validates."""
    cfg = s.SourcesConfig.model_validate(yaml.safe_load(EXAMPLE))
    names = [name for name, _ in cfg.enabled_sources()]
    assert names == ["servicenow", "jira", "monitoring", "files"]
    sn = cfg.source("servicenow")
    assert sn.backfill_for("incident").slice_days == 7
    assert sn.backfill_for("change_request").slice_days == 30
    assert str(sn.backfill.start) == "2023-09-01"
    assert cfg.source("snowflake").enabled is False
    assert cfg.sources.mongodb is None


def test_ut01_02_version_and_source_lookup() -> None:
    """UT01-02 `version` must be 1; `source()` raises `ConfigError` for a missing section."""
    with pytest.raises(ValidationError, match="version"):
        s.SourcesConfig.model_validate({"sources": {}})
    with pytest.raises(ValidationError, match="version"):
        s.SourcesConfig.model_validate({"version": 2})
    with pytest.raises(ValidationError, match="version must be 1"):
        s.SourcesConfig.model_validate({"version": True})
    cfg = s.SourcesConfig.model_validate({"version": 1})
    assert cfg.sources == s.SourcesSection()
    assert cfg.enabled_sources() == []
    with pytest.raises(ConfigError, match="source mongodb is not configured"):
        cfg.source("mongodb")
    with pytest.raises(ConfigError, match="source splunk is not configured"):
        cfg.source("splunk")
    disabled = _config(files=d.files(enabled=False))
    assert disabled.enabled_sources() == []
    assert isinstance(disabled.source("files"), s.FilesSettings)


def test_ut01_05_read_only_mappings_copy_and_deepcopy() -> None:
    """UT01-05 non-empty entity mappings survive copy, deepcopy and model_copy(deep=True) (N1)."""
    cfg = s.ServiceNowSettings.model_validate(d.servicenow())
    for clone in (copy.deepcopy(cfg), cfg.model_copy(deep=True), copy.copy(cfg)):
        assert clone == cfg
        assert type(clone.entities) is type(cfg.entities)
        with pytest.raises(TypeError, match="read-only"):
            clone.entities.clear()  # type: ignore[attr-defined]
    deep = copy.deepcopy(cfg.entities)
    assert deep == cfg.entities
    assert deep["incident"] is not cfg.entities["incident"]
    rebuilt, args = cfg.entities.__reduce__()[:2]
    assert rebuilt(*args) == cfg.entities


# UT01-13 ----------------------------------------------------------------------


def test_ut01_13_allowed_hosts_exact_set() -> None:
    """UT01-13 SN, adapter and Dataverse `base_url` hosts plus `hosts` entries; nothing derived."""
    cfg = _config(
        servicenow=d.servicenow(base_url="https://ACME.service-now.com:8443/"),
        monitoring=d.monitoring(
            prometheus=d.adapter("prometheus"),
            datadog=d.adapter("datadog", base_url="https://api.datadoghq.eu"),
            splunk=d.adapter("splunk", enabled=False),
        ),
        snowflake=d.snowflake(
            hosts=["acme-xy12345.snowflakecomputing.com", "ocsp.snowflakecomputing.com"]
        ),
        dataverse=d.dataverse(),
        jira=d.jira(enabled=False),
        mongodb=d.mongodb(enabled=False, hosts=["db9.example.com"]),
    )
    assert s.allowed_hosts(cfg) == frozenset({
        "acme.service-now.com",
        "prometheus.example.com",
        "api.datadoghq.eu",
        "acme-xy12345.snowflakecomputing.com",
        "ocsp.snowflakecomputing.com",
        "acme.crm.dynamics.com",
        "login.microsoftonline.com",
    })  # fmt: skip
    assert s.allowed_hosts(s.SourcesConfig.model_validate({"version": 1})) == frozenset()


def test_ut01_13_disabled_monitoring_adds_no_adapter_hosts() -> None:
    """UT01-13 adapters of a disabled monitoring section contribute nothing."""
    cfg = _config(monitoring=d.monitoring() | {"enabled": False})
    assert s.allowed_hosts(cfg) == frozenset()


# UT01-97 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hosts", "match"),
    [
        (["10.0.0.1"], "hosts entry 0 is not a host name"),
        (["db0.example.com", "db1.example.com:27017"], "hosts entry 1 is not a host name"),
        (["db0.example.com", "DB0.example.com"], "hosts entry 1 is a duplicate"),
        ([], "hosts must list every MongoDB host"),
    ],
)
def test_ut01_97_hosts_entries_rejected(hosts: list[str], match: str) -> None:
    """UT01-97 IP literal, port and duplicate fail; an empty list fails for MongoDB."""
    with pytest.raises(ValidationError, match=match):
        s.MongoSettings.model_validate(d.mongodb(hosts=hosts))


def test_ut01_97_hosts_rules_per_source() -> None:
    """UT01-97 upper case is lower-cased; SN accepts `hosts`; SDK sources require theirs."""
    mongo = s.MongoSettings.model_validate(d.mongodb(hosts=["DB0.Example.COM"]))
    assert mongo.hosts == ("db0.example.com",)
    no_hosts = {k: v for k, v in d.mongodb().items() if k != "hosts"}
    with pytest.raises(ValidationError, match="hosts must list every MongoDB host"):
        s.MongoSettings.model_validate(no_hosts)
    with pytest.raises(ValidationError, match="hosts must list the Snowflake account host"):
        s.SnowflakeSettings.model_validate(d.snowflake(hosts=["ocsp.snowflakecomputing.com"]))
    with pytest.raises(ValidationError, match=r"hosts must list login.microsoftonline.com"):
        s.DataverseSettings.model_validate(d.dataverse(hosts=["acme.crm.dynamics.com"]))
    cfg = _config(
        servicenow=d.servicenow(hosts=["sso.example.com"]),
        mongodb=d.mongodb(hosts=["db0.example.com", "db1.example.com"]),
    )
    sn = cfg.source("servicenow")
    assert sn.base_url == "https://acme.service-now.com"
    assert s.allowed_hosts(cfg) == frozenset({
        "acme.service-now.com", "sso.example.com", "db0.example.com", "db1.example.com",
    })  # fmt: skip
