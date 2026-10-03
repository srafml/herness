"""Tests for herness.connectors.mapping_check: `herness sync --check-mapping` over a staging
SQL fixture (impl 01 U01-56; T01-12). The fixture file is rendered by the real build renderer
and walked with sqlglot; the config is a real loaded `HernessConfig`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.support.sync_env import init_sync_config
from tests.unit.connectors._settings_data import files, jira, monitoring, servicenow

from herness.connectors.mapping_check import STAGING_FILES, MappingIssue, check_mapping
from herness.core.config import HernessConfig
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

FILE = "110_stg_servicenow.sql"
_INC = "{{ lake.get('servicenow', 'incident').glob | sqlstr }}"
_PRB = "{{ lake.get('servicenow', 'problem').glob | sqlstr }}"
STAGING = f"""\
SELECT x.close_code, x."number", x._record_id, x.sys_updated_on, x.state_display
FROM read_parquet({_INC}, filename = true) AS x;
SELECT opened_at, filename FROM read_parquet({_INC}, filename = true)
QUALIFY row_number() OVER (PARTITION BY _record_id ORDER BY filename DESC) = 1;
WITH p AS (SELECT * FROM read_parquet({_PRB}))
SELECT p.known_error, state FROM p;
SELECT o.close_code, a."number", priority
FROM read_parquet({_INC}) AS a JOIN stg.other AS o ON o.k = a."number";
SELECT count(*) FROM stg.unrelated;
SELECT j.summary FROM read_parquet('raw/jira/issue/a.parquet') AS j;
SELECT v.close_code FROM read_parquet(['raw/servicenow/incident/a.parquet']) AS v;
"""  # noqa: S608 - Jinja template text of a test fixture; no data enters it
INCIDENT = ["number", "opened_at", "state"]


def _sql_dir(tmp_path: Path, text: str = STAGING) -> Path:
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / FILE).write_text(text, encoding="utf-8")
    return sql_dir


def _cfg(tmp_path: Path, **sections: dict[str, Any]) -> HernessConfig:
    text = yaml.safe_dump({"version": 1, "sources": sections})
    return init_sync_config(tmp_path / "root", text)


def _sn(incident: list[str]) -> dict[str, Any]:
    entities = {"incident": {"fields": incident}, "problem": {"fields": ["known_error", "state"]}}
    return servicenow(entities=entities)


def test_ut01_58_missing_close_code_is_one_issue(tmp_path: Path) -> None:
    """UT01-58 `x.close_code` and unqualified columns; config missing close_code: one issue."""
    cfg = _cfg(tmp_path, servicenow=_sn(INCIDENT))
    assert check_mapping("servicenow", cfg, sql_dir=_sql_dir(tmp_path)) == [
        MappingIssue("incident", "close_code", FILE)
    ]


def test_ut01_58_complete_config_passes(tmp_path: Path) -> None:
    """UT01-58 complete config (close_code fetched) gives `[]`."""
    cfg = _cfg(tmp_path, servicenow=_sn([*INCIDENT, "close_code"]))
    assert check_mapping("servicenow", cfg, sql_dir=_sql_dir(tmp_path)) == []


def test_ut01_58_unqualified_column_counts_only_with_one_source(tmp_path: Path) -> None:
    """UT01-58 an unqualified column of a single raw source counts; issues sort by entity."""
    cfg = _cfg(tmp_path, servicenow=_sn(["number"]))
    issues = check_mapping("servicenow", cfg, sql_dir=_sql_dir(tmp_path))
    assert [(i.entity, i.column) for i in issues] == [
        ("incident", "close_code"),
        ("incident", "opened_at"),
        ("incident", "state_display"),  # `state` is not fetched, so neither is its display
    ]


def test_ut01_58_skipped_sources_return_empty(tmp_path: Path) -> None:
    """UT01-58 files and jira (T01-17) are skipped: `[]`."""
    cfg = _cfg(tmp_path, jira=jira(), monitoring=monitoring(), files=files())
    for source in ("files", "jira"):
        assert check_mapping(source, cfg) == []


def test_ut01_58_monitoring_fetches_every_staged_column(tmp_path: Path) -> None:
    """UT01-58 monitoring (T01-19) is checked against EVENT_COLUMNS / METRIC_COLUMNS: the
    shipped 130_stg_monitoring.sql reads only fetched columns, so `[]`; a column outside
    them is an issue."""
    cfg = _cfg(tmp_path, monitoring=monitoring())
    assert check_mapping("monitoring", cfg) == []
    name = STAGING_FILES["monitoring"]
    glob = "{{ lake.get('monitoring', 'event').glob | sqlstr }}"
    sql_dir = tmp_path / "mon_sql"
    sql_dir.mkdir()
    text = f"SELECT e.ts, e.source_tool, e.priority FROM read_parquet({glob}) AS e;"  # noqa: S608 - fixture
    (sql_dir / name).write_text(text, encoding="utf-8")
    assert check_mapping("monitoring", cfg, sql_dir=sql_dir) == [
        MappingIssue("event", "priority", name)
    ]
    assert STAGING_FILES["jira"] == "120_stg_jira.sql"
    assert len(STAGING_FILES) == 7


def test_ut01_58_unconfigured_source_is_config_error(tmp_path: Path) -> None:
    """UT01-58 a source without a section raises ConfigError."""
    cfg = _cfg(tmp_path, files=files())
    with pytest.raises(ConfigError, match="source servicenow is not configured"):
        check_mapping("servicenow", cfg)


@pytest.mark.parametrize(
    "text",
    [
        "SELECT FROM WHERE ((( ;",  # parse failure
        "SELECT {{ not_in_context }};",  # render failure
    ],
)
def test_ut01_58_render_or_parse_failure(tmp_path: Path, text: str) -> None:
    """UT01-58 a render or parse failure is `cannot parse <file>`, chained from the original."""
    cfg = _cfg(tmp_path, servicenow=_sn(INCIDENT))
    with pytest.raises(ConfigError, match=f"^cannot parse {FILE}$") as info:
        check_mapping("servicenow", cfg, sql_dir=_sql_dir(tmp_path, text))
    assert info.value.__cause__ is not None


def test_ut01_58_missing_staging_file(tmp_path: Path) -> None:
    """UT01-58 a SQL directory without the staging file is `cannot parse <file>`."""
    cfg = _cfg(tmp_path, servicenow=_sn(INCIDENT))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ConfigError, match=f"^cannot parse {FILE}$"):
        check_mapping("servicenow", cfg, sql_dir=empty)
