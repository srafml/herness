"""Tests for the action levers (impl 04 U04-69 ... U04-71; design 04 §5.9; T04-18).

Each test builds the empty `metrics_tiny` DDL with the real stage 400 facts
(`tests.support.metrics_funding.warehouse`) for one hand-computed entity, writes its own
`score.org` rows and runs `run_levers_step`. `as_of` is 2026-04-01 in America/New_York.

The entity: team T1 "Payments" in org O1 "Ops" (child of O0 "Group"), owning service S1
(criticality 1). Base quantities over the trailing 12 months (hand-computed from the weights of
design 04 §10.1):
- incidents I1 (2026-01-18), I2, I3 (P1, 60 impact minutes each, so downtime 10000 USD each);
  I2 resolves after 2 business hours, toil 2 h x 1.5 x 100 USD = 300 USD; I3 is caused by C1.
  N = 3, sum downtime 30000, sum toil 300, avg total 10100, avg toil 100. Not counted: the
  in-window canceled I8 (caused by C3; its fact row is given money so a missing exclusion
  shows), I7 opened after as_of (caused by C3), and I9 of another team (T9, no org).
- changes C1 (failed: caused I3), C2 and C6 (successful), C3 (backed_out) deployed in the
  window, C4 canceled, C5 outside the window: D = 4, F = 2, CC = 10000 (I3 only; I8 is
  excluded, I7 is after as_of), so CC / F = 5000.
- events E1-E4 (severity major) in the window; one info event and one outside the window do
  not count: E = 4.
- observed days = 2026-01-18 .. 2026-04-01 = 73, so the annualization factor 365 / 73 = 5.

Lever metrics hold x = 10 (mttr_hours, peers 10, 8, 6, 4, 2, median 6, lower quartile 4) or
x = 0.5 (rates, peers 0.5, 0.375, 0.25, 0.125, 0.0625, median 0.25, lower quartile 0.125).
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

import duckdb
import pytest
from tests.support.metrics_funding import AS_OF, incident, warehouse
from tests.support.metrics_tiny import BUILD_ID, shipped_catalog, tiny_weights

from herness.core.errors import SchemaViolation
from herness.metrics import _catalog_checks
from herness.metrics.catalog import MetricCatalog
from herness.metrics.context import StepContext
from herness.metrics.levers import LEVER_PLACEHOLDERS, LEVER_TABLE, USD_MODELS, run_levers_step
from herness.metrics.render import render_named
from herness.metrics.settings import WeightsConfig

pytestmark = pytest.mark.unit

SQL_CAP: Final = 20_000  # Evidence.sql limit
ORG_QID: Final = "q_00000000000000aa"
MODEL_METRIC: Final = {
    "mttr": "mttr_hours",
    "repeat": "repeat_incident_rate",
    "reopen": "reopen_rate",
    "reassign": "reassignment_rate",
    "sla": "sla_breach_rate",
    "cfr": "change_failure_rate",
    "noise": "alert_noise_ratio",
}
MTTR_VALUES: Final = (10.0, 8.0, 6.0, 4.0, 2.0)
RATE_VALUES: Final = (0.5, 0.375, 0.25, 0.125, 0.0625)
_ORG_DDL: Final = (
    "CREATE TABLE score.org (entity_type VARCHAR, entity_id VARCHAR, metric VARCHAR,"
    " value DOUBLE, peer_group VARCHAR, peer_median DOUBLE, z_score DOUBLE, trend_slope DOUBLE,"
    " sample_size BIGINT, composite DOUBLE, rank BIGINT, unconfirmed BOOLEAN, flags VARCHAR[],"
    " query_ids VARCHAR[])"
)
_LEVER_COLUMNS: Final = (
    "entity_type, entity_id, metric, target_kind, current_value, target_value, delta_usd,"
    " rationale_template, template_params, unconfirmed, query_ids"
)


def _event(event_id: str, ts: str, severity: str = "major") -> dict[str, object]:
    return {"event_id": event_id, "ts": ts, "service_id": "S1", "severity": severity}


def _change(record_id: str, end: str | None, outcome: str) -> dict[str, object]:
    return {
        "record_id": record_id,
        "team_id": "T1",
        "service_id": "S1",
        "opened_at": "2025-01-01 09:00:00-05",
        "actual_end": end,
        "outcome": outcome,
    }


def _rows() -> dict[str, list[dict[str, object]]]:
    i2 = incident("I2", opened="2026-02-01 10:00:00-05")
    i2.update(resolved_at="2026-02-01 12:00:00-05", business_duration_s=7200)
    i3 = incident("I3", opened="2026-03-01 10:00:00-05")
    i3["caused_by_change_id"] = "C1"
    canceled = incident("I8", opened="2026-02-15 10:00:00-05")
    canceled.update(state="canceled", caused_by_change_id="C3")
    late = incident("I7", opened="2026-04-05 10:00:00-04")
    late["caused_by_change_id"] = "C3"
    old = incident("I9", opened="2026-01-20 10:00:00-05")
    old.update(team_id="T9")
    return {
        "core.org": [
            {"org_id": "O0", "name": "Group", "source": "servicenow"},
            {"org_id": "O1", "name": "Ops", "parent_org_id": "O0", "source": "servicenow"},
        ],
        "core.team": [
            {"team_id": "T1", "name": "Payments", "org_id": "O1", "active": True},
            {"team_id": "T9", "name": "Other", "org_id": None, "active": True},
        ],
        "core.service": [{"service_id": "S1", "criticality": 1, "org_id": "O1"}],
        "core.service_map": [
            {"service_id": "S1", "team_id": "T1", "role": "owner", "confidence": 1.0}
        ],
        "core.incident": [
            incident("I1", opened="2026-01-18 10:00:00-05"),
            i2,
            i3,
            canceled,
            late,
            old,
        ],
        "core.change": [
            _change("C1", "2026-02-28 12:00:00-05", "successful"),
            _change("C2", "2026-03-10 12:00:00-05", "successful"),
            _change("C3", "2026-03-15 12:00:00-05", "backed_out"),
            _change("C4", None, "canceled"),
            _change("C5", "2025-01-10 12:00:00-05", "backed_out"),
            _change("C6", "2026-03-20 12:00:00-04", "successful"),
        ],
        "core.event": [
            *(_event(f"E{k}", f"2026-03-0{k} 08:00:00-05") for k in range(1, 5)),
            _event("E5", "2026-03-05 08:00:00-05", "info"),
            _event("E6", "2025-01-05 08:00:00-05"),
        ],
    }


def _weights(
    *, sla_penalty: float = 0.0, confirmed: Sequence[str] = (), base: WeightsConfig | None = None
) -> WeightsConfig:
    """Tiny weights with an SLA penalty and the named blocks marked confirmed."""
    w = base if base is not None else tiny_weights()
    update: dict[str, object] = {
        name: getattr(w, name).model_copy(update={"unconfirmed": False}) for name in confirmed
    }
    sla = update.get("sla_penalty_usd", w.sla_penalty_usd)
    update["sla_penalty_usd"] = sla.model_copy(update={"value": sla_penalty})  # type: ignore[attr-defined]
    return w.model_copy(update=update)


def _ctx(weights: WeightsConfig | None = None, *, top_entities: int = 50) -> StepContext:
    w = weights if weights is not None else tiny_weights()
    cfg = shipped_catalog().config
    levers = cfg.scoring.levers.model_copy(update={"top_entities": top_entities})
    scoring = cfg.scoring.model_copy(update={"levers": levers})
    catalog = MetricCatalog(cfg.model_copy(update={"scoring": scoring}))
    return StepContext(BUILD_ID, catalog, w, AS_OF, w.business_timezone, frozenset())


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    c = warehouse(_rows())
    c.execute(
        "UPDATE metrics.incident_fact SET downtime_usd = 7000, toil_usd = 700, total_usd = 7700"
        " WHERE record_id = 'I8'"
    )
    c.execute(_ORG_DDL)
    try:
        yield c
    finally:
        c.close()


def _org(  # noqa: PLR0913 - one score.org row
    con: duckdb.DuckDBPyConnection,
    entity_id: str,
    metric: str,
    value: float | None,
    *,
    peer_median: float,
    z: float,
    rank: int = 1,
    entity_type: str = "team",
    peer_group: str = "team:all",
) -> None:
    con.execute(
        "INSERT INTO score.org VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 20, 1.0, ?, false, [], ?)",
        [entity_type, entity_id, metric, value, peer_group, peer_median, z, rank, [ORG_QID]],
    )


def _peers(
    con: duckdb.DuckDBPyConnection, metric: str, values: Sequence[float], *, z: float = 1.0
) -> None:
    """T1 (rank 1, z > 0) holds values[0]; T2 ... hold the rest with z <= 0."""
    median = sorted(values)[len(values) // 2]
    for k, value in enumerate(values):
        z_k = z if k == 0 else -1.0
        _org(con, f"T{k + 1}", metric, value, peer_median=median, z=z_k, rank=k + 1)


def _levers(con: duckdb.DuckDBPyConnection) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    cur = con.execute(f"SELECT {_LEVER_COLUMNS} FROM {LEVER_TABLE}")  # noqa: S608 - constants
    names = [d[0] for d in cur.description]
    out: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in cur.fetchall():
        rec = dict(zip(names, row, strict=True))
        rec["template_params"] = json.loads(rec["template_params"])
        out[rec["entity_type"], rec["entity_id"], rec["metric"], rec["target_kind"]] = rec
    return out


def _delta(
    con: duckdb.DuckDBPyConnection, model: str, values: Sequence[float], ctx: StepContext
) -> dict[str, Decimal]:
    """Run the step for one lever metric; delta_usd by target kind for team T1."""
    metric = MODEL_METRIC[model]
    _peers(con, metric, values)
    run_levers_step(con, ctx)
    return {
        k[3]: r["delta_usd"] for k, r in _levers(con).items() if k[:3] == ("team", "T1", metric)
    }


# --- UT04-92 ... UT04-98 delta_usd per model ---------------------------------------------------


def test_ut04_92_mttr_delta(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-92 mttr: (30000 + 300) x (x - t) / x x 5; x 10, median 6, quartile 4."""
    got = _delta(con, "mttr", MTTR_VALUES, _ctx())
    assert got == {"peer_median": Decimal("60600.00"), "top_quartile": Decimal("90900.00")}


def test_ut04_92_mttr_org_entity_uses_closure_and_service_org(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """UT04-92 parent org O0 sees O1's records through org_closure; a lone org has no quartile."""
    kind = "org"
    _org(
        con,
        "O0",
        "mttr_hours",
        10.0,
        peer_median=6.0,
        z=1.0,
        entity_type=kind,
        peer_group="org:all",
    )
    run_levers_step(con, _ctx())
    rows = _levers(con)
    assert list(rows) == [("org", "O0", "mttr_hours", "peer_median")]
    row = rows["org", "O0", "mttr_hours", "peer_median"]
    assert row["delta_usd"] == Decimal("60600.00")
    assert row["template_params"]["entity_name"] == "Group"


def test_ut04_92_mttr_zero_value_is_skipped(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-92 x = 0 has no mttr delta (division by x), even with an improving target."""
    _org(con, "T1", "mttr_hours", 0.0, peer_median=-1.0, z=1.0)
    assert run_levers_step(con, _ctx()).row_counts == {LEVER_TABLE: 0}


def test_ut04_93_repeat_delta(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-93 repeat: 3 x (x - t) x 10100 x 5; x 0.5, median 0.25, quartile 0.125."""
    got = _delta(con, "repeat", RATE_VALUES, _ctx())
    assert got == {"peer_median": Decimal("37875.00"), "top_quartile": Decimal("56812.50")}


def test_ut04_94_reopen_delta(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-94 reopen: 3 x (x - t) x avg toil 100 x factor 0.5 x 5."""
    got = _delta(con, "reopen", RATE_VALUES, _ctx())
    assert got == {"peer_median": Decimal("187.50"), "top_quartile": Decimal("281.25")}


def test_ut04_95_reassign_delta(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-95 reassign: 3 x (x - t) x 0.5 h x 100 USD/h x 5."""
    got = _delta(con, "reassign", RATE_VALUES, _ctx())
    assert got == {"peer_median": Decimal("187.50"), "top_quartile": Decimal("281.25")}


def test_ut04_96_sla_delta(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-96 sla: 3 x (x - t) x penalty 1000 USD x 5."""
    got = _delta(con, "sla", RATE_VALUES, _ctx(_weights(sla_penalty=1000.0)))
    assert got == {"peer_median": Decimal("3750.00"), "top_quartile": Decimal("5625.00")}


def test_ut04_97_cfr_delta(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-97 cfr: D 4 x (x - t) x (backout 4 h x 100 + CC 10000 / F 2) x 5.

    CC holds I3 only: the excluded I8 and I7 (opened after as_of) of failed C3 do not count.
    """
    got = _delta(con, "cfr", RATE_VALUES, _ctx())
    assert got == {"peer_median": Decimal("27000.00"), "top_quartile": Decimal("40500.00")}


def test_ut04_97_cc_counts_only_failed_changes(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-97 C1 not failed: F 1 (C3), CC 0, so D 4 x 0.25 x 400 x 5 = 2000."""
    con.execute("UPDATE metrics.change_fact SET failed = false WHERE record_id = 'C1'")
    got = _delta(con, "cfr", RATE_VALUES, _ctx())
    assert got == {"peer_median": Decimal("2000.00"), "top_quartile": Decimal("3000.00")}


def test_ut04_97_cfr_without_failed_changes_uses_backout_only(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """UT04-97 F = 0: the change-caused term is 0; D 3 x 0.25 x 400 x 5 = 1500."""
    con.execute("UPDATE metrics.change_fact SET failed = false")
    con.execute("DELETE FROM metrics.change_fact WHERE record_id = 'C3'")
    got = _delta(con, "cfr", RATE_VALUES, _ctx())
    assert got == {"peer_median": Decimal("1500.00"), "top_quartile": Decimal("2250.00")}


def test_ut04_98_noise_delta(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-98 noise: E 4 x (x - t) x 3 min / 60 x 100 USD/h x 5."""
    got = _delta(con, "noise", RATE_VALUES, _ctx())
    assert got == {"peer_median": Decimal("25.00"), "top_quartile": Decimal("37.50")}


# --- UT04-99 skipped rows, unconfirmed and the step adapter ------------------------------------


def test_ut04_99_target_not_improving_is_skipped(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-99 a target equal to x (quartile 4 of 4, 4, 9) gives no row; z <= 0, NULL x none.

    T1 keeps only its peer median 3: 30300 x (4 - 3) / 4 x 5 = 37875.
    """
    _org(con, "T1", "mttr_hours", 4.0, peer_median=3.0, z=0.5)
    _org(con, "T2", "mttr_hours", 4.0, peer_median=4.0, z=0.5, rank=2)
    _org(con, "T3", "mttr_hours", 9.0, peer_median=4.0, z=0.0, rank=3)
    _org(con, "T4", "mttr_hours", None, peer_median=4.0, z=2.0, rank=4)
    run_levers_step(con, _ctx())
    rows = _levers(con)
    assert list(rows) == [("team", "T1", "mttr_hours", "peer_median")]
    assert rows["team", "T1", "mttr_hours", "peer_median"]["delta_usd"] == Decimal("37875.00")


def test_ut04_99_zero_sla_penalty_skips_sla(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-99 the shipped SLA penalty 0 skips the sla lever."""
    _peers(con, "sla_breach_rate", RATE_VALUES)
    assert run_levers_step(con, _ctx()).row_counts == {LEVER_TABLE: 0}


def test_ut04_99_rank_above_top_entities_is_skipped(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-99 T1 (with facts) at rank 2 gets no lever with top_entities 1, one with 2."""
    _org(con, "T1", "mttr_hours", 10.0, peer_median=6.0, z=1.0, rank=2)
    assert run_levers_step(con, _ctx(top_entities=1)).row_counts == {LEVER_TABLE: 0}
    assert run_levers_step(con, _ctx(top_entities=2)).row_counts == {LEVER_TABLE: 1}


@pytest.mark.parametrize("z", [0.0, -0.5])
def test_ut04_99_non_positive_z_is_skipped(con: duckdb.DuckDBPyConnection, z: float) -> None:
    """UT04-99 T1 (with facts, improving target) gets no lever when z_score <= 0."""
    _org(con, "T1", "mttr_hours", 10.0, peer_median=6.0, z=z)
    assert run_levers_step(con, _ctx()).row_counts == {LEVER_TABLE: 0}
    con.execute("UPDATE score.org SET z_score = 0.1")
    # other binds (top 49) so the control run is a new query, not a nondeterministic rerun
    assert run_levers_step(con, _ctx(top_entities=49)).row_counts == {LEVER_TABLE: 1}


def test_ut04_99_metric_without_usd_model_yields_no_row(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-99 TH04-10: a score.org metric outside s_lever_models_k never becomes a lever."""
    for metric in ("cycle_time_days", "unplanned_work_ratio", "made_up_metric"):
        _peers(con, metric, MTTR_VALUES)
    assert run_levers_step(con, _ctx()).row_counts == {LEVER_TABLE: 0}


def test_ut04_99_unconfirmed_per_model(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-99 confirming toil and engineer cost clears reassign and noise only."""
    for model in ("mttr", "reassign", "noise", "sla"):
        _peers(con, MODEL_METRIC[model], MTTR_VALUES if model == "mttr" else RATE_VALUES)
    confirmed = ("toil", "cost_per_engineer_hour")
    run_levers_step(con, _ctx(_weights(sla_penalty=1000.0, confirmed=confirmed)))
    flags = {k[2]: r["unconfirmed"] for k, r in _levers(con).items()}
    assert flags == {
        "mttr_hours": True,
        "reassignment_rate": False,
        "alert_noise_ratio": False,
        "sla_breach_rate": True,
    }
    run_levers_step(con, _ctx(_weights(sla_penalty=1000.0)))
    assert {r["unconfirmed"] for r in _levers(con).values()} == {True}


def test_ut04_99_missing_score_org_raises(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-99 U04-70: no score.org table is a SchemaViolation naming the org step."""
    con.execute("DROP TABLE score.org")
    with pytest.raises(SchemaViolation, match=r"score\.org missing; run step org first"):
        run_levers_step(con, _ctx())


def test_ut04_99_query_error_is_schema_violation(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-99 a failing lever query (missing fact table) surfaces as SchemaViolation."""
    con.execute("DROP TABLE metrics.change_fact")
    with pytest.raises(SchemaViolation, match="levers failed"):
        run_levers_step(con, _ctx())


def test_ut04_99_query_ids_and_evidence(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-99 U04-70: query_ids = [own, org query id]; one score evidence row per run."""
    _peers(con, "mttr_hours", MTTR_VALUES)
    result = run_levers_step(con, _ctx())
    assert result.row_counts == {LEVER_TABLE: 2}
    assert (result.warnings, result.flags, result.failed_checks) == ([], [], [])
    ids = con.execute(f"SELECT DISTINCT query_ids FROM {LEVER_TABLE}").fetchall()  # noqa: S608
    assert len(ids) == 1
    own, upstream = ids[0][0]
    assert upstream == ORG_QID
    evidence = con.execute(
        "SELECT producer, row_count, sql FROM meta.evidence WHERE query_id = ?", [own]
    ).fetchall()
    assert len(evidence) == 1
    assert evidence[0][:2] == ("score", 2)
    assert len(evidence[0][2]) < SQL_CAP


def test_ut04_99_rerun_over_new_org_rows_is_new_evidence(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-99 (T04-21 input pin) a same-build rerun after `org` rewrote score.org under a new
    query id (other values, same binds) records new lever evidence instead of a TH04-06
    "nondeterministic result": the `inputs` digest pins the score.org rows read."""
    _peers(con, "mttr_hours", MTTR_VALUES)
    run_levers_step(con, _ctx())
    first = con.execute(f"SELECT DISTINCT query_ids[1] FROM {LEVER_TABLE}").fetchall()  # noqa: S608
    con.execute("UPDATE score.org SET value = value * 2 WHERE entity_id = 'T1'")
    con.execute("UPDATE score.org SET query_ids = ['q_00000000000000bb']")  # the new org run
    result = run_levers_step(con, _ctx())
    assert result.row_counts == {LEVER_TABLE: 2}
    second = con.execute(f"SELECT DISTINCT query_ids FROM {LEVER_TABLE}").fetchall()  # noqa: S608
    assert len(second) == 1
    own, upstream = second[0][0]
    assert (own != first[0][0], upstream) == (True, "q_00000000000000bb")
    assert _count_evidence(con) == 2


def _count_evidence(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute("SELECT count(*) FROM meta.evidence WHERE producer = 'score'").fetchone()
    assert row is not None
    return int(row[0])


def test_ut04_99_empty_score_org_has_no_upstream(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-99 an empty score.org gives an empty, typed action_lever table."""
    assert run_levers_step(con, _ctx()).row_counts == {LEVER_TABLE: 0}
    types = dict(
        con.execute(
            "SELECT column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = 'score' AND table_name = 'action_lever'"
        ).fetchall()
    )
    assert types == {
        "entity_type": "VARCHAR",
        "entity_id": "VARCHAR",
        "metric": "VARCHAR",
        "target_kind": "VARCHAR",
        "current_value": "DOUBLE",
        "target_value": "DOUBLE",
        "delta_usd": "DECIMAL(18,2)",
        "rationale_template": "VARCHAR",
        "template_params": "JSON",
        "unconfirmed": "BOOLEAN",
        "query_ids": "VARCHAR[]",
    }


def test_ut04_99_rendered_sql_binds_and_cap() -> None:
    """UT04-99 the template renders under the evidence SQL cap with typed binds only."""
    rendered = render_named("levers", {}, _ctx().binds())
    assert len(rendered.sql) < SQL_CAP
    assert "--" not in rendered.sql
    assert "/*" not in rendered.sql
    assert set(rendered.bind) == {
        *("as_of", "as_of_ts", "tz", "s_window_days", "s_top_entities", "s_lower_better"),
        *("s_lever_models_k", "s_lever_models_v", "s_templates_k", "s_templates_v"),
        *("s_metric_units_k", "s_metric_units_v", "s_unconfirmed_models"),
        *("d_noise_severities", "d_change_link_min_score"),
        *("w_engineer_hour", "w_triage_minutes", "w_reassign_hours", "w_reopen_factor"),
        *("w_backout_hours", "w_sla_penalty"),
    }


# --- UT04-100 template_params ------------------------------------------------------------------


def test_ut04_100_template_params_keys_are_placeholders(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-100 every row: template_params keys = LEVER_PLACEHOLDERS, values from the query."""
    for model, metric in MODEL_METRIC.items():
        _peers(con, metric, MTTR_VALUES if model == "mttr" else RATE_VALUES)
    run_levers_step(con, _ctx(_weights(sla_penalty=1000.0)))
    rows = _levers(con)
    catalog = shipped_catalog()
    templates = catalog.scoring.levers.templates
    assert {k[2] for k in rows} == set(MODEL_METRIC.values())
    assert len(rows) == 2 * len(MODEL_METRIC)
    n_basis = {"cfr": 4, "noise": 4}
    for (_, _, metric, kind), row in rows.items():
        model = catalog.get(metric).usd_model
        params: Mapping[str, object] = row["template_params"]
        assert set(params) == LEVER_PLACEHOLDERS
        assert row["rationale_template"] == templates[model]  # type: ignore[index]
        assert params == {
            "entity_name": "Payments",
            "metric_label": metric,
            "current_value": row["current_value"],
            "target_value": row["target_value"],
            "target_kind": kind,
            "unit": catalog.get(metric).unit,
            "delta_usd": str(row["delta_usd"]),
            "n_basis": n_basis.get(str(model), 3),
            "peer_group": "team:all",
            "period": "t12w",
        }
        assert row["rationale_template"].format(**params)


def test_ut04_100_constants() -> None:
    """UT04-100 U04-71: USD_MODELS order; one LEVER_PLACEHOLDERS shared with the validator."""
    assert USD_MODELS == ("mttr", "repeat", "reopen", "reassign", "sla", "cfr", "noise")
    assert LEVER_PLACEHOLDERS is _catalog_checks.LEVER_PLACEHOLDERS
    assert (
        frozenset(
            {"entity_name", "metric_label", "current_value", "target_value", "target_kind"}
            | {"unit", "delta_usd", "n_basis", "peer_group", "period"}
        )
        == LEVER_PLACEHOLDERS
    )
    assert set(MODEL_METRIC) == set(USD_MODELS)
    lever_models = {shipped_catalog().get(m).usd_model for m in MODEL_METRIC.values()}
    assert lever_models == set(USD_MODELS)


def test_ut04_100_metric_without_unit_uses_other(
    con: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-100 a lever metric missing from s_metric_units_k gets unit 'other'."""
    real = StepContext.binds

    def binds(self: StepContext) -> dict[str, object]:
        out = real(self)
        keys = list(out["s_metric_units_k"])  # type: ignore[call-overload]
        vals = list(out["s_metric_units_v"])  # type: ignore[call-overload]
        pos = keys.index("mttr_hours")
        out["s_metric_units_k"] = keys[:pos] + keys[pos + 1 :]
        out["s_metric_units_v"] = vals[:pos] + vals[pos + 1 :]
        return out

    monkeypatch.setattr(StepContext, "binds", binds)
    _org(con, "T1", "mttr_hours", 10.0, peer_median=6.0, z=1.0)
    run_levers_step(con, _ctx())
    assert [r["template_params"]["unit"] for r in _levers(con).values()] == ["other"]
