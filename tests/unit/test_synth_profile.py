"""IT11-03 (U11-27): the generator and the `synth` profile agree (design 11 §5.1.2).

Tiny seed 7 is generated once per module; the `synth` profile is loaded with
`HERNESS_SYNTH_CONFIG=<root>/synth_mappings.yaml`. The file lives in `tests/unit/` as the
unit spec names it and carries the `integration` marker (it generates a lake).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from herness.connectors.jira import JIRA_ISSUE_COLUMNS
from herness.connectors.mapping_check import _fetched, check_mapping
from herness.core.config import HernessConfig, load_config
from tools import synth_data
from tools.synth.shards import CONFIG_DIR

pytestmark = pytest.mark.integration

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
def cfg(tiny_root: Path) -> HernessConfig:
    env = {"HERNESS_SYNTH_CONFIG": str(tiny_root / "synth_mappings.yaml")}
    return load_config("synth", config_dir=CONFIG_DIR, env=env)


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
