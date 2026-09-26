"""Delivery plant T6 funded outcomes and the plant epic builder (U11-15, design §5.1.5).

Split out of `tools.synth.plants_delivery` (which re-exports `plant_t6` and
`planned_counts_t6`) for the module line budget; the epic builder is shared by E2, E2d,
E6p and E6u.

Thinning and the plan: `planned_counts_t6` books a negative count in each month after
`effective_at + 14 days`, so the month's background shrinks and `seq_start` stays
gap-free. `plant_t6` then drops each S6p incident of that window with probability 0.4 (in
record order) and puts in its place, with the same number and open time, a background-like
incident of any service except S6p, drawn by the background incident weights renormalised
without S6p (`refill_pool`). The record count therefore stays the planned count and S6p
loses 40 % of its post-effect volume. The planned shrink equals the expected refills
(`t6_drop`), so every other service, S6u and the other plant services included, keeps its
background volume in expectation. The shrink is spread over the whole boundary month while
refills land on post-effect days only, so S6p's post/pre ratio there is about
0.6 x (1 - drop / month count), within the UT11-18 tolerance. The kept S6p incidents
resolve after 0.8 x their MTTR.

Plant order: on an incident shard, U11-19 applies T6 to the background records first,
then T1, T3 and T5, so a refill is a background record for them (a refill on a T1
service, on S5 or on C3 gets the same treatment as any background incident). A replaced
record's id is listed in `PlantOutput.replaced`; U11-19 drops its label and PII rows.
"""

import dataclasses
import math
from datetime import datetime, timedelta
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from tools.synth import jira_changelog as lifecycle
from tools.synth.catalog_plan import plant_range, span_months
from tools.synth.catalog_rows import Catalog, MonthKey, ServiceRow
from tools.synth.jira_changelog import DONE, IN_PROGRESS, TODO, Step, jira_ts
from tools.synth.jira_links import remote_link
from tools.synth.params import SynthParams
from tools.synth.servicenow_common import (
    Record,
    day_weight,
    parse_ts,
    service_index,
    shard_days,
    span_start,
)
from tools.synth.servicenow_incidents import IncidentSpec, make_incident, retime
from tools.synth.shards import PlantOutput, Shard
from tools.synth.text import TemplateBank

Issue = dict[str, Any]

EPIC_START_LAG: Final = timedelta(days=1)  # epic created -> In Progress
T6_DROP: Final = 0.4
T6_MTTR: Final = 0.8
T6_SETTLE: Final = timedelta(days=14)  # effect starts at effective_at + 14 days
E6_LEAD: Final = timedelta(days=14)  # E6p/E6u created 14 days before effective_at
E6_POINTS: Final = 21
E6_COST: Final = 70_000
_UPDATE_LAG_S: Final = 2 * 3600.0  # updated = latest change + U(0, 2 h), as for background


def find_service(cat: Catalog, sys_id: str) -> ServiceRow:
    return next(s for s in cat.services if s.sys_id == sys_id)


@dataclasses.dataclass(frozen=True, slots=True)
class EpicSpec:
    """A plant epic: service (project, component, owner team), sequence number, created
    time, text, (points, cost estimate), status transitions and remote-link tickets."""

    service: ServiceRow
    seq: int
    created: datetime
    summary: str
    description: str
    sizing: tuple[int, int]
    steps: tuple[Step, ...]
    remotelinks: tuple[str, ...] = ()


def epic_issue(cat: Catalog, spec: EpicSpec, rng: np.random.Generator) -> Issue:
    """A Jira Cloud-shaped Epic issue shaped like a background issue (one `rng` draw)."""
    project = next(p for p in cat.projects if p.key == spec.service.jira_project)
    team = next(t.name for t in cat.teams if t.sys_id == spec.service.owner_team_sys_id)
    issue_id = str(project.id_base + spec.seq)
    steps = list(spec.steps)
    status, resolved = lifecycle.status(steps)
    latest = max([spec.created, *(s[0] for s in steps)])
    updated = latest + timedelta(seconds=float(rng.uniform(0.0, _UPDATE_LAG_S)))
    fields: Issue = {
        "issuetype": {"name": "Epic"},
        "project": {"key": project.key, "name": project.name},
        "components": [{"name": spec.service.jira_component}],
        "labels": [],
        "status": {
            "name": status,
            "id": lifecycle.STATUS_IDS[status],
            "statusCategory": {"key": lifecycle.CATEGORIES[status]},
        },
        "created": jira_ts(spec.created),
        "resolutiondate": None if resolved is None else jira_ts(resolved),
        "customfield_10016": spec.sizing[0],
        "customfield_10050": spec.sizing[1],
        "customfield_10060": team,
        "summary": spec.summary,
        "description": spec.description,
        "updated": jira_ts(updated),
        "issuelinks": [],
    }
    links = [remote_link(f"{issue_id}{k + 1:02d}", t) for k, t in enumerate(spec.remotelinks)]
    return {
        "id": issue_id,
        "key": f"{project.key}-{spec.seq}",
        "fields": fields,
        "changelog": lifecycle.changelog(issue_id, steps),
        "remotelinks": links,
    }


def e6_created(cat: Catalog, params: SynthParams) -> datetime:
    """Created time of E6p and E6u: 14 days before `effective_at`, not before the span."""
    return max(cat.plants.effective_at - E6_LEAD, span_start(params))


def t6_drop(share: float, count: float, post: float) -> float:
    """Expected shrink D of a month with `count` stub records, S6p incident-weight share
    `share` and post-effect day-weight fraction `post`: D x (1 - share) =
    0.4 x share x post x (count - D), i.e. the shrink equals the expected refills that
    land on services other than S6p, so each of them keeps `share_s x count`."""
    thinned = T6_DROP * share * post
    return thinned * count / (1.0 - share + thinned)


def planned_counts_t6(cat: Catalog, params: SynthParams) -> dict[MonthKey, int]:
    """E6p and E6u in the Jira month of their creation; per incident month with days from
    `effective_at + 14 days`, minus round(`t6_drop`) with S6p's incident-weight share and
    the post-effect fraction of the arrival model's day weights."""
    counts: dict[MonthKey, int] = {
        ("jira", "issue", e6_created(cat, params).date().replace(day=1)): 2
    }
    share = find_service(cat, cat.plants.s6p).incident_weight / math.fsum(
        s.incident_weight for s in cat.services
    )
    since = (cat.plants.effective_at + T6_SETTLE).date()
    for month, _ in span_months(params):
        key = ("servicenow", "incident", month)
        weights = {d: day_weight(params.arrival, d) for d in shard_days(params, month)}
        post = math.fsum(w for d, w in weights.items() if d >= since)
        if key in cat.month_counts and post:
            fraction = post / math.fsum(weights.values())
            if drop := round(t6_drop(share, cat.month_counts[key], fraction)):
                counts[key] = -drop
    return counts


def refill_pool(cat: Catalog) -> tuple[list[ServiceRow], npt.NDArray[np.float64]]:
    """Services a dropped S6p slot is refilled from (every service except S6p) and their
    probabilities (the background incident weights renormalised without S6p)."""
    index = service_index(cat)
    keep = np.array([s.sys_id != cat.plants.s6p for s in index.services], dtype=bool)
    services = [s for s, k in zip(index.services, keep, strict=True) if k]
    p = np.asarray(index.p, dtype=np.float64)[keep]
    return services, p / p.sum()


def _thin(
    records: list[Record],
    cat: Catalog,
    params: SynthParams,
    rng: np.random.Generator,
) -> PlantOutput:
    index, bank = service_index(cat), TemplateBank()
    services, p = refill_pool(cat)
    since = cat.plants.effective_at + T6_SETTLE
    out = PlantOutput()
    for k, record in enumerate(records):
        opened = parse_ts(record["opened_at"])
        if (
            record["business_service"]["value"] != cat.plants.s6p
            or opened is None
            or opened < since
        ):
            continue
        if rng.random() < T6_DROP:
            service = services[int(rng.choice(len(services), p=p))]
            seq = int(record["number"]["value"][3:])
            out.replaced.append(f"servicenow:incident:{record['sys_id']['value']}")
            spec = IncidentSpec(service, opened, seq)
            records[k], labels = make_incident(params, rng, bank, index, spec)
            out.labels.extend(labels)
            continue
        resolved = parse_ts(record["resolved_at"])
        mttr = None
        if resolved is not None:
            mttr = timedelta(seconds=round((resolved - opened).total_seconds() * T6_MTTR))
        retime(record, params, rng, opened=opened, mttr=mttr)
    return out


def _e6_epics(
    shard: Shard, cat: Catalog, params: SynthParams, rng: np.random.Generator
) -> PlantOutput:
    first, n = plant_range(cat, planned_counts_t6, (shard.source, shard.entity, shard.month))
    if not n:
        return PlantOutput()
    created, done = e6_created(cat, params), cat.plants.effective_at
    steps = ((created + EPIC_START_LAG, TODO, IN_PROGRESS), (done, IN_PROGRESS, DONE))
    out = PlantOutput()
    for i, sys_id in enumerate((cat.plants.s6p, cat.plants.s6u)):
        service = find_service(cat, sys_id)
        spec = EpicSpec(
            service,
            first + i,
            created,
            f"Reduce incident volume on {service.name}",
            f"h3. Context\n{service.name} needs fewer incidents and faster recovery.",
            (E6_POINTS, E6_COST),
            steps,
        )
        out.records.append(epic_issue(cat, spec, rng))
    return out


def plant_t6(
    records: list[Record],
    shard: Shard,
    cat: Catalog,
    params: SynthParams,
    rng: np.random.Generator,
) -> PlantOutput:
    """T6 for one shard (lake side). `servicenow/incident`: S6p incidents opened at or after
    `effective_at + 14 days` are dropped with probability 0.4 in record order (each
    replaced in `records` by an incident of a `refill_pool` service with the same number
    and open time; its labels are returned and the dropped record's id is listed in
    `replaced`, whose label and PII rows U11-19 discards); the kept ones get MTTR x 0.8;
    S6u is untouched. Runs on the background before T1, T3 and T5. `jira/issue` of the
    creation month: epics E6p and E6u, Done with `resolutiondate` = `effective_at`. Other
    shards: nothing."""
    if (shard.source, shard.entity) == ("servicenow", "incident"):
        return _thin(records, cat, params, rng)
    if (shard.source, shard.entity) == ("jira", "issue"):
        return _e6_epics(shard, cat, params, rng)
    return PlantOutput()


__all__ = [
    "E6_COST",
    "E6_POINTS",
    "EPIC_START_LAG",
    "T6_DROP",
    "T6_MTTR",
    "T6_SETTLE",
    "EpicSpec",
    "e6_created",
    "epic_issue",
    "find_service",
    "planned_counts_t6",
    "plant_t6",
    "refill_pool",
    "t6_drop",
]
