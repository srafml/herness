"""Background incident records, truth labels and PII rows (U11-07 incident algorithm).

Split out of `tools.synth.servicenow` for the module line budget; the public entry point
is `tools.synth.servicenow.gen_incidents`. Step numbers refer to the U11-07 algorithm.
"""

import dataclasses
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

import numpy as np
import numpy.typing as npt

from tools.synth.catalog_rows import Catalog, CiRow, ServiceRow, TeamRow
from tools.synth.params import SynthParams
from tools.synth.pii import inject_pii
from tools.synth.servicenow_common import (
    Pair,
    Record,
    ServiceIndex,
    arrival_times,
    cluster,
    new_sys_id,
    number,
    pair,
    parse_ts,
    ref,
    service_index,
    span_end,
    ts_pair,
    updated_on,
)
from tools.synth.shards import Shard
from tools.synth.text import RenderedText, TemplateBank, render_incident_text

_PRIORITY_LABELS: Final = {
    1: "1 - Critical",
    2: "2 - High",
    3: "3 - Moderate",
    4: "4 - Low",
    5: "5 - Planning",
}
# Impact phrase level (none, minor, degraded, outage) by priority; the spec leaves it open.
_IMPACT_LEVEL: Final = {1: 3, 2: 2, 3: 1, 4: 0, 5: 0}
_HIGH_PRIORITIES: Final = (1, 2)
_LOW_PRIORITIES: Final = (4, 5)
_CLOSE_LAG_H: Final = 72.0  # closed_at = resolved_at + U(0, 72 h)
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_CLOSE_CODE: Final = "Solved (Permanently)"
_CLOSE_CODE_REPEAT: Final = "Solved (Work Around)"
_QUESTIONS: Final = (
    "root_cause",
    "change_caused",
    "repeat_issue",
    "business_impact",
    "owning_team",
)


@dataclasses.dataclass(frozen=True, slots=True)
class _Times:
    opened: datetime
    ack: datetime | None
    resolved: datetime | None
    closed: datetime | None
    duration: timedelta  # to resolved_at, or to the span end for open records


def priority_probs(params: SynthParams, criticality: int) -> npt.NDArray[np.float64]:
    """P1..P5 shares; criticality 1 multiplies P1/P2 and takes the difference from P4."""
    shares = params.priority.shares
    p = np.array([shares[k] for k in range(1, 6)], dtype=np.float64)
    if criticality == 1:
        extra = (params.priority.criticality1_high_multiplier - 1.0) * (p[0] + p[1])
        p[0:2] *= params.priority.criticality1_high_multiplier
        p[3] = max(p[3] - extra, 0.0)
    return p / p.sum()


def _lognormal(rng: np.random.Generator, median: float, sigma: float) -> float:
    return float(rng.lognormal(math.log(median), sigma))


def _times(
    params: SynthParams,
    rng: np.random.Generator,
    priority: int,
    team: TeamRow,
    opened: datetime,
    end: datetime,
) -> _Times:
    """Steps 4-5: acknowledge, MTTR, resolve and close; records resolving after `end` stay
    open and acknowledgements never fall after the resolution or the span end."""
    ack = None
    if rng.random() < params.ack.share:
        minutes = _lognormal(rng, params.ack.median_minutes[priority], params.ack.sigma)
        ack = opened + timedelta(minutes=minutes)
    hours = _lognormal(rng, params.mttr.median_hours[priority], params.mttr.sigma)
    resolve_at = opened + timedelta(hours=hours * team.mttr_multiplier)
    close_at = resolve_at + timedelta(hours=float(rng.uniform(0.0, _CLOSE_LAG_H)))
    resolved = None if resolve_at > end else resolve_at
    closed = None if resolved is None or close_at > end else close_at
    if ack is not None and resolved is not None:
        ack = min(ack, resolved)
    if ack is not None and ack > end:
        ack = None
    return _Times(opened, ack, resolved, closed, (resolved or end) - opened)


def _duration_pair(t: _Times) -> Pair:
    if t.resolved is None:
        return pair("")
    total = int(t.duration.total_seconds())
    days, rest = divmod(total, 86_400)
    display = f"{days} Days {rest // 3600} Hours {rest % 3600 // 60} Minutes"
    return pair(ts_pair(_EPOCH + timedelta(seconds=total))["value"], display)


def _state(t: _Times) -> Pair:
    if t.resolved is None:
        return pair("2", "In Progress")
    return pair("7", "Closed") if t.closed is not None else pair("6", "Resolved")


def _impact(
    params: SynthParams, rng: np.random.Generator, priority: int, t: _Times, *, force: bool
) -> Pair:
    """Step 8: customer impact minutes on `impact.share` of P1/P2 (on every incident when
    `force`, for plant clusters), empty otherwise."""
    if not force and (priority not in _HIGH_PRIORITIES or rng.random() >= params.impact.share):
        return pair("")
    factor = float(rng.uniform(params.impact.min_factor, params.impact.max_factor))
    return pair(str(round(t.duration.total_seconds() / 60.0 * factor)))


@dataclasses.dataclass(frozen=True, slots=True)
class _Draw:
    """One incident's drawn values before it is shaped into a record."""

    service: ServiceRow
    team: TeamRow
    priority: int
    times: _Times
    member: bool
    text: RenderedText
    impact: bool = False  # customer impact on every incident (T2/T2c plant clusters)


def _record(
    params: SynthParams,
    rng: np.random.Generator,
    index: ServiceIndex,
    d: _Draw,
    seq: int,
    description: str,
) -> Record:
    t, end, resolved = d.times, span_end(params), d.times.resolved is not None
    rate = params.reassign.reopen_rate_p4_p5 if d.priority in _LOW_PRIORITIES else None
    reopen = int(rng.random() < (params.reassign.reopen_rate if rate is None else rate))
    reassign = int(rng.poisson(params.reassign.base_mean + d.team.reassign_extra))
    limit = timedelta(hours=params.sla.limit_hours[d.priority])
    ci = index.ci(rng, d.service)
    code = (_CLOSE_CODE_REPEAT if d.member else _CLOSE_CODE) if resolved else ""
    return {
        "sys_id": pair(new_sys_id(rng)),
        "number": pair(number("INC", seq)),
        "opened_at": ts_pair(t.opened),
        "u_acknowledged_at": ts_pair(t.ack),
        "resolved_at": ts_pair(t.resolved),
        "closed_at": ts_pair(t.closed),
        "priority": pair(str(d.priority), _PRIORITY_LABELS[d.priority]),
        "state": _state(t),
        "business_service": ref(d.service.sys_id, d.service.name),
        "cmdb_ci": ref(ci.sys_id, ci.name),
        "assignment_group": ref(d.team.sys_id, d.team.name),
        "reassignment_count": pair(str(reassign)),
        "reopen_count": pair(str(reopen)),
        "short_description": pair(d.text.short_description),
        "description": pair(description),
        "close_notes": pair(d.text.close_notes if resolved else ""),
        "close_code": pair(code),
        "problem_id": pair(""),
        "caused_by": pair(""),
        "made_sla": pair("false" if t.duration > limit else "true"),
        "business_duration": _duration_pair(t),
        "u_customer_impact_minutes": _impact(params, rng, d.priority, t, force=d.impact),
        "sys_updated_on": updated_on(rng, (t.opened, t.ack, t.resolved, t.closed), end),
    }


def _labels(record_id: str, d: _Draw, *, change_caused: bool = False) -> list[dict[str, str]]:
    """Step 11: one truth label row per question; only T3 plant incidents are change-caused."""
    answers = (
        d.text.root_cause,
        "true" if change_caused else "false",
        "true" if d.member else "false",
        str(_IMPACT_LEVEL[d.priority]),
        d.team.sys_id,
    )
    return [
        {"record_id": record_id, "question": q, "answer": a}
        for q, a in zip(_QUESTIONS, answers, strict=True)
    ]


def _pii(
    params: SynthParams,
    rng: np.random.Generator,
    text: str,
    names: Sequence[tuple[str, str]],
) -> tuple[str, list[dict[str, object]]]:
    """Step 10: PII spans in `pii.incident_share` of descriptions (record id added later)."""
    if rng.random() >= params.pii.incident_share:
        return text, []
    n_spans = int(rng.integers(params.pii.spans_min, params.pii.spans_max + 1))
    text, spans = inject_pii(text, "description", rng, names, n_spans=n_spans)
    return text, [{"field": s.field, "start": s.start, "end": s.end, "type": s.type} for s in spans]


def generate(
    cat: Catalog,
    params: SynthParams,
    shard: Shard,
    rng: np.random.Generator,
    bank: TemplateBank,
    names: Sequence[tuple[str, str]],
) -> tuple[list[Record], list[dict[str, str]], list[dict[str, object]]]:
    """Records, label rows and PII rows of `shard.n_records` background incidents."""
    index, end = service_index(cat), span_end(params)
    opened = arrival_times(params, shard, rng, shard.n_records)
    services = index.draw(rng, shard.n_records)
    probs = {s.criticality: priority_probs(params, s.criticality) for s in cat.services}
    per_service: Counter[str] = Counter()
    clusters: dict[str, tuple[str, dict[str, str]]] = {}
    records: list[Record] = []
    labels: list[dict[str, str]] = []
    pii: list[dict[str, object]] = []
    for i, (at, service) in enumerate(zip(opened, services, strict=True)):
        priority = int(rng.choice(5, p=probs[service.criticality])) + 1
        team = index.support(service)
        per_service[service.sys_id] += 1
        member = per_service[service.sys_id] % params.problem.incidents_per_problem == 0
        if member and service.sys_id not in clusters:
            clusters[service.sys_id] = cluster(bank, service.sys_id)
        family, slots = clusters[service.sys_id] if member else (None, None)
        rendered = render_incident_text(
            bank,
            rng,
            family=family,
            slots=slots,
            change_flavored=False,
            repeat_flavored=member,
            impact_level=_IMPACT_LEVEL[priority],
            component=service.name,
            text=params.text,
        )
        times = _times(params, rng, priority, team, at, end)
        draw = _Draw(service, team, priority, times, member, rendered)
        description, spans = _pii(params, rng, rendered.description, names)
        record = _record(params, rng, index, draw, shard.seq_start + i, description)
        record_id = f"servicenow:incident:{record['sys_id']['value']}"
        records.append(record)
        labels += _labels(record_id, draw)
        pii += [{"record_id": record_id, **span} for span in spans]
    return records, labels, pii


@dataclasses.dataclass(frozen=True, slots=True)
class IncidentSpec:
    """A plant incident's fixed values: service, open time, sequence number, optional CI
    (else drawn from the service's CIs), change-flavored text (T3), and for plant
    clusters (T2, T2c) a fixed priority, template family and slot set, repeat-flavored
    text and customer impact on every incident."""

    service: ServiceRow
    opened: datetime
    seq: int
    ci: CiRow | None = None
    change_flavored: bool = False
    priority: int | None = None  # None: drawn from the service's priority mix
    family: str | None = None
    slots: Mapping[str, str] | None = None
    repeat: bool = False
    customer_impact: bool = False


def make_incident(
    params: SynthParams,
    rng: np.random.Generator,
    bank: TemplateBank,
    index: ServiceIndex,
    spec: IncidentSpec,
) -> tuple[Record, list[dict[str, str]]]:
    """One plant incident drawn like a background one (priority mix, MTTR model of the
    service's support team, text), with no problem cluster and no PII, unless `spec` fixes
    them; returns the record and its label rows (`change_caused` true for change-flavored
    incidents, `repeat_issue` true for `spec.repeat`)."""
    service, priority = spec.service, spec.priority
    if priority is None:
        priority = int(rng.choice(5, p=priority_probs(params, service.criticality))) + 1
    team = index.support(service)
    rendered = render_incident_text(
        bank,
        rng,
        family=spec.family,
        slots=spec.slots,
        change_flavored=spec.change_flavored,
        repeat_flavored=spec.repeat,
        impact_level=_IMPACT_LEVEL[priority],
        component=service.name,
        text=params.text,
    )
    times = _times(params, rng, priority, team, spec.opened, span_end(params))
    draw = _Draw(service, team, priority, times, spec.repeat, rendered, spec.customer_impact)
    record = _record(params, rng, index, draw, spec.seq, rendered.description)
    if spec.ci is not None:
        record["cmdb_ci"] = ref(spec.ci.sys_id, spec.ci.name)
    record_id = f"servicenow:incident:{record['sys_id']['value']}"
    return record, _labels(record_id, draw, change_caused=spec.change_flavored)


def retime(
    record: Record,
    params: SynthParams,
    rng: np.random.Generator,
    *,
    opened: datetime,
    mttr: timedelta | None,
) -> None:
    """Move an incident to `opened` and resolve it after `mttr` (`None` keeps it open),
    keeping its acknowledgement offset and close lag. Recomputes state, `made_sla`,
    `business_duration`, impact minutes (same factor of the duration), close fields and
    `sys_updated_on` (one `rng` draw); a resolution after the span end leaves it open."""
    end = span_end(params)
    old_opened = parse_ts(record["opened_at"]) or opened
    ack, resolved, closed = (
        parse_ts(record[f]) for f in ("u_acknowledged_at", "resolved_at", "closed_at")
    )
    old_duration = ((resolved or end) - old_opened).total_seconds()
    new_resolved = opened + mttr if mttr is not None and opened + mttr <= end else None
    new_closed = None
    if new_resolved is not None and resolved is not None and closed is not None:
        new_closed = new_resolved + (closed - resolved)
        new_closed = new_closed if new_closed <= end else None
    new_ack = None if ack is None else opened + (ack - old_opened)
    if new_ack is not None and new_resolved is not None:
        new_ack = min(new_ack, new_resolved)
    new_ack = new_ack if new_ack is not None and new_ack <= end else None
    t = _Times(opened, new_ack, new_resolved, new_closed, (new_resolved or end) - opened)
    limit = timedelta(hours=params.sla.limit_hours[int(record["priority"]["value"])])
    record["opened_at"], record["u_acknowledged_at"] = ts_pair(opened), ts_pair(new_ack)
    record["resolved_at"], record["closed_at"] = ts_pair(new_resolved), ts_pair(new_closed)
    record["state"], record["business_duration"] = _state(t), _duration_pair(t)
    record["made_sla"] = pair("false" if t.duration > limit else "true")
    if new_resolved is None:
        record["close_notes"], record["close_code"] = pair(""), pair("")
    impact = record["u_customer_impact_minutes"]["value"]
    if impact and old_duration > 0:
        scaled = int(impact) * t.duration.total_seconds() / old_duration
        record["u_customer_impact_minutes"] = pair(str(round(scaled)))
    record["sys_updated_on"] = updated_on(rng, (opened, new_ack, new_resolved, new_closed), end)


__all__ = ["IncidentSpec", "generate", "make_incident", "priority_probs", "retime"]
