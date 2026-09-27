"""Shared fixtures for the metric rendering tests (impl 04 T04-04).

The render tests exercise hand-written metric entries (not the shipped catalog), so these
helpers build a partial catalog: the shipped `defaults` and `scoring` sections plus those
entries, behind the catalog read surface `render` uses.
"""

import datetime
from pathlib import Path
from typing import Any

import duckdb
import yaml

from herness.metrics.settings import (
    MetricDef,
    MetricsDefaults,
    ScoringConfig,
    WeightsConfig,
)
from herness.metrics.windows import Window, custom_window, default_window

ROOT = Path(__file__).resolve().parents[2]
TZ = "America/New_York"
AS_OF = datetime.date(2024, 3, 13)

# Catalog SELECT shaped like design 04 §4.1 `mttr_hours`, parameters only through p().
MTTR_SQL = """\
SELECT {{ entity_col('incident', 'f') }} AS entity_id,
       {{ period_start('f.resolved_at') }} AS period_start,
       avg(f.resolve_h) AS value, sum(f.resolve_h) AS numerator,
       count(f.resolve_h) AS denominator, count(f.resolve_h) AS sample_size
FROM metrics.incident_fact f {{ entity_join('incident', 'f') }}
WHERE f.resolved_at >= {{ p('window_start') }} AND f.resolved_at < {{ p('window_end') }}
  AND f.resolve_h IS NOT NULL AND NOT f.excluded
  {{ filter_clause('incident', 'f') }} {{ entity_filter() }}
GROUP BY ALL
"""

# Generic per-source SELECT (anchor column per source) used to render every (source, grain).
SOURCE_TABLES = {
    "incident": ("metrics.incident_fact", "f", "f.opened_at"),
    "change": ("metrics.change_fact", "c", "c.actual_end"),
    "event": ("core.event", "e", "e.ts"),
    "work_item": ("metrics.work_item_fact", "w", "w.done_at"),
    "metric_daily": ("core.metric_daily", "m", "m.date"),
}


def source_sql(source: str) -> str:
    """A count template over `source` with every macro a catalog entry uses."""
    table, alias, anchor = SOURCE_TABLES[source]
    start = "period_start_date" if source == "metric_daily" else "period_start"
    lo, hi = (
        ("window_start_date", "window_end_date")
        if source == "metric_daily"
        else ("window_start", "window_end")
    )
    return (
        f"SELECT {{{{ entity_col('{source}', '{alias}') }}}} AS entity_id,\n"
        f"  {{{{ {start}('{anchor}') }}}} AS period_start,\n"
        "  count(*) AS value, count(*) AS numerator, NULL AS denominator,\n"
        "  count(*) AS sample_size\n"
        f"FROM {table} {alias} {{{{ entity_join('{source}', '{alias}') }}}}\n"
        f"WHERE {anchor} >= {{{{ p('{lo}') }}}} AND {anchor} < {{{{ p('{hi}') }}}}\n"
        f"  {{{{ filter_clause('{source}', '{alias}') }}}} {{{{ entity_filter() }}}}\n"
        "GROUP BY ALL\n"
    )


def _yaml(name: str) -> dict[str, Any]:
    data = yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def weights(**flags: bool) -> WeightsConfig:
    """Shipped weights.yaml with the given blocks' `unconfirmed` flags overridden."""
    raw = _yaml("weights.yaml")
    for block, value in flags.items():
        raw[block]["unconfirmed"] = value
    return WeightsConfig.model_validate(raw)


def metric(**overrides: Any) -> MetricDef:
    """A valid catalog entry (defaults: `mttr_hours`), fields overridable."""
    raw: dict[str, Any] = {
        "name": "mttr_hours",
        "description": "Mean wall-clock hours from opened_at to resolved_at.",
        "domain": "ops",
        "grains": ["service", "team", "org", "cluster"],
        "unit": "hours",
        "better": "lower",
        "aggregation": "mean",
        "min_sample_size": 2,
        "owner": "sre-analytics",
        "estimate": False,
        "uses_weights": [],
        "usd_model": "mttr",
        "filters": ["priority", "service_id", "team_id", "org_id", "cluster_id"],
        "enabled": True,
        "requires_columns": [],
        "sql": MTTR_SQL,
    }
    raw.update(overrides)
    return MetricDef.model_validate(raw)


class FakeCatalog:
    """Catalog read surface of `MetricCatalog` (U04-23) over partial config sections."""

    def __init__(self, metrics: list[MetricDef] | None = None) -> None:
        raw = _yaml("metrics.yaml")
        self.defaults = MetricsDefaults.model_validate(raw["defaults"])
        self.scoring = ScoringConfig.model_validate(raw["scoring"])
        self._metrics = {m.name: m for m in (metrics or [metric()])}

    def get(self, name: str) -> MetricDef:
        return self._metrics[name]

    def names(self, *, enabled_only: bool = True) -> list[str]:
        return sorted(n for n, m in self._metrics.items() if m.enabled or not enabled_only)


def week_window(*, custom: bool = False) -> Window:
    if custom:
        return custom_window(
            "week", datetime.date(2024, 1, 3), datetime.date(2024, 2, 1), AS_OF, TZ
        )
    return default_window("week", AS_OF, TZ, {"week": 4, "month": 3, "quarter": 2})


def tiny_warehouse() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with the columns the test templates read (not the spec 02 DDL)."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA metrics; CREATE SCHEMA core")
    con.execute(
        "CREATE TABLE metrics.incident_fact (record_id VARCHAR, service_id VARCHAR,"
        " team_id VARCHAR, org_id VARCHAR, cluster_id VARCHAR, priority SMALLINT,"
        " opened_at TIMESTAMPTZ, resolved_at TIMESTAMPTZ, resolve_h DOUBLE, excluded BOOLEAN)"
    )
    con.execute("CREATE TABLE metrics.org_closure (org_id VARCHAR, ancestor_org_id VARCHAR)")
    con.execute(
        "INSERT INTO metrics.incident_fact VALUES"
        " ('i1', 'svc-a', 'team-a', 'org-a', 'cl-1', 1,"
        "  TIMESTAMPTZ '2024-02-20 10:00:00+00', TIMESTAMPTZ '2024-02-20 14:00:00+00', 4, false),"
        " ('i2', 'svc-a', 'team-a', 'org-a', 'cl-1', 2,"
        "  TIMESTAMPTZ '2024-02-21 10:00:00+00', TIMESTAMPTZ '2024-02-21 12:00:00+00', 2, false),"
        " ('i3', 'svc-b', 'team-b', 'org-b', NULL, 3,"
        "  TIMESTAMPTZ '2024-02-22 10:00:00+00', TIMESTAMPTZ '2024-02-22 11:00:00+00', 1, false)"
    )
    con.execute(
        "INSERT INTO metrics.org_closure VALUES ('org-a', 'org-a'), ('org-b', 'org-b'),"
        " ('org-a', 'org-root'), ('org-b', 'org-root')"
    )
    return con
