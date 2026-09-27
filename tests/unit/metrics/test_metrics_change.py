"""Tests for the change and monitoring catalog metrics #15-#19, #27, #28 (impl 04 U04-48,
UT04-50 … UT04-54, UT04-62, UT04-63) and the `period_end_date` macro (U04-35).

`metrics_tiny` changes in quarter 2026Q1 (business timezone America/New_York): CH1 (normal,
S1/T1, lead 6 h, failed through I3's 0.8 link), CH2 (emergency, S1/T1, lead 2 h, unsuccessful),
CH3 (standard, S1/T1, no opened_at), CH4 (normal, S2/T2, lead 24 h, failed through I3's 0.9
link); CH5 has no actual_end and CH6 is canceled, so neither is deployed. T1 belongs to O2 and
T2 to O3 (O3 under O2 under O1). I2's 0.5 link to CH1 is below `change_link_min_score` 0.7.

`core.metric_daily` (S1 in O2, S2 in O3): availability (date, service) pairs 01-05/S1 = avg(99,
98) = 98.5, 01-06/S1 = 100, 01-05/S2 = 97; request counts 1000, 3000 and max(1000, 500) = 1000;
error rates 0.02, 0.01 and 0.05.

The default quarter window is [2024-04-01, 2026-04-01) (as_of 2026-04-01), so 2026Q1 holds 90
days = 90/7 weeks. Every test runs `compute_metric` at quarter for every declared grain, with
and without a filter set that keeps every row. Where the fixture holds fewer records than the
shipped `min_sample_size`, the test first shows the shipped result (value NULL,
`insufficient_sample`) and then lowers the minimum to check the hand-computed value.
"""

import dataclasses
import datetime
from collections.abc import Iterator, Mapping
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
from freezegun import freeze_time
from tests.support.metrics_tiny import (
    BUILD_ID,
    build_metrics_tiny,
    patch_facts_config,
    shipped_catalog,
    tiny_weights,
)

from herness.metrics import compute
from herness.metrics.catalog import MetricCatalog
from herness.metrics.compute import MetricResult, compute_metric
from herness.metrics.facts import materialize_facts
from herness.metrics.render import RenderState, make_environment

pytestmark = pytest.mark.unit

Q1 = datetime.date(2026, 1, 1)
WEEKS = 90 / 7
CHANGE_GRAINS = {"service": ["S1", "S2"], "team": ["T1", "T2"], "org": ["O1", "O2", "O3"]}
DAILY_GRAINS = {"service": ["S1", "S2"], "org": ["O1", "O2", "O3"]}
CHANGE_FILTERS: dict[str, list[object]] = {
    "service_id": ["S1", "S2"],
    "team_id": ["T1", "T2"],
    "org_id": ["O1"],
    "change_type": ["emergency", "normal", "standard"],
}
# S2 has no owner team, so a team_id filter drops it (checked separately).
DAILY_FILTERS: dict[str, list[object]] = {"service_id": ["S1", "S2"], "org_id": ["O1"]}
SQL_CAP = 20_000  # Evidence.sql limit (to_evidence)
DROP_RC = (
    "DELETE FROM core.metric_daily WHERE metric_name = 'request_count' AND date = DATE '2026-01-06'"
)


@dataclasses.dataclass(frozen=True)
class Expect:
    """Hand-computed columns of an entity's one Q1 row."""

    value: float | None
    numerator: float | None
    denominator: float | None
    sample_size: int
    flags: list[str] = dataclasses.field(default_factory=list)


def _use(monkeypatch: pytest.MonkeyPatch, catalog: MetricCatalog) -> None:
    monkeypatch.setattr(compute, "catalog_from_config", lambda: catalog)


@pytest.fixture
def tiny(monkeypatch: pytest.MonkeyPatch) -> Iterator[duckdb.DuckDBPyConnection]:
    """`metrics_tiny` with materialized facts; compute reads the shipped catalog."""
    patch_facts_config(monkeypatch)
    con = build_metrics_tiny()
    with freeze_time("2026-04-01 06:30:00"):
        materialize_facts(con, BUILD_ID)
    cfg = SimpleNamespace(weights=tiny_weights())
    monkeypatch.setattr(compute, "get_config", lambda: cfg)
    _use(monkeypatch, shipped_catalog())
    yield con
    con.close()


def _approx(value: float | None) -> object:
    return None if value is None else pytest.approx(value, rel=1e-9)


def _assert_row(row: Any, expect: Expect) -> None:
    assert row.value == _approx(expect.value)
    assert row.numerator == _approx(expect.numerator)
    assert row.denominator == _approx(expect.denominator)
    assert row.sample_size == expect.sample_size
    assert row.flags == expect.flags


def _check(
    con: duckdb.DuckDBPyConnection,
    name: str,
    expect: Mapping[str, Expect],
    grains: dict[str, list[str]],
    filters: dict[str, list[object]],
) -> list[MetricResult]:
    """Every grain at quarter, unfiltered and with `filters`: one Q1 row per entity with the
    expected columns and flags; evidence SQL under the cap."""
    metric = compute.catalog_from_config().get(name)
    assert sorted(metric.grains) == sorted(grains)
    assert set(filters) <= set(metric.filters)
    results: list[MetricResult] = []
    for grain, ids in grains.items():
        for chosen in (None, filters):
            result = compute_metric(name, grain, None, "quarter", chosen, con=con)  # type: ignore[arg-type]
            assert [(r.entity_id, r.period_start) for r in result.rows] == [(i, Q1) for i in ids]
            for row in result.rows:
                _assert_row(row, expect[row.entity_id])
            assert result.recorded().to_evidence(None).query_id == result.query_id
            assert len(result.sql) < SQL_CAP
            results.append(result)
    return results


def _shipped_insufficient(
    con: duckdb.DuckDBPyConnection, name: str, grain: str, sizes: dict[str, int]
) -> None:
    """At the shipped min_sample_size every value is NULL and flagged; counts still reported."""
    rows = compute_metric(name, grain, None, "quarter", con=con).rows  # type: ignore[arg-type]
    assert {r.entity_id: r.sample_size for r in rows} == sizes
    assert all(r.value is None and "insufficient_sample" in r.flags for r in rows)


def _by_entity(**groups: Expect) -> dict[str, Expect]:
    """Expectations keyed by entity id; a key like `S1_T1` names several entities."""
    return {eid: exp for key, exp in groups.items() for eid in key.split("_")}


# --- UT04-50 change_count and period_end_date --------------------------------------------------


@pytest.mark.parametrize("period", ["week", "month", "quarter"])
def test_ut04_50_period_end_date_calendar(period: str) -> None:
    """UT04-50 period_end_date(ps) is the start of the next calendar period (U04-35)."""
    rs = RenderState()
    ctx = {"entity_type": "service", "period": period, "filters": {}, "rs": rs}
    mod = make_environment().get_template("_macros.sql.j2").make_module(vars=ctx)
    assert str(mod.period_end_date("ps")) == f"CAST(ps + INTERVAL 1 {period} AS DATE)"
    assert rs.used == set()


@pytest.mark.parametrize("period", ["t12w", "t12m"])
def test_ut04_50_period_end_date_rolling(period: str) -> None:
    """UT04-50 rolling periods end at the window end date (U04-35)."""
    rs = RenderState()
    ctx = {"entity_type": "service", "period": period, "filters": {}, "rs": rs}
    mod = make_environment().get_template("_macros.sql.j2").make_module(vars=ctx)
    assert str(mod.period_end_date("ps")) == "CAST($window_end_date AS DATE)"
    assert rs.used == {"window_end_date"}


def test_ut04_50_change_count_every_grain(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-50 change_count = deployed changes per week of 2026Q1 (90/7 weeks, DD04-05):
    3 on S1/T1, 1 on S2/T2, 4 in O1 and O2, 1 in O3; CH5 (no actual_end) and CH6 (canceled)
    are not deployed. Shipped minimum 1."""
    expect = _by_entity(
        S1_T1=Expect(3 / WEEKS, 3.0, WEEKS, 3),
        S2_T2_O3=Expect(1 / WEEKS, 1.0, WEEKS, 1),
        O1_O2=Expect(4 / WEEKS, 4.0, WEEKS, 4),
    )
    _check(tiny, "change_count", expect, CHANGE_GRAINS, CHANGE_FILTERS)


def test_ut04_50_change_count_partial_and_rolling_windows(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-50 the week denominator is the period cut to the window: a custom month window
    [2026-01-05, 2026-03-01) gives January 27 days (CH2: 7/27, partial_period) and February 28
    days (CH3, CH4: 2/4); t12w [2026-01-07, 2026-04-01) is 12 weeks with CH2-CH4: 3/12."""
    window = (datetime.date(2026, 1, 5), datetime.date(2026, 3, 1))
    month = compute_metric("change_count", "org", ["O1"], "month", window=window, con=tiny)
    assert [(r.period_start, r.flags) for r in month.rows] == [
        (datetime.date(2026, 1, 1), ["partial_period"]),
        (datetime.date(2026, 2, 1), []),
    ]
    assert [(r.value, r.numerator, r.denominator) for r in month.rows] == [
        (_approx(7 / 27), 1.0, _approx(27 / 7)),
        (_approx(0.5), 2.0, _approx(4.0)),
    ]
    rolling = compute_metric("change_count", "org", ["O1"], "t12w", con=tiny)
    assert [(r.period_start, r.value, r.numerator, r.denominator) for r in rolling.rows] == [
        (datetime.date(2026, 1, 7), _approx(0.25), 3.0, _approx(12.0))
    ]


# --- UT04-51 … UT04-54 -------------------------------------------------------------------------


def test_ut04_51_change_failure_rate_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-51 change_failure_rate = failed / deployed: CH1 and CH4 fail through incident
    links, CH2 by outcome, so S1/T1 2/3, S2/T2/O3 1/1, O1/O2 3/4. Below the shipped minimum
    of 10 the value is NULL; an emergency-only filter keeps CH2 (1/1)."""
    _shipped_insufficient(tiny, "change_failure_rate", "team", {"T1": 3, "T2": 1})
    _use(monkeypatch, shipped_catalog(change_failure_rate=1))
    expect = _by_entity(
        S1_T1=Expect(2 / 3, 2.0, 3.0, 3),
        S2_T2_O3=Expect(1.0, 1.0, 1.0, 1),
        O1_O2=Expect(3 / 4, 3.0, 4.0, 4),
    )
    _check(tiny, "change_failure_rate", expect, CHANGE_GRAINS, CHANGE_FILTERS)
    emergency = compute_metric(
        "change_failure_rate", "team", None, "quarter", {"change_type": "emergency"}, con=tiny
    )
    assert [(r.entity_id, r.value, r.denominator) for r in emergency.rows] == [("T1", 1.0, 1.0)]


def test_ut04_52_change_caused_incident_count_every_grain(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-52 change_caused_incident_count counts distinct non-excluded incidents by opened_at,
    grouped by the causing change's entity: I3 (links 0.8 to CH1 and 0.9 to CH4) counts once
    per entity, also once in O1/O2 where both changes meet; I2's 0.5 link does not qualify.
    Adding caused_by_change_id CH2 to I1 adds one on S1/T1/O1/O2; the excluded I4 on CH3 adds
    nothing. The change filter applies to the causing change."""
    one = Expect(1.0, 1.0, None, 1)
    every = _by_entity(S1_T1_S2_T2_O1_O2_O3=one)
    _check(tiny, "change_caused_incident_count", every, CHANGE_GRAINS, CHANGE_FILTERS)
    tiny.execute("UPDATE core.incident SET caused_by_change_id = 'CH2' WHERE record_id = 'I1'")
    tiny.execute("UPDATE core.incident SET caused_by_change_id = 'CH3' WHERE record_id = 'I4'")
    expect = _by_entity(S1_T1_O1_O2=Expect(2.0, 2.0, None, 2), S2_T2_O3=one)
    _check(tiny, "change_caused_incident_count", expect, CHANGE_GRAINS, CHANGE_FILTERS)
    emergency = compute_metric(
        "change_caused_incident_count",
        "service",
        None,
        "quarter",
        {"change_type": "emergency"},
        con=tiny,
    )
    assert [(r.entity_id, r.value) for r in emergency.rows] == [("S1", 1.0)]


def test_ut04_53_change_lead_time_hours_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-53 change_lead_time_hours = median lead time of deployed changes with opened_at:
    S1/T1 median(6, 2) = 4.0 (CH3 has none: coverage 2/3, low_coverage), S2/T2/O3 24.0,
    O1/O2 median(2, 6, 24) = 6.0 (coverage 3/4, low_coverage). Below the shipped minimum of
    10 the value is NULL."""
    _shipped_insufficient(tiny, "change_lead_time_hours", "team", {"T1": 2, "T2": 1})
    _use(monkeypatch, shipped_catalog(change_lead_time_hours=1))
    expect = _by_entity(
        S1_T1=Expect(4.0, None, 2.0, 2, ["low_coverage"]),
        S2_T2_O3=Expect(24.0, None, 1.0, 1),
        O1_O2=Expect(6.0, None, 3.0, 3, ["low_coverage"]),
    )
    _check(tiny, "change_lead_time_hours", expect, CHANGE_GRAINS, CHANGE_FILTERS)


def test_ut04_54_emergency_change_ratio_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-54 emergency_change_ratio = deployed emergency / deployed: S1/T1 1/3, S2/T2/O3
    0/1, O1/O2 1/4. Below the shipped minimum of 10 the value is NULL."""
    _shipped_insufficient(tiny, "emergency_change_ratio", "team", {"T1": 3, "T2": 1})
    _use(monkeypatch, shipped_catalog(emergency_change_ratio=1))
    expect = _by_entity(
        S1_T1=Expect(1 / 3, 1.0, 3.0, 3),
        S2_T2_O3=Expect(0.0, 0.0, 1.0, 1),
        O1_O2=Expect(1 / 4, 1.0, 4.0, 4),
    )
    _check(tiny, "emergency_change_ratio", expect, CHANGE_GRAINS, CHANGE_FILTERS)


# --- UT04-62, UT04-63 ---------------------------------------------------------------------------


def test_ut04_62_availability_pct_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-62 availability_pct: service = mean of daily pairs (S1 (98.5 + 100) / 2 = 99.25,
    S2 97); org = request-weighted when every pair has a count: O1/O2 (98.5 x 1000 + 100 x
    3000 + 97 x 1000) / 5000 = 99.1, O3 97; sample_size = distinct days. Without the 01-06 S1
    count O1/O2 fall back to the plain mean 98.5 flagged unweighted. A team filter keeps the
    owner-mapped S1 only. Below the shipped minimum of 7 days the value is NULL."""
    _shipped_insufficient(tiny, "availability_pct", "service", {"S1": 2, "S2": 1})
    _use(monkeypatch, shipped_catalog(availability_pct=1))
    expect = _by_entity(
        S1=Expect(99.25, 198.5, 2.0, 2),
        S2=Expect(97.0, 97.0, 1.0, 1),
        O1_O2=Expect(99.1, 495500.0, 5000.0, 2),
        O3=Expect(97.0, 97000.0, 1000.0, 1),
    )
    _check(tiny, "availability_pct", expect, DAILY_GRAINS, DAILY_FILTERS)
    team = compute_metric("availability_pct", "org", ["O1"], "quarter", {"team_id": "T1"}, con=tiny)
    assert [(r.value, r.denominator, r.sample_size) for r in team.rows] == [
        (_approx(99.625), 4000.0, 2)
    ]
    tiny.execute(DROP_RC)
    expect = {
        **expect,
        **_by_entity(O1_O2=Expect(98.5, 295.5, 3.0, 2, ["unweighted"])),
    }
    _check(tiny, "availability_pct", expect, DAILY_GRAINS, DAILY_FILTERS)


def test_ut04_63_error_rate_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-63 error_rate is request-weighted at every grain: S1 (0.02 x 1000 + 0.01 x 3000) /
    4000 = 0.0125, S2/O3 0.05, O1/O2 100 / 5000 = 0.02. Without the 01-06 S1 count S1 and
    O1/O2 fall back to plain means (0.015, 0.08 / 3) flagged unweighted. Below the shipped
    minimum of 7 days the value is NULL."""
    _shipped_insufficient(tiny, "error_rate", "service", {"S1": 2, "S2": 1})
    _use(monkeypatch, shipped_catalog(error_rate=1))
    weighted_s2 = Expect(0.05, 50.0, 1000.0, 1)
    expect = _by_entity(
        S1=Expect(0.0125, 50.0, 4000.0, 2),
        S2_O3=weighted_s2,
        O1_O2=Expect(0.02, 100.0, 5000.0, 2),
    )
    _check(tiny, "error_rate", expect, DAILY_GRAINS, DAILY_FILTERS)
    tiny.execute(DROP_RC)
    expect = _by_entity(
        S1=Expect(0.015, 0.03, 2.0, 2, ["unweighted"]),
        S2_O3=weighted_s2,
        O1_O2=Expect(0.08 / 3, 0.08, 3.0, 2, ["unweighted"]),
    )
    _check(tiny, "error_rate", expect, DAILY_GRAINS, DAILY_FILTERS)
