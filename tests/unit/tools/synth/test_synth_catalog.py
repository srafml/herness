"""Tests for tools.synth.catalog (U11-04): UT11-05."""

import dataclasses
import time
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from tools.synth.catalog import Catalog, build_catalog
from tools.synth.catalog_plan import background_count, planner_id
from tools.synth.catalog_rows import MonthKey, ServiceRow
from tools.synth.params import SynthParams, SynthUsageError, load_params
from tools.synth.plants_delivery import planned_counts_t6

pytestmark = pytest.mark.unit

_ALL_SOURCES = ("servicenow", "jira", "monitoring", "files")
_START = date(2023, 9, 1)
_END = date(2026, 8, 31)


def _load(scale: str, params_file: Path | None = None, **kw: Any) -> SynthParams:
    args: dict[str, Any] = {
        "start": _START,
        "end": _END,
        "sources": _ALL_SOURCES,
        "dirty": "default",
        "fetch_mode": "initial",
        "params_file": params_file,
    }
    return load_params(scale, **(args | kw))


@pytest.fixture(scope="module")
def small() -> Catalog:
    return build_catalog(42, _load("small"))


@pytest.fixture(scope="module")
def full() -> Catalog:
    return build_catalog(42, _load("full"))


@pytest.fixture(scope="module")
def tiny() -> Catalog:
    return build_catalog(42, _load("tiny"))


def _top_share(services: tuple[ServiceRow, ...], share: float = 0.05) -> float:
    weights = np.sort(np.array([s.incident_weight for s in services]))
    k = max(1, round(len(weights) * share))
    return float(weights[-k:].sum() / weights.sum())


@pytest.mark.parametrize("scale", ["small", "full"])
def test_ut11_05_standard_catalog_sizes(scale: str, small: Catalog, full: Catalog) -> None:
    """UT11-05 small and full catalogs hold 12 orgs, 150 teams and 400 services."""
    cat = small if scale == "small" else full
    assert (len(cat.orgs), len(cat.teams), len(cat.services)) == (12, 150, 400)
    assert len({t.name for t in cat.teams}) == 150
    assert len({s.name for s in cat.services}) == 400
    assert len({o.name for o in cat.orgs}) == 12


@pytest.mark.parametrize("scale", ["small", "full", "tiny"])
def test_ut11_05_plant_services_and_teams_are_pairwise_disjoint(
    scale: str, small: Catalog, full: Catalog, tiny: Catalog
) -> None:
    """UT11-05 plant services and teams are pairwise disjoint (design §5.1.5)."""
    cat = {"small": small, "full": full, "tiny": tiny}[scale]
    services, teams = cat.plants.services, cat.plants.teams
    assert len(set(services)) == len(services) == 11
    assert len(set(teams)) == len(teams) == 2
    assert not set(cat.plants.s6_peers) & set(services)
    service_ids = {s.sys_id for s in cat.services}
    assert set(services) <= service_ids
    assert set(teams) <= {t.sys_id for t in cat.teams}


@pytest.mark.parametrize("scale", ["small", "full"])
def test_ut11_05_top_five_percent_share_is_calibrated(
    scale: str, small: Catalog, full: Catalog
) -> None:
    """UT11-05 the top 5 % of services carry a share of incident weight in [0.38, 0.42]."""
    cat = small if scale == "small" else full
    assert 0.38 <= _top_share(cat.services) <= 0.42


def test_ut11_05_small_and_full_catalogs_equal(small: Catalog, full: Catalog) -> None:
    """UT11-05 small and full catalogs are equal apart from volume-dependent fields.

    Allowed differences: the month plan (preset totals differ) and incident weights. S6p and
    S6u carry 5 % of background incidents at small and 1.5 % at full (U11-04 step 9), and the
    calibration exponent follows from that; every other weight is the same base weight raised
    to that exponent (T1 weights are then clamped to the median band).
    """
    for name in ("orgs", "teams", "cis", "rels", "projects", "change_schedule", "plants"):
        assert getattr(small, name) == getattr(full, name), name
    strip = {"incident_weight": 0.0}
    assert [dataclasses.replace(s, **strip) for s in small.services] == [
        dataclasses.replace(s, **strip) for s in full.services
    ]
    skip = {small.plants.s6p, small.plants.s6u, *small.plants.t1_services}
    pairs = [
        (a.incident_weight, b.incident_weight)
        for a, b in zip(small.services, full.services, strict=True)
        if a.sys_id not in skip
    ]
    ratios = np.log([b for _, b in pairs]) / np.log([a for a, _ in pairs])
    assert np.allclose(ratios, ratios[0], rtol=1e-9)


def _team_roles(cat: Catalog) -> dict[str, set[str]]:
    """Map each plant service to its owner and support team sys_ids."""
    by_id = {s.sys_id: s for s in cat.services}
    return {
        sid: {by_id[sid].owner_team_sys_id, by_id[sid].support_team_sys_id}
        for sid in cat.plants.services
    }


@pytest.mark.parametrize(("scale", "seeds"), [("small", range(40)), ("tiny", range(120))])
def test_ut11_05_plant_teams_are_disjoint_over_seeds(scale: str, seeds: range) -> None:
    """UT11-05 plant teams are disjoint from other plants' services (design §5.1.5).

    T1 and T5 hold no role on any plant service except T1 supporting its three services and
    T5 supporting S5; S3's and S4's owner and support teams avoid T1, T5 and each other.
    """
    params = _load(scale)
    for seed in seeds:
        cat = build_catalog(seed, params)
        plants, roles = cat.plants, _team_roles(cat)
        by_id = {s.sys_id: s for s in cat.services}
        own = {plants.t1_team: set(plants.t1_services), plants.t5_team: {plants.s5}}
        for team, services in own.items():
            tied = {sid for sid, teams in roles.items() if team in teams}
            assert tied == services, (seed, team)
            assert all(by_id[sid].support_team_sys_id == team for sid in services)
            assert not any(by_id[sid].owner_team_sys_id == team for sid in services)
        s3_teams, s4_teams = roles[plants.s3], roles[plants.s4]
        assert not s3_teams & s4_teams, seed
        assert not (s3_teams | s4_teams) & set(plants.teams), seed


@pytest.mark.parametrize("seed", range(30))
def test_ut11_05_team_counts_per_org(seed: int) -> None:
    """UT11-05 standard: per-org team counts stay in [6, 20] and total 150; tiny: 4 per org."""
    for scale, low, high, total in (("small", 6, 20, 150), ("tiny", 4, 4, 12)):
        cat = build_catalog(seed, _load(scale))
        per_org = [sum(t.org_sys_id == o.sys_id for t in cat.teams) for o in cat.orgs]
        assert sum(per_org) == total
        assert all(low <= n <= high for n in per_org), (scale, per_org)


def test_ut11_05_effective_at_and_peak_window(small: Catalog, tiny: Catalog) -> None:
    """UT11-05 effective_at is 2026-05-01T00:00:00Z at standard; tiny uses end - 42 days."""
    assert small.plants.effective_at == datetime(2026, 5, 1, tzinfo=UTC)
    assert small.plants.peak_window == ("07-15", "08-31")
    assert tiny.plants.effective_at == datetime(2026, 7, 20, tzinfo=UTC)
    assert tiny.plants.peak_window == ("08-02", "08-31")
    assert small.plants.in_peak(date(2025, 8, 1))
    assert not small.plants.in_peak(date(2025, 9, 1))


def test_ut11_05_peak_window_wraps_year_end() -> None:
    """UT11-05 a tiny peak window crossing the year end still selects its 30 days."""
    cat = build_catalog(7, _load("tiny", end=date(2026, 1, 10)))
    assert cat.plants.peak_window == ("12-12", "01-10")
    assert cat.plants.in_peak(date(2025, 12, 31))
    assert cat.plants.in_peak(date(2026, 1, 1))
    assert not cat.plants.in_peak(date(2025, 12, 11))


def test_ut11_05_catalog_is_deterministic_per_seed(small: Catalog) -> None:
    """UT11-05 the same seed and params give an equal catalog; another seed differs."""
    params = _load("small")
    assert build_catalog(42, params) == small
    assert build_catalog(43, params).services != small.services


def test_ut11_05_plant_targets_follow_design(small: Catalog) -> None:
    """UT11-05 plant targets meet the criticality, team and weight rules of step 8 and 9."""
    plants = small.plants
    by_id = {s.sys_id: s for s in small.services}
    teams = {t.sys_id: t for t in small.teams}
    median = float(np.median([s.incident_weight for s in small.services]))
    for sid in plants.t1_services:
        assert by_id[sid].criticality == 2
        assert by_id[sid].support_team_sys_id == plants.t1_team
        assert abs(by_id[sid].incident_weight - median) <= 0.2 * median + 1e-9
    supported_by_t1 = {s.sys_id for s in small.services if s.support_team_sys_id == plants.t1_team}
    assert supported_by_t1 == set(plants.t1_services)
    t5_services = [s.sys_id for s in small.services if s.support_team_sys_id == plants.t5_team]
    assert t5_services == [plants.s5]
    for team in (plants.t1_team, plants.t5_team):
        assert (teams[team].mttr_multiplier, teams[team].reassign_extra) == (1.0, 0.0)
    assert by_id[plants.s2].criticality == 1
    assert by_id[plants.s2c].criticality <= 2
    assert by_id[plants.s3].criticality == 2
    c3 = next(c for c in small.cis if c.sys_id == plants.c3)
    assert (c3.sys_class_name, c3.service_sys_id) == ("cmdb_ci_appl", plants.s3)
    assert by_id[plants.s4].event_weight == 0.0
    assert all(by_id[s].criticality == 3 for s in (plants.s6p, plants.s6u, *plants.s6_peers))
    assert len(plants.s6_peers) >= 5
    decoys = [s for s in small.services if s.criticality == 4 and s.sys_id not in plants.services]
    assert by_id[plants.e2d_service].criticality == 4
    assert all(by_id[plants.e2d_service].incident_weight <= s.incident_weight for s in decoys)


@pytest.mark.parametrize(("scale", "share"), [("small", 0.05), ("full", 0.015), ("tiny", 0.05)])
def test_ut11_05_s6_services_carry_their_background_share(
    scale: str, share: float, small: Catalog, full: Catalog, tiny: Catalog
) -> None:
    """UT11-05 S6p and S6u each carry 5 % of incident weight (1.5 % at full)."""
    cat = {"small": small, "full": full, "tiny": tiny}[scale]
    total = sum(s.incident_weight for s in cat.services)
    for sid in (cat.plants.s6p, cat.plants.s6u):
        weight = next(s.incident_weight for s in cat.services if s.sys_id == sid)
        assert weight / total == pytest.approx(share)


def test_ut11_05_cis_relations_and_projects(small: Catalog) -> None:
    """UT11-05 each service has its CIs and relations; projects map one per org."""
    by_service: dict[str, list[str]] = {}
    for ci in small.cis:
        by_service.setdefault(ci.service_sys_id, []).append(ci.sys_class_name)
    for service in small.services:
        classes = by_service[service.sys_id]
        assert classes.count("cmdb_ci_service") == classes.count("cmdb_ci_appl") == 1
        assert 1 <= classes.count("cmdb_ci_server") <= 3
        assert classes.count("cmdb_ci_db_instance") == (1 if service.criticality <= 2 else 0)
        assert service.jira_component == service.name
    assert {c.sys_id for c in small.cis if c.sys_class_name == "cmdb_ci_service"} == {
        s.sys_id for s in small.services
    }
    ci_ids = {c.sys_id for c in small.cis}
    assert len(ci_ids) == len(small.cis)
    assert all(r.parent in ci_ids and r.child in ci_ids for r in small.rels)
    assert {r.type for r in small.rels} == {"Depends on::Used by", "Runs on::Runs"}
    assert len(small.rels) == len(small.cis) - len(small.services)
    keys = [p.key for p in small.projects]
    assert len(set(keys)) == len(keys) == 12
    assert all(len(k) == 4 and k[:3].isalpha() and k[:3].isupper() for k in keys)
    assert {s.jira_project for s in small.services} <= set(keys)


def test_ut11_05_change_schedule_has_the_t3_changes(small: Catalog) -> None:
    """UT11-05 the change schedule holds 40 emergency and 40 normal changes on C3."""
    slots = small.change_schedule
    assert len(slots) == 80
    assert sum(s.emergency for s in slots) == 40
    assert [s.seq for s in slots] == list(range(80))
    assert [s.work_end for s in slots] == sorted(s.work_end for s in slots)
    assert all(2 <= s.follow_up_incidents <= 6 for s in slots if s.emergency)
    assert all(s.follow_up_incidents == 0 for s in slots if not s.emergency)
    first = datetime(2023, 9, 1, tzinfo=UTC)
    assert all(first <= s.work_end <= datetime(2026, 8, 31, 22, tzinfo=UTC) for s in slots)


def test_ut11_05_month_counts_split_preset_totals() -> None:
    """UT11-05 month counts split each preset total by days; seq_start is the prefix sum."""
    small = build_catalog(42, _load("small"), planners=())  # background plan only
    tiny = build_catalog(42, _load("tiny"), planners=())
    totals: dict[tuple[str, str], int] = {}
    for (source, entity, month), count in small.month_counts.items():
        assert month.day == 1
        totals[source, entity] = totals.get((source, entity), 0) + count
    assert totals == {
        ("jira", "issue"): 4_000,
        ("monitoring", "event"): 40_000,
        ("monitoring", "metric_daily"): 400 * 4 * 1_096,
        ("servicenow", "change_request"): 10_000,
        ("servicenow", "incident"): 100_000,
        ("servicenow", "problem"): 1_500,
    }
    assert len([k for k in small.month_counts if k[1] == "incident"]) == 36
    assert small.month_counts["servicenow", "incident", date(2024, 2, 1)] in (2_645, 2_646)
    assert set(tiny.month_counts) == set(tiny.seq_start)
    assert tiny.seq_start["servicenow", "incident", date(2026, 6, 1)] == 1
    july = tiny.seq_start["servicenow", "incident", date(2026, 7, 1)]
    assert july == 1 + tiny.month_counts["servicenow", "incident", date(2026, 6, 1)]


def test_ut11_05_default_planners_add_plant_records(small: Catalog) -> None:
    """UT11-05 the default plan adds the T3 changes and follow-ups, 7 % S4 events, the
    T5 peak-day incidents, the T2/T2c clusters and four epics (E2, E2d, E6p, E6u) to the
    background totals and removes 40 % of S6p's expected post-effect incidents (T6);
    plant ranges close each month."""
    totals: dict[tuple[str, str], int] = {}
    for (source, entity, _), count in small.month_counts.items():
        totals[source, entity] = totals.get((source, entity), 0) + count
    follow_ups = sum(s.follow_up_incidents for s in small.change_schedule)
    assert totals["servicenow", "change_request"] == 10_000 + 80
    assert totals["monitoring", "event"] == 40_000 + 2_800
    clusters = round(0.03 * 100_000) + round(10 * ((_END - _START).days + 1) / 30)
    params = _load("small")
    thinned = planned_counts_t6(build_catalog(42, params, planners=()), params)
    t6 = sum(n for key, n in thinned.items() if key[1] == "incident")
    assert -0.4 * 100_000 < t6 < 0  # 40 % of S6p's expected post-effect incidents
    t5 = totals["servicenow", "incident"] - 100_000 - follow_ups - clusters - t6
    assert 0 < t5 < 100_000  # three peak windows of S5 extras
    assert totals["jira", "issue"] == 4_000 + 4
    for (pid, key), (first, n) in small.plant_seq.items():
        assert pid.startswith(("tools.synth.plants_ops", "tools.synth.plants_delivery"))
        assert small.seq_start[key] <= first
        assert first + n <= small.seq_start[key] + small.month_counts[key]


def test_ut11_05_month_counts_follow_selected_sources() -> None:
    """UT11-05 only selected sources get a month plan."""
    cat = build_catalog(42, _load("tiny", sources=("jira",)))
    assert {key[:2] for key in cat.month_counts} == {("jira", "issue")}


def test_ut11_05_planned_plant_counts_are_added() -> None:
    """UT11-05 planners (U11-10..U11-15 `planned_counts`) add records before seq_start."""
    params = _load("tiny")
    base = build_catalog(42, params, planners=())
    june = date(2026, 6, 1)

    def planner(stub: Catalog, p: SynthParams) -> Mapping[MonthKey, int]:
        assert stub.plants == base.plants
        assert stub.month_counts == base.month_counts
        return {("servicenow", "incident", june): 5, ("monitoring", "unknown", june): 3}

    cat = build_catalog(42, params, planners=(planner,))
    key = ("servicenow", "incident", june)
    assert cat.month_counts[key] == base.month_counts[key] + 5
    assert cat.month_counts["monitoring", "unknown", june] == 3
    july = ("servicenow", "incident", date(2026, 7, 1))
    assert cat.seq_start[july] == base.seq_start[july] + 5
    after_background = base.seq_start[key] + base.month_counts[key]
    assert cat.plant_seq[planner_id(planner), key] == (after_background, 5)
    assert background_count(cat, key) == base.month_counts[key]

    def negative(stub: Catalog, p: SynthParams) -> Mapping[MonthKey, int]:
        return {key: -10_000}

    with pytest.raises(SynthUsageError):
        build_catalog(42, params, planners=(negative,))


def test_ut11_05_inconsistent_preset_raises(tmp_path: Path) -> None:
    """UT11-05 a criticality mix with too few criticality-3 services raises SynthUsageError."""
    path = tmp_path / "params.yaml"
    mix = {1: 0.3, 2: 0.3, 3: 0.01, 4: 0.39}
    path.write_text(yaml.safe_dump({"org": {"criticality": mix}}), encoding="utf-8")
    with pytest.raises(SynthUsageError):
        build_catalog(42, _load("tiny", params_file=path))


def test_ut11_05_tiny_catalog_builds_for_many_seeds() -> None:
    """UT11-05 tiny catalogs build (3 orgs, 12 teams, 20 services) for a range of seeds."""
    params = _load("tiny")
    for seed in range(25):
        cat = build_catalog(seed, params)
        assert (len(cat.orgs), len(cat.teams), len(cat.services)) == (3, 12, 20)
        assert cat.plants.effective_at == datetime.combine(
            params.end - timedelta(days=42), datetime.min.time(), UTC
        )


def test_ut11_05_full_catalog_builds_under_one_second() -> None:
    """UT11-05 acceptance: the catalog for full builds in < 1 s."""
    params = _load("full")
    started = time.perf_counter()
    build_catalog(42, params)
    assert time.perf_counter() - started < 1.0
