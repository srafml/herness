"""Property tests PT04-03 and PT04-04 (impl 04 §11.2, design 04 §10.2) on random fact rows.

Random rows go straight into the stage 400 fact tables of one `metrics_tiny` warehouse (plus
`core.work_item_transition`, which `cat_at` reads, and `metrics.work_item_closure`); each
example replaces them. Teams, services and the org tree are the fixture's (T1 in O2, T2 in O3,
O3 under O2 under O1). Every covered metric runs with `min_sample_size` 1, so values are only
NULL where the definition makes them NULL.
"""

import dataclasses
import datetime
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Final

import duckdb
import pytest
from freezegun import freeze_time
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.metrics_oracle import (
    COVERED,
    Change,
    Facts,
    Frame,
    Incident,
    OracleRow,
    WorkItem,
    oracle,
)
from tests.support.metrics_tiny import (
    BUILD_ID,
    build_metrics_tiny,
    patch_facts_config,
    shipped_catalog,
    tiny_weights,
)

from herness.metrics import compute
from herness.metrics.compute import MetricRow, compute_metric
from herness.metrics.facts import materialize_facts
from herness.metrics.windows import DEFAULT_PERIOD_COUNTS, default_window

pytestmark = pytest.mark.unit

TZ: Final = "America/New_York"
AS_OF: Final = datetime.date(2026, 4, 1)
BASE: Final = datetime.datetime(2025, 3, 1, 5, tzinfo=datetime.UTC)  # 2025-03-01 00:00 -05
SPAN_MINUTES: Final = 420 * 24 * 60  # to late April 2026, past the as_of
PERIODS: Final = ("week", "month", "quarter", "t12w", "t12m")
ORG_OF_TEAM: Final[dict[str | None, str | None]] = {"T1": "O2", "T2": "O3", None: None}
ORG_PARENTS: Final = {"O1": None, "O2": "O1", "O3": "O2"}
RANK: Final = {"todo": 1, "in_progress": 2, "done": 3}
CATALOG: Final = shipped_catalog(**dict.fromkeys(COVERED, 1))
SLOW_OK: Final = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
# PT04-04: additive metrics. Counts and sums add up; for ratios (and the mean) the numerator
# and denominator add up and the value is their quotient. change_count is left out: its
# denominator is calendar weeks, and a month without deployments has no row to add its weeks.
# The spine metrics (carryover, backlog, WIP) and medians are not additive over periods.
SUMMED: Final = ("incident_count", "p1p2_count", "throughput", "toil_hours_est")
RATIOS: Final = (
    *("mttr_hours", "repeat_incident_rate", "reopen_rate", "sla_breach_rate"),
    *("change_failure_rate", "emergency_change_ratio", "unplanned_work_ratio"),
)
_TOL: Final = {"rel": 1e-9, "abs": 1e-9}

_ts = st.integers(0, SPAN_MINUTES).map(lambda m: BASE + datetime.timedelta(minutes=m))
_team = st.sampled_from(["T1", "T2", None])
_service = st.sampled_from(["S1", "S2", None])


@st.composite
def _incident(draw: st.DrawFn, rid: str) -> Incident:
    opened = draw(_ts)
    minutes = draw(st.none() | st.integers(0, 6000))
    resolved = None if minutes is None else opened + datetime.timedelta(minutes=minutes)
    valid = resolved is not None and draw(st.booleans() | st.just(True))
    team = draw(_team)
    return Incident(
        record_id=rid,
        opened_at=opened,
        resolved_at=resolved,
        priority=draw(st.sampled_from([None, 1, 2, 3, 4, 5])),
        service_id=draw(_service),
        team_id=team,
        org_id=ORG_OF_TEAM[team],
        cluster_id=draw(st.sampled_from(["C1", "C2", None])),
        excluded=draw(st.booleans()),
        resolve_h=minutes / 60.0 if valid and minutes is not None else None,
        is_repeat=draw(st.booleans()),
        is_reopened=draw(st.booleans()),
        is_reassigned=draw(st.booleans()),
        sla_breached=draw(st.sampled_from([None, True, False])),
        toil_h=draw(st.integers(0, 400)) / 20.0,
    )


@st.composite
def _change(draw: st.DrawFn, rid: str) -> Change:
    end = draw(st.none() | _ts)
    deployed = end is not None and draw(st.booleans() | st.just(True))
    team = draw(_team)
    lead = draw(st.none() | st.integers(0, 2000))
    return Change(
        record_id=rid,
        type=draw(st.sampled_from(["normal", "emergency", "standard"])),
        service_id=draw(_service),
        team_id=team,
        org_id=ORG_OF_TEAM[team],
        actual_end=end,
        deployed=deployed,
        failed=deployed and draw(st.booleans()),
        lead_time_h=None if lead is None else lead / 4.0,
    )


@st.composite
def _item(draw: st.DrawFn, rid: str, earlier: list[str]) -> WorkItem:
    """A work item with 0-3 transitions after creation; facts derived as stage 400 does."""
    created = draw(_ts)
    steps = draw(
        st.lists(st.tuples(st.integers(0, 60 * 24 * 60), st.sampled_from(sorted(RANK))), max_size=3)
    )
    at, moves = created, []
    for gap, category in steps:
        at += datetime.timedelta(minutes=gap)
        moves.append((at, category))
    status = max((a, RANK[c], c) for a, c in moves)[2] if moves else "todo"
    started = min((a for a, c in moves if c == "in_progress"), default=None)
    done = max((a for a, c in moves if c == "done"), default=None) if status == "done" else None
    cycle = None
    if started is not None and done is not None and done >= started:
        cycle = (done - started).total_seconds() / 86400.0
    kind = draw(st.sampled_from(["story", "bug", "task", "epic", "sub-task"]))
    team = draw(_team)
    return WorkItem(
        record_id=rid,
        type=kind,
        parent_id=draw(st.sampled_from([None, *earlier])),
        service_id=draw(_service),
        team_id=team,
        org_id=ORG_OF_TEAM[team],
        story_points=draw(st.sampled_from([None, 0.5, 1.0, 2.0, 3.0, 5.0])),
        created_at=created,
        first_in_progress_at=started,
        done_at=done,
        cycle_days=cycle,
        is_unplanned=kind == "bug" or draw(st.booleans()),
        transitions=tuple(moves),
    )


@st.composite
def fact_sets(draw: st.DrawFn) -> Facts:
    incidents = [draw(_incident(f"I{i}")) for i in range(draw(st.integers(0, 8)))]
    changes = [draw(_change(f"C{i}")) for i in range(draw(st.integers(0, 6)))]
    items: list[WorkItem] = []
    for i in range(draw(st.integers(0, 9))):
        items.append(draw(_item(f"W{i}", [w.record_id for w in items])))
    return Facts(incidents, changes, items, ORG_PARENTS)


@pytest.fixture(scope="module")
def warehouse() -> Iterator[duckdb.DuckDBPyConnection]:
    """One `metrics_tiny` warehouse with materialized facts; compute reads CATALOG."""
    with pytest.MonkeyPatch.context() as mp:
        patch_facts_config(mp)
        con = build_metrics_tiny()
        with freeze_time("2026-04-01 06:30:00"):
            materialize_facts(con, BUILD_ID)
        cfg = SimpleNamespace(weights=tiny_weights())
        mp.setattr(compute, "get_config", lambda: cfg)
        mp.setattr(compute, "catalog_from_config", lambda: CATALOG)
        yield con
        con.close()


def _insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict[str, object]]) -> None:
    con.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
    if rows:
        cols = list(rows[0])
        marks = ", ".join("?" * len(cols))
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})"  # noqa: S608 - fixed names
        con.executemany(sql, [[r[c] for c in cols] for r in rows])


def _closure(items: list[WorkItem]) -> list[dict[str, object]]:
    by_id = {w.record_id: w for w in items}
    rows: list[dict[str, object]] = []
    for w in items:
        node: WorkItem | None = w
        depth = 0
        while node is not None:
            rows.append(
                {"record_id": w.record_id, "ancestor_record_id": node.record_id, "depth": depth}
            )
            node = by_id.get(node.parent_id) if node.parent_id is not None else None
            depth += 1
    return rows


def _load(con: duckdb.DuckDBPyConnection, facts: Facts) -> None:
    """Replace the fact rows with `facts`."""
    _insert(con, "metrics.incident_fact", [dataclasses.asdict(i) for i in facts.incidents])
    _insert(con, "metrics.change_fact", [dataclasses.asdict(c) for c in facts.changes])
    items = [
        {k: v for k, v in dataclasses.asdict(w).items() if k not in {"parent_id", "transitions"}}
        | {"key": w.record_id, "parent_key": w.parent_id}
        for w in facts.items
    ]
    _insert(con, "metrics.work_item_fact", items)
    _insert(con, "metrics.work_item_closure", _closure(list(facts.items)))
    moves = [
        {"record_id": w.record_id, "to_category": c, '"at"': a}
        for w in facts.items
        for a, c in w.transitions
    ]
    _insert(con, "core.work_item_transition", moves)


def _frame(period: str) -> Frame:
    win = default_window(period, AS_OF, TZ, DEFAULT_PERIOD_COUNTS)  # type: ignore[arg-type]
    return Frame(period, win.start_ts, win.end_ts, win.start, win.end, win.as_of, TZ)


def _got(rows: list[MetricRow]) -> dict[tuple[str, datetime.date], OracleRow]:
    return {
        (r.entity_id, r.period_start): (r.value, r.numerator, r.denominator, r.sample_size)
        for r in rows
    }


def _check_invariants(metric: str, rows: list[MetricRow]) -> None:
    """Durations >= 0; ratios in [0, 1]; counts non-negative integers (design 04 §10.2)."""
    m = CATALOG.get(metric)
    for r in rows:
        assert r.sample_size >= 0
        if r.value is None:
            continue
        if m.unit == "ratio":
            assert 0.0 <= r.value <= 1.0
        if m.unit in {"hours", "days", "minutes"} or m.name == "change_count":
            assert r.value >= 0.0
        if m.aggregation in {"count", "snapshot"}:
            assert r.value >= 0.0
            assert r.value == int(r.value)


@SLOW_OK
@given(facts=fact_sets(), metric=st.sampled_from(sorted(COVERED)), data=st.data())
def test_pt04_03_sql_equals_oracle(
    warehouse: duckdb.DuckDBPyConnection, facts: Facts, metric: str, data: st.DataObject
) -> None:
    """PT04-03 random fact data: every covered metric at a random declared grain and period
    equals `metrics_oracle` row for row (value, numerator, denominator, sample_size); durations
    are >= 0, ratios in [0, 1] and counts non-negative integers."""
    grain = data.draw(st.sampled_from(CATALOG.get(metric).grains))
    period = data.draw(st.sampled_from(PERIODS))
    _load(warehouse, facts)
    rows = compute_metric(metric, grain, None, period, con=warehouse).rows  # type: ignore[arg-type]
    got, expect = _got(rows), oracle(metric, grain, facts, _frame(period))
    assert sorted(got) == sorted(expect)
    for key, (value, num, den, n) in expect.items():
        assert got[key] == (
            None if value is None else pytest.approx(value, **_TOL),
            None if num is None else pytest.approx(num, **_TOL),
            None if den is None else pytest.approx(den, **_TOL),
            n,
        )
    _check_invariants(metric, rows)


def _quarter(day: datetime.date) -> datetime.date:
    return day.replace(month=3 * ((day.month - 1) // 3) + 1, day=1)


@SLOW_OK
@given(facts=fact_sets(), metric=st.sampled_from([*SUMMED, *RATIOS]), data=st.data())
def test_pt04_04_quarter_is_sum_of_months(
    warehouse: duckdb.DuckDBPyConnection, facts: Facts, metric: str, data: st.DataObject
) -> None:
    """PT04-04 over the same window (the default quarter and month windows both cover
    [2024-04-01, 2026-04-01)): a count or sum metric at quarter equals the sum of its month
    values; for ratio metrics the quarter numerator and denominator are the sums of the month
    ones and the quarter value is their quotient."""
    assert (_frame("quarter").window_start, _frame("quarter").window_end) == (
        _frame("month").window_start,
        _frame("month").window_end,
    )
    grain = data.draw(st.sampled_from(CATALOG.get(metric).grains))
    _load(warehouse, facts)
    quarter = _got(compute_metric(metric, grain, None, "quarter", con=warehouse).rows)  # type: ignore[arg-type]
    months = compute_metric(metric, grain, None, "month", con=warehouse).rows  # type: ignore[arg-type]
    sums: dict[tuple[str, datetime.date], list[float]] = {}
    for r in months:
        acc = sums.setdefault((r.entity_id, _quarter(r.period_start)), [0.0, 0.0, 0.0, 0.0])
        acc[0] += r.value or 0.0
        acc[1] += r.numerator or 0.0
        acc[2] += r.denominator or 0.0
        acc[3] += r.sample_size
    assert sorted(quarter) == sorted(sums)
    for key, (value, num, den, n) in sums.items():
        q_value, q_num, q_den, q_n = quarter[key]
        assert q_n == n
        assert q_num == pytest.approx(num, **_TOL)
        if metric in SUMMED:
            assert q_value == pytest.approx(value, **_TOL)
        else:
            assert q_den == pytest.approx(den, **_TOL)
            assert q_value == pytest.approx(num / den, **_TOL)
