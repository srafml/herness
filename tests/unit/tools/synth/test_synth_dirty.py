"""Tests for tools.synth.dirty (U11-16): UT11-19 and PT11-03."""

import copy
import dataclasses
import json
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tools.synth.dirty import (
    COST_FIELD,
    FUTURE_SHIFT,
    DirtyCounters,
    apply_dirty,
    effective_rates,
)
from tools.synth.jira_changelog import jira_ts
from tools.synth.params import SynthParams, load_params
from tools.synth.servicenow_common import pair, parse_ts, ts_pair

pytestmark = pytest.mark.unit

Rec = dict[str, Any]

_ENTITIES = ("incident", "change_request", "problem", "issue", "event", "metric_daily")
_TS_FIELDS = {
    "incident": ("opened_at", "u_acknowledged_at", "resolved_at", "closed_at"),
    "change_request": ("opened_at", "start_date", "end_date", "work_start", "work_end"),
}
_T0 = datetime(2024, 1, 3, 9, 0, 0, tzinfo=UTC)
_N = 10_000
_SIGMAS = 6.0


def _params(dirty: str) -> SynthParams:
    return load_params(
        "tiny",
        start=date(2024, 1, 1),
        end=date(2024, 3, 31),
        sources=("servicenow", "jira", "monitoring"),
        dirty=dirty,  # type: ignore[arg-type]
        fetch_mode="initial",
        params_file=None,
    )


def _incident(i: int, *, resolved: bool) -> dict[str, Any]:
    opened = _T0 + timedelta(minutes=7 * i)
    done = opened + timedelta(hours=5) if resolved else None
    closed = done + timedelta(hours=1) if done else None
    return {
        "sys_id": pair(f"{i:032x}"),
        "number": pair(f"INC{i:07d}"),
        "opened_at": ts_pair(opened),
        "u_acknowledged_at": ts_pair(opened + timedelta(minutes=10)),
        "resolved_at": ts_pair(done),
        "closed_at": ts_pair(closed),
        "priority": pair("3", "3 - Moderate"),
        "state": pair("7", "Closed") if resolved else pair("2", "In Progress"),
        "business_service": pair(f"svc{i % 7:029x}", "Checkout"),
        "cmdb_ci": pair(f"ci{i % 5:030x}", "checkout-app"),
        "sys_updated_on": ts_pair((closed or opened) + timedelta(minutes=3)),
    }


def _change(i: int) -> dict[str, Any]:
    start = _T0 + timedelta(minutes=11 * i)
    stamps = [start + timedelta(hours=h) for h in range(5)]
    names = _TS_FIELDS["change_request"]
    return {
        "sys_id": pair(f"{i:032x}"),
        "type": pair("normal", "Normal"),
        **{name: ts_pair(at) for name, at in zip(names, stamps, strict=True)},
        "sys_updated_on": ts_pair(stamps[-1]),
    }


def _issue(i: int) -> dict[str, Any]:
    fields = {
        "status": {"name": "To Do", "id": "10000", "statusCategory": {"key": "new"}},
        "updated": jira_ts(_T0 + timedelta(hours=i)),
        COST_FIELD: 12_000,
    }
    return {"id": str(10_000 + i), "key": f"CHK-{i + 1}", "fields": fields}


def _records(entity: str, n: int) -> list[dict[str, Any]]:
    if entity == "incident":
        return [_incident(i, resolved=i % 4 != 0) for i in range(n)]
    if entity == "change_request":
        return [_change(i) for i in range(n)]
    if entity == "problem":
        return [{"sys_id": pair(f"{i:032x}"), "sys_updated_on": ts_pair(_T0)} for i in range(n)]
    if entity == "issue":
        return [_issue(i) for i in range(n)]
    if entity == "event":
        return [{"event_key": str(i), "ts": "2024-01-03T09:00:00Z"} for i in range(n)]
    return [
        {"date": "2024-01-03", "metric_name": "error_rate", "value": float(i)} for i in range(n)
    ]


def _ts(value: dict[str, str]) -> datetime:
    at = parse_ts(value)
    assert at is not None
    return at


def _canon(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True)


def _bad_ts(old: dict[str, str], new: dict[str, str]) -> bool:
    if new == old:
        return False
    value = new["value"]
    return value in {"", "31/02/2024"} or value.isdigit()


def _observe(
    entity: str, before: list[Rec], after: list[Rec], reemits: list[Rec]
) -> dict[str, int]:
    """Defect counts read off the output records (compared with the untouched inputs)."""
    seen = dict.fromkeys([f.name for f in dataclasses.fields(DirtyCounters)], 0)
    for old, new in zip(before, after, strict=True):
        if entity in _TS_FIELDS:
            seen["bad_timestamp"] += any(_bad_ts(old[f], new[f]) for f in _TS_FIELDS[entity])
            kind = new["priority" if entity == "incident" else "type"]["value"]
            seen["unknown_enum"] += kind in {"P2-ish", "Emergency "}
        if entity == "incident":
            opened, resolved = new["opened_at"], new["resolved_at"]
            moved = opened != old["opened_at"] and not _bad_ts(old["opened_at"], opened)
            seen["future_ts"] += moved and _ts(opened) == _ts(old["opened_at"]) + FUTURE_SHIFT
            back = resolved != old["resolved_at"] and not _bad_ts(old["resolved_at"], resolved)
            seen["resolved_before_opened"] += back and _ts(resolved) < _ts(opened)
            gone = new["business_service"]["value"] == "" and new["cmdb_ci"]["value"] == ""
            seen["missing_service"] += gone and old["business_service"]["value"] != ""
    outputs = {_canon(rec) for rec in after}
    for extra in reemits:
        if extra.get("__tombstone__"):
            seen["tombstones"] += 1
        elif _canon(extra) in outputs:
            seen["duplicate_rows"] += 1
        else:
            seen["later_versions"] += 1
    return seen


def _run(
    entity: str, dirty: str, n: int, seed: int, month_index: int = 0
) -> tuple[list[Rec], list[Rec], list[Rec], DirtyCounters]:
    records = _records(entity, n)
    before = copy.deepcopy(records)
    counters = DirtyCounters()
    rng = np.random.default_rng(seed)
    reemits = apply_dirty(entity, records, month_index, _params(dirty), rng, counters)
    return before, records, reemits, counters


@pytest.mark.parametrize("entity", _ENTITIES)
def test_ut11_19_none_leaves_records_unchanged(entity: str) -> None:
    """UT11-19 dirty `none`: no re-emits, records unchanged and all counters zero."""
    before, after, reemits, counters = _run(entity, "none", _N, seed=1, month_index=30)
    assert reemits == []
    assert after == before
    assert dataclasses.asdict(counters) == dict.fromkeys(dataclasses.asdict(counters), 0)


@pytest.mark.parametrize("dirty", ["default", "heavy"])
@pytest.mark.parametrize("entity", _ENTITIES)
def test_ut11_19_counters_equal_observed_defects(entity: str, dirty: str) -> None:
    """UT11-19 10,000 records per entity: counters equal the defects counted in the output
    and each count lies within 6 sigma of n x the effective rate."""
    before, after, reemits, counters = _run(entity, dirty, _N, seed=19)
    counts = dataclasses.asdict(counters)
    assert counts == _observe(entity, before, after, reemits)
    rates = effective_rates(_params(dirty))
    applicable = {
        "incident": set(counts),
        "change_request": {"bad_timestamp", "unknown_enum", "duplicate_rows"},
        "issue": {"duplicate_rows", "later_versions", "tombstones"},
    }.get(entity, {"duplicate_rows"})
    for name, count in counts.items():
        if name not in applicable:
            assert count == 0, name
            continue
        n = _N * (0.75 if name == "resolved_before_opened" else 1.0)  # resolved incidents only
        expected = n * rates[name]
        assert abs(count - expected) <= _SIGMAS * math.sqrt(expected) + 2, name


def test_ut11_19_heavy_rates_are_five_times_default_capped() -> None:
    """UT11-19 heavy rates are 5 x the default rates, capped at 1.0; none rates are 0."""
    default, heavy = effective_rates(_params("default")), effective_rates(_params("heavy"))
    assert set(default) == {f.name for f in dataclasses.fields(DirtyCounters)}
    for name, rate in default.items():
        assert heavy[name] == pytest.approx(min(1.0, 5 * rate))
    assert default["missing_service"] == pytest.approx(0.08)
    assert set(effective_rates(_params("none")).values()) == {0.0}


def test_ut11_19_heavy_observed_counts_scale_by_five() -> None:
    """UT11-19 at heavy the observed missing-service count is about 5 x the default count."""
    low = _run("incident", "default", _N, seed=3)[3].missing_service
    high = _run("incident", "heavy", _N, seed=3)[3].missing_service
    assert 4.5 < high / low < 5.5


def test_ut11_19_same_seed_same_output() -> None:
    """UT11-19 determinism: the same seed gives identical records, re-emits and counters."""
    first, second = _run("incident", "heavy", 500, seed=5), _run("incident", "heavy", 500, seed=5)
    assert first[1:] == second[1:]


def test_ut11_19_reemit_shapes() -> None:
    """UT11-19 later versions are newer (+1 h..10 d) with the state advanced; tombstones
    carry only the marker, key and deleted_at; duplicates equal their record."""
    _, after, reemits, counters = _run("incident", "heavy", 2_000, seed=7)
    by_id = {rec["sys_id"]["value"]: rec for rec in after}
    later = [r for r in reemits if not r.get("__tombstone__") and r not in after]
    assert len(later) == counters.later_versions > 0
    for rec in later:
        orig = by_id[rec["sys_id"]["value"]]
        gap = _ts(rec["sys_updated_on"]) - _ts(orig["sys_updated_on"])
        assert timedelta(hours=1) <= gap <= timedelta(days=10)
        assert {orig["state"]["value"], rec["state"]["value"]} in ({"2", "6"}, {"7"})
    tombs = [r for r in reemits if r.get("__tombstone__")]
    assert tombs
    for tomb in tombs:
        assert set(tomb) == {"__tombstone__", "key", "deleted_at"}
        assert tomb["key"] in by_id
        assert _ts(pair(tomb["deleted_at"])) > _ts(by_id[tomb["key"]]["sys_updated_on"])


def test_ut11_19_issue_later_version_advances_status() -> None:
    """UT11-19 a Jira later version has a newer `updated` and the next status."""
    _, after, reemits, _ = _run("issue", "heavy", 1_000, seed=8)
    later = [r for r in reemits if not r.get("__tombstone__") and r not in after]
    assert later
    for rec in later:
        assert rec["fields"]["status"]["name"] == "In Progress"
        assert rec["fields"]["status"]["statusCategory"] == {"key": "indeterminate"}
        assert rec["fields"]["updated"] > after[int(rec["id"]) - 10_000]["fields"]["updated"]
    tombs = [r for r in reemits if r.get("__tombstone__")]
    assert tombs
    assert all(t["key"].isdigit() for t in tombs)


def test_ut11_19_schema_drift_by_month() -> None:
    """UT11-19 schema drift: incidents gain `u_business_impact` from month 25 (index 24);
    the Jira shard of month index 29 drops the cost custom field, others keep it."""
    _, early, _, _ = _run("incident", "default", 50, seed=2, month_index=23)
    _, late, _, _ = _run("incident", "default", 50, seed=2, month_index=24)
    assert all("u_business_impact" not in r for r in early)
    assert all(set(r["u_business_impact"]) == {"value", "display_value"} for r in late)
    for month, present in ((28, True), (29, False), (30, True)):
        _, issues, _, _ = _run("issue", "default", 20, seed=2, month_index=month)
        assert all((COST_FIELD in r["fields"]) is present for r in issues)


def test_ut11_19_counters_add() -> None:
    """UT11-19 per-shard counters sum field by field in the parent."""
    total = DirtyCounters(tombstones=1)
    total.add(DirtyCounters(tombstones=2, duplicate_rows=3))
    assert total == DirtyCounters(tombstones=3, duplicate_rows=3)
    assert total.as_dict()["duplicate_rows"] == 3


@settings(max_examples=40, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=2**32 - 1),
    entity=st.sampled_from(_ENTITIES),
    dirty=st.sampled_from(["default", "heavy"]),
    month_index=st.integers(min_value=0, max_value=35),
)
def test_pt11_03_counters_equal_observable_defects(
    seed: int, entity: str, dirty: str, month_index: int
) -> None:
    """PT11-03 for any seed and entity, counters equal the defects observable in the output."""
    before, after, reemits, counters = _run(entity, dirty, 300, seed, month_index)
    assert dataclasses.asdict(counters) == _observe(entity, before, after, reemits)
