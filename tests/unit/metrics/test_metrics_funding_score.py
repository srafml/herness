"""Tests for the funding score (impl 04 U04-65, U04-66 steps 3-4; T04-15).

Each test builds a small graph (`tests.support.metrics_funding`) on the empty `metrics_tiny`
DDL with real stage 400 facts, runs `run_funding_step` and reads `score.funding`.
`as_of` is 2026-04-01 (a Wednesday); a 60-minute incident on a criticality-1 service costs
10000 USD at priority 1 and 1000 USD at priority 3. Incident `H0`, opened 400 days before
`as_of` on S3 (no candidate), is outside the funding window and only makes the observed
history 365 days. Shipped expected reduction: epic 0.25, cluster_fix 0.40; engineer hour
100 USD, 6 hours per story point, cluster-fix effort 160 hours.
"""

import datetime
import math
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import duckdb
import pytest
from tests.support.metrics_funding import (
    AS_OF,
    SCORE_COLUMNS,
    cluster_fix_weights,
    incident,
    item,
    member,
    mention,
    scores,
    step_context,
    warehouse,
)
from tests.support.metrics_tiny import tiny_weights

from herness.metrics.evidence import WRITABLE_TABLES
from herness.metrics.funding import ATTRIBUTION_TABLE, FUNDING_TABLE, run_funding_step
from herness.metrics.render import render_named
from herness.metrics.settings import WeightsConfig

pytestmark = pytest.mark.unit

EVIDENCE_SQL_CAP = 20_000


@pytest.fixture
def closing() -> Iterator[list[duckdb.DuckDBPyConnection]]:
    """Connections appended here are closed after the test."""
    cons: list[duckdb.DuckDBPyConnection] = []
    yield cons
    for con in cons:
        con.close()


def at(days_before: int) -> str:
    """Noon (UTC-5) on the day `days_before` days before `as_of`."""
    return f"{AS_OF - datetime.timedelta(days=days_before)} 12:00:00-05"


def history() -> dict[str, object]:
    """Incident H0: 400 days of history, outside the funding window, attributed to nobody."""
    return incident("H0", opened=at(400), service="S3")


def linked(rid: str, key: str, **kw: Any) -> tuple[dict[str, object], dict[str, object]]:
    """An incident on S3 (no service path) and its direct link to work item `key`."""
    return incident(rid, service="S3", **kw), mention(key, rid)


def graph(
    items: list[dict[str, object]],
    links: list[tuple[str, str, dict[str, Any]]],
    **extra: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    """Rows for `items`, the history incident and directly linked incidents (id, key, kw)."""
    pairs = [linked(rid, key, **kw) for rid, key, kw in links]
    rows: dict[str, list[dict[str, object]]] = {
        "core.work_item": items,
        "core.incident": [history(), *(p[0] for p in pairs)],
        "core.work_item_link": [p[1] for p in pairs],
    }
    for table, table_rows in extra.items():
        name = table.replace("__", ".")
        rows[name] = rows.get(name, []) + table_rows
    return rows


def build(
    closing: list[duckdb.DuckDBPyConnection],
    rows: dict[str, list[dict[str, object]]],
    weights: WeightsConfig | None = None,
) -> dict[str, dict[str, Any]]:
    con = warehouse(rows, weights)
    closing.append(con)
    return scores(con, weights)


# --- UT04-80 annualization and short history -----------------------------------------------------


@pytest.mark.parametrize(
    ("days", "annual", "addressable", "flags"),
    [
        # 10000 x 365 / 180; x 0.25 = 5069.445, cast half away from zero
        (180, Decimal("20277.78"), Decimal("5069.45"), []),
        # 10000 x 365 / 179; x 0.25 = 5097.765
        (179, Decimal("20391.06"), Decimal("5097.77"), ["short_history"]),
    ],
)
def test_ut04_80_annualized_pain_and_short_history(
    closing: list[duckdb.DuckDBPyConnection],
    days: int,
    annual: Decimal,
    addressable: Decimal,
    flags: list[str],
) -> None:
    """UT04-80 annual pain = pain x 365 / observed days; short_history only below 180 days."""
    con = warehouse(
        {
            "core.work_item": [item("A", "PAY-1", estimate=1000)],
            "core.incident": [incident("I1", opened=at(days), service="S3")],
            "core.work_item_link": [mention("PAY-1", "I1")],
        }
    )
    closing.append(con)
    row = scores(con)["A"]
    assert row["annual_pain_usd"] == annual
    assert row["addressable_pain_usd"] == addressable
    assert row["flags"] == flags
    c_hist = days / 365.0
    assert row["confidence"] == pytest.approx(max(0.05, math.sqrt(1 / 30.0) * c_hist))


# --- UT04-81 effort chain ------------------------------------------------------------------------


def test_ut04_81_effort_chain_and_no_estimate(closing: list[duckdb.DuckDBPyConnection]) -> None:
    """UT04-81 estimate, else remaining points x 600, cluster_fix 160 h x 100, else NULL."""
    items = [
        item("A", "PAY-1", estimate=5000, points=8),
        item("B", "PAY-2", points=2),
        item("B1", "PAY-21", kind="story", parent="PAY-2", points=3, status="todo"),
        item("B2", "PAY-22", kind="story", parent="PAY-2", points=5, status="done"),
        item("B3", "PAY-23", kind="story", parent="PAY-2"),
        item("C", "PAY-3"),
        item("D", "PAY-4"),
        item("D1", "PAY-41", kind="story", parent="PAY-4"),
        item("E", "PAY-5"),
        item("E1", "PAY-51", kind="story", parent="PAY-5", points=8, status="done"),
    ]
    cluster = [incident(r, service="S3") for r in ("IF0", "IF1")]
    rows = graph(
        items,
        [],
        core__incident=cluster,
        enrich__cluster_member=[member("IF0", "C9"), member("IF1", "C9")],
    )
    got = build(closing, rows, cluster_fix_weights())
    effort = {k: r["effort_cost_usd"] for k, r in got.items()}
    assert effort == {
        "A": Decimal("5000.00"),
        "B": Decimal("3000.00"),  # (2 + 3) points x 6 h x 100 USD
        "C": None,
        "D": None,
        "E": None,
        "cluster_fix:C9": Decimal("16000.00"),
    }
    assert {k: r["flags"] for k, r in got.items()} == {
        k: ([] if v is not None else ["no_estimate"]) for k, v in effort.items()
    }
    for k in ("C", "D", "E"):
        assert (got[k]["priority"], got[k]["wsjf"]) == (None, None)
    cf = got["cluster_fix:C9"]
    assert (cf["candidate_type"], cf["title"], cf["expected_reduction"]) == (
        "cluster_fix",
        "cluster_fix:C9",
        pytest.approx(0.40),
    )
    assert cf["annual_pain_usd"] == Decimal("20000.00")
    assert cf["addressable_pain_usd"] == Decimal("8000.00")
    assert (got["A"]["title"], got["A"]["candidate_type"]) == ("PAY-1", "epic")


# --- UT04-82 confidence --------------------------------------------------------------------------


def test_ut04_82_confidence_formula(closing: list[duckdb.DuckDBPyConnection]) -> None:
    """UT04-82 confidence = clamp(c_map x c_sample x c_hist, 0.05, 1)."""
    items = [
        item("X", "PAY-1", service="S2", estimate=1000),
        item("Y", "PAY-2", estimate=1000),
        item("Z", "PAY-3", estimate=1000),
    ]
    links: list[tuple[str, str, dict[str, Any]]] = [("I1", "PAY-1", {})]
    links += [(f"Z{n}", "PAY-3", {}) for n in range(30)]
    # I2 (120 minutes on S2, unlinked) reaches X only through the service path, q = 0.5.
    rows = graph(items, links, core__incident=[incident("I2", service="S2", minutes=120)])
    got = build(closing, rows)
    x = got["X"]
    assert x["annual_pain_usd"] == Decimal("30000.00")
    assert x["n_incidents"] == 2
    c_map = (10000 * 1.0 + 20000 * 0.5) / 30000
    assert x["confidence"] == pytest.approx(c_map * math.sqrt(2 / 30.0) * 1.0)
    # No pain: c_map = 0, clamped to 0.05; priority 0.
    assert (got["Y"]["confidence"], got["Y"]["priority"]) == (pytest.approx(0.05), 0.0)
    assert (got["Y"]["annual_pain_usd"], got["Y"]["n_incidents"]) == (Decimal("0.00"), 0)
    # 30 direct incidents over a full year: every factor is 1.
    assert (got["Z"]["n_incidents"], got["Z"]["confidence"]) == (30, pytest.approx(1.0))


def test_ut04_82_n_incidents_rounds_share_sum(closing: list[duckdb.DuckDBPyConnection]) -> None:
    """UT04-82 n_incidents = round(Σ share) over incident rows: 1 + 1/3 rounds to 1."""
    items = [item(k, f"PAY-{k}", estimate=1000) for k in ("A", "B", "C")]
    rows = graph(
        items,
        [("I1", "PAY-A", {}), ("I2", "PAY-A", {})],
        core__work_item_link=[mention("PAY-B", "I2"), mention("PAY-C", "I2")],
    )
    got = build(closing, rows)
    assert [got[k]["n_incidents"] for k in ("A", "B", "C")] == [1, 0, 0]


# --- UT04-83 strategic weight --------------------------------------------------------------------


def _strategic_weights() -> WeightsConfig:
    w = cluster_fix_weights()
    sw = w.strategic_weights.model_copy(
        update={
            "default": 1.0,
            "clip": (0.5, 2.0),
            "portfolio": {"INIT-TOP": 1.5, "INIT-1": 1.1, "PAY": 0.8},
            "org": {"O1": 1.6, "O3": 0.2},
        }
    )
    return w.model_copy(update={"strategic_weights": sw})


def test_ut04_83_strategic_weight_product_clip_default(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-83 strategic weight = clip(portfolio x org); missing keys weigh 1.0."""
    teams = [
        {"team_id": t, "org_id": o, "active": True}
        for t, o in (("T1", "O1"), ("T2", "O2"), ("T3", "O3"))
    ]
    items = [
        item("TOP", "INIT-TOP", kind="initiative", project="OPS", team="T2"),
        item("MID", "INIT-1", kind="initiative", parent="INIT-TOP", project="OPS", team="T2"),
        item("E1", "OPS-1", parent="INIT-1", project="OPS"),
        item("E2", "PAY-2"),
        item("E3", "OPS-3", project="OPS", team="T3"),
        item("E4", "OPS-4", project="OPS", team="T2"),
        item("E5", "OPS-5", project="OPS", team=None, service="S1"),
    ]
    cluster = [incident(r, service="S1") for r in ("IF0", "IF1")]
    rows = graph(
        items,
        [],
        core__incident=cluster,
        enrich__cluster_member=[member("IF0", "C9"), member("IF1", "C9")],
    )
    rows["core.team"] = teams
    got = build(closing, rows, _strategic_weights())
    weight = {k: r["strategic_weight"] for k, r in got.items()}
    assert weight == {
        "TOP": pytest.approx(1.5),  # own key is the deepest initiative; O2 unmapped
        "MID": pytest.approx(1.5),  # deepest initiative ancestor INIT-TOP
        "E1": pytest.approx(2.0),  # 1.5 x 1.6 = 2.4 clipped to 2.0
        "E2": pytest.approx(1.28),  # project PAY 0.8 x O1 1.6
        "E3": pytest.approx(0.5),  # 1.0 x 0.2 clipped to 0.5
        "E4": pytest.approx(1.0),  # both defaults
        "E5": pytest.approx(1.6),  # no team: org of service S1
        "cluster_fix:C9": pytest.approx(1.6),  # owning service S1 -> O1
    }


# --- UT04-84 priority, rank and the step adapter -------------------------------------------------


def _five_candidates() -> dict[str, list[dict[str, object]]]:
    items = [
        item("B", "PAY-B", estimate=1000),
        item("C", "PAY-C", estimate=2000),
        item("D", "PAY-D", estimate=500),
        item("E", "PAY-E", estimate=500),
        item("F", "PAY-F"),
    ]
    links: list[tuple[str, str, dict[str, Any]]] = [
        ("IB", "PAY-B", {}),
        ("IC", "PAY-C", {"minutes": 120}),
        ("IF", "PAY-F", {"minutes": 600}),
    ]
    return graph(items, links)


def _rank_key(r: dict[str, Any]) -> tuple[Any, ...]:
    p = r["priority"]
    return (p is None, -(p or 0.0), -r["addressable_pain_usd"], -r["confidence"], r["candidate_id"])


def test_ut04_84_priority_and_rank_ties(closing: list[duckdb.DuckDBPyConnection]) -> None:
    """UT04-84 priority = addressable x confidence x weight / effort; rank by the design ties."""
    got = build(closing, _five_candidates())
    conf = math.sqrt(1 / 30.0)
    assert got["B"]["priority"] == pytest.approx(2500 * conf / 1000)
    # Equal priority: C has the larger addressable pain; D and E tie on everything but ID;
    # F has the largest pain but no estimate (priority NULL, last).
    assert got["B"]["priority"] == got["C"]["priority"]
    assert (got["D"]["priority"], got["E"]["priority"], got["F"]["priority"]) == (0.0, 0.0, None)
    assert got["F"]["addressable_pain_usd"] == Decimal("25000.00")
    assert {k: r["rank"] for k, r in got.items()} == {"C": 1, "B": 2, "D": 3, "E": 4, "F": 5}
    by_key = sorted(got.values(), key=_rank_key)
    assert [r["rank"] for r in by_key] == [1, 2, 3, 4, 5]
    assert all(r["unconfirmed"] is True for r in got.values())  # shipped weights unconfirmed


def test_ut04_84_step_records_score_with_upstream(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-84 the step stores score.funding with query_ids [own, attribution] and evidence."""
    con = warehouse(_five_candidates())
    closing.append(con)
    sc = step_context()
    result = run_funding_step(con, sc)
    assert result.row_counts == {ATTRIBUTION_TABLE: 3, FUNDING_TABLE: 5}
    assert (result.warnings, result.flags, result.failed_checks) == ([], [], [])
    attr_ids = con.execute(f"SELECT DISTINCT query_id FROM {ATTRIBUTION_TABLE}").fetchall()  # noqa: S608
    assert len(attr_ids) == 1
    ids = con.execute(f"SELECT DISTINCT query_ids FROM {FUNDING_TABLE}").fetchall()  # noqa: S608
    assert len(ids) == 1
    own, upstream = ids[0][0]
    assert upstream == attr_ids[0][0]
    evidence = con.execute(
        "SELECT producer, row_count, sql, params FROM meta.evidence WHERE query_id = ?", [own]
    ).fetchall()
    assert len(evidence) == 1
    producer, row_count, sql, params = evidence[0]
    assert (producer, row_count) == ("score", 5)
    assert len(sql) < EVIDENCE_SQL_CAP
    assert '"name":"funding_score"' in params
    assert run_funding_step(con, sc).row_counts == {ATTRIBUTION_TABLE: 3, FUNDING_TABLE: 5}
    assert con.execute(f"SELECT DISTINCT query_ids FROM {FUNDING_TABLE}").fetchall() == ids  # noqa: S608


def test_ut04_84_empty_score_warns(closing: list[duckdb.DuckDBPyConnection]) -> None:
    """UT04-84 no candidates: score.funding is empty and the step warns."""
    con = warehouse({})
    closing.append(con)
    result = run_funding_step(con, step_context())
    assert result.row_counts == {ATTRIBUTION_TABLE: 0, FUNDING_TABLE: 0}
    assert result.warnings == ["no funding candidates"]
    described = con.execute(f"DESCRIBE {FUNDING_TABLE}").fetchall()
    assert [(r[0], r[1]) for r in described] == [
        ("candidate_id", "VARCHAR"),
        ("candidate_type", "VARCHAR"),
        ("title", "VARCHAR"),
        ("annual_pain_usd", "DECIMAL(18,2)"),
        ("addressable_pain_usd", "DECIMAL(18,2)"),
        ("expected_reduction", "DOUBLE"),
        ("n_incidents", "BIGINT"),
        ("confidence", "DOUBLE"),
        ("strategic_weight", "DOUBLE"),
        ("effort_cost_usd", "DECIMAL(18,2)"),
        ("priority", "DOUBLE"),
        ("wsjf", "DOUBLE"),
        ("rank", "BIGINT"),
        ("unconfirmed", "BOOLEAN"),
        ("flags", "VARCHAR[]"),
        ("query_ids", "VARCHAR[]"),
    ]
    assert [r[0] for r in described][:-1] == list(SCORE_COLUMNS)
    assert FUNDING_TABLE in WRITABLE_TABLES


def test_ut04_84_binds_and_rendered_size() -> None:
    """UT04-84 funding_score uses only typed binds and renders under the evidence SQL cap."""
    rendered = render_named("funding_score", {}, {**step_context().binds(), "unconfirmed": False})
    assert len(rendered.sql) < EVIDENCE_SQL_CAP
    assert set(rendered.bind) == {
        *("as_of", "as_of_ts", "tz", "s_window_days", "unconfirmed"),
        *("w_engineer_hour", "w_hours_per_point", "w_cf_effort_hours"),
        *("w_er_epic", "w_er_feature", "w_er_initiative", "w_er_cluster_fix"),
        *("w_er_override_k", "w_er_override_v"),
        *("w_sw_portfolio_k", "w_sw_portfolio_v", "w_sw_org_k", "w_sw_org_v"),
        *("w_sw_default", "w_sw_clip_min", "w_sw_clip_max"),
    }
    assert rendered.template == {"name": "funding_score"}


# --- UT04-85 WSJF --------------------------------------------------------------------------------


def _week(t: int) -> str:
    """Wednesday noon of week t (0..11) of the 12 complete weeks before as_of."""
    return f"{datetime.date(2026, 1, 7) + datetime.timedelta(weeks=t)} 12:00:00-05"


def test_ut04_85_fibonacci_mapping_p1_step_and_wsjf(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-85 BV, TC (Theil-Sen, P1 step), RR and JS on the Fibonacci scale; WSJF."""
    items = [
        item("A", "PAY-A", estimate=1000),
        item("B", "PAY-B", estimate=2000),
        item("C", "PAY-C", estimate=3000),
        item("D", "PAY-D", estimate=4000),
        item("E", "PAY-E", estimate=5000, service="S9"),
    ]
    p3 = {"priority": 3}
    old = {"priority": 3, "opened": "2025-12-03 12:00:00-05"}
    links: list[tuple[str, str, dict[str, Any]]] = [("IA", "PAY-A", {"opened": _week(11)})]
    links += [(f"IB{t}", "PAY-B", {**p3, "opened": _week(t)}) for t in range(6, 12)]
    links += [(f"IC{t}", "PAY-C", {**p3, "opened": _week(t)}) for t in range(6)]
    links += [(f"ID{n}", "PAY-D", old) for n in range(3)]
    links += [("IE0", "PAY-E", old)]
    blocks = [{"from_key": "PAY-A", "to_key": k, "link_type": "blocks"} for k in ("PAY-B", "PAY-C")]
    got = build(closing, graph(items, links, core__work_item_link=blocks))
    # BV: 2500, 1500, 1500, 750, 250 -> 20, 5, 5, 2, 1.
    # TC slopes: A 0 (one week), B > 0, C < 0, D = E = 0 -> 2, 20, 1, 2, 2; A has a P1 in
    # the last 30 days: 2 -> 3. RR: A blocks 2, E 5 - criticality 1 = 4 -> 8, 1, 1, 1, 20.
    # JS: 1, 2, 5, 8, 20.
    assert {k: r["wsjf"] for k, r in got.items()} == {
        "A": pytest.approx((20 + 3 + 8) / 1),
        "B": pytest.approx((5 + 20 + 1) / 2),
        "C": pytest.approx((5 + 1 + 1) / 5),
        "D": pytest.approx((2 + 2 + 1) / 8),
        "E": pytest.approx((1 + 2 + 20) / 20),
    }


def test_ut04_85_fibonacci_breakpoints(closing: list[duckdb.DuckDBPyConnection]) -> None:
    """UT04-85 percent ranks k/20 map to 1, 2, 3, 5, 8, 13, 20 at .15, .30 ... .90."""
    items = [item(f"W{k:02d}", f"PAY-{k:02d}", estimate=100 * (k + 1)) for k in range(21)]
    got = build(closing, graph(items, []))
    # BV, TC and RR all tie at percent rank 0 (fib 1): wsjf = 3 / JS.
    js = [round(3 / got[f"W{k:02d}"]["wsjf"]) for k in range(21)]
    assert js == [1, 1, 1, 1, 2, 2, 2, 3, 3, 3, 5, 5, 5, 8, 8, 8, 13, 13, 13, 20, 20]


# --- UT04-87 parent with child candidate ---------------------------------------------------------


def test_ut04_87_parent_pain_is_own_plus_child(closing: list[duckdb.DuckDBPyConnection]) -> None:
    """UT04-87 a parent candidate's pain, incidents and points include its child candidate."""
    items = [
        item("P", "PAY-P", kind="initiative", points=3),
        item("C", "PAY-C", parent="PAY-P", points=2),
    ]
    rows = graph(items, [("I1", "PAY-C", {}), ("I2", "PAY-P", {"minutes": 120})])
    got = build(closing, rows)
    assert (got["C"]["annual_pain_usd"], got["P"]["annual_pain_usd"]) == (
        Decimal("10000.00"),
        Decimal("30000.00"),
    )
    assert (got["C"]["n_incidents"], got["P"]["n_incidents"]) == (1, 2)
    assert (got["C"]["effort_cost_usd"], got["P"]["effort_cost_usd"]) == (
        Decimal("1200.00"),
        Decimal("3000.00"),
    )
    assert got["P"]["addressable_pain_usd"] == Decimal("7500.00")  # initiative 0.25


def test_ut04_80_expected_reduction_override(closing: list[duckdb.DuckDBPyConnection]) -> None:
    """UT04-80 addressable = annual x expected reduction; an override by key wins."""
    w = tiny_weights()
    er = w.expected_reduction.model_copy(update={"overrides": {"PAY-C": 0.6}})
    weights = w.model_copy(update={"expected_reduction": er})
    items = [item("P", "PAY-P", kind="initiative"), item("C", "PAY-C", parent="PAY-P")]
    got = build(closing, graph(items, [("I1", "PAY-C", {})]), weights)
    assert (got["C"]["expected_reduction"], got["P"]["expected_reduction"]) == (
        pytest.approx(0.6),
        pytest.approx(0.25),
    )
    assert got["C"]["addressable_pain_usd"] == Decimal("6000.00")
