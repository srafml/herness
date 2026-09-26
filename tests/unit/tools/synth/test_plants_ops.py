"""Tests for tools.synth.plants_ops (U11-10, U11-12, U11-13, U11-14): UT11-13, UT11-15..17."""

import copy
import math
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from tools.synth import plants_ops_t3
from tools.synth.catalog import Catalog, build_catalog, default_planners
from tools.synth.catalog_plan import background_count, plant_range
from tools.synth.monitoring import gen_metric_daily
from tools.synth.params import SynthParams, SynthUsageError, load_params
from tools.synth.pii import build_name_list
from tools.synth.plants_ops import (
    T4_TITLES,
    planned_counts_t1,
    planned_counts_t3,
    planned_counts_t4,
    planned_counts_t5,
    plant_t1,
    plant_t3,
    plant_t4,
    plant_t5,
)
from tools.synth.servicenow import gen_incidents
from tools.synth.servicenow_common import service_index, span_end, span_start, ts_pair
from tools.synth.servicenow_incidents import IncidentSpec, make_incident
from tools.synth.shards import IncidentTimeIndex, PlantOutput, Shard
from tools.synth.text import TemplateBank

pytestmark = pytest.mark.unit

_END = date(2026, 8, 31)
_MONTHS = (date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1))
_TWO_H = timedelta(hours=2)
_HALF_H = timedelta(minutes=30)
_LIMIT_HOURS = {1: 4, 2: 12, 3: 72, 4: 168, 5: 720}

Rec = dict[str, Any]


@pytest.fixture(scope="module")
def params() -> SynthParams:
    return load_params(
        "tiny",
        start=date(2026, 1, 1),
        end=_END,
        sources=("servicenow", "monitoring"),
        dirty="none",
        fetch_mode="initial",
        params_file=None,
    )


@pytest.fixture(scope="module")
def cat(params: SynthParams) -> Catalog:
    return build_catalog(42, params)


@pytest.fixture(scope="module")
def bank() -> TemplateBank:
    return TemplateBank()


@pytest.fixture(scope="module")
def names() -> tuple[tuple[str, str], ...]:
    return build_name_list(7)


def _v(record: Rec, field: str) -> str:
    value = record[field]["value"]
    assert isinstance(value, str)
    return value


def _ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _shard(cat: Catalog, source: str, entity: str, month: date) -> Shard:
    key = (source, entity, month)
    return Shard(source, entity, month, background_count(cat, key), cat.seq_start[key], 0)


def _background(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: Any, month: date, seed: int = 5
) -> list[Rec]:
    shard = _shard(cat, "servicenow", "incident", month)
    rng = np.random.default_rng([seed, month.month])
    return gen_incidents(cat, params, shard, rng, bank, names).records


def _services(cat: Catalog) -> dict[str, Any]:
    return {s.sys_id: s for s in cat.services}


# --- planners -------------------------------------------------------------------------


def test_ut11_13_planners_are_registered_and_t1_adds_nothing(
    cat: Catalog, params: SynthParams
) -> None:
    """UT11-13 default_planners holds the four ops planners; T1 plans no records."""
    assert default_planners()[:4] == (
        planned_counts_t1,
        planned_counts_t3,
        planned_counts_t4,
        planned_counts_t5,
    )
    assert planned_counts_t1(cat, params) == {}
    base = build_catalog(42, params, planners=())
    for key, count in cat.month_counts.items():
        plants = count - background_count(cat, key)
        assert background_count(cat, key) == base.month_counts[key]
        assert plants >= 0
    first = dict(cat.seq_start)
    for key in cat.month_counts:  # plant ranges follow the background, gap-free
        n_bg = background_count(cat, key)
        cursor = first[key] + n_bg
        for planner in default_planners():
            start, n = plant_range(cat, planner, key)
            if n:
                assert start == cursor
                cursor += n
        assert cursor == first[key] + cat.month_counts[key]


def test_ut11_15_t3_planner_counts_follow_the_schedule(cat: Catalog, params: SynthParams) -> None:
    """UT11-15 the T3 planner books 80 changes and every follow-up by work_end month."""
    plan = planned_counts_t3(cat, params)
    changes = sum(n for (s, e, _), n in plan.items() if e == "change_request")
    incidents = sum(n for (s, e, _), n in plan.items() if e == "incident")
    assert changes == 80
    assert incidents == sum(s.follow_up_incidents for s in cat.change_schedule)


def test_ut11_16_t4_planner_books_seven_percent_of_events(
    cat: Catalog, params: SynthParams
) -> None:
    """UT11-16 the T4 planner books round(0.07 x preset.events) S4 events split by days."""
    plan = planned_counts_t4(cat, params)
    assert sum(plan.values()) == round(0.07 * 400)
    assert {k[:2] for k in plan} == {("monitoring", "event")}
    assert plan["monitoring", "event", date(2026, 6, 1)] < plan["monitoring", "event", _MONTHS[1]]


def test_ut11_17_t5_planner_uses_the_baseline_daily_count(
    cat: Catalog, params: SynthParams
) -> None:
    """UT11-17 extra S5 incidents = round(1.8 x S5 baseline daily count x peak days)."""
    base = build_catalog(42, params, planners=())
    weights = [s.incident_weight for s in cat.services]
    share = _services(cat)[cat.plants.s5].incident_weight / math.fsum(weights)
    plan = planned_counts_t5(base, params)  # planners see the stub (background counts)
    assert all(plant_range(cat, planned_counts_t5, k)[1] == n for k, n in plan.items())
    days = {_MONTHS[0]: 28, _MONTHS[1]: 31, _MONTHS[2]: 31}
    peak = {_MONTHS[0]: 0, _MONTHS[1]: 0, _MONTHS[2]: 30}
    for month in _MONTHS:
        key = ("servicenow", "incident", month)
        expected = round(1.8 * share * base.month_counts[key] / days[month] * peak[month])
        assert plan.get(key, 0) == expected


# --- T1 -------------------------------------------------------------------------------


def _t1_records(cat: Catalog, params: SynthParams, bank: TemplateBank) -> list[Rec]:
    """100 T1 incidents on a grid where round(d x m) is exact: 2160 s steps, whole hours."""
    index, services = service_index(cat), _services(cat)
    start = span_start(params)
    rng = np.random.default_rng(3)
    out = []
    for i in range(100):
        service = services[cat.plants.t1_services[i % 3]]
        opened = start + timedelta(seconds=2160 * 36 * i)
        record, _ = make_incident(params, rng, bank, index, IncidentSpec(service, opened, 1 + i))
        resolved = opened + timedelta(hours=1 + i % 5)
        record["resolved_at"] = ts_pair(resolved)
        record["closed_at"] = ts_pair(resolved + _HALF_H)
        out.append(record)
    return out


def _m(params: SynthParams, opened: datetime) -> float:
    start, end = span_start(params), span_end(params)
    return 2.0 + (opened - start).total_seconds() / (end - start).total_seconds()


def test_ut11_13_t1_mttr_ratio_is_two_plus_elapsed_fraction(
    cat: Catalog, params: SynthParams, bank: TemplateBank
) -> None:
    """UT11-13 MTTR ratio to unplanted copies = 2.0 + elapsed fraction within 1e-9."""
    records = _t1_records(cat, params, bank)
    before = copy.deepcopy(records)
    plant_t1(records, cat, params, np.random.default_rng(9))
    team = next(t for t in cat.teams if t.sys_id == cat.plants.t1_team)
    for old, new in zip(before, records, strict=True):
        opened = _ts(_v(old, "opened_at"))
        old_d = (_ts(_v(old, "resolved_at")) - opened).total_seconds()
        new_d = (_ts(_v(new, "resolved_at")) - opened).total_seconds()
        assert abs(new_d / old_d - _m(params, opened)) < 1e-9
        assert _v(new, "assignment_group") == team.sys_id
        assert new["assignment_group"]["display_value"] == team.name
        lag = _ts(_v(new, "closed_at")) - _ts(_v(new, "resolved_at"))
        assert lag == _HALF_H
        assert int(_v(new, "reassignment_count")) >= int(_v(old, "reassignment_count"))
    ratios = [
        _m(params, _ts(_v(r, "opened_at"))) for r in records
    ]  # the grid spans the whole range
    assert min(ratios) == 2.0
    assert max(ratios) > 2.95


def test_ut11_13_t1_recomputes_dependent_fields(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: Any
) -> None:
    """UT11-13 background T1 incidents: MTTR x m(t), dependent fields recomputed, rest kept."""
    records = [r for m in _MONTHS for r in _background(cat, params, bank, names, m)]
    before = copy.deepcopy(records)
    plant_t1(records, cat, params, np.random.default_rng(4))
    t1 = set(cat.plants.t1_services)
    end = span_end(params)
    extra = 0
    seen = 0
    for old, new in zip(before, records, strict=True):
        if _v(old, "business_service") not in t1:
            assert new == old
            continue
        seen += 1
        assert _v(new, "assignment_group") == cat.plants.t1_team
        extra += int(_v(new, "reassignment_count")) - int(_v(old, "reassignment_count"))
        opened = _ts(_v(new, "opened_at"))
        assert _v(new, "opened_at") == _v(old, "opened_at")
        if not _v(old, "resolved_at"):
            assert not _v(new, "resolved_at")
            continue
        old_d = (_ts(_v(old, "resolved_at")) - opened).total_seconds()
        target = old_d * _m(params, opened)
        if opened + timedelta(seconds=round(target)) > end:
            assert _v(new, "state") == "2"
            assert not _v(new, "resolved_at")
            assert not _v(new, "closed_at")
            assert not _v(new, "close_code")
            assert not _v(new, "business_duration")
            duration = (end - opened).total_seconds()
        else:
            new_d = (_ts(_v(new, "resolved_at")) - opened).total_seconds()
            assert abs(new_d - target) <= 0.5
            duration = new_d
            if _v(new, "closed_at"):
                assert _v(new, "state") == "7"
                old_lag = _ts(_v(old, "closed_at")) - _ts(_v(old, "resolved_at"))
                assert _ts(_v(new, "closed_at")) - _ts(_v(new, "resolved_at")) == old_lag
            else:
                assert _v(new, "state") == "6"
        limit = _LIMIT_HOURS[int(_v(new, "priority"))] * 3600
        assert _v(new, "made_sla") == ("false" if duration > limit else "true")
        stamps = [_ts(_v(new, f)) for f in ("opened_at", "resolved_at", "closed_at") if _v(new, f)]
        assert _ts(_v(new, "sys_updated_on")) >= max(s for s in stamps if s <= end)
        if _v(old, "u_customer_impact_minutes"):
            assert int(_v(new, "u_customer_impact_minutes")) >= int(
                _v(old, "u_customer_impact_minutes")
            )
    assert seen > 20
    assert 0.4 * seen < extra < 1.8 * seen  # Poisson(1.0) per incident


# --- T3 -------------------------------------------------------------------------------


def _t3_run(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: Any, inject: bool
) -> tuple[list[Rec], list[PlantOutput], list[Rec], list[tuple[Rec, datetime]]]:
    """Run plant_t3 over every change and incident shard; with `inject`, first move one
    background incident per control change onto C3 inside its window."""
    changes: list[Rec] = []
    outputs: list[PlantOutput] = []
    backgrounds: list[Rec] = []
    moved: list[tuple[Rec, datetime]] = []
    controls = [s for s in cat.change_schedule if not s.emergency]
    c3 = next(ci for ci in cat.cis if ci.sys_id == cat.plants.c3)
    for month in _MONTHS:
        shard = _shard(cat, "servicenow", "change_request", month)
        out = plant_t3(shard, cat, params, np.random.default_rng([1, month.month]), bank, [])
        changes += out.records
        background = _background(cat, params, bank, names, month)
        if inject:
            mine = [s for s in controls if s.work_end.month == month.month]
            for slot, record in zip(mine, background, strict=False):
                opened = slot.work_end + timedelta(minutes=45)
                record["opened_at"] = ts_pair(opened)
                record["u_acknowledged_at"] = ts_pair(opened + timedelta(minutes=5))
                record["resolved_at"] = ts_pair(opened + timedelta(hours=1))
                record["closed_at"] = ts_pair(opened + timedelta(hours=2))
                record["cmdb_ci"] = {"value": c3.sys_id, "display_value": c3.name}
                moved.append((record, opened))
        shard = _shard(cat, "servicenow", "incident", month)
        rng = np.random.default_rng([2, month.month])
        outputs.append(plant_t3(shard, cat, params, rng, bank, background))
        backgrounds += background
    return changes, outputs, backgrounds, moved


def test_ut11_15_t3_changes_follow_the_schedule(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: Any
) -> None:
    """UT11-15 40 emergency and 40 normal changes on C3 at the scheduled work_end."""
    changes, _, _, _ = _t3_run(cat, params, bank, names, inject=False)
    slots = {s.sys_id: s for s in cat.change_schedule}
    assert len(changes) == 80
    assert {_v(c, "sys_id") for c in changes} == set(slots)
    assert Counter(_v(c, "type") for c in changes) == {"emergency": 40, "normal": 40}
    numbers = [_v(c, "number") for c in changes]
    assert len(set(numbers)) == 80
    for change in changes:
        slot = slots[_v(change, "sys_id")]
        assert (_v(change, "type") == "emergency") is slot.emergency
        assert _v(change, "cmdb_ci") == cat.plants.c3
        assert _v(change, "work_end") == ts_pair(slot.work_end)["value"]
        assert _ts(_v(change, "work_start")) < slot.work_end
        assert _v(change, "state") == "3"
        assert _v(change, "close_code")
        month = slot.work_end.date().replace(day=1)
        start, n = plant_range(cat, planned_counts_t3, ("servicenow", "change_request", month))
        assert start <= int(_v(change, "number")[3:]) < start + n


def test_ut11_15_t3_incidents_open_after_their_change(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: Any
) -> None:
    """UT11-15 plant incidents open in (t, t + 2 h]; caused_by share = round(0.3 n)/n."""
    changes, outputs, _, _ = _t3_run(cat, params, bank, names, inject=False)
    by_id = {f"servicenow:change_request:{_v(c, 'sys_id')}": c for c in changes}
    incidents = [r for o in outputs for r in o.records]
    links = [link for o in outputs for link in o.links]
    labels = [row for o in outputs for row in o.labels]
    n = sum(s.follow_up_incidents for s in cat.change_schedule)
    assert len(incidents) == len(links) == n
    assert [m for o in outputs for m in o.members] == [x["incident_record_id"] for x in links]
    records = {f"servicenow:incident:{_v(r, 'sys_id')}": r for r in incidents}
    caused = 0
    for link in links:
        record, change = records[link["incident_record_id"]], by_id[link["change_record_id"]]
        assert _v(change, "type") == "emergency"
        t = _ts(_v(change, "work_end"))
        assert t < _ts(_v(record, "opened_at")) <= t + _TWO_H
        assert _v(record, "cmdb_ci") == cat.plants.c3
        assert _v(record, "business_service") == cat.plants.s3
        if _v(record, "caused_by"):
            caused += 1
            assert _v(record, "caused_by") == _v(change, "sys_id")
            assert record["caused_by"]["display_value"] == _v(change, "number")
    assert caused == round(0.3 * n)
    assert caused / n == round(0.3 * n) / n
    answers = {r["answer"] for r in labels if r["question"] == "change_caused"}
    assert answers == {"true"}
    assert len(labels) == 5 * n
    numbers = [int(_v(r, "number")[3:]) for r in incidents]
    assert len(set(numbers)) == n


def test_ut11_15_t3_clears_control_windows(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: Any
) -> None:
    """UT11-15 no background incident on C3 within 2 h after a control change."""
    _, _, backgrounds, moved = _t3_run(cat, params, bank, names, inject=True)
    assert len(moved) >= 30
    ends = [s.work_end for s in cat.change_schedule if not s.emergency]
    end = span_end(params)
    for record in backgrounds:
        if _v(record, "cmdb_ci") != cat.plants.c3:
            continue
        opened = _ts(_v(record, "opened_at"))
        assert opened < end
        assert not any(t < opened <= t + _TWO_H for t in ends)
    shifted = sum(_ts(_v(r, "opened_at")) == at + timedelta(hours=3) for r, at in moved)
    assert shifted >= len(moved) - 2
    for record, _ in moved:  # the whole record moves: ack, MTTR and close lag are kept
        opened = _ts(_v(record, "opened_at"))
        assert _ts(_v(record, "u_acknowledged_at")) == opened + timedelta(minutes=5)
        if _v(record, "resolved_at"):
            assert _ts(_v(record, "resolved_at")) == opened + timedelta(hours=1)
        if _v(record, "closed_at"):
            assert _ts(_v(record, "closed_at")) == opened + timedelta(hours=2)


def test_ut11_15_t3_shift_stays_inside_the_span() -> None:
    """UT11-15 a control-window incident whose +3 h shift would pass the span end moves
    back in -3 h steps, skipping any control window."""
    end = datetime(2026, 9, 1, tzinfo=UTC)
    ends = [end - timedelta(hours=5), end - timedelta(hours=2)]
    opened = end - timedelta(hours=1)  # in the window of the last control change
    moved = plants_ops_t3._free_time(opened, ends, end)
    assert moved == opened - timedelta(hours=6)  # -3 h lands in the earlier window
    assert not plants_ops_t3._in_window(moved, ends)
    early = ends[0] + timedelta(minutes=10)  # +3 h hits the last window, +6 h passes the end
    assert plants_ops_t3._free_time(early, ends, end) == early - timedelta(hours=3)
    chain = [end - timedelta(hours=20), end - timedelta(hours=17)]
    start = chain[0] + timedelta(minutes=10)  # +3 h hits the next window, +6 h is free
    assert plants_ops_t3._free_time(start, chain, end) == start + timedelta(hours=6)


def test_ut11_15_t3_rejects_other_shards(cat: Catalog, params: SynthParams) -> None:
    """UT11-15 plant_t3 accepts only ServiceNow incident and change shards."""
    shard = Shard("monitoring", "event", _MONTHS[0], 0, 1, 0)
    with pytest.raises(SynthUsageError):
        plant_t3(shard, cat, params, np.random.default_rng(1), TemplateBank(), [])


def test_ut11_15_caused_by_flags_ride_on_the_schedule(cat: Catalog) -> None:
    """UT11-15 the catalog carries one caused_by flag per follow-up incident, 30 % true."""
    n = sum(s.follow_up_incidents for s in cat.change_schedule)
    flags = [f for s in cat.change_schedule for f in s.caused_by]
    assert all(len(s.caused_by) == s.follow_up_incidents for s in cat.change_schedule)
    assert sum(flags) == round(0.3 * n)


# --- T4 -------------------------------------------------------------------------------


def _index(service: str, times: list[datetime]) -> IncidentTimeIndex:
    stamps = np.array(sorted(int(t.timestamp()) for t in times), dtype=np.int64)
    ids = tuple(f"{i:032x}" for i in range(len(stamps)))
    numbers = tuple(f"INC{i:07d}" for i in range(len(stamps)))
    return IncidentTimeIndex({service: stamps}, {service: ids}, {service: numbers})


def _t4_run(cat: Catalog, params: SynthParams, index: IncidentTimeIndex) -> list[Rec]:
    rows: list[Rec] = []
    for month in _MONTHS:
        shard = _shard(cat, "monitoring", "event", month)
        out = plant_t4(shard, cat, params, np.random.default_rng([3, month.month]), index)
        assert out.labels == out.members == out.links == []
        rows += out.records
    return rows


def test_ut11_16_t4_events_are_flapping_noise(cat: Catalog, params: SynthParams) -> None:
    """UT11-16 severities only minor/warning; incident_ref NULL; < 5 % near an S4 incident."""
    rng = np.random.default_rng(8)
    start = span_start(params)
    times = [start + timedelta(seconds=int(s)) for s in rng.integers(0, 90 * 86_400, 200)]
    index = _index(cat.plants.s4, times)
    rows = _t4_run(cat, params, index)
    s4 = _services(cat)[cat.plants.s4]
    assert len(rows) == round(0.07 * 400)
    assert {r["severity_raw"] for r in rows} <= {"minor", "warning"}
    assert all(r["incident_ref"] is None for r in rows)
    assert all(r["service"] == s4.name for r in rows)
    assert {r["title"] for r in rows} <= set(T4_TITLES)
    assert len(T4_TITLES) == 5
    for row in rows:
        assert row["dedup_key"] == row["title"].lower().replace(" ", "-")
        assert row["_source_key"] == f"{row['source_tool']}:{row['event_key']}"
    stamps = index.opened_at[cat.plants.s4]
    near = 0
    for row in rows:
        at = int(_iso(row["ts"]).timestamp())
        k = int(np.searchsorted(stamps, at - 1800))
        near += k < len(stamps) and stamps[k] <= at + 1800
    assert near / len(rows) < 0.05
    assert len({r["event_key"] for r in rows}) == len(rows)


def test_ut11_16_t4_redraws_at_most_three_times(cat: Catalog, params: SynthParams) -> None:
    """UT11-16 when every time is near an S4 incident, each event keeps its fourth draw."""
    start = span_start(params)
    dense = [start + timedelta(minutes=20 * k) for k in range(90 * 72 + 1)]
    rows = _t4_run(cat, params, _index(cat.plants.s4, dense))
    assert len(rows) == round(0.07 * 400)
    end = span_end(params)
    assert all(start <= _iso(r["ts"]) < end for r in rows)
    empty = IncidentTimeIndex({}, {}, {})
    assert len(_t4_run(cat, params, empty)) == len(rows)


def test_ut11_16_t4_rejects_other_shards(cat: Catalog, params: SynthParams) -> None:
    """UT11-16 plant_t4 accepts only monitoring event shards."""
    shard = Shard("servicenow", "incident", _MONTHS[0], 0, 1, 0)
    with pytest.raises(SynthUsageError):
        plant_t4(shard, cat, params, np.random.default_rng(1), IncidentTimeIndex({}, {}, {}))


# --- T5 -------------------------------------------------------------------------------


def _day(record: Rec) -> date:
    return _ts(_v(record, "opened_at")).date()


def test_ut11_17_t5_incident_volume_rises_2_8_on_peak_days(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: Any
) -> None:
    """UT11-17 S5 peak-day incident volume is 2.8 x non-peak within a Poisson bound."""
    s5, t5 = cat.plants.s5, cat.plants.t5_team
    peak_n = off_n = 0
    for seed in range(12):
        for month in _MONTHS:
            background = _background(cat, params, bank, names, month, seed=seed)
            shard = _shard(cat, "servicenow", "incident", month)
            rng = np.random.default_rng([seed, 50, month.month])
            out = plant_t5(background, [], shard, cat, params, rng)
            key = ("servicenow", "incident", month)
            assert len(out.records) == plant_range(cat, planned_counts_t5, key)[1]
            assert len(out.labels) == 5 * len(out.records)
            for record in out.records:
                assert _v(record, "business_service") == s5
                assert _v(record, "assignment_group") == t5
                assert cat.plants.in_peak(_day(record))
            for record in [*background, *out.records]:
                if _v(record, "business_service") != s5:
                    continue
                assert _v(record, "assignment_group") == t5
                if cat.plants.in_peak(_day(record)):
                    peak_n += 1
                else:
                    off_n += 1
    peak_days, off_days = 30, 90 - 30
    ratio = (peak_n / peak_days) / (off_n / off_days)
    bound = 4.0 * ratio * math.sqrt(1.0 / peak_n + 1.0 / off_n) + 0.1
    assert abs(ratio - 2.8) <= bound
    assert bound < 1.0


def test_ut11_17_t5_request_count_rises_2_8_on_peak_days(cat: Catalog, params: SynthParams) -> None:
    """UT11-17 S5 request_count on peak days is 2.8 x its unplanted value; others untouched."""
    s5 = _services(cat)[cat.plants.s5]
    peak: list[float] = []
    off: list[float] = []
    for month in _MONTHS:
        shard = Shard("monitoring", "metric_daily", month, 0, 1, 0)
        rows = gen_metric_daily(cat, params, shard, np.random.default_rng(month.month),
                                frozenset(), {})  # fmt: skip
        before = copy.deepcopy(rows)
        out = plant_t5([], rows, shard, cat, params, np.random.default_rng(1))
        assert out.records == []
        for old, new in zip(before, rows, strict=True):
            day = date.fromisoformat(str(new["date"]))
            hit = new["service"] == s5.name and new["metric_name"] == "request_count"
            if hit and cat.plants.in_peak(day):
                assert new["value"] == float(round(float(old["value"]) * 2.8))
                peak.append(float(new["value"]))
            else:
                assert new == old
                if hit:
                    off.append(float(new["value"]))
    assert len(peak) == 30
    ratio = float(np.mean(peak)) / float(np.mean(off))
    assert abs(ratio - 2.8) <= 2.8 * 0.12  # +/-5 % jitter plus the weekday/season curve


def test_ut11_17_t5_ignores_other_shards(cat: Catalog, params: SynthParams) -> None:
    """UT11-17 plant_t5 does nothing on a shard it does not plant."""
    shard = Shard("jira", "issue", _MONTHS[2], 0, 1, 0)
    out = plant_t5([], [], shard, cat, params, np.random.default_rng(1))
    assert out == PlantOutput()
