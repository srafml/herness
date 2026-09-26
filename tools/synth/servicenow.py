"""ServiceNow records of the synthetic lake (U11-07, design §5.1.2 and §5.1.3).

Records are Table API shaped: every field is a `{value, display_value}` pair, as returned
with `sysparm_display_value=all`; references carry the target's sys_id with its name.
Field sets equal design §5.1.2; `cmdb_ci` rows omit `busines_criticality` and
`cmdb_ci_service` rows carry it (R-60). Dimension rows (groups, CIs, relations) are
stamped at `start` 00:00:00; the per-shard generators draw only from `rng`. Incidents live
in `tools.synth.servicenow_incidents`, shared helpers in `tools.synth.servicenow_common`.
Plants (T1-T6) add their own records separately.
"""

import dataclasses
import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final

import numpy as np

from tools.synth import servicenow_incidents
from tools.synth.catalog_rows import Catalog, CiRow, OrgRow, ServiceRow, TeamRow
from tools.synth.params import SynthParams
from tools.synth.servicenow_common import (
    Pair,
    Record,
    ServiceIndex,
    arrival_times,
    check_shard,
    cluster,
    new_sys_id,
    number,
    pair,
    ref,
    service_index,
    span_end,
    span_start,
    stable_id,
    ts_pair,
    updated_on,
)
from tools.synth.shards import Shard
from tools.synth.text import TemplateBank, render_change_text

_CLASS_LABELS: Final = {
    "cmdb_ci_service": "Business Service",
    "cmdb_ci_appl": "Application",
    "cmdb_ci_server": "Server",
    "cmdb_ci_db_instance": "Database Instance",
}
_CRITICALITY: Final = {
    1: "1 - most critical",
    2: "2 - somewhat critical",
    3: "3 - less critical",
    4: "4 - not critical",
}
_GROUP_TYPE: Final = "itil"
_CHANGE_TYPES: Final = {"standard": "Standard", "normal": "Normal", "emergency": "Emergency"}
_RISK: Final = {"standard": ("4", "Low"), "normal": ("3", "Moderate"), "emergency": ("2", "High")}
_CLOSE_LABELS: Final = {
    "successful": "Successful",
    "successful_with_issues": "Successful with issues",
    "unsuccessful": "Unsuccessful",
    "backed_out": "Backed out",
}
_WINDOW_H: Final = (1.0, 8.0)  # planned window length
_WORK_SHIFT_MIN: Final = 30.0  # work window within +/- 30 min of plan
_LEAD_H: Final = (1.0, 168.0)  # opened_at before start_date; the spec leaves it open
_PROBLEM_MEDIAN_DAYS: Final = 30.0  # problem resolution; the spec leaves it open
_MIN_WORK: Final = timedelta(minutes=1)


@dataclasses.dataclass(frozen=True, slots=True)
class IncidentBatch:
    """Background incidents of one shard with their truth label and PII rows."""

    records: list[dict[str, dict[str, str]]]
    labels: list[dict[str, str]]  # {record_id, question, answer}
    pii: list[dict[str, object]]  # {record_id, field, start, end, type}


def _group(
    sys_id: str, name: str, parent: Pair, manager: Pair, cost_center: str, stamp: Pair
) -> Record:
    return {
        "sys_id": pair(sys_id),
        "name": pair(name),
        "parent": parent,
        "manager": manager,
        "cost_center": pair(cost_center),
        "active": pair("true"),
        "type": pair(_GROUP_TYPE),
        "sys_updated_on": stamp,
    }


def gen_groups(cat: Catalog, params: SynthParams) -> list[Record]:
    """`sys_user_group` rows: one per org (no parent) and one per team (parent = its org);
    each team row carries its org's `cost_center` for impl 02 department mode."""
    stamp = span_start(params)
    orgs = {o.sys_id: o for o in cat.orgs}
    rows = [
        _group(o.sys_id, o.name, pair(""), pair(""), o.cost_center, ts_pair(stamp))
        for o in cat.orgs
    ]
    for t in cat.teams:
        org = orgs[t.org_sys_id]
        manager = ref(stable_id("sys_user", t.manager_name), t.manager_name)
        parent = ref(org.sys_id, org.name)
        rows.append(_group(t.sys_id, t.name, parent, manager, org.cost_center, ts_pair(stamp)))
    return rows


def _ci(
    ci: CiRow, service: ServiceRow, teams: dict[str, TeamRow], orgs: dict[str, OrgRow]
) -> Record:
    owner, support = teams[service.owner_team_sys_id], teams[service.support_team_sys_id]
    org = orgs[owner.org_sys_id]
    return {
        "sys_id": pair(ci.sys_id),
        "name": pair(ci.name),
        "sys_class_name": pair(ci.sys_class_name, _CLASS_LABELS[ci.sys_class_name]),
        "owned_by": ref(owner.sys_id, owner.name),
        "support_group": ref(support.sys_id, support.name),
        "cost_center": pair(service.cost_center),
        "company": ref(org.sys_id, org.name),
    }


def gen_cis(cat: Catalog, params: SynthParams) -> tuple[list[Record], list[Record]]:
    """(`cmdb_ci` rows for every CI, `cmdb_ci_service` rows with `busines_criticality`)."""
    services = {s.sys_id: s for s in cat.services}
    teams = {t.sys_id: t for t in cat.teams}
    orgs = {o.sys_id: o for o in cat.orgs}
    ci_rows = [_ci(ci, services[ci.service_sys_id], teams, orgs) for ci in cat.cis]
    by_id = {row["sys_id"]["value"]: row for row in ci_rows}
    service_rows = [
        {field: dict(value) for field, value in by_id[s.sys_id].items()}
        | {"busines_criticality": pair(_CRITICALITY[s.criticality])}
        for s in cat.services
    ]  # copies, so the two lists share no mutable pair
    return ci_rows, service_rows


def gen_rels(cat: Catalog, params: SynthParams) -> list[Record]:
    """`cmdb_rel_ci` rows; `type` references the relation type by a stable id."""
    names = {ci.sys_id: ci.name for ci in cat.cis}
    return [
        {
            "sys_id": pair(rel.sys_id),
            "parent": ref(rel.parent, names[rel.parent]),
            "child": ref(rel.child, names[rel.child]),
            "type": ref(stable_id("cmdb_rel_type", rel.type), rel.type),
        }
        for rel in cat.rels
    ]


def gen_incidents(
    cat: Catalog,
    params: SynthParams,
    shard: Shard,
    rng: np.random.Generator,
    bank: TemplateBank,
    names: Sequence[tuple[str, str]],
) -> IncidentBatch:
    """Exactly `shard.n_records` background incidents numbered `INC` + `seq_start + i`."""
    check_shard(shard, "incident")
    records, labels, pii = servicenow_incidents.generate(cat, params, shard, rng, bank, names)
    return IncidentBatch(records, labels, pii)


def close_code_probs(params: SynthParams, kind: str) -> dict[str, float]:
    """Close-code probabilities in sorted code order; emergency changes multiply the
    non-success shares by `emergency_failure_multiplier`, renormalized."""
    codes = params.change.close_codes
    boost = params.change.emergency_failure_multiplier if kind == "emergency" else 1.0
    keys = sorted(codes)
    weights = np.array([codes[k] * (1.0 if k == "successful" else boost) for k in keys])
    return dict(zip(keys, (weights / weights.sum()).tolist(), strict=True))


def _close_code(params: SynthParams, rng: np.random.Generator, kind: str) -> str:
    probs = close_code_probs(params, kind)
    keys = list(probs)
    return keys[int(rng.choice(len(keys), p=np.array(list(probs.values()))))]


def _change_state(
    params: SynthParams,
    rng: np.random.Generator,
    kind: str,
    work: tuple[datetime, datetime],
    end: datetime,
) -> tuple[Pair, datetime | None, datetime | None, str]:
    """State, actual window and close code; work not finished by `end` leaves it open."""
    start, finish = work
    if finish <= end:
        return pair("3", "Closed"), start, finish, _close_code(params, rng, kind)
    if start <= end:
        return pair("-1", "Implement"), start, None, ""
    return pair("-2", "Scheduled"), None, None, ""


@dataclasses.dataclass(frozen=True, slots=True)
class _ChangeCtx:
    params: SynthParams
    bank: TemplateBank
    index: ServiceIndex
    end: datetime


def _change(
    ctx: _ChangeCtx, rng: np.random.Generator, service: ServiceRow, start: datetime, seq: int
) -> Record:
    params = ctx.params
    types = params.change.types
    keys = sorted(types)
    kind = keys[int(rng.choice(len(keys), p=np.array([types[k] for k in keys])))]
    planned_end = start + timedelta(hours=float(rng.uniform(*_WINDOW_H)))
    shift = rng.uniform(-_WORK_SHIFT_MIN, _WORK_SHIFT_MIN, 2)
    work_start = start + timedelta(minutes=float(shift[0]))
    work_end = max(planned_end + timedelta(minutes=float(shift[1])), work_start + _MIN_WORK)
    # the lead time never takes opened_at before the span start (planned starts lie inside)
    lead = timedelta(hours=float(rng.uniform(*_LEAD_H)))
    opened = max(start - lead, span_start(params))
    state, actual_start, actual_end, code = _change_state(
        params, rng, kind, (work_start, work_end), ctx.end
    )
    text = render_change_text(ctx.bank, rng, component=service.name, emergency=kind == "emergency")
    ci, team = ctx.index.ci(rng, service), ctx.index.support(service)
    stamps = (opened, start, planned_end, actual_start, actual_end)
    return {
        "sys_id": pair(new_sys_id(rng)),
        "number": pair(number("CHG", seq)),
        "type": pair(kind, _CHANGE_TYPES[kind]),
        "state": state,
        "risk": pair(*_RISK[kind]),
        "opened_at": ts_pair(opened),
        "start_date": ts_pair(start),
        "end_date": ts_pair(planned_end),
        "work_start": ts_pair(actual_start),
        "work_end": ts_pair(actual_end),
        "business_service": ref(service.sys_id, service.name),
        "cmdb_ci": ref(ci.sys_id, ci.name),
        "assignment_group": ref(team.sys_id, team.name),
        "close_code": pair(code, _CLOSE_LABELS.get(code, "")),
        "short_description": pair(text.short_description),
        "description": pair(text.description),
        "sys_updated_on": updated_on(rng, stamps, ctx.end),
    }


def gen_changes(
    cat: Catalog, params: SynthParams, shard: Shard, rng: np.random.Generator, bank: TemplateBank
) -> list[Record]:
    """`shard.n_records` background changes numbered `CHG` + `seq_start + i`; the arrival
    model places the planned start, services follow incident weight."""
    check_shard(shard, "change_request")
    ctx = _ChangeCtx(params, bank, service_index(cat), span_end(params))
    starts = arrival_times(params, shard, rng, shard.n_records)
    services = ctx.index.draw(rng, len(starts))
    return [
        _change(ctx, rng, service, start, shard.seq_start + i)
        for i, (start, service) in enumerate(zip(starts, services, strict=True))
    ]


def gen_problems(
    cat: Catalog, params: SynthParams, shard: Shard, rng: np.random.Generator, bank: TemplateBank
) -> list[Record]:
    """`shard.n_records` problems numbered `PRB` + `seq_start + i`, one per service cluster
    draw (services by incident weight); `cause_notes` from the cluster's root cause."""
    check_shard(shard, "problem")
    index, end = service_index(cat), span_end(params)
    opened = arrival_times(params, shard, rng, shard.n_records)
    causes: dict[str, str] = {}
    rows = []
    for i, (at, service) in enumerate(zip(opened, index.draw(rng, len(opened)), strict=True)):
        if service.sys_id not in causes:
            causes[service.sys_id] = bank.family(cluster(bank, service.sys_id)[0]).cause
        days = float(rng.lognormal(math.log(_PROBLEM_MEDIAN_DAYS), params.mttr.sigma))
        resolve_at = at + timedelta(days=days)
        resolved = None if resolve_at > end else resolve_at
        state = pair("106", "Resolved") if resolved else pair("103", "Root Cause Analysis")
        known = "true" if rng.random() < params.problem.known_error_rate else "false"
        team = index.support(service)
        rows.append(
            {
                "sys_id": pair(new_sys_id(rng)),
                "number": pair(number("PRB", shard.seq_start + i)),
                "opened_at": ts_pair(at),
                "resolved_at": ts_pair(resolved),
                "state": state,
                "business_service": ref(service.sys_id, service.name),
                "assignment_group": ref(team.sys_id, team.name),
                "known_error": pair(known),
                "cause_notes": pair(f"Root cause: {causes[service.sys_id]}."),
                "sys_updated_on": updated_on(rng, (at, resolved), end),
            }
        )
    return rows


__all__ = [
    "IncidentBatch",
    "close_code_probs",
    "gen_changes",
    "gen_cis",
    "gen_groups",
    "gen_incidents",
    "gen_problems",
    "gen_rels",
]
