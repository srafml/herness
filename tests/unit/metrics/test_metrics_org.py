"""Tests for the org score (impl 04 U04-67, U04-68, U04-35 `team_bucket`/`observed_days`; T04-16).

Each test builds an empty `metrics_tiny` warehouse (spec 02 DDL, one `building` build), adds
teams, orgs and `metrics.metric_value` rows of its own, and runs `run_org_step`. `as_of` is
2026-04-01 (UTC), so the 12 complete weeks run from 2026-01-05 to 2026-03-30 (exclusive).
"""

import functools
import math
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Final

import duckdb
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.metrics_org_oracle import composite, robust_scale, robust_z, theil_sen
from tests.support.metrics_tiny import BUILD_ID, build_metrics_tiny, shipped_catalog, tiny_weights

from herness.core.errors import SchemaViolation
from herness.metrics.catalog import MetricCatalog
from herness.metrics.context import StepContext
from herness.metrics.org import run_org_step
from herness.metrics.render import RenderState, make_environment

pytestmark = pytest.mark.unit

AS_OF: Final = date(2026, 4, 1)
W0: Final = date(2026, 1, 5)
W_END: Final = date(2026, 3, 30)
MTTR: Final = "mttr_hours"  # better: lower
EPIC: Final = "epic_predictability"  # better: higher
_WEIGHTS: Final = tiny_weights()
_MV_DDL: Final = (
    "CREATE TABLE metrics.metric_value (metric VARCHAR NOT NULL, entity_type VARCHAR NOT NULL,"
    " entity_id VARCHAR NOT NULL, period VARCHAR NOT NULL, period_start DATE NOT NULL,"
    " value DOUBLE, numerator DOUBLE, denominator DOUBLE, sample_size BIGINT NOT NULL,"
    " unit VARCHAR NOT NULL, flags VARCHAR[] NOT NULL, query_id VARCHAR NOT NULL)"
)
_COLUMNS: Final = (
    "entity_type VARCHAR, entity_id VARCHAR, metric VARCHAR, value DOUBLE, peer_group VARCHAR,"
    " peer_median DOUBLE, z_score DOUBLE, trend_slope DOUBLE, sample_size BIGINT,"
    " composite DOUBLE, rank BIGINT, unconfirmed BOOLEAN, flags VARCHAR[], query_ids VARCHAR[]"
)


@functools.cache
def _base() -> MetricCatalog:
    return shipped_catalog()


def _catalog(
    metrics: Mapping[str, float],
    *,
    min_peer_group: int = 2,
    trend_weight: float = 0.5,
    min_weight_coverage: float = 0.5,
) -> MetricCatalog:
    """The shipped catalog with this scorecard (model_copy: no range validation)."""
    cfg = _base().config
    org = cfg.scoring.org.model_copy(
        update={
            "metrics": dict(metrics),
            "min_peer_group": min_peer_group,
            "trend_weight": trend_weight,
            "min_weight_coverage": min_weight_coverage,
        }
    )
    scoring = cfg.scoring.model_copy(update={"org": org})
    return MetricCatalog(cfg.model_copy(update={"scoring": scoring}))


def _ctx(catalog: MetricCatalog) -> StepContext:
    return StepContext(BUILD_ID, catalog, _WEIGHTS, AS_OF, "UTC", frozenset())


WINDOW_START: Final = _ctx(_base()).binds()["window_start_date"]


def _warehouse(teams: Mapping[str, int | None]) -> duckdb.DuckDBPyConnection:
    """Empty tiny warehouse with `metric_value`, `org_closure` and the given teams.

    A team with a criticality owns one service of that criticality; None maps no service.
    """
    con = build_metrics_tiny(rows=False)
    con.execute(_MV_DDL)
    con.execute(
        "CREATE TABLE metrics.org_closure (org_id VARCHAR, ancestor_org_id VARCHAR, depth INTEGER)"
    )
    for team, crit in teams.items():
        con.execute(
            "INSERT INTO core.team (team_id, org_id, active) VALUES (?, 'O1', true)", [team]
        )
        if crit is not None:
            con.execute(
                "INSERT INTO core.service (service_id, criticality) VALUES (?, ?)",
                [f"S{team}", crit],
            )
            con.execute(
                "INSERT INTO core.service_map (service_id, team_id, role) VALUES (?, ?, 'owner')",
                [f"S{team}", team],
            )
    return con


def _mv(  # noqa: PLR0913 - one metric_value row
    con: duckdb.DuckDBPyConnection,
    metric: str,
    entity_id: str,
    value: float | None,
    *,
    entity_type: str = "team",
    period: str = "t12w",
    period_start: date | None = None,
    sample_size: int = 20,
    flags: Sequence[str] = (),
) -> None:
    start = WINDOW_START if period_start is None else period_start
    con.execute(
        "INSERT INTO metrics.metric_value VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, 'hours', ?,"
        " 'q_0000000000000000')",
        [metric, entity_type, entity_id, period, start, value, sample_size, list(flags)],
    )


def _rows(con: duckdb.DuckDBPyConnection, entity_type: str = "team") -> dict[tuple[str, str], Any]:
    cur = con.execute(
        "SELECT entity_id, metric, value, peer_group, peer_median, z_score, trend_slope,"
        " sample_size, composite, rank, unconfirmed, flags, query_ids FROM score.org"
        " WHERE entity_type = ? ORDER BY entity_id, metric",
        [entity_type],
    )
    names = [d[0] for d in cur.description]
    return {(r[0], r[1]): dict(zip(names, r, strict=True)) for r in cur.fetchall()}


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    c = _warehouse(dict.fromkeys(("A", "B", "C", "D", "E")))
    try:
        yield c
    finally:
        c.close()


# --- UT04-88 robust z ---------------------------------------------------------------------------


def test_ut04_88_duckdb_mad_is_unscaled() -> None:
    """UT04-88 VI04-06: DuckDB `mad()` is the unscaled median absolute deviation."""
    c = duckdb.connect()
    sql = "SELECT CAST(mad(x) AS DOUBLE) FROM (SELECT unnest(CAST(? AS DOUBLE[])) AS x)"
    assert c.execute(sql, [[1, 2, 3, 4, 100]]).fetchone() == (1.0,)
    assert c.execute(sql, [[1, 2, 4, 7]]).fetchone() == (1.5,)
    assert c.execute(sql, [[1, 1, 1, 5, 9]]).fetchone() == (0.0,)


def test_ut04_88_mad_zero_uses_meanad_fallback(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-88 MAD 0 with spread: denom = 1.2533 x mean |x - median|."""
    for team, x in zip("ABCDE", (1.0, 1.0, 1.0, 5.0, 9.0), strict=True):
        _mv(con, MTTR, team, x)
    result = run_org_step(con, _ctx(_catalog({MTTR: 1.0}, min_peer_group=5)))
    rows = _rows(con)
    denom = 1.2533 * 2.4
    assert result.row_counts == {"score.org": 5}
    assert rows["E", MTTR]["z_score"] == pytest.approx(8.0 / denom)
    assert rows["D", MTTR]["z_score"] == pytest.approx(4.0 / denom)
    assert rows["A", MTTR]["z_score"] == 0.0
    assert {r["peer_median"] for r in rows.values()} == {1.0}
    assert {r["peer_group"] for r in rows.values()} == {"team:crit_none"}
    assert rows["E", MTTR]["composite"] == pytest.approx(8.0 / denom)
    assert rows["E", MTTR]["flags"] == ["no_trend"]


def test_ut04_88_all_equal_gives_zero(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-88 all values equal: no spread, z = 0 and composite 0."""
    for team in "ABCDE":
        _mv(con, MTTR, team, 3.0)
    run_org_step(con, _ctx(_catalog({MTTR: 1.0})))
    rows = _rows(con)
    assert {(r["z_score"], r["composite"], r["peer_median"]) for r in rows.values()} == {
        (0.0, 0.0, 3.0)
    }


def test_ut04_88_z_stored_unclipped_badness_clipped(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-88 z_score keeps 97/1.4826; b clips to +5 (lower) and -5 (higher is better)."""
    for team, x in zip("ABCDE", (1.0, 2.0, 3.0, 4.0, 100.0), strict=True):
        _mv(con, MTTR, team, x)
        _mv(con, EPIC, team, x)
    run_org_step(con, _ctx(_catalog({MTTR: 0.15, EPIC: 0.05})))
    rows = _rows(con)
    assert rows["E", MTTR]["z_score"] == pytest.approx(97.0 / 1.4826)
    assert rows["E", EPIC]["z_score"] == pytest.approx(97.0 / 1.4826)
    assert rows["E", MTTR]["composite"] == pytest.approx((0.15 * 5 - 0.05 * 5) / 0.2)
    assert rows["A", MTTR]["composite"] == pytest.approx(
        (0.15 * -2 / 1.4826 + 0.05 * 2 / 1.4826) / 0.2
    )


# --- UT04-89 Theil-Sen trend --------------------------------------------------------------------


def test_ut04_89_theil_sen_slope_with_outlier(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-89 12 weeks of slope 2 with one outlier give 2.0; 5 points give NULL."""
    for team, x in zip("ABC", (1.0, 2.0, 4.0), strict=True):
        _mv(con, MTTR, team, x)
    for t in range(12):
        week = W0 + timedelta(weeks=t)
        _mv(con, MTTR, "A", 1000.0 if t == 5 else 2.0 * t + 3, period="week", period_start=week)
        if t < 5:
            _mv(con, MTTR, "B", 50.0 * t, period="week", period_start=week)
    # Outside [w0, w_end) on B (5 points in the window): either leak would make 6 points and a
    # non-NULL slope. A NULL weekly value is no point either.
    _mv(con, MTTR, "B", -999.0, period="week", period_start=W0 - timedelta(weeks=1))
    _mv(con, MTTR, "B", 999.0, period="week", period_start=W_END)
    _mv(con, MTTR, "B", None, period="week", period_start=W0 + timedelta(weeks=6))
    run_org_step(con, _ctx(_catalog({MTTR: 1.0})))
    rows = _rows(con)
    assert rows["A", MTTR]["trend_slope"] == 2.0
    assert rows["B", MTTR]["trend_slope"] is None
    assert rows["B", MTTR]["flags"] == ["no_trend"]
    assert rows["A", MTTR]["flags"] == []
    b = (1.0 - 2.0) / 1.4826
    assert rows["A", MTTR]["composite"] == pytest.approx(b + 0.5 * 5.0)  # trend_b clipped
    assert rows["B", MTTR]["composite"] == pytest.approx(0.0)


def test_ut04_89_trend_weight_comes_from_config(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-89 the composite uses `scoring.org.trend_weight` (2.0 here), not a fixed 0.5."""
    for team, x in zip("ABC", (1.0, 2.0, 4.0), strict=True):
        _mv(con, MTTR, team, x)
    for t in range(6):
        _mv(con, MTTR, "A", 0.1 * t, period="week", period_start=W0 + timedelta(weeks=t))
    run_org_step(con, _ctx(_catalog({MTTR: 1.0}, trend_weight=2.0)))
    row = _rows(con)["A", MTTR]
    trend_b = 0.1 * 12 / 1.4826
    assert row["trend_slope"] == pytest.approx(0.1)
    assert row["composite"] == pytest.approx(-1.0 / 1.4826 + 2.0 * trend_b)


def test_ut04_89_trend_without_spread_is_ignored(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-89 trend_b is NULL when denom is NULL, so the composite is b alone."""
    _mv(con, MTTR, "A", 2.0)
    _mv(con, MTTR, "B", 2.0)
    for t in range(6):
        _mv(con, MTTR, "A", float(t), period="week", period_start=W0 + timedelta(weeks=t))
    run_org_step(con, _ctx(_catalog({MTTR: 1.0})))
    row = _rows(con)["A", MTTR]
    assert (row["trend_slope"], row["composite"]) == (1.0, 0.0)


# --- UT04-90 coverage, flags, ranks and the step --------------------------------------------


def test_ut04_90_low_coverage_and_ranks(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-90 missing metrics below coverage: composite NULL, `low_coverage`; ranks."""
    for team, x in zip("ABCD", (1.0, 3.0, 2.0, 5.0), strict=True):
        _mv(con, EPIC, team, 0.5)
        if team != "D":
            _mv(con, MTTR, team, x)
    # a non-NULL value flagged insufficient_sample still gives x NULL
    _mv(con, MTTR, "D", 7.0, sample_size=3, flags=["insufficient_sample"])
    result = run_org_step(con, _ctx(_catalog({MTTR: 0.6, EPIC: 0.4})))
    rows = _rows(con)
    assert result.row_counts == {"score.org": 10}
    assert rows["D", MTTR]["z_score"] is None
    assert rows["B", MTTR]["z_score"] == pytest.approx(1.0 / 1.4826)  # group {1, 3, 2} only
    assert rows["E", MTTR]["flags"] == ["low_coverage", "no_data", "no_trend"]
    assert rows["E", MTTR]["sample_size"] == 0
    assert rows["D", MTTR]["flags"] == ["insufficient_sample", "low_coverage", "no_trend"]
    assert (rows["D", MTTR]["value"], rows["D", MTTR]["sample_size"]) == (None, 3)
    assert rows["D", EPIC]["flags"] == ["low_coverage", "no_trend"]
    assert rows["D", EPIC]["composite"] is None
    assert rows["D", EPIC]["value"] == 0.5
    ranks = {team: rows[team, MTTR]["rank"] for team in "ABCDE"}
    assert ranks == {"B": 1, "C": 2, "A": 3, "D": 4, "E": 5}
    assert all(r["rank"] == ranks[k[0]] for k, r in rows.items())
    composites = {rows[team, EPIC]["composite"] for team in "ABC"}
    assert len(composites) == 3
    assert all(r["unconfirmed"] is False for r in rows.values())


def test_ut04_90_coverage_at_threshold_is_enough(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-90 covered / total == min_weight_coverage keeps the composite (>=, not >)."""
    for team, x in zip("ABC", (1.0, 3.0, 2.0), strict=True):
        _mv(con, MTTR, team, x)
    _mv(con, EPIC, "B", 0.5)
    _mv(con, EPIC, "C", 0.5)
    run_org_step(con, _ctx(_catalog({MTTR: 0.5, EPIC: 0.5}, min_weight_coverage=0.5)))
    rows = _rows(con)
    assert rows["A", MTTR]["composite"] == pytest.approx(-1.0 / 1.4826)
    assert rows["A", EPIC]["flags"] == ["no_data", "no_trend"]
    run_org_step(con, _ctx(_catalog({MTTR: 0.5, EPIC: 0.5}, min_weight_coverage=0.6)))
    rows = _rows(con)
    assert rows["A", MTTR]["composite"] is None
    assert "low_coverage" in rows["A", MTTR]["flags"]


def test_ut04_90_step_records_one_query(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-90 score.org holds query_ids = [own]; one evidence row; rerun replaces."""
    _mv(con, MTTR, "A", 1.0)
    run_org_step(con, _ctx(_catalog({MTTR: 1.0})))
    first = con.execute("SELECT * FROM score.org").fetchall()
    result = run_org_step(con, _ctx(_catalog({MTTR: 1.0})))
    assert con.execute("SELECT * FROM score.org").fetchall() == first
    evidence = con.execute("SELECT query_id, producer, result_hash, row_count FROM meta.evidence")
    (qid, producer, digest, count), *rest = evidence.fetchall()
    assert rest == []
    assert (producer, count, len(digest)) == ("score", 5, 64)
    ids = con.execute("SELECT DISTINCT query_ids FROM score.org").fetchall()
    assert ids == [([qid],)]
    assert result.row_counts == {"score.org": 5}
    assert (result.warnings, result.flags, result.failed_checks) == ([], [], [])
    sql = con.execute("SELECT sql FROM meta.evidence").fetchone()
    assert sql is not None
    assert len(sql[0]) < 10_000
    described = con.execute("DESCRIBE score.org").fetchall()
    assert ", ".join(f"{r[0]} {r[1]}" for r in described) == _COLUMNS


def test_ut04_90_metric_value_missing_raises() -> None:
    """UT04-90 without metrics.metric_value the step raises SchemaViolation."""
    c = build_metrics_tiny(rows=False)
    try:
        with pytest.raises(SchemaViolation, match="metric_value missing; run step metrics first"):
            run_org_step(c, _ctx(_catalog({MTTR: 1.0})))
    finally:
        c.close()


def test_ut04_90_query_failure_is_schema_violation(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-90 a failing org query (org_closure missing) surfaces as SchemaViolation."""
    con.execute("DROP TABLE metrics.org_closure")
    with pytest.raises(SchemaViolation, match="org score failed"):
        run_org_step(con, _ctx(_catalog({MTTR: 1.0})))


# --- UT04-91 peer groups and the shared macros ----------------------------------------------


def test_ut04_91_team_buckets() -> None:
    """UT04-91 crit 1, 2 (support), 4 + 2 (minimum), 3, 5 and none: hi/hi/hi/lo/none/none."""
    c = _warehouse({"H1": 1, "LO": 3, "MX": 4, "N5": 5, "NO": None, "OFF": 1})
    c.execute("INSERT INTO core.service (service_id, criticality) VALUES ('S2', 2)")
    c.execute(
        "INSERT INTO core.service_map VALUES ('S2', 'H2', NULL, NULL, NULL, 'support', 'c', 1),"
        " ('S2', 'MX', NULL, NULL, NULL, 'support', 'c', 1)"
    )
    c.execute("INSERT INTO core.team (team_id, active) VALUES ('H2', true)")
    c.execute("UPDATE core.team SET active = false WHERE team_id = 'OFF'")
    try:
        for team in ("H1", "H2", "LO", "MX", "N5", "NO"):
            _mv(c, MTTR, team, 1.0)
        run_org_step(c, _ctx(_catalog({MTTR: 1.0}, min_peer_group=1)))
        groups = {k[0]: r["peer_group"] for k, r in _rows(c).items()}
        assert groups == {
            "H1": "team:crit_hi",
            "H2": "team:crit_hi",
            "LO": "team:crit_lo",
            "MX": "team:crit_hi",
            "N5": "team:crit_none",
            "NO": "team:crit_none",
        }
        run_org_step(c, _ctx(_catalog({MTTR: 1.0}, min_peer_group=4)))
        rows = _rows(c)
        assert {r["peer_group"] for r in rows.values()} == {"team:all"}
        assert all("peer_fallback" in r["flags"] for r in rows.values())
    finally:
        c.close()


def test_ut04_91_org_levels(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-91 orgs group by closure depth ('org:level<d>'), else 'org:all'."""
    con.execute("INSERT INTO core.org (org_id, parent_org_id) VALUES ('O1', NULL), ('O2', 'O1')")
    con.execute(
        "INSERT INTO metrics.org_closure VALUES ('O1', 'O1', 0), ('O2', 'O2', 0), ('O2', 'O1', 1)"
    )
    _mv(con, MTTR, "O1", 1.0, entity_type="org")
    _mv(con, MTTR, "O2", 3.0, entity_type="org")
    run_org_step(con, _ctx(_catalog({MTTR: 1.0}, min_peer_group=1)))
    rows = _rows(con, "org")
    assert [rows[o, MTTR]["peer_group"] for o in ("O1", "O2")] == ["org:level0", "org:level1"]
    assert [rows[o, MTTR]["rank"] for o in ("O1", "O2")] == [1, 2]
    run_org_step(con, _ctx(_catalog({MTTR: 1.0}, min_peer_group=2)))
    rows = _rows(con, "org")
    assert [rows[o, MTTR]["peer_group"] for o in ("O1", "O2")] == ["org:all", "org:all"]
    assert rows["O2", MTTR]["z_score"] == pytest.approx(1.0 / 1.4826)
    assert [rows[o, MTTR]["rank"] for o in ("O1", "O2")] == [2, 1]


def _macro_sql(name: str, *args: str) -> tuple[str, set[str]]:
    rs = RenderState()
    ctx = {"entity_type": "team", "period": "t12w", "filters": {}, "rs": rs}
    module = make_environment().get_template("_macros.sql.j2").make_module(vars=ctx)
    return str(getattr(module, name)(*args)), rs.used


def test_ut04_91_team_bucket_macro(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-91 `team_bucket(expr)` renders a bind-free scalar subquery over owner/support rows."""
    sql, used = _macro_sql("team_bucket", "t.team_id")
    assert used == set()
    con.execute("INSERT INTO core.service (service_id, criticality) VALUES ('SX', 4)")
    con.execute(
        "INSERT INTO core.service_map (service_id, team_id, role) VALUES ('SX', 'A', 'support')"
    )
    con.execute(
        "INSERT INTO core.service_map (service_id, team_id, role) VALUES ('SX', 'B', 'other')"
    )
    got = con.execute(f"SELECT t.team_id, {sql} FROM core.team t ORDER BY 1").fetchall()  # noqa: S608
    assert dict(got) == {"A": "lo", "B": "none", "C": "none", "D": "none", "E": "none"}


def test_ut04_91_observed_days_macro(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-91 `observed_days()`: local days since the first non-excluded incident, 1..window."""
    sql, used = _macro_sql("observed_days")
    assert used == {"s_window_days", "tz", "as_of"}
    con.execute("CREATE TABLE metrics.incident_fact (opened_at TIMESTAMPTZ, excluded BOOLEAN)")
    binds = {"s_window_days": 365, "tz": "America/New_York", "as_of": AS_OF}

    def days() -> int:
        row = con.execute(f"SELECT {sql}", binds).fetchone()
        assert row is not None
        return int(row[0])

    assert days() == 365  # no incidents: least() ignores the NULL difference
    con.execute(
        "INSERT INTO metrics.incident_fact VALUES (TIMESTAMPTZ '2026-04-01 02:00:00+00', false)"
    )
    assert days() == 1  # 2026-03-31 local, one day before as_of
    con.execute(
        "INSERT INTO metrics.incident_fact VALUES (TIMESTAMPTZ '2024-01-01 00:00:00+00', true)"
    )
    con.execute(
        "INSERT INTO metrics.incident_fact VALUES (TIMESTAMPTZ '2026-03-22 12:00:00+00', false)"
    )
    assert days() == 10
    con.execute(
        "INSERT INTO metrics.incident_fact VALUES (TIMESTAMPTZ '2025-01-01 12:00:00+00', false)"
    )
    assert days() == 365
    con.execute("DELETE FROM metrics.incident_fact")
    con.execute(
        "INSERT INTO metrics.incident_fact VALUES (TIMESTAMPTZ '2026-04-01 12:00:00+00', false)"
    )
    assert days() == 1  # same day: floored at 1


# --- PT04-07 oracle -----------------------------------------------------------------------------

_TEAMS: Final = tuple(f"T{i}" for i in range(6))
_value = st.one_of(st.none(), st.integers(0, 400).map(lambda n: n / 4))
_series = st.dictionaries(st.integers(0, 11), st.integers(-200, 200).map(lambda n: n / 2))


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    xs=st.lists(_value, min_size=len(_TEAMS), max_size=len(_TEAMS)),
    series=st.lists(_series, min_size=len(_TEAMS), max_size=len(_TEAMS)),
    lower=st.booleans(),
)
def test_pt04_07_robust_z_and_theil_sen_match_oracle(
    xs: list[float | None], series: list[dict[int, float]], *, lower: bool
) -> None:
    """PT04-07 robust z, Theil-Sen slope and composite equal the pure-Python oracle."""
    metric = MTTR if lower else EPIC
    c = _warehouse(dict.fromkeys(_TEAMS))
    try:
        for team, x, points in zip(_TEAMS, xs, series, strict=True):
            if x is not None:
                _mv(c, metric, team, x)
            for t, v in points.items():
                _mv(c, metric, team, v, period="week", period_start=W0 + timedelta(weeks=t))
        run_org_step(c, _ctx(_catalog({metric: 1.0})))
        rows = _rows(c)
    finally:
        c.close()
    med, denom = robust_scale([x for x in xs if x is not None])
    for team, x, points in zip(_TEAMS, xs, series, strict=True):
        row = rows[team, metric]
        slope = theil_sen(points)
        expected = (
            med,
            robust_z(x, med, denom),
            slope,
            composite(x, med, denom, slope, sign=1.0 if lower else -1.0, trend_weight=0.5),
        )
        got = (row["peer_median"], row["z_score"], row["trend_slope"], row["composite"])
        for e, g in zip(expected, got, strict=True):
            assert (e is None) == (g is None), (team, expected, got)
            if e is not None and g is not None:
                assert math.isclose(e, g, rel_tol=1e-9, abs_tol=1e-9), (team, expected, got)
