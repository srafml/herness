"""Tests for tools.synth.dirty (U11-16): UT11-19 and PT11-03."""

import copy
import dataclasses
import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from tools.synth.dirty import (
    COST_FIELD,
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
_FUTURE_FROM = datetime(2025, 3, 31, tzinfo=UTC)  # span end + 1 year: only future_ts lies beyond


def _params(dirty: str, params_file: Path | None = None) -> SynthParams:
    return load_params(
        "tiny",
        start=date(2024, 1, 1),
        end=date(2024, 3, 31),
        sources=("servicenow", "jira", "monitoring"),
        dirty=dirty,  # type: ignore[arg-type]
        fetch_mode="initial",
        params_file=params_file,
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


def _ts(value: dict[str, str]) -> datetime | None:
    """Parsed stamp; `None` when empty or unparseable (a dirty value)."""
    try:
        return parse_ts(value)
    except ValueError:
        return None


def _canon(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True)


def _bad(value: dict[str, str]) -> bool:
    text = value["value"]
    return text in {"", "31/02/2024"} or text.isdigit()


def _expected_stamps(entity: str, rec: Rec) -> tuple[str, ...]:
    """Stamps a clean record always fills: all of them, except on incidents by state
    (2 open: no `resolved_at`/`closed_at`; 6 resolved: no `closed_at`)."""
    if entity == "incident":
        return _TS_FIELDS[entity][: {"2": 2, "6": 3}.get(rec["state"]["value"], 4)]
    return _TS_FIELDS[entity]


def _observe(entity: str, after: list[Rec], reemits: list[Rec]) -> dict[str, int]:
    """Defect counts read off the output records alone (what downstream DQ can see)."""
    seen = dict.fromkeys([f.name for f in dataclasses.fields(DirtyCounters)], 0)
    for rec in after:
        if entity in _TS_FIELDS:
            seen["bad_timestamp"] += any(_bad(rec[f]) for f in _expected_stamps(entity, rec))
            kind = rec["priority" if entity == "incident" else "type"]["value"]
            seen["unknown_enum"] += kind in {"P2-ish", "Emergency "}
        if entity == "incident":
            opened, resolved = _ts(rec["opened_at"]), _ts(rec["resolved_at"])
            seen["future_ts"] += opened is not None and opened > _FUTURE_FROM
            back = opened is not None and resolved is not None and resolved < opened
            seen["resolved_before_opened"] += back
            gone = rec["business_service"]["value"] == "" and rec["cmdb_ci"]["value"] == ""
            seen["missing_service"] += gone
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
    entity: str,
    dirty: str,
    n: int,
    seed: int,
    month_index: int = 0,
    params: SynthParams | None = None,
) -> tuple[list[Rec], list[Rec], list[Rec], DirtyCounters]:
    records = _records(entity, n)
    before = copy.deepcopy(records)
    counters = DirtyCounters()
    rng = np.random.default_rng(seed)
    p = _params(dirty) if params is None else params
    reemits = apply_dirty(entity, records, month_index, p, rng, counters)
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
    _, after, reemits, counters = _run(entity, dirty, _N, seed=19)
    counts = dataclasses.asdict(counters)
    assert counts == _observe(entity, after, reemits)
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
        new_at, old_at = _ts(rec["sys_updated_on"]), _ts(orig["sys_updated_on"])
        assert new_at is not None
        assert old_at is not None
        assert timedelta(hours=1) <= new_at - old_at <= timedelta(days=10)
        assert {orig["state"]["value"], rec["state"]["value"]} in ({"2", "6"}, {"7"})
        opened, resolved = _ts(rec["opened_at"]), _ts(rec["resolved_at"])
        if rec["state"]["value"] == "6":  # newly resolved: stamped, not before opened_at
            assert resolved is not None
            assert resolved <= new_at
            assert opened is None or opened <= resolved
    # later versions add no observable defect: per type, a copy shows what its record shows
    origs = [by_id[rec["sys_id"]["value"]] for rec in later]
    assert _observe("incident", later, []) == _observe("incident", origs, [])
    tombs = [r for r in reemits if r.get("__tombstone__")]
    assert tombs
    for tomb in tombs:
        assert set(tomb) == {"__tombstone__", "key", "deleted_at"}
        assert tomb["key"] in by_id
        deleted, updated = _ts(pair(tomb["deleted_at"])), _ts(by_id[tomb["key"]]["sys_updated_on"])
        assert deleted is not None
        assert updated is not None
        assert deleted > updated


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


def test_ut11_19_heavy_cap_and_consistent_future(tmp_path: Path) -> None:
    """UT11-19 a default rate whose 5 x exceeds 1 is capped at 1.0, so every eligible record
    is hit; `future_ts` shifts the whole lifecycle, so it adds no resolved-before-opened."""
    path = tmp_path / "params.yaml"
    rates_yaml = {"dirty_rates": {"missing_service": 0.3, "future_ts": 0.3}}
    path.write_text(json.dumps(rates_yaml), encoding="utf-8")  # JSON is valid YAML
    params = _params("heavy", path)
    rates = effective_rates(params)
    assert rates["missing_service"] == rates["future_ts"] == 1.0
    _, after, reemits, counters = _run("incident", "heavy", 500, seed=9, params=params)
    assert counters.missing_service == counters.future_ts == 500
    assert dataclasses.asdict(counters) == _observe("incident", after, reemits)
    for rec in after:
        opened = _ts(rec["opened_at"])
        assert opened is not None
        assert opened > _FUTURE_FROM
        for name in ("u_acknowledged_at", "closed_at"):
            at = _ts(rec[name])
            assert at is None or at >= opened


def test_ut11_19_later_version_stamp_not_before_future_opened(tmp_path: Path) -> None:
    """UT11-19 with future_ts and later_versions at 1.0, a later version's new
    `resolved_at`/`closed_at` stamp is never before the shifted `opened_at`, so later
    versions show exactly the defects of their records."""
    path = tmp_path / "params.yaml"
    rates_yaml = {"dirty_rates": {"future_ts": 0.3, "later_versions": 0.3}}
    path.write_text(json.dumps(rates_yaml), encoding="utf-8")  # JSON is valid YAML
    params = _params("heavy", path)
    _, after, reemits, counters = _run("incident", "heavy", 400, seed=11, params=params)
    assert counters.future_ts == counters.later_versions == 400
    by_id = {rec["sys_id"]["value"]: rec for rec in after}
    outputs = {_canon(rec) for rec in after}
    later = [r for r in reemits if not r.get("__tombstone__") and _canon(r) not in outputs]
    assert len(later) == 400
    origs = [by_id[rec["sys_id"]["value"]] for rec in later]
    assert _observe("incident", later, []) == _observe("incident", origs, [])
    stamped = 0
    for rec, orig in zip(later, origs, strict=True):
        opened = _ts(rec["opened_at"])
        assert opened is not None
        for name in ("resolved_at", "closed_at"):
            if rec[name] != orig[name]:
                stamped += 1
                at = _ts(rec[name])
                assert at is not None
                assert at >= opened
    assert stamped > 0


def test_ut11_19_counters_add() -> None:
    """UT11-19 per-shard counters sum field by field in the parent."""
    total = DirtyCounters(tombstones=1)
    total.add(DirtyCounters(tombstones=2, duplicate_rows=3))
    assert total == DirtyCounters(tombstones=3, duplicate_rows=3)
    assert total.as_dict()["duplicate_rows"] == 3


@settings(max_examples=40, deadline=None)
@example(seed=805, entity="incident", dirty="heavy", month_index=0)
@example(seed=912, entity="incident", dirty="heavy", month_index=0)
@example(seed=1453, entity="incident", dirty="heavy", month_index=0)
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
    _, after, reemits, counters = _run(entity, dirty, 300, seed, month_index)
    assert dataclasses.asdict(counters) == _observe(entity, after, reemits)
