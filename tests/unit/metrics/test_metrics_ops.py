"""Tests for the ops, monitoring and cost catalog metrics #5-#14 (impl 04 U04-48, UT04-40-49).

`metrics_tiny` (design 04 §10.1): incidents I1-I3 of team T1 (org O2 under O1), service S1
(criticality 1) and cluster C1 resolve in 2, 4 and 6 hours with business durations 7200, 14400
and 21600 s; I4 is canceled (excluded). I1 is acknowledged after 15 minutes, I2 after 45; I3's
acknowledgement precedes its opening and counts as missing. Six S1 events (owner team T1) fall
in quarter 2026Q1: five with a noise severity, three of those without an incident, one `info`.
Weights are §7.2 with `cost_per_downtime_hour[1] = 10000` and `cost_per_engineer_hour = 100`.

Every test runs `compute_metric` at quarter for every declared grain, with and without the
metric's full filter set. Where the fixture holds fewer records than the shipped
`min_sample_size`, the test first shows the shipped result (value NULL, `insufficient_sample`)
and then lowers the minimum to check the hand-computed value.
"""

import dataclasses
import datetime
from collections.abc import Iterator
from types import SimpleNamespace

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

pytestmark = pytest.mark.unit

Q1 = datetime.date(2026, 1, 1)
GRAINS = {"service": ["S1"], "team": ["T1"], "org": ["O1", "O2"], "cluster": ["C1"]}
NO_CLUSTER = {k: v for k, v in GRAINS.items() if k != "cluster"}
ALL_FILTERS: dict[str, list[object]] = {
    "priority": [1, 2, 3],
    "service_id": ["S1"],
    "team_id": ["T1"],
    "org_id": ["O1"],
    "cluster_id": ["C1"],
    "severity": ["critical", "info", "major", "minor", "warning"],
}
SQL_CAP = 20_000  # Evidence.sql limit (to_evidence)
UNCONFIRMED = ["unconfirmed_weights"]
ESTIMATE = ["estimate", "unconfirmed_weights"]


@dataclasses.dataclass(frozen=True)
class Expect:
    """Hand-computed columns of the one Q1 row every entity gets."""

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
    return None if value is None else pytest.approx(value, rel=1e-12)


def _check(
    con: duckdb.DuckDBPyConnection,
    name: str,
    expect: Expect,
    grains: dict[str, list[str]] = GRAINS,
) -> list[MetricResult]:
    """Every grain in `grains` at quarter, unfiltered and with every allowed filter: one Q1
    row per entity with the expected columns and flags; evidence SQL under the cap."""
    metric = compute.catalog_from_config().get(name)
    assert sorted(metric.grains) == sorted(grains)
    filters = {key: ALL_FILTERS[key] for key in metric.filters}
    results: list[MetricResult] = []
    for grain, ids in grains.items():
        for chosen in (None, filters):
            result = compute_metric(name, grain, None, "quarter", chosen, con=con)  # type: ignore[arg-type]
            assert [(r.entity_id, r.period_start) for r in result.rows] == [(i, Q1) for i in ids]
            for row in result.rows:
                assert row.value == _approx(expect.value)
                assert row.numerator == _approx(expect.numerator)
                assert row.denominator == _approx(expect.denominator)
                assert row.sample_size == expect.sample_size
                assert row.flags == expect.flags
            assert result.recorded().to_evidence(None).query_id == result.query_id
            assert len(result.sql) < SQL_CAP
            results.append(result)
    return results


def _shipped_insufficient(con: duckdb.DuckDBPyConnection, name: str, n: int) -> None:
    """At the shipped min_sample_size the value is NULL and flagged; counts still reported."""
    (row,) = compute_metric(name, "team", None, "quarter", con=con).rows
    assert row.value is None
    assert row.sample_size == n
    assert "insufficient_sample" in row.flags


# --- UT04-40 … UT04-49 -------------------------------------------------------------------------


def test_ut04_40_mttr_business_hours_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-40 mttr_business_hours = mean(2, 4, 6) = 4.0 business hours (coverage 1) at every
    grain; below the shipped minimum of 10 the value is NULL. Dropping I3's business duration
    gives 3.0 over 2 with coverage 2/3 < 0.8, flagged low_coverage."""
    _shipped_insufficient(tiny, "mttr_business_hours", 3)
    _use(monkeypatch, shipped_catalog(mttr_business_hours=2))
    _check(tiny, "mttr_business_hours", Expect(4.0, 12.0, 3.0, 3))
    tiny.execute("UPDATE metrics.incident_fact SET resolve_bh = NULL WHERE record_id = 'I3'")
    _check(tiny, "mttr_business_hours", Expect(3.0, 6.0, 2.0, 2, ["low_coverage"]))


def test_ut04_41_mtta_minutes_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-41 mtta_minutes = mean(15, 45) = 30.0 minutes over the two valid acknowledgements
    (I3's precedes its opening: NULL); coverage 2/3 < 0.8 flags low_coverage. Service, team and
    org grains only; below the shipped minimum of 10 the value is NULL."""
    _shipped_insufficient(tiny, "mtta_minutes", 2)
    _use(monkeypatch, shipped_catalog(mtta_minutes=2))
    _check(tiny, "mtta_minutes", Expect(30.0, 60.0, 2.0, 2, ["low_coverage"]), NO_CLUSTER)


def test_ut04_42_customer_impact_minutes_every_grain(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-42 customer_impact_minutes = 90 + 60 = 150 (I2 is P3, above the fallback's
    max_priority 2: no impact, estimated_count 0) at the shipped settings; an estimated impact
    row adds the estimate flag."""
    _check(tiny, "customer_impact_minutes", Expect(150.0, 150.0, None, 3, UNCONFIRMED))
    tiny.execute("UPDATE metrics.incident_fact SET impact_estimated = true WHERE record_id = 'I2'")
    _check(tiny, "customer_impact_minutes", Expect(150.0, 150.0, None, 3, ESTIMATE))


def test_ut04_43_repeat_incident_rate_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-43 repeat_incident_rate = 1/3 (I2 is 15 days after I1; I3 is 36 days after I2);
    below the shipped minimum of 20 the value is NULL."""
    _shipped_insufficient(tiny, "repeat_incident_rate", 3)
    _use(monkeypatch, shipped_catalog(repeat_incident_rate=3))
    _check(tiny, "repeat_incident_rate", Expect(1 / 3, 1.0, 3.0, 3))


def test_ut04_44_reopen_rate_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-44 reopen_rate = 1/3 (I2 reopened once); below the shipped minimum of 20 the value
    is NULL."""
    _shipped_insufficient(tiny, "reopen_rate", 3)
    _use(monkeypatch, shipped_catalog(reopen_rate=3))
    _check(tiny, "reopen_rate", Expect(1 / 3, 1.0, 3.0, 3))


def test_ut04_45_reassignment_rate_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-45 reassignment_rate = 2/3 (I1 and I3 reassigned); below the shipped minimum of 20
    the value is NULL."""
    _shipped_insufficient(tiny, "reassignment_rate", 3)
    _use(monkeypatch, shipped_catalog(reassignment_rate=3))
    _check(tiny, "reassignment_rate", Expect(2 / 3, 2.0, 3.0, 3))


def test_ut04_46_sla_breach_rate_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-46 sla_breach_rate = 1/3 (I1 breached); below the shipped minimum of 20 the value
    is NULL. An incident with unknown sla_breached leaves the population."""
    _shipped_insufficient(tiny, "sla_breach_rate", 3)
    _use(monkeypatch, shipped_catalog(sla_breach_rate=2))
    _check(tiny, "sla_breach_rate", Expect(1 / 3, 1.0, 3.0, 3))
    tiny.execute("UPDATE metrics.incident_fact SET sla_breached = NULL WHERE record_id = 'I3'")
    _check(tiny, "sla_breach_rate", Expect(1 / 2, 1.0, 2.0, 2))


def test_ut04_47_alert_noise_ratio_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-47 alert_noise_ratio = 3/5: five S1 events with a noise severity, three without an
    incident; the info event is outside the population. Team via the owner service_map row,
    org via the service's org (O2 under O1). Below the shipped minimum of 50 the value is NULL."""
    _shipped_insufficient(tiny, "alert_noise_ratio", 5)
    _use(monkeypatch, shipped_catalog(alert_noise_ratio=5))
    _check(tiny, "alert_noise_ratio", Expect(3 / 5, 3.0, 5.0, 5), NO_CLUSTER)
    major = compute_metric(
        "alert_noise_ratio", "team", None, "quarter", {"severity": "major"}, con=tiny
    )
    assert [(r.value, r.numerator, r.denominator) for r in major.rows] == [(None, 1.0, 1.0)]


def test_ut04_48_toil_hours_est_every_grain(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-48 toil_hours_est = 2 x 1.5 + 4 x 0.2 + 6 x 0.5 = 6.8 hours at the shipped settings
    (minimum 1); estimate and unconfirmed_weights (toil) flags."""
    _check(tiny, "toil_hours_est", Expect(6.8, 6.8, None, 3, ESTIMATE))


def test_ut04_49_incident_cost_usd_every_grain(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-49 incident_cost_usd = downtime 1.5 x 10000 x 1.0 + 1.0 x 10000 x 0.5 = 20000 plus
    toil 6.8 x 100 = 680, so 20680 at the shipped settings (minimum 1); estimate flags."""
    results = _check(tiny, "incident_cost_usd", Expect(20680.0, 20680.0, None, 3, ESTIMATE))
    assert all(isinstance(r.rows[0].value, float) for r in results)
