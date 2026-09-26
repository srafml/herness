"""The `metrics_tiny` warehouse (impl 04 §11): hand-written rows in an in-memory DuckDB.

`tests/fixtures/metrics_tiny/<schema>.<table>.csv` files are loaded by column name into the
spec 02 tables: `herness/model/sql/000_settings.sql` (T02-12) for the schemas, `meta.*` and
the empty `enrich.*` tables, and `CORE_DDL` for the `core.*` tables. `CORE_DDL` repeats the
column shapes files 200-280 build (T02-15 ... T02-17): those files are CTAS over staged lake
data, so they cannot create empty tables here. Weights are design 04 §10.1: the shipped
`weights.yaml` with `cost_per_downtime_hour[1] = 10000` and `cost_per_engineer_hour = 100`.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import duckdb
import pytest
import yaml
from tests.support.metrics_render import FakeCatalog

from herness.metrics import facts
from herness.metrics.settings import WeightsConfig

ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURE_DIR: Final = ROOT / "tests" / "fixtures" / "metrics_tiny"
SETTINGS_SQL: Final = ROOT / "herness" / "model" / "sql" / "000_settings.sql"
BUILD_ID: Final = "20260401-060000-TNY001"
_TS: Final = "TIMESTAMPTZ"

CORE_DDL: Final[dict[str, str]] = {
    "core.org": "org_id VARCHAR, name VARCHAR, parent_org_id VARCHAR, cost_center VARCHAR,"
    " source VARCHAR",
    "core.team": "team_id VARCHAR, name VARCHAR, org_id VARCHAR, source VARCHAR, active BOOLEAN",
    "core.service": "service_id VARCHAR, name VARCHAR, ci_class VARCHAR, criticality SMALLINT,"
    " business_owner_team_id VARCHAR, org_id VARCHAR, source VARCHAR",
    "core.service_map": "service_id VARCHAR, team_id VARCHAR, jira_project VARCHAR,"
    " jira_component VARCHAR, org_id VARCHAR, role VARCHAR, link_source VARCHAR,"
    " confidence DOUBLE",
    "core.incident": f"record_id VARCHAR, number VARCHAR, opened_at {_TS},"
    f" acknowledged_at {_TS}, resolved_at {_TS}, closed_at {_TS}, priority SMALLINT,"
    " state VARCHAR, service_id VARCHAR, ci_id VARCHAR, team_id VARCHAR,"
    " reassignment_count INTEGER, reopen_count INTEGER, short_description VARCHAR,"
    " description VARCHAR, close_notes VARCHAR, close_code VARCHAR, problem_id VARCHAR,"
    " caused_by_change_id VARCHAR, sla_breached BOOLEAN, business_duration_s BIGINT,"
    f" customer_impact_minutes DOUBLE, content_hash VARCHAR, source_updated_at {_TS}",
    "core.change": f"record_id VARCHAR, number VARCHAR, state VARCHAR, risk VARCHAR,"
    f" type VARCHAR, opened_at {_TS}, planned_start {_TS}, planned_end {_TS},"
    f" actual_start {_TS}, actual_end {_TS}, service_id VARCHAR, ci_id VARCHAR,"
    " team_id VARCHAR, outcome VARCHAR, short_description VARCHAR, description VARCHAR,"
    f" source_updated_at {_TS}",
    "core.problem": f"record_id VARCHAR, number VARCHAR, opened_at {_TS}, resolved_at {_TS},"
    " state VARCHAR, service_id VARCHAR, team_id VARCHAR, known_error BOOLEAN,"
    f" root_cause_text VARCHAR, source_updated_at {_TS}",
    "core.event": f"event_id VARCHAR, source_tool VARCHAR, ts {_TS}, service_id VARCHAR,"
    " host VARCHAR, severity VARCHAR, alert_name VARCHAR, status VARCHAR, dedup_key VARCHAR,"
    " duration_s BIGINT, incident_id VARCHAR",
    "core.metric_daily": "date DATE, service_id VARCHAR, metric_name VARCHAR, value DOUBLE,"
    " unit VARCHAR, source_tool VARCHAR",
    "core.work_item": "record_id VARCHAR, key VARCHAR, type VARCHAR, parent_key VARCHAR,"
    " project VARCHAR, component VARCHAR, components VARCHAR[], labels VARCHAR[],"
    f" status VARCHAR, status_category VARCHAR, created_at {_TS}, resolved_at {_TS},"
    " story_points DOUBLE, estimate_cost_usd DECIMAL(18,2), team_id VARCHAR,"
    f" service_id VARCHAR, summary VARCHAR, description VARCHAR, source_updated_at {_TS}",
    "core.work_item_transition": "record_id VARCHAR, from_status VARCHAR, to_status VARCHAR,"
    f' from_category VARCHAR, to_category VARCHAR, "at" {_TS}',
    "core.work_item_link": "from_key VARCHAR, to_key VARCHAR, link_type VARCHAR",
}


def load_csvs(con: duckdb.DuckDBPyConnection, directory: Path = FIXTURE_DIR) -> None:
    """Insert every `<schema>.<table>.csv` of `directory` into that table, by column name."""
    for path in sorted(directory.glob("*.csv")):
        table = path.stem
        con.execute(
            f"INSERT INTO {table} BY NAME"  # noqa: S608 - fixture file names
            " SELECT * FROM read_csv(?, header = true, all_varchar = true)",
            [str(path)],
        )


def build_metrics_tiny(*, rows: bool = True) -> duckdb.DuckDBPyConnection:
    """In-memory warehouse with the spec 02 DDL, the fixture rows and one `building` build."""
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone = 'UTC'")
    con.execute(SETTINGS_SQL.read_text(encoding="utf-8"))
    for table, columns in CORE_DDL.items():
        con.execute(f"CREATE TABLE {table} ({columns})")
    if rows:
        load_csvs(con)
    con.execute(
        "INSERT INTO meta.build (build_id, started_at, status)"
        " VALUES (?, TIMESTAMPTZ '2026-04-01 06:00:00+00', 'building')",
        [BUILD_ID],
    )
    return con


def tiny_weights() -> WeightsConfig:
    """Shipped weights with the design 04 §10.1 overrides."""
    raw: dict[str, Any] = yaml.safe_load((ROOT / "config" / "weights.yaml").read_text("utf-8"))
    raw["cost_per_downtime_hour"]["by_criticality"][1] = 10000
    raw["cost_per_engineer_hour"]["value"] = 100
    return WeightsConfig.model_validate(raw)


def patch_facts_config(
    monkeypatch: pytest.MonkeyPatch, weights: WeightsConfig | None = None
) -> None:
    """Point `materialize_facts` at the shipped catalog sections and the tiny weights.

    `config/metrics.yaml` loads as a whole only once T04-08 adds catalog entries, so the
    catalog is the partial `FakeCatalog` (shipped `defaults` and `scoring`).
    """
    cfg = SimpleNamespace(weights=weights if weights is not None else tiny_weights())
    monkeypatch.setattr(facts, "catalog_from_config", FakeCatalog)
    monkeypatch.setattr(facts, "get_config", lambda: cfg)
