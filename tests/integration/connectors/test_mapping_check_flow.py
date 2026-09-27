"""Integration test of `herness sync --check-mapping` (impl 01 IT01-07; flow F01-06; T01-12).

Real pieces: the config loader with the shipped `synth` profile overlay, the build renderer,
the shipped staging SQL `herness/model/sql/110-170` (T02-13, T02-14) and sqlglot. The shipped
synth profile carries no `sources` section yet (T11-16 carry-over), so the test writes a
synth-style `sources.yaml` whose ServiceNow field lists follow design 01 §4.2 and a
`mappings.yaml` naming the synthetic custom field columns.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from tests.support.config_tree import SHIPPED
from tests.support.sync_env import write_sync_config
from tests.unit.connectors._settings_data import (
    dataverse,
    files,
    jira,
    mongo_entity,
    mongodb,
    monitoring,
    servicenow,
    snowflake,
)

from herness.connectors.mapping_check import STAGING_FILES, check_mapping
from herness.core import config as c

pytestmark = pytest.mark.integration

_CI = ["name", "owned_by", "support_group", "cost_center", "company"]
SERVICENOW_FIELDS: dict[str, list[str]] = {
    "incident": [
        "number", "opened_at", "resolved_at", "closed_at", "priority", "state", "incident_state",
        "business_service", "cmdb_ci", "assignment_group", "problem_id", "caused_by",
        "reassignment_count", "reopen_count", "short_description", "description", "close_notes",
        "close_code", "made_sla", "business_duration", "u_customer_impact_minutes",
        "u_acknowledged_at",
    ],
    "change_request": [
        "number", "type", "state", "risk", "opened_at", "start_date", "end_date", "work_start",
        "work_end", "business_service", "cmdb_ci", "assignment_group", "close_code",
        "short_description", "description",
    ],
    "problem": [
        "number", "opened_at", "resolved_at", "state", "problem_state", "business_service",
        "assignment_group", "known_error", "cause_notes",
    ],
    "cmdb_ci": _CI,
    "cmdb_ci_service": [*_CI, "busines_criticality"],
    "cmdb_rel_ci": ["parent", "child", "type"],
    "sys_user_group": ["name", "parent", "manager", "cost_center", "type", "active"],
    "cmn_department": ["name", "parent", "cost_center"],
    "task_sla": ["task", "has_breached"],
}  # fmt: skip
MAPPINGS = {
    "version": 1,
    "custom_fields": {
        "servicenow": {
            "acknowledged_at": "u_acknowledged_at",
            "customer_impact_minutes": "u_customer_impact_minutes",
        }
    },
}


def _servicenow() -> dict[str, object]:
    entities: dict[str, dict[str, object]] = {
        e: {"fields": f} for e, f in SERVICENOW_FIELDS.items()
    }
    entities["cmdb_ci"]["classes"] = ["cmdb_ci_service"]
    return servicenow(entities=entities)


def _synth_config(root: Path) -> c.HernessConfig:
    sources = {
        "version": 1,
        "sources": {
            "servicenow": _servicenow(),
            "jira": jira(),
            "monitoring": monitoring(),
            "mongodb": mongodb(entities={"orders": mongo_entity(fields=["customerId", "total"])}),
            "snowflake": snowflake(),
            "dataverse": dataverse(),
            "files": files(),
        },
    }
    cfg_dir = write_sync_config(root, yaml.safe_dump(sources), yaml.safe_dump(MAPPINGS))
    shutil.copyfile(SHIPPED / "profiles" / "synth.yaml", cfg_dir / "profiles" / "synth.yaml")
    c.reset_config()
    return c.init_config("synth", config_dir=cfg_dir, env={})


def test_it01_07_synth_config_matches_every_staging_file(tmp_path: Path) -> None:
    """IT01-07 synth profile config and the shipped staging SQL: `[]` for every source."""
    cfg = _synth_config(tmp_path)
    assert cfg.profile == "synth"
    for source in STAGING_FILES:
        assert check_mapping(source, cfg) == [], source


def test_it01_07_dropped_field_is_reported(tmp_path: Path) -> None:
    """IT01-07 control: without `close_code` on change_request, the shipped SQL reports it."""
    SERVICENOW_FIELDS["change_request"].remove("close_code")
    try:
        cfg = _synth_config(tmp_path)
    finally:
        SERVICENOW_FIELDS["change_request"].append("close_code")
    issues = check_mapping("servicenow", cfg)
    assert [(i.entity, i.column, i.file) for i in issues] == [
        ("change_request", "close_code", "110_stg_servicenow.sql")
    ]
