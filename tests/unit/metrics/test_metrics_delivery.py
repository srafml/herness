"""Tests for the delivery catalog metrics #20-#26 (impl 04 U04-48, UT04-55 … UT04-61).

`metrics_tiny` work items (business timezone America/New_York; T1/S1 in O2, T2/S2 in O3, O3
under O2 under O1). Types story/bug/task count; epics, the initiative W1 and the sub-task W4
never do.

| Item | Type, parent | Team | Created | in_progress → done | Points |
|------|--------------|------|---------|--------------------|--------|
| W3 | story, W2 (epic) | T1 | 12-03 | 12-15 → — | — |
| W5 | epic | T2 | 12-05 | 01-05 → 03-20 | — |
| W6 | story, W5 | T2 | 12-06 09:00 | — (todo) | — |
| W7 | epic, W1 | T1 | 12-12 | 01-10 → 03-01 | — |
| W8 | story, W5 | T2 | 12-10 | 12-20 → 02-10 (52 d) | 3 |
| W9 | bug, W7 | T1 | 12-15 09:00 | 02-15 → 02-20 (5 d) | 2 |
| W10 | task, W7 | T1 | 01-12 | — → 02-01 (no cycle) | — |
| W11 | story, W7 | T1 | 12-20 | 12-28 → 03-05 (67 d) | — |

W10 is unplanned through a `mentions_incident` link, W9 as a bug. The default quarter window
is [2024-04-01, 2026-04-01); the spine metrics (carryover, backlog, WIP) have rows for 2025Q4
(snapshot 2026-01-01 00:00 -05) and 2026Q1 (snapshot as_of 2026-04-01 00:00 -04). Every test
runs `compute_metric` at quarter for every declared grain, unfiltered and with a filter set
that keeps every row; below the shipped `min_sample_size` it first shows the shipped result
(value NULL, `insufficient_sample`) and then lowers the minimum for the hand-computed value.
"""

import dataclasses
import datetime
from collections.abc import Iterator, Mapping
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

Q4 = datetime.date(2025, 10, 1)
Q1 = datetime.date(2026, 1, 1)
SQL_CAP = 20_000  # Evidence.sql limit (to_evidence)
FILTERS: dict[str, list[object]] = {
    "service_id": ["S1", "S2"],
    "team_id": ["T1", "T2"],
    "org_id": ["O1"],
    "work_item_type": ["bug", "story", "task"],
}
EPIC_FILTERS: dict[str, list[object]] = {k: v for k, v in FILTERS.items() if k != "work_item_type"}
AGE_W6_Q1 = 116 - 10 / 24  # 2025-12-06 14:00Z → 2026-04-01 04:00Z

type Key = tuple[str, datetime.date]


@dataclasses.dataclass(frozen=True)
class Expect:
    """Hand-computed columns of one (entity, period) row."""

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


def _rows(**groups: Expect) -> dict[Key, Expect]:
    """Q1 expectations keyed by (entity, Q1); a key like `S1_T1` names several entities."""
    return {(eid, Q1): exp for key, exp in groups.items() for eid in key.split("_")}


def _q4(**groups: Expect) -> dict[Key, Expect]:
    """As `_rows`, for the 2025Q4 spine row."""
    return {(eid, Q4): exp for key, exp in groups.items() for eid in key.split("_")}


def _check(
    con: duckdb.DuckDBPyConnection,
    name: str,
    expect: Mapping[str, Mapping[Key, Expect]],
    filters: dict[str, list[object]],
) -> list[MetricResult]:
    """Every declared grain at quarter, unfiltered and with `filters`: exactly the expected
    (entity, period) rows with the expected columns and flags; evidence SQL under the cap."""
    metric = compute.catalog_from_config().get(name)
    assert sorted(metric.grains) == sorted(expect)
    assert set(filters) <= set(metric.filters)
    results: list[MetricResult] = []
    for grain, rows in expect.items():
        for chosen in (None, filters):
            result = compute_metric(name, grain, None, "quarter", chosen, con=con)  # type: ignore[arg-type]
            assert [(r.entity_id, r.period_start) for r in result.rows] == sorted(rows)
            for row in result.rows:
                exp = rows[(row.entity_id, row.period_start)]
                assert row.value == _approx(exp.value)
                assert row.numerator == _approx(exp.numerator)
                assert row.denominator == _approx(exp.denominator)
                assert row.sample_size == exp.sample_size
                assert row.flags == exp.flags
            assert result.recorded().to_evidence(None).query_id == result.query_id
            assert len(result.sql) < SQL_CAP
            results.append(result)
    return results


def _shipped_insufficient(
    con: duckdb.DuckDBPyConnection, name: str, grain: str, sizes: dict[Key, int]
) -> None:
    """At the shipped min_sample_size every value is NULL and flagged; counts still reported."""
    rows = compute_metric(name, grain, None, "quarter", con=con).rows  # type: ignore[arg-type]
    assert {(r.entity_id, r.period_start): r.sample_size for r in rows} == sizes
    assert all(r.value is None and "insufficient_sample" in r.flags for r in rows)


# --- UT04-55 throughput ------------------------------------------------------------------------


def test_ut04_55_throughput_every_grain(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-55 throughput counts story/bug/task items done in the period: W9, W10, W11 on
    S1/T1, W8 on S2/T2, 4 in O1/O2; the done epics W5 and W7 are not counted. At the
    work_item grain an item counts for every ancestor-or-self (W1 and W7: 3). Shipped minimum 1;
    a work_item_type filter keeps only the bug W9."""
    three, one = Expect(3.0, 3.0, None, 3), Expect(1.0, 1.0, None, 1)
    expect = {
        "service": _rows(S1=three, S2=one),
        "team": _rows(T1=three, T2=one),
        "org": _rows(O1_O2=Expect(4.0, 4.0, None, 4), O3=one),
        "work_item": _rows(W1_W7=three, W5_W8_W9_W10_W11=one),
    }
    _check(tiny, "throughput", expect, FILTERS)
    bug = compute_metric("throughput", "team", None, "quarter", {"work_item_type": "bug"}, con=tiny)
    assert [(r.entity_id, r.value, r.sample_size) for r in bug.rows] == [("T1", 1.0, 1)]


# --- UT04-56 cycle_time_days -------------------------------------------------------------------


def test_ut04_56_cycle_time_days_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-56 cycle_time_days = median cycle_days of items done in the period with a cycle:
    S1/T1 median(5, 67) = 36 (W10 has no in_progress transition, so no cycle), S2/T2/O3 52,
    O1/O2 median(5, 52, 67) = 52; denominator and sample_size count the items with a cycle.
    Below the shipped minimum of 10 the value is NULL."""
    _shipped_insufficient(tiny, "cycle_time_days", "team", {("T1", Q1): 2, ("T2", Q1): 1})
    _use(monkeypatch, shipped_catalog(cycle_time_days=1))
    t1, t2 = Expect(36.0, None, 2.0, 2), Expect(52.0, None, 1.0, 1)
    expect = {
        "service": _rows(S1=t1, S2=t2),
        "team": _rows(T1=t1, T2=t2),
        "org": _rows(O1_O2=Expect(52.0, None, 3.0, 3), O3=t2),
        "work_item": _rows(
            W1_W7=t1,
            W5_W8=t2,
            W9=Expect(5.0, None, 1.0, 1),
            W11=Expect(67.0, None, 1.0, 1),
        ),
    }
    _check(tiny, "cycle_time_days", expect, FILTERS)


# --- UT04-57 carryover_rate --------------------------------------------------------------------


def test_ut04_57_carryover_rate_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-57 carryover_rate over the period spine: items in progress at the start of 2026Q1
    are W3, W11 (T1) and W8 (T2); only W3 is not done at the period end, so T1 1/2, T2 0/1,
    O1/O2 1/3, O3 0/1. Below the shipped minimum of 10 the value is NULL."""
    _shipped_insufficient(tiny, "carryover_rate", "team", {("T1", Q1): 2, ("T2", Q1): 1})
    _use(monkeypatch, shipped_catalog(carryover_rate=1))
    none_of_one, one_of_one = Expect(0.0, 0.0, 1.0, 1), Expect(1.0, 1.0, 1.0, 1)
    half = Expect(0.5, 1.0, 2.0, 2)
    expect = {
        "team": _rows(T1=half, T2=none_of_one),
        "org": _rows(O1_O2=Expect(1 / 3, 1.0, 3.0, 3), O3=none_of_one),
        "work_item": _rows(W1=half, W2_W3=one_of_one, W5_W7_W8_W11=none_of_one),
    }
    _check(tiny, "carryover_rate", expect, FILTERS)


def test_ut04_57_carryover_rate_month_spine(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-57 at month the spine gives one row per month: January W3, W8, W11 all still open
    at 02-01 (3/3), February 2/3 (W8 done 02-10), March 1/2 (W11 done 03-05)."""
    _use(monkeypatch, shipped_catalog(carryover_rate=1))
    rows = compute_metric("carryover_rate", "org", ["O1"], "month", con=tiny).rows
    assert [(r.period_start, r.value, r.numerator, r.denominator) for r in rows] == [
        (datetime.date(2026, 1, 1), 1.0, 3.0, 3.0),
        (datetime.date(2026, 2, 1), _approx(2 / 3), 2.0, 3.0),
        (datetime.date(2026, 3, 1), 0.5, 1.0, 2.0),
    ]


# --- UT04-58 backlog_age_days ------------------------------------------------------------------


def test_ut04_58_backlog_age_days_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-58 backlog_age_days = median age of todo items at the snapshot: at 2026-01-01
    00:00 -05 W9 (bug, created 12-15 09:00 -05) is 16.625 days old and W6 25.625 (O1/O2 median
    21.125); at the as_of snapshot 2026-04-01 00:00 -04 only W6 is todo (116 d - 10 h). Items
    in progress (W3, W8, W11), epics and the sub-task are not backlog. Below the shipped minimum
    of 10 the value is NULL."""
    _shipped_insufficient(
        tiny, "backlog_age_days", "team", {("T1", Q4): 1, ("T2", Q4): 1, ("T2", Q1): 1}
    )
    _use(monkeypatch, shipped_catalog(backlog_age_days=1))
    w9, w6 = Expect(16.625, None, 1.0, 1), Expect(25.625, None, 1.0, 1)
    w6_q1 = Expect(AGE_W6_Q1, None, 1.0, 1)
    expect = {
        "service": {**_q4(S1=w9, S2=w6), **_rows(S2=w6_q1)},
        "team": {**_q4(T1=w9, T2=w6), **_rows(T2=w6_q1)},
        "org": {**_q4(O1_O2=Expect(21.125, None, 2.0, 2), O3=w6), **_rows(O1_O2_O3=w6_q1)},
        "work_item": {**_q4(W1_W7_W9=w9, W5_W6=w6), **_rows(W5_W6=w6_q1)},
    }
    _check(tiny, "backlog_age_days", expect, FILTERS)


# --- UT04-59 wip_count -------------------------------------------------------------------------


def test_ut04_59_wip_count_every_grain(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-59 wip_count = items in progress at the snapshot: 2026-01-01 W3, W11 (T1) and W8
    (T2); 2026-04-01 only W3 (W8, W11 done, W9 was in progress 02-15 … 02-20 only). Shipped
    minimum 1."""
    two, one = Expect(2.0, 2.0, None, 2), Expect(1.0, 1.0, None, 1)
    expect = {
        "team": {**_q4(T1=two, T2=one), **_rows(T1=one)},
        "org": {**_q4(O1_O2=Expect(3.0, 3.0, None, 3), O3=one), **_rows(O1_O2=one)},
        "work_item": {**_q4(W1=two, W2_W3_W5_W7_W8_W11=one), **_rows(W1_W2_W3=one)},
    }
    _check(tiny, "wip_count", expect, FILTERS)


def test_ut04_59_wip_count_rolling_snapshot(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-59 at t12w the spine is one row starting at the window start (2026-01-07) with the
    snapshot at as_of: W3 only."""
    rows = compute_metric("wip_count", "org", ["O1"], "t12w", con=tiny).rows
    assert [(r.period_start, r.value, r.sample_size) for r in rows] == [
        (datetime.date(2026, 1, 7), 1.0, 1)
    ]


# --- UT04-60 unplanned_work_ratio --------------------------------------------------------------


def test_ut04_60_unplanned_work_ratio_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-60 unplanned_work_ratio = unplanned / done story-bug-task items: the bug W9 and the
    incident-linked W10 on S1/T1 (2/3), none on S2/T2/O3 (0/1), O1/O2 2/4. Below the shipped
    minimum of 20 the value is NULL."""
    _shipped_insufficient(tiny, "unplanned_work_ratio", "team", {("T1", Q1): 3, ("T2", Q1): 1})
    _use(monkeypatch, shipped_catalog(unplanned_work_ratio=1))
    t1, t2 = Expect(2 / 3, 2.0, 3.0, 3), Expect(0.0, 0.0, 1.0, 1)
    expect = {
        "service": _rows(S1=t1, S2=t2),
        "team": _rows(T1=t1, T2=t2),
        "org": _rows(O1_O2=Expect(0.5, 2.0, 4.0, 4), O3=t2),
    }
    _check(tiny, "unplanned_work_ratio", expect, FILTERS)


# --- UT04-61 epic_predictability ---------------------------------------------------------------


def test_ut04_61_epic_predictability_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-61 epic_predictability (design 04 §5.2.1): W7 starts 01-10, so W9 (2 points, done
    02-20) and W11 (1 point, done 03-05 after W7's 03-01) are committed and W10 (created 01-12)
    is not: 2/3. W5 starts 01-05: W6 (1, open) and W8 (3, done) give 3/4. O1/O2 weight by
    committed points: 5/7 over 2 epics. Below the shipped minimum of 3 epics the value is
    NULL."""
    _shipped_insufficient(tiny, "epic_predictability", "team", {("T1", Q1): 1, ("T2", Q1): 1})
    _use(monkeypatch, shipped_catalog(epic_predictability=1))
    w7, w5 = Expect(2 / 3, 2.0, 3.0, 1), Expect(0.75, 3.0, 4.0, 1)
    expect = {
        "team": _rows(T1=w7, T2=w5),
        "org": _rows(O1_O2=Expect(5 / 7, 5.0, 7.0, 2), O3=w5),
        "work_item": _rows(W1_W7=w7, W5=w5),
    }
    _check(tiny, "epic_predictability", expect, EPIC_FILTERS)


def test_ut04_61_epic_without_committed_child_is_skipped(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-61 an epic whose children were all created after its start is skipped: moving W5's
    start before W6 and W8 exist leaves only W7 in O3's parent O2."""
    _use(monkeypatch, shipped_catalog(epic_predictability=1))
    tiny.execute(
        "UPDATE metrics.work_item_fact SET first_in_progress_at = TIMESTAMPTZ"
        " '2025-12-05 12:00:00-05' WHERE record_id = 'W5'"
    )
    rows = compute_metric("epic_predictability", "org", None, "quarter", con=tiny).rows
    assert [(r.entity_id, r.value, r.sample_size) for r in rows] == [
        ("O1", _approx(2 / 3), 1),
        ("O2", _approx(2 / 3), 1),
    ]
