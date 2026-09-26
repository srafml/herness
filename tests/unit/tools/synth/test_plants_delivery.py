"""Tests for tools.synth.plants_delivery (U11-11, U11-15): UT11-14 and UT11-18."""

import copy
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from tools.synth.catalog import Catalog, build_catalog, default_planners
from tools.synth.catalog_plan import background_count, plant_range
from tools.synth.jira_changelog import jira_ts
from tools.synth.params import SynthParams, load_params
from tools.synth.pii import build_name_list
from tools.synth.plants_delivery import (
    E2_LINKS,
    cluster_slots,
    e2_links,
    epic_month,
    planned_counts_t2,
    planned_counts_t2c,
    planned_counts_t6,
    plant_t2,
    plant_t2c,
    plant_t6,
    t2_times,
)
from tools.synth.plants_delivery_t6 import e6_created
from tools.synth.servicenow import gen_incidents
from tools.synth.servicenow_common import service_index, span_start
from tools.synth.servicenow_incidents import IncidentSpec, make_incident
from tools.synth.shards import PlantOutput, Shard
from tools.synth.text import TemplateBank

pytestmark = pytest.mark.unit

_END = date(2026, 8, 31)  # tiny: 90 days, 2026-06-03 .. 2026-08-31
_MONTHS = (date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1))

Rec = dict[str, Any]


@pytest.fixture(scope="module")
def params() -> SynthParams:
    return load_params(
        "tiny",
        start=date(2026, 1, 1),
        end=_END,
        sources=("servicenow", "jira"),
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


def _v(record: Rec, field: str) -> str:
    value = record[field]["value"]
    assert isinstance(value, str)
    return value


def _ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _shard(cat: Catalog, source: str, entity: str, month: date) -> Shard:
    key = (source, entity, month)
    return Shard(source, entity, month, background_count(cat, key), cat.seq_start[key], 0)


def _labels(out: PlantOutput, question: str) -> list[str]:
    return [row["answer"] for row in out.labels if row["question"] == question]


def _t2_all(
    cat: Catalog, params: SynthParams, bank: TemplateBank, seed: int = 1
) -> list[PlantOutput]:
    return [
        plant_t2(
            _shard(cat, "servicenow", "incident", m),
            cat,
            params,
            np.random.default_rng([seed, m.month]),
            bank,
        )
        for m in _MONTHS
    ]


def _epics(cat: Catalog, params: SynthParams, bank: TemplateBank) -> list[Rec]:
    shard = _shard(cat, "jira", "issue", epic_month(params))
    return plant_t2(shard, cat, params, np.random.default_rng(9), bank).records


# --- UT11-14 T2 and T2c ---------------------------------------------------------------


def test_ut11_14_planners_are_registered_after_the_ops_planners(
    cat: Catalog, params: SynthParams
) -> None:
    """UT11-14 T2, T2c and T6 planners follow the four ops planners; T2 books the cluster
    by days and two epics in the month of start + 30 days, T2c 10 per 30 days."""
    assert default_planners()[4:] == (planned_counts_t2, planned_counts_t2c, planned_counts_t6)
    t2 = planned_counts_t2(cat, params)
    assert sum(n for k, n in t2.items() if k[:2] == ("servicenow", "incident")) == round(
        0.03 * 1_200
    )
    assert epic_month(params) == date(2026, 7, 1)  # 2026-06-03 + 30 days
    assert t2["jira", "issue", date(2026, 7, 1)] == 2
    assert t2["servicenow", "incident", _MONTHS[0]] < t2["servicenow", "incident", _MONTHS[1]]
    t2c = planned_counts_t2c(cat, params)
    assert sum(t2c.values()) == round(10 * 90 / 30)
    assert {k[:2] for k in t2c} == {("servicenow", "incident")}
    for planner in (planned_counts_t2, planned_counts_t2c):
        for key, n in planner(cat, params).items():
            assert plant_range(cat, planner, key)[1] == n


def test_ut11_14_t2_cluster_size_priorities_impact_and_family(
    cat: Catalog, params: SynthParams, bank: TemplateBank
) -> None:
    """UT11-14 plant_t2 over all months: cluster size = round(0.03 x 1,200); 40 % P2, the
    rest P3; all with customer impact on S2; root cause capacity; planned numbers."""
    outs = _t2_all(cat, params, bank)
    records = [r for out in outs for r in out.records]
    assert len(records) == round(0.03 * 1_200) == 36
    assert Counter(_v(r, "priority") for r in records) == {"2": 14, "3": 22}
    assert all(_v(r, "business_service") == cat.plants.s2 for r in records)
    assert all(int(_v(r, "u_customer_impact_minutes")) >= 0 for r in records)
    for month, out in zip(_MONTHS, outs, strict=True):
        first, n = plant_range(cat, planned_counts_t2, ("servicenow", "incident", month))
        numbers = [_v(r, "number") for r in out.records]
        assert numbers == [f"INC{first + i:07d}" for i in range(n)]
        opened = [_ts(_v(r, "opened_at")) for r in out.records]
        assert opened == sorted(opened)
        assert all(o.date().replace(day=1) == month for o in opened)
        assert out.members == [f"servicenow:incident:{_v(r, 'sys_id')}" for r in out.records]
        assert set(_labels(out, "root_cause")) == {"capacity"}
        assert set(_labels(out, "repeat_issue")) == {"true"}
        assert set(_labels(out, "change_caused")) == {"false"}
        assert len(out.labels) == 5 * n
        assert out.links == []


def test_ut11_14_t2_open_times_are_keyed_by_service_and_month(
    cat: Catalog, params: SynthParams, bank: TemplateBank
) -> None:
    """UT11-14 T2 open times and numbers do not depend on the shard rng, so the Jira shard
    can name the incidents; text and MTTR still come from the shard rng."""
    a, b = _t2_all(cat, params, bank, seed=1), _t2_all(cat, params, bank, seed=2)
    for x, y in zip(a, b, strict=True):
        assert [_v(r, "opened_at") for r in x.records] == [_v(r, "opened_at") for r in y.records]
        assert [_v(r, "number") for r in x.records] == [_v(r, "number") for r in y.records]
        assert [_v(r, "sys_id") for r in x.records] != [_v(r, "sys_id") for r in y.records]
    month = _MONTHS[1]
    assert t2_times(cat, params, month, 5) == t2_times(cat, params, month, 5)


def test_ut11_14_t2_cluster_uses_one_fixed_slot_set(
    cat: Catalog, params: SynthParams, bank: TemplateBank
) -> None:
    """UT11-14 the T2 slot set is fixed per family and service; with 20 % slot variation
    most descriptions carry the fixed symptom."""
    slots = cluster_slots(bank, "tpl_conn_pool", cat.plants.s2)
    assert slots == cluster_slots(bank, "tpl_conn_pool", cat.plants.s2)
    assert slots != cluster_slots(bank, "tpl_cert_expiry", cat.plants.s2c)
    records = [r for out in _t2_all(cat, params, bank) for r in out.records]
    fixed = sum(slots["symptom"].lower() in _v(r, "short_description").lower() for r in records)
    assert fixed >= 0.6 * len(records)


def test_ut11_14_e2_and_decoy_e2d(cat: Catalog, params: SynthParams, bank: TemplateBank) -> None:
    """UT11-14 E2 34 points and 120,000, In Progress, in S2's project and component, with
    exactly 20 remotelinks naming the first 20 cluster incidents after start + 30 days;
    E2d 400 points and 900,000 on a criticality-4 service with alarming words."""
    e2, e2d = _epics(cat, params, bank)
    services = {s.sys_id: s for s in cat.services}
    s2, decoy = services[cat.plants.s2], services[cat.plants.e2d_service]
    first, n = plant_range(cat, planned_counts_t2, ("jira", "issue", date(2026, 7, 1)))
    projects = {p.key: p for p in cat.projects}
    assert n == 2
    assert e2["key"] == f"{s2.jira_project}-{first}"
    assert e2["id"] == str(projects[s2.jira_project].id_base + first)
    assert e2d["key"] == f"{decoy.jira_project}-{first + 1}"
    f2, fd = e2["fields"], e2d["fields"]
    assert f2["issuetype"]["name"] == fd["issuetype"]["name"] == "Epic"
    assert (f2["customfield_10016"], f2["customfield_10050"]) == (34, 120_000)
    assert (fd["customfield_10016"], fd["customfield_10050"]) == (400, 900_000)
    assert f2["project"]["key"] == s2.jira_project
    assert f2["components"] == [{"name": s2.jira_component}]
    assert f2["status"]["name"] == "In Progress"
    assert f2["status"]["statusCategory"]["key"] == "indeterminate"
    assert f2["resolutiondate"] is None
    assert "connection pooling" in f2["summary"]
    assert e2["changelog"]["total"] == 1
    assert decoy.criticality == 4
    assert fd["components"] == [{"name": decoy.jira_component}]
    for word in ("urgent", "critical risk", "outage exposure"):
        assert word in fd["summary"].lower()
    assert e2d["remotelinks"] == []
    # the 20 titles are the first 20 generated cluster incidents opened after start + 30 d
    after = span_start(params) + timedelta(days=30)
    records = [r for out in _t2_all(cat, params, bank) for r in out.records]
    records.sort(key=lambda r: _v(r, "opened_at"))
    expected = [_v(r, "number") for r in records if _ts(_v(r, "opened_at")) > after][:20]
    titles = [link["object"]["title"] for link in e2["remotelinks"]]
    assert len(titles) == E2_LINKS == 20
    assert titles == expected == e2_links(cat, params)
    assert all("incident.do" in link["object"]["url"] for link in e2["remotelinks"])
    assert len({link["id"] for link in e2["remotelinks"]}) == 20


def test_ut11_14_t2_other_shards_add_nothing(
    cat: Catalog, params: SynthParams, bank: TemplateBank
) -> None:
    """UT11-14 T2 adds nothing to other Jira months, other entities or unplanned months."""
    rng = np.random.default_rng(0)
    june = _shard(cat, "jira", "issue", _MONTHS[0])
    assert plant_t2(june, cat, params, rng, bank) == PlantOutput()
    change = _shard(cat, "servicenow", "change_request", _MONTHS[0])
    assert plant_t2(change, cat, params, rng, bank) == PlantOutput()
    empty = Shard("servicenow", "incident", date(2025, 1, 1), 0, 1, 0)
    assert plant_t2(empty, cat, params, rng, bank) == PlantOutput()
    no_jira = build_catalog(42, params, planners=())
    assert plant_t2(june, no_jira, params, rng, bank) == PlantOutput()
    assert e2_links(no_jira, params) == []


def test_ut11_14_t2c_cluster_without_epic(
    cat: Catalog, params: SynthParams, bank: TemplateBank
) -> None:
    """UT11-14 T2c: 10 incidents per 30 days on S2c, 60 % P2, 40 % P3, all with customer
    impact, root cause access_identity, and never named by an epic."""
    outs = [
        plant_t2c(
            _shard(cat, "servicenow", "incident", m),
            cat,
            params,
            np.random.default_rng(m.month),
            bank,
        )
        for m in _MONTHS
    ]
    records = [r for out in outs for r in out.records]
    assert len(records) == 30
    assert Counter(_v(r, "priority") for r in records) == {"2": 18, "3": 12}
    assert all(_v(r, "business_service") == cat.plants.s2c for r in records)
    assert all(_v(r, "u_customer_impact_minutes") != "" for r in records)
    assert {a for out in outs for a in _labels(out, "root_cause")} == {"access_identity"}
    assert sum(len(out.members) for out in outs) == 30
    linked = {
        link["object"]["title"] for e in _epics(cat, params, bank) for link in e["remotelinks"]
    }
    assert not linked & {_v(r, "number") for r in records}
    jira = _shard(cat, "jira", "issue", epic_month(params))
    assert plant_t2c(jira, cat, params, np.random.default_rng(0), bank) == PlantOutput()


# --- UT11-18 T6 -----------------------------------------------------------------------


def test_ut11_18_planner_shrinks_s6p_post_effect_months(cat: Catalog, params: SynthParams) -> None:
    """UT11-18 the T6 plan books E6p/E6u in their creation month and a negative count in
    each month with days from effective_at + 14 days; background shrinks, seq stays
    gap-free."""
    base = build_catalog(42, params, planners=())
    plan = planned_counts_t6(base, params)  # planners see the stub (background counts)
    assert cat.plants.effective_at == datetime(2026, 7, 20, tzinfo=UTC)
    assert plan["jira", "issue", date(2026, 7, 1)] == 2
    assert e6_created(cat, params) == datetime(2026, 7, 6, tzinfo=UTC)
    incident = {k: n for k, n in plan.items() if k[:2] == ("servicenow", "incident")}
    assert set(incident) == {("servicenow", "incident", date(2026, 8, 1))}
    assert all(n < 0 for n in incident.values())
    services = {s.sys_id: s for s in cat.services}
    share = services[cat.plants.s6p].incident_weight / sum(s.incident_weight for s in cat.services)
    key = ("servicenow", "incident", date(2026, 8, 1))
    expected = 0.4 * share * base.month_counts[key] * 29 / 31  # 08-03 .. 08-31
    assert abs(-incident[key] - expected) <= 1.5  # day weights differ from 1 by a little
    assert background_count(cat, key) == base.month_counts[key] + incident[key]


def _records(cat: Catalog, params: SynthParams, bank: TemplateBank) -> list[Rec]:
    names = build_name_list(7)
    out: list[Rec] = []
    for month in _MONTHS:
        shard = _shard(cat, "servicenow", "incident", month)
        rng = np.random.default_rng([11, month.month])
        out += gen_incidents(cat, params, shard, rng, bank, names).records
    return out


def _seconds(record: Rec) -> float | None:
    resolved = _v(record, "resolved_at")
    if not resolved:
        return None
    return (_ts(resolved) - _ts(_v(record, "opened_at"))).total_seconds()


def test_ut11_18_thinning_keeps_count_numbers_and_s6u(
    cat: Catalog, params: SynthParams, bank: TemplateBank
) -> None:
    """UT11-18 plant_t6 on background incidents: the count and gap-free numbers stay the
    plan's; dropped S6p incidents are replaced in place by non-plant incidents with the
    same number and open time; kept ones have MTTR x 0.8; S6u and earlier S6p unchanged."""
    original = _records(cat, params, bank)
    records = copy.deepcopy(original)
    since = cat.plants.effective_at + timedelta(days=14)
    out = PlantOutput()
    rng = np.random.default_rng(4)
    for month in _MONTHS:
        shard = _shard(cat, "servicenow", "incident", month)
        lo = sum(background_count(cat, ("servicenow", "incident", m)) for m in _MONTHS if m < month)
        part = records[lo : lo + shard.n_records]
        got = plant_t6(part, shard, cat, params, rng)
        records[lo : lo + shard.n_records] = part
        out.labels.extend(got.labels)
    assert len(records) == len(original)
    numbers = [int(_v(r, "number")[3:]) for r in records]
    assert numbers == [int(_v(r, "number")[3:]) for r in original]
    plants = set(cat.plants.services)
    replaced = kept = 0
    for old, new in zip(original, records, strict=True):
        service = _v(old, "business_service")
        post = _ts(_v(old, "opened_at")) >= since
        if service != cat.plants.s6p or not post:
            assert new == old
        elif _v(new, "sys_id") != _v(old, "sys_id"):
            replaced += 1
            assert _v(new, "business_service") not in plants
            assert _v(new, "opened_at") == _v(old, "opened_at")
        else:
            kept += 1
            before, after = _seconds(old), _seconds(new)
            if before is not None and after is not None:
                assert abs(after - round(before * 0.8)) <= 1
    assert replaced > 0
    assert kept > 0
    assert len(out.labels) == 5 * replaced
    assert any(_v(r, "business_service") == cat.plants.s6u for r in records)


def test_ut11_18_s6p_post_effect_volume_is_sixty_percent(
    cat: Catalog, params: SynthParams, bank: TemplateBank
) -> None:
    """UT11-18 S6p post-effect volume 0.60 +/- 0.05 of the pre-effect rate (one S6p
    incident per 30 min over the span, so both rates are known exactly before thinning)."""
    services = {s.sys_id: s for s in cat.services}
    index, rng = service_index(cat), np.random.default_rng(8)
    start, step = span_start(params), timedelta(minutes=30)
    records = []
    for i in range(90 * 48):
        spec = IncidentSpec(services[cat.plants.s6p], start + i * step, 1 + i)
        records.append(make_incident(params, rng, bank, index, spec)[0])
    shard = Shard("servicenow", "incident", date(2026, 8, 1), len(records), 1, 0)
    plant_t6(records, shard, cat, params, np.random.default_rng(5))
    since = cat.plants.effective_at + timedelta(days=14)
    s6p = [_ts(_v(r, "opened_at")) for r in records if _v(r, "business_service") == cat.plants.s6p]
    pre = sum(t < since for t in s6p) / ((since - start).total_seconds() / 1800)
    post_slots = (datetime(2026, 9, 1, tzinfo=UTC) - since).total_seconds() / 1800
    post = sum(t >= since for t in s6p) / post_slots
    assert pre == 1.0
    assert abs(post / pre - 0.60) <= 0.05


def test_ut11_18_e6_epics_done_at_effective_at(cat: Catalog, params: SynthParams) -> None:
    """UT11-18 E6p (S6p project/component) and E6u (S6u) are Done with resolutiondate =
    effective_at, written by the Jira shard of their creation month."""
    shard = _shard(cat, "jira", "issue", date(2026, 7, 1))
    out = plant_t6([], shard, cat, params, np.random.default_rng(2))
    first, n = plant_range(cat, planned_counts_t6, ("jira", "issue", date(2026, 7, 1)))
    assert n == 2 == len(out.records)
    services = {s.sys_id: s for s in cat.services}
    for k, (epic, sys_id) in enumerate(
        zip(out.records, (cat.plants.s6p, cat.plants.s6u), strict=True)
    ):
        fields, service = epic["fields"], services[sys_id]
        assert epic["key"] == f"{service.jira_project}-{first + k}"
        assert fields["issuetype"]["name"] == "Epic"
        assert fields["components"] == [{"name": service.jira_component}]
        assert fields["status"]["name"] == "Done"
        assert fields["status"]["statusCategory"]["key"] == "done"
        assert fields["resolutiondate"] == jira_ts(cat.plants.effective_at)
        assert fields["created"] == jira_ts(cat.plants.effective_at - timedelta(days=14))
        assert epic["changelog"]["total"] == 2
        assert epic["remotelinks"] == []
    other = _shard(cat, "jira", "issue", date(2026, 6, 1))
    assert plant_t6([], other, cat, params, np.random.default_rng(2)) == PlantOutput()
    change = _shard(cat, "servicenow", "change_request", date(2026, 8, 1))
    assert plant_t6([], change, cat, params, np.random.default_rng(2)) == PlantOutput()
