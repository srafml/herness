"""Small funding graphs for the attribution tests (impl 04 T04-14, UT04-75 ... PT04-05).

Each test builds its own warehouse: the empty `metrics_tiny` DDL (no fixture rows), a few
`core`/`enrich` rows, then the real stage 400 facts. `as_of` is 2026-04-01 in the business
timezone (America/New_York), so the funding window is [2025-04-01, 2026-04-01) local time.
Incidents carry no `resolved_at`, so their toil is 0 and `total_usd` is
`customer_impact_minutes / 60 x 10000 x priority multiplier` on a criticality-1 service.
"""

import datetime
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any, Final
from unittest import mock

import duckdb
from freezegun import freeze_time
from tests.support.metrics_tiny import BUILD_ID, build_metrics_tiny, shipped_catalog, tiny_weights

from herness.metrics import facts
from herness.metrics.context import StepContext
from herness.metrics.funding import ATTRIBUTION_TABLE, FUNDING_TABLE, run_funding_step
from herness.metrics.settings import WeightsConfig

AS_OF: Final = datetime.date(2026, 4, 1)
FACTS_TIME: Final = "2026-04-01 06:30:00"
COLUMNS: Final = (
    "candidate_id",
    "record_id",
    "record_kind",
    "tier",
    "weight",
    "share",
    "pain_usd",
    "quality",
)
_BASE: Final[dict[str, list[dict[str, object]]]] = {
    "core.org": [{"org_id": "O1", "source": "servicenow"}],
    "core.team": [{"team_id": "T1", "org_id": "O1", "active": True}],
    "core.service": [
        {"service_id": s, "criticality": 1, "org_id": "O1"} for s in ("S1", "S2", "S3", "S9")
    ],
}

SCORE_COLUMNS: Final = (
    "candidate_id",
    "candidate_type",
    "title",
    "annual_pain_usd",
    "addressable_pain_usd",
    "expected_reduction",
    "n_incidents",
    "confidence",
    "strategic_weight",
    "effort_cost_usd",
    "priority",
    "wsjf",
    "rank",
    "unconfirmed",
    "flags",
)

type Rows = Mapping[str, Sequence[Mapping[str, object]]]


def insert(
    con: duckdb.DuckDBPyConnection, table: str, rows: Sequence[Mapping[str, object]]
) -> None:
    """Insert `rows` (column -> value) into `table` by column name."""
    for row in rows:
        cols = ", ".join(f'"{c}"' for c in row)
        marks = ", ".join("?" for _ in row)
        con.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))  # noqa: S608 - test table names


def incident(
    record_id: str,
    *,
    opened: str = "2026-02-01 10:00:00-05",
    service: str | None = "S1",
    minutes: float = 60,
    priority: int = 1,
) -> dict[str, object]:
    """A `core.incident` row; its number is `N` + the record ID."""
    return {
        "record_id": record_id,
        "number": f"N{record_id}",
        "opened_at": opened,
        "priority": priority,
        "state": "closed",
        "service_id": service,
        "team_id": "T1",
        "customer_impact_minutes": minutes,
    }


def item(  # noqa: PLR0913 - one keyword per work item column the tests vary
    record_id: str,
    key: str,
    *,
    kind: str = "epic",
    parent: str | None = None,
    service: str | None = None,
    status: str = "in_progress",
    components: list[str] | None = None,
    project: str = "PAY",
    team: str | None = "T1",
    estimate: float | None = None,
    points: float | None = None,
) -> dict[str, object]:
    """A `core.work_item` row (project PAY unless given)."""
    return {
        "record_id": record_id,
        "key": key,
        "type": kind,
        "parent_key": parent,
        "project": project,
        "components": components,
        "status_category": status,
        "created_at": "2025-12-01 09:00:00-05",
        "team_id": team,
        "service_id": service,
        "estimate_cost_usd": estimate,
        "story_points": points,
    }


def mention(work_key: str, incident_record: str, *, reverse: bool = False) -> dict[str, object]:
    """A `mentions_incident` link between a work item key and `N<incident_record>`."""
    ends = (work_key, f"N{incident_record}")
    a, b = reversed(ends) if reverse else ends
    return {"from_key": a, "to_key": b, "link_type": "mentions_incident"}


def member(record_id: str, cluster_id: str, prob: float = 0.9) -> dict[str, object]:
    """An `enrich.cluster_member` row."""
    return {"record_id": record_id, "cluster_id": cluster_id, "membership_prob": prob}


def warehouse(rows: Rows, weights: WeightsConfig | None = None) -> duckdb.DuckDBPyConnection:
    """Empty `metrics_tiny` DDL plus the base org/team/services, `rows` and the stage 400 facts."""
    w = weights if weights is not None else tiny_weights()
    con = build_metrics_tiny(rows=False)
    for table, table_rows in {**_BASE, **rows}.items():
        insert(con, table, table_rows)
    cfg = SimpleNamespace(weights=w)
    with (
        mock.patch.object(facts, "catalog_from_config", shipped_catalog),
        mock.patch.object(facts, "get_config", return_value=cfg),
        freeze_time(FACTS_TIME),
    ):
        facts.materialize_facts(con, BUILD_ID)
    return con


def step_context(weights: WeightsConfig | None = None) -> StepContext:
    """The funding step context at `AS_OF`."""
    w = weights if weights is not None else tiny_weights()
    return StepContext(BUILD_ID, shipped_catalog(), w, AS_OF, w.business_timezone, frozenset())


def attribution(
    con: duckdb.DuckDBPyConnection, weights: WeightsConfig | None = None
) -> list[dict[str, Any]]:
    """Run the funding step and return the stored rows (without query_id), sorted."""
    run_funding_step(con, step_context(weights))
    cols = ", ".join(COLUMNS)
    sql = f"SELECT {cols} FROM {ATTRIBUTION_TABLE} ORDER BY record_kind, record_id, candidate_id"  # noqa: S608 - constants
    return [dict(zip(COLUMNS, row, strict=True)) for row in con.execute(sql).fetchall()]


def share_sums(con: duckdb.DuckDBPyConnection) -> list[float]:
    """Per attributed record, the sum of its shares."""
    sql = f"SELECT sum(share) FROM {ATTRIBUTION_TABLE} GROUP BY record_kind, record_id"  # noqa: S608 - constant
    return [float(r[0]) for r in con.execute(sql).fetchall()]


def cluster_fix_weights(weights: WeightsConfig | None = None) -> WeightsConfig:
    """Weights whose cluster-fix thresholds a two-incident cluster of 10000 USD passes."""
    w = weights if weights is not None else tiny_weights()
    cf = w.cluster_fix.model_copy(
        update={"min_incidents_12m": 2, "min_annual_pain_usd": 1000, "max_linked_share": 0.5}
    )
    return w.model_copy(update={"cluster_fix": cf})


def scores(
    con: duckdb.DuckDBPyConnection, weights: WeightsConfig | None = None
) -> dict[str, dict[str, Any]]:
    """Run the funding step and return the `score.funding` rows (without query_ids) by ID."""
    run_funding_step(con, step_context(weights))
    cols = ", ".join(SCORE_COLUMNS)
    rows = con.execute(f"SELECT {cols} FROM {FUNDING_TABLE}").fetchall()  # noqa: S608 - constants
    return {row[0]: dict(zip(SCORE_COLUMNS, row, strict=True)) for row in rows}
