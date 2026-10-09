"""IT11-03 (U11-27): the generator and the `synth` profile agree (design 11 §5.1.2).

Tiny seed 7 is generated once per module; the `synth` profile is loaded with
`HERNESS_SYNTH_CONFIG=<root>/synth_mappings.yaml`. The file lives in `tests/unit/` as the
unit spec names it and carries the `integration` marker (it generates a lake).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from herness.connectors.jira import JIRA_ISSUE_COLUMNS
from herness.connectors.mapping_check import _fetched, check_mapping
from herness.core.config import HernessConfig, load_config
from tools import synth_data
from tools.synth.shards import CONFIG_DIR

pytestmark = pytest.mark.integration

# The generator-lake `sources` block (design 11 section 5.1.2, U11-27). Read from the synthetic
# lake,
# never fetched: every section stays disabled, so no host joins the socket allowlist. `fields` are
# the
# generated columns plus `incident_state`/`problem_state` (the staging SQL coalesces them with
# `state`;
# the lake has `state` only). It lives here, not in config/profiles/synth.yaml, until impl 01 adapts
# IT01-07's `_synth_config` to drop the profile's `sources`; then it moves back.
_SOURCES_OVERLAY = """\nsources:
  version: 1
  sources:
    servicenow:
      enabled: false
      base_url: http://127.0.0.1:8080
      auth:
        method: oauth_client_credentials
        credentials: "secret:sn_oauth"  # pragma: allowlist secret
      entities:
        change_request:
          fields:
            [sys_id, sys_updated_on, number, type, state, risk, opened_at, start_date,
             end_date, work_start, work_end, business_service, cmdb_ci,
             assignment_group, close_code, short_description, description]
        cmdb_ci:
          fields:
            [sys_id, sys_updated_on, sys_class_name, name, owned_by, support_group,
             cost_center, company]
          classes: [cmdb_ci_server]
        cmdb_ci_service:
          fields:
            [sys_id, sys_updated_on, name, sys_class_name, owned_by, support_group,
             cost_center, company, busines_criticality]
        cmdb_rel_ci:
          fields: [sys_id, sys_updated_on, parent, child, type]
        cmn_department:
          fields: [sys_id, sys_updated_on, name, parent, cost_center]
        incident:
          fields:
            [incident_state, sys_id, sys_updated_on, number, opened_at,
             u_acknowledged_at, resolved_at, closed_at, priority, state,
             business_service, cmdb_ci, assignment_group, reassignment_count,
             reopen_count, short_description, description, close_notes, close_code,
             problem_id, caused_by, made_sla, business_duration,
             u_customer_impact_minutes]
        problem:
          fields:
            [problem_state, sys_id, sys_updated_on, number, opened_at, resolved_at,
             state, business_service, assignment_group, known_error, cause_notes]
        sys_user_group:
          fields: [sys_id, sys_updated_on, name, parent, manager, cost_center, active, type]
        task_sla:
          fields: [sys_id, sys_updated_on, task, sla, stage, has_breached]
    jira:
      enabled: false
      flavor: cloud
      base_url: http://127.0.0.1:8081
      auth: {method: api_token, credentials: "secret:jira_token"}  # pragma: allowlist secret
    files:
      enabled: false
      entities:
        service_costs: {pattern: "*.csv", key_field: [service_name], mode: snapshot}
"""

_CUSTOM_FIELDS = {
    "customfield_10016",
    "customfield_10050",
    "customfield_10060",
    "u_customer_impact_minutes",
    "u_acknowledged_at",
}


@pytest.fixture(scope="module")
def tiny_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("profile") / "root"
    synth_data.generate(7, "tiny", root, workers=2)
    return root


@pytest.fixture(scope="module")
def cfg(tiny_root: Path, tmp_path_factory: pytest.TempPathFactory) -> HernessConfig:
    config_dir = tmp_path_factory.mktemp("config") / "config"
    shutil.copytree(CONFIG_DIR, config_dir)
    profile = config_dir / "profiles" / "synth.yaml"
    text = profile.read_text(encoding="utf-8")
    profile.write_text(f"{text}\n{_SOURCES_OVERLAY}", encoding="utf-8", newline="\n")
    env = {"HERNESS_SYNTH_CONFIG": str(tiny_root / "synth_mappings.yaml")}
    return load_config("synth", config_dir=config_dir, env=env)


def _files(root: Path, source: str, entity: str) -> list[Path]:
    return sorted((root / "data" / "raw" / source / entity).rglob("*.parquet"))


def _columns(root: Path, source: str, entity: str) -> set[str]:
    return {n for f in _files(root, source, entity) for n in pq.read_schema(f).names}


def _values(root: Path, source: str, entity: str, column: str) -> set[str]:
    out: set[str] = set()
    for f in _files(root, source, entity):
        out |= {v for v in pq.read_table(f, columns=[column]).column(0).to_pylist() if v}
    return out


def test_it11_03_synth_profile_agrees_with_generator(tiny_root: Path, cfg: HernessConfig) -> None:
    """IT11-03 custom fields and enums agree; check_mapping is clean; the aux tables exist."""
    fields = cfg.mappings.custom_fields
    sn_cols = _columns(tiny_root, "servicenow", "incident")
    jira_cols = {c for c in _columns(tiny_root, "jira", "issue") if not c.startswith("_")}
    mapped = {
        *fields.servicenow.model_dump().values(),
        *(v for v in fields.jira.model_dump().values() if v),
    }
    assert mapped == _CUSTOM_FIELDS
    assert {fields.servicenow.customer_impact_minutes, fields.servicenow.acknowledged_at} <= sn_cols
    assert jira_cols == set(JIRA_ISSUE_COLUMNS) | {c for c in _CUSTOM_FIELDS if c[0] == "c"}

    states = _values(tiny_root, "servicenow", "incident", "state")
    assert states <= set(cfg.mappings.enums["servicenow.incident_state"]), states
    types = _values(tiny_root, "servicenow", "change_request", "type")
    assert types <= set(cfg.mappings.enums["servicenow.change_type"]), types

    for source in ("servicenow", "jira"):
        assert check_mapping(source, cfg) == [], source
    for entity in ("cmdb_ci_service", "cmn_department", "task_sla"):
        assert _files(tiny_root, "servicenow", entity), entity

    section = cfg.sources.source("servicenow")
    assert set(section.entities) == {
        p.name for p in (tiny_root / "data" / "raw" / "servicenow").iterdir()
    }
    for entity in section.entities:
        generated = {c for c in _columns(tiny_root, "servicenow", entity) if c[0] != "_"}
        assert generated <= _fetched(section, entity), entity
