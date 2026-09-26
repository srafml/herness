"""Delivery plants T2, T2c and T6 (U11-11, U11-15, design §5.1.5).

Each plant has a `planned_counts_*` planner registered in
`tools.synth.catalog.default_planners()` after the operations planners, and its records
take their count and first sequence number from the catalog plan (`plant_range`). The T2
cluster open times come from a stream keyed by S2 and the month, so the Jira shard that
writes epic E2 knows in advance which incident numbers its remote links name. T6 lives in
`tools.synth.plants_delivery_t6` for the line budget and is re-exported here.
"""

import dataclasses
from datetime import date, datetime, timedelta
from typing import Final

import numpy as np

from tools.synth.catalog_plan import plant_range, span_months, split_counts
from tools.synth.catalog_rows import Catalog, MonthKey, Planner
from tools.synth.jira_changelog import IN_PROGRESS, TODO
from tools.synth.param_groups import TextParams
from tools.synth.params import SynthParams
from tools.synth.plants_delivery_t6 import (
    EPIC_START_LAG,
    EpicSpec,
    epic_issue,
    find_service,
    planned_counts_t6,
    plant_t6,
)
from tools.synth.rng import shard_key_hash
from tools.synth.servicenow_common import arrival_times, number, service_index, span_start
from tools.synth.servicenow_incidents import IncidentSpec, make_incident
from tools.synth.shards import PlantOutput, Shard
from tools.synth.text import TemplateBank, render_incident_text, render_jira_text

T2_FAMILY: Final = "tpl_conn_pool"  # root cause `capacity`
T2C_FAMILY: Final = "tpl_cert_expiry"  # root cause `access_identity`
E2_POINTS: Final = 34
E2_COST: Final = 120_000
E2D_POINTS: Final = 400
E2D_COST: Final = 900_000
E2_LINKS: Final = 20  # cluster incidents named in E2 remotelinks
_T2_SHARE: Final = 0.03  # of preset.incidents
_T2_P2_PERCENT: Final = 40  # remaining P3
_T2C_PER_30_DAYS: Final = 10
_T2C_P2_PERCENT: Final = 60  # remaining P3
_E2_OFFSET: Final = timedelta(days=30)  # epics in the Jira shard of start + 30 days
_P2, _P3 = 2, 3
_TIMES_STREAM: Final = "plants:t2:times"
_SLOTS_STREAM: Final = "plants:cluster:slots"
_INCIDENT: Final = ("servicenow", "incident")
_ISSUE: Final = ("jira", "issue")


@dataclasses.dataclass(frozen=True, slots=True)
class _Cluster:
    """A planted recurring cluster: its planner, service, family, P2 share and total."""

    planner: Planner
    service: str
    family: str
    p2_percent: int
    total: int
    keyed_times: bool  # open times from the S2/month stream (T2) instead of the shard rng


def _t2_total(params: SynthParams) -> int:
    return round(_T2_SHARE * params.preset.incidents)


def _t2c_total(params: SynthParams) -> int:
    return round(_T2C_PER_30_DAYS * ((params.end - params.start).days + 1) / 30)


def _split(params: SynthParams, total: int) -> list[tuple[date, int, int]]:
    """(month, count, global index of the month's first record): `total` by days."""
    months = span_months(params)
    counts = split_counts(total, [days for _, days in months])
    out, offset = [], 0
    for (month, _), n in zip(months, counts, strict=True):
        out.append((month, n, offset))
        offset += n
    return out


def _incident_counts(params: SynthParams, total: int) -> dict[MonthKey, int]:
    return {(*_INCIDENT, m): n for m, n, _ in _split(params, total) if n}


def epic_month(params: SynthParams) -> date:
    """First day of the month of `start + 30 days`, whose Jira shard writes E2 and E2d."""
    return (span_start(params) + _E2_OFFSET).date().replace(day=1)


def planned_counts_t2(cat: Catalog, params: SynthParams) -> dict[MonthKey, int]:
    """round(0.03 x preset.incidents) S2 cluster incidents split across months by days in
    span, and the two epics E2 and E2d in the Jira month of `start + 30 days`."""
    return _incident_counts(params, _t2_total(params)) | {(*_ISSUE, epic_month(params)): 2}


def planned_counts_t2c(cat: Catalog, params: SynthParams) -> dict[MonthKey, int]:
    """round(10 x span days / 30) S2c cluster incidents split across months by days."""
    return _incident_counts(params, _t2c_total(params))


def _priority(k: int, p2_percent: int) -> int:
    """P2 for exactly floor(n x share) of the first n records (evenly spread), else P3."""
    return _P2 if (k + 1) * p2_percent // 100 > k * p2_percent // 100 else _P3


def cluster_slots(bank: TemplateBank, family: str, service_sys_id: str) -> dict[str, str]:
    """The fixed slot set of a planted cluster, derived from its family and service."""
    rng = np.random.default_rng(shard_key_hash((_SLOTS_STREAM, family, service_sys_id)))
    rendered = render_incident_text(
        bank,
        rng,
        family=family,
        slots=None,
        change_flavored=False,
        repeat_flavored=False,
        impact_level=0,
        component="",
        text=TextParams(),
    )
    return dict(rendered.slots)


def t2_times(cat: Catalog, params: SynthParams, month: date, n: int) -> list[datetime]:
    """Sorted open times of the month's `n` T2 cluster incidents (S2/month stream)."""
    rng = np.random.default_rng(shard_key_hash((_TIMES_STREAM, cat.plants.s2, month.isoformat())))
    return arrival_times(params, Shard(*_INCIDENT, month, n, 0, 0), rng, n)


def _plant_cluster(
    shard: Shard,
    cat: Catalog,
    params: SynthParams,
    rng: np.random.Generator,
    bank: TemplateBank,
    c: _Cluster,
) -> PlantOutput:
    first, n = plant_range(cat, c.planner, (shard.source, shard.entity, shard.month))
    if not n:
        return PlantOutput()
    if c.keyed_times:
        times = t2_times(cat, params, shard.month, n)
    else:
        times = arrival_times(params, shard, rng, n)
    offset = next(o for m, _, o in _split(params, c.total) if m == shard.month)
    service, index = find_service(cat, c.service), service_index(cat)
    slots = cluster_slots(bank, c.family, service.sys_id)
    out = PlantOutput()
    for i, at in enumerate(times):
        spec = IncidentSpec(
            service,
            at,
            first + i,
            priority=_priority(offset + i, c.p2_percent),
            family=c.family,
            slots=slots,
            repeat=True,
            customer_impact=True,
        )
        record, labels = make_incident(params, rng, bank, index, spec)
        out.records.append(record)
        out.labels.extend(labels)
        out.members.append(f"servicenow:incident:{record['sys_id']['value']}")
    return out


def e2_links(cat: Catalog, params: SynthParams) -> list[str]:
    """Numbers of the first 20 T2 cluster incidents opened after `start + 30 days`
    (numbers follow open times within each month, months in order)."""
    after, found = span_start(params) + _E2_OFFSET, list[str]()
    for month, _ in span_months(params):
        first, n = plant_range(cat, planned_counts_t2, (*_INCIDENT, month))
        for i, at in enumerate(t2_times(cat, params, month, n)):
            if at > after and len(found) < E2_LINKS:
                found.append(number("INC", first + i))
    return found


def _t2_epics(
    shard: Shard,
    cat: Catalog,
    params: SynthParams,
    rng: np.random.Generator,
    bank: TemplateBank,
) -> PlantOutput:
    first, n = plant_range(cat, planned_counts_t2, (shard.source, shard.entity, shard.month))
    if not n:
        return PlantOutput()
    created = span_start(params) + _E2_OFFSET
    s2, decoy = find_service(cat, cat.plants.s2), find_service(cat, cat.plants.e2d_service)
    text = render_jira_text(bank, rng, issue_type="epic", component=s2.name, theme=T2_FAMILY)
    e2 = EpicSpec(
        s2,
        first,
        created,
        f"Rework database connection pooling for {s2.name}",
        f"h3. Context\n{text.description}",
        (E2_POINTS, E2_COST),
        ((created + EPIC_START_LAG, TODO, IN_PROGRESS),),
        tuple(e2_links(cat, params)),
    )
    decoy_text = render_jira_text(
        bank, rng, issue_type="epic", component=decoy.name, theme=None
    ).description
    e2d = EpicSpec(
        decoy,
        first + 1,
        created,
        f"Urgent: critical risk of outage exposure in {decoy.name}",
        f"h3. Context\n{decoy_text}\nThis is urgent: a critical risk with broad outage exposure.",
        (E2D_POINTS, E2D_COST),
        (),
    )
    return PlantOutput(records=[epic_issue(cat, e2, rng), epic_issue(cat, e2d, rng)])


def plant_t2(
    shard: Shard,
    cat: Catalog,
    params: SynthParams,
    rng: np.random.Generator,
    bank: TemplateBank,
) -> PlantOutput:
    """T2 for one shard. `servicenow/incident`: the month's S2 cluster incidents (40 % P2,
    the rest P3, all with customer impact, family `tpl_conn_pool` with one fixed slot set).
    `jira/issue` of the month of `start + 30 days`: epic E2 (34 points, 120,000, In
    Progress, remote links to 20 cluster incidents) and the decoy E2d (400 points,
    900,000). Other shards: nothing."""
    kind = (shard.source, shard.entity)
    if kind == _INCIDENT:
        total = _t2_total(params)
        c = _Cluster(planned_counts_t2, cat.plants.s2, T2_FAMILY, _T2_P2_PERCENT, total, True)
        return _plant_cluster(shard, cat, params, rng, bank, c)
    if kind == _ISSUE:
        return _t2_epics(shard, cat, params, rng, bank)
    return PlantOutput()


def plant_t2c(
    shard: Shard,
    cat: Catalog,
    params: SynthParams,
    rng: np.random.Generator,
    bank: TemplateBank,
) -> PlantOutput:
    """T2c for one `servicenow/incident` shard: the month's S2c cluster incidents (60 % P2,
    40 % P3, all with customer impact, family `tpl_cert_expiry`, no work-item link)."""
    if (shard.source, shard.entity) != _INCIDENT:
        return PlantOutput()
    total = _t2c_total(params)
    c = _Cluster(planned_counts_t2c, cat.plants.s2c, T2C_FAMILY, _T2C_P2_PERCENT, total, False)
    return _plant_cluster(shard, cat, params, rng, bank, c)


__all__ = [
    "E2D_COST",
    "E2D_POINTS",
    "E2_COST",
    "E2_LINKS",
    "E2_POINTS",
    "T2C_FAMILY",
    "T2_FAMILY",
    "cluster_slots",
    "e2_links",
    "epic_month",
    "planned_counts_t2",
    "planned_counts_t2c",
    "planned_counts_t6",
    "plant_t2",
    "plant_t2c",
    "plant_t6",
    "t2_times",
]
