"""Tests of order-independent DOUBLE aggregation in catalog metrics (impl 04 T04-23, UT04-123).

A request-weighted `error_rate` numerator `Σ v·rc` over four (date, service) pairs whose
exact sum lands on a `.9g` rounding halfway point: adding the same four products front to
back or back to front gives two doubles one ulp apart whose `.9g` tokens differ
(748.433362 vs 748.433361). DuckDB's parallel `sum` adds in a run-dependent order, which is
how a re-run of a stored evidence query flipped its `result_hash` (merge batch 41). The
catalog's `dsum` / `davg` macros (U04-35) order the input by value, so the token and the
`result_hash` are the same for either row order.
"""

import datetime
from collections.abc import Iterator, Sequence
from decimal import Decimal
from types import SimpleNamespace

import duckdb
import pytest
from freezegun import freeze_time
from tests.support.metrics_funding import item as funding_item
from tests.support.metrics_funding import step_context
from tests.support.metrics_funding import warehouse as funding_warehouse
from tests.support.metrics_tiny import (
    BUILD_ID,
    build_metrics_tiny,
    patch_facts_config,
    shipped_catalog,
    tiny_weights,
)

from herness.metrics import compute
from herness.metrics._encode import encode_cell
from herness.metrics.compute import MetricResult, compute_metric
from herness.metrics.facts import materialize_facts
from herness.metrics.funding import run_funding_step

pytestmark = pytest.mark.unit

# Error rates rounded to 6 significant digits (as tools/synth/monitoring.py writes them) and
# integer request counts; Σ v·rc is 748.43336150 exactly at the 10th significant digit.
RATES = (0.0395151, 0.03897, 0.0350352, 0.0335369)
COUNTS = (7222.0, 3504.0, 3977.0, 5581.0)
DAYS = tuple(datetime.date(2026, 1, 5) + datetime.timedelta(days=i) for i in range(4))
FORWARD = (0, 1, 2, 3)
BACKWARD = (3, 2, 1, 0)
PLAIN_SUM = (
    "SELECT sum(m.v * m.rc) FROM (SELECT d.date,"
    " max(d.value) FILTER (WHERE d.metric_name = 'error_rate') AS v,"
    " max(d.value) FILTER (WHERE d.metric_name = 'request_count') AS rc"
    " FROM core.metric_daily d GROUP BY d.date) m"
)


def _token(value: object) -> str:
    return encode_cell(value, "DOUBLE")


def _sequential(order: Sequence[int]) -> float:
    total = 0.0
    for i in order:
        total += RATES[i] * COUNTS[i]
    return total


def _load(con: duckdb.DuckDBPyConnection, order: Sequence[int]) -> None:
    """Replace `core.metric_daily` with the four S1 pairs: day `k` holds pair `order[k]`, so
    the two orders hold the same products but group (and add) them in a different order."""
    con.execute("DELETE FROM core.metric_daily")
    for day, i in zip(DAYS, order, strict=True):
        for name, value in (("error_rate", RATES[i]), ("request_count", COUNTS[i])):
            con.execute(
                "INSERT INTO core.metric_daily VALUES (?, 'S1', ?, ?, 'ratio', 'datadog')",
                [day, name, value],
            )


@pytest.fixture
def tiny(monkeypatch: pytest.MonkeyPatch) -> Iterator[duckdb.DuckDBPyConnection]:
    """`metrics_tiny` with facts, one DuckDB thread (scan order = insertion order) and the
    shipped catalog with `error_rate` minimum sample 1."""
    patch_facts_config(monkeypatch)
    con = build_metrics_tiny()
    con.execute("SET threads = 1")
    with freeze_time("2026-04-01 06:30:00"):
        materialize_facts(con, BUILD_ID)
    cfg = SimpleNamespace(weights=tiny_weights())
    monkeypatch.setattr(compute, "get_config", lambda: cfg)
    catalog = shipped_catalog(error_rate=1)
    monkeypatch.setattr(compute, "catalog_from_config", lambda: catalog)
    yield con
    con.close()


def _error_rate(con: duckdb.DuckDBPyConnection, order: Sequence[int]) -> MetricResult:
    _load(con, order)
    return compute_metric("error_rate", "service", ["S1"], "quarter", con=con)


def test_ut04_123_halfway_sum_token_stable_across_aggregation_orders(
    tiny: duckdb.DuckDBPyConnection,
) -> None:
    """UT04-123 a halfway-point Σ v·rc gives different `.9g` tokens when added in two orders
    (Python and DuckDB's unordered `sum`), but the catalog `error_rate` (U04-48 #28 with the
    U04-35 `dsum`/`davg` macros) returns the same numerator token, value and `result_hash`
    for both row orders."""
    assert _token(_sequential(FORWARD)) == "748.433362"
    assert _token(_sequential(BACKWARD)) == "748.433361"
    plain = []
    for order in (FORWARD, BACKWARD):
        _load(tiny, order)
        row = tiny.execute(PLAIN_SUM).fetchone()
        assert row is not None
        plain.append(_token(row[0]))
    assert plain[0] != plain[1]

    first, second = (_error_rate(tiny, order) for order in (FORWARD, BACKWARD))
    assert [(r.entity_id, r.sample_size) for r in first.rows] == [("S1", 4)]
    one, two = first.rows[0], second.rows[0]
    assert _token(one.numerator) == _token(two.numerator) == "748.433361"
    assert one.numerator == two.numerator
    assert one.value == two.value == one.numerator / sum(COUNTS)
    assert first.query_id == second.query_id
    assert first.result_hash == second.result_hash


# Remaining story points of the three stories under one epic. The doubles add to
# 0.00027499999999999996 front to back and 0.000275 back to front; funding effort is
# points x 100 USD/h x 6 h/point cast to DECIMAL(18, 2), and that product sits on a cent
# rounding halfway point, so the one-ulp difference moves the stored effort by a cent.
POINTS = (0.000025, 0.000075, 0.000175)


def _effort(order: Sequence[int]) -> object:
    """`effort_cost_usd` of an epic with no estimate whose stories are inserted in `order`."""
    stories = [
        funding_item(f"S{i}", f"PAY-{i + 2}", kind="story", parent="PAY-1", points=POINTS[i])
        for i in order
    ]
    con = funding_warehouse(
        {"core.work_item": [funding_item("E", "PAY-1", service="S1"), *stories]}
    )
    try:
        con.execute("SET threads = 1")
        run_funding_step(con, step_context())
        row = con.execute(
            "SELECT effort_cost_usd FROM score.funding WHERE candidate_id = 'E'"
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    return row[0]


def test_ut04_123_funding_effort_points_sum_stable_across_row_orders() -> None:
    """UT04-123 the `funding_score` remaining-points sum (`dsum`) gives one `effort_cost_usd`
    (0.16) whatever the story row order and run. The DOUBLE sums of the same points differ in
    the last ulp by addition order, and the plain `sum` ends up as 0.16 or 0.17 from run to
    run, so the check repeats every order four times."""
    forward = (POINTS[0] + POINTS[1]) + POINTS[2]
    backward = (POINTS[2] + POINTS[1]) + POINTS[0]
    assert forward != backward
    efforts = {_effort(order) for order in ((0, 1, 2), (0, 2, 1), (2, 1, 0)) * 4}
    assert efforts == {Decimal("0.16")}
