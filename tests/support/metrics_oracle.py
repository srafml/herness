"""Pure-Python reference formulas for catalog metrics (impl 04 §2 test-side file; PT04-03).

`oracle(metric, grain, facts, frame)` recomputes a metric from fact rows held in Python with
the U04-48 table A definitions, for the property test that compares it with the recorded SQL.
Covered: incident #1-#4, #8-#11, #13; change #15, #16, #18, #19; delivery #20-#26. Not
covered (they read `core.*`/`enrich.*` rows beyond the facts or weight binds): #5-#7, #12, #14,
#17, #27, #28. Rows are `(value, numerator, denominator, sample_size)` keyed by
`(entity_id, period_start)`; the min-sample rule and flags belong to the wrapper and are not
modelled.
"""

import datetime
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Final, Protocol

from tests.support._metrics_oracle_types import Change, Facts, Frame, Incident, WorkItem

__all__ = [
    "COVERED",
    "Change",
    "Facts",
    "Frame",
    "Incident",
    "OracleRow",
    "WorkItem",
    "oracle",
]

type OracleRow = tuple[float | None, float | None, float | None, int]
type Key = tuple[str, datetime.date]

DELIVERY_TYPES: Final = frozenset({"story", "bug", "task"})
_CATEGORY_RANK: Final = {"done": 3, "in_progress": 2}
_MAX_DEPTH: Final = 10


class _Owned(Protocol):
    @property
    def service_id(self) -> str | None: ...
    @property
    def team_id(self) -> str | None: ...
    @property
    def org_id(self) -> str | None: ...


def _org_ancestors(org: str | None, parents: Mapping[str, str | None]) -> list[str]:
    out: list[str] = []
    while org is not None and org not in out and len(out) <= _MAX_DEPTH * 2:
        out.append(org)
        org = parents.get(org)
    return out


def _item_ancestors(item: WorkItem, by_id: Mapping[str, WorkItem]) -> list[str]:
    out = [item.record_id]
    parent = item.parent_id
    while parent is not None and parent in by_id and len(out) <= _MAX_DEPTH:
        out.append(parent)
        parent = by_id[parent].parent_id
    return out


def _entities(rec: _Owned, grain: str, facts: Facts) -> list[str]:
    if grain == "service":
        return [rec.service_id] if rec.service_id is not None else []
    if grain == "team":
        return [rec.team_id] if rec.team_id is not None else []
    if grain == "org":
        return _org_ancestors(rec.org_id, facts.org_parents)
    if grain == "cluster" and isinstance(rec, Incident):
        return [rec.cluster_id] if rec.cluster_id is not None else []
    if grain == "work_item" and isinstance(rec, WorkItem):
        return _item_ancestors(rec, {w.record_id: w for w in facts.items})
    msg = f"grain {grain} not modelled"
    raise ValueError(msg)


def _group[R: _Owned](
    records: Iterable[tuple[R, datetime.date]], grain: str, facts: Facts
) -> dict[Key, list[R]]:
    groups: dict[Key, list[R]] = defaultdict(list)
    for rec, ps in records:
        for entity in _entities(rec, grain, facts):
            groups[(entity, ps)].append(rec)
    return dict(groups)


def _anchored[R: _Owned](
    recs: Iterable[R], anchor: Callable[[R], datetime.datetime | None], frame: Frame
) -> list[tuple[R, datetime.date]]:
    out: list[tuple[R, datetime.date]] = []
    for rec in recs:
        ts = anchor(rec)
        if ts is not None and frame.contains(ts):
            out.append((rec, frame.period_start(ts)))
    return out


def _count(recs: Sequence[object]) -> OracleRow:
    return (float(len(recs)), float(len(recs)), None, len(recs))


def _ratio(hits: int, n: int) -> OracleRow:
    return (hits / n, float(hits), float(n), n)


# --- incident and change metrics ---------------------------------------------------------------


def _opened(facts: Facts, frame: Frame) -> list[tuple[Incident, datetime.date]]:
    return _anchored([i for i in facts.incidents if not i.excluded], lambda i: i.opened_at, frame)


def _resolved(facts: Facts, frame: Frame) -> list[tuple[Incident, datetime.date]]:
    live = [i for i in facts.incidents if not i.excluded and i.resolve_h is not None]
    return _anchored(live, lambda i: i.resolved_at, frame)


def _deployed(facts: Facts, frame: Frame) -> list[tuple[Change, datetime.date]]:
    return _anchored([c for c in facts.changes if c.deployed], lambda c: c.actual_end, frame)


def _hours(recs: Sequence[Incident]) -> list[float]:
    return [i.resolve_h for i in recs if i.resolve_h is not None]


def _incident_metric(name: str, grain: str, facts: Facts, frame: Frame) -> dict[Key, OracleRow]:
    if name in {"incident_count", "p1p2_count", "repeat_incident_rate"}:
        rows = _opened(facts, frame)
        if name == "p1p2_count":
            rows = [(i, ps) for i, ps in rows if i.priority is not None and i.priority <= 2]
        if name == "repeat_incident_rate":
            rows = [(i, ps) for i, ps in rows if i.service_id is not None]
            groups = _group(rows, grain, facts)
            return {k: _ratio(sum(i.is_repeat for i in g), len(g)) for k, g in groups.items()}
        return {k: _count(g) for k, g in _group(rows, grain, facts).items()}
    rows = _resolved(facts, frame)
    if name == "sla_breach_rate":
        rows = [(i, ps) for i, ps in rows if i.sla_breached is not None]
    out: dict[Key, OracleRow] = {}
    for key, g in _group(rows, grain, facts).items():
        hours = _hours(g)
        n = len(g)
        if name == "mttr_hours":
            out[key] = (sum(hours) / n, sum(hours), float(n), n)
        elif name == "mttr_p50_hours":
            out[key] = (statistics.median(hours), None, float(n), n)
        elif name == "toil_hours_est":
            toil = sum(i.toil_h for i in g)
            out[key] = (toil, toil, None, n)
        else:
            flag = {
                "reopen_rate": lambda i: i.is_reopened,
                "reassignment_rate": lambda i: i.is_reassigned,
                "sla_breach_rate": lambda i: bool(i.sla_breached),
            }[name]
            out[key] = _ratio(sum(flag(i) for i in g), n)
    return out


def _weeks(ps: datetime.date, frame: Frame) -> float:
    start = max(ps, frame.window_start_date)
    end = min(frame.period_end(ps), frame.window_end_date)
    return (end - start).days / 7.0


def _change_metric(name: str, grain: str, facts: Facts, frame: Frame) -> dict[Key, OracleRow]:
    out: dict[Key, OracleRow] = {}
    for key, g in _group(_deployed(facts, frame), grain, facts).items():
        n = len(g)
        if name == "change_count":
            weeks = _weeks(key[1], frame)
            out[key] = (n / weeks, float(n), weeks, n)
        elif name == "change_failure_rate":
            out[key] = _ratio(sum(c.failed for c in g), n)
        elif name == "emergency_change_ratio":
            out[key] = _ratio(sum(c.type == "emergency" for c in g), n)
        else:
            lead = [c.lead_time_h for c in g if c.lead_time_h is not None]
            if lead:  # a group with no lead time has median NULL
                out[key] = (statistics.median(lead), None, float(len(lead)), len(lead))
            else:
                out[key] = (None, None, 0.0, 0)
    return out


# --- delivery metrics --------------------------------------------------------------------------


def cat_at(item: WorkItem, ts: datetime.datetime) -> str | None:
    """Category at `ts`: the latest transition at or before `ts` (done beats in_progress beats
    todo at one instant), else 'todo' once created, else None."""
    before = [(at, _CATEGORY_RANK.get(cat, 1), cat) for at, cat in item.transitions if at <= ts]
    if before:
        return max(before)[2]
    return "todo" if item.created_at <= ts else None


def _done_items(facts: Facts, frame: Frame) -> list[tuple[WorkItem, datetime.date]]:
    live = [w for w in facts.items if w.type in DELIVERY_TYPES]
    return _anchored(live, lambda w: w.done_at, frame)


def _spine_metric(name: str, grain: str, facts: Facts, frame: Frame) -> dict[Key, OracleRow]:
    live = [w for w in facts.items if w.type in DELIVERY_TYPES]
    out: dict[Key, OracleRow] = {}
    for ps, pe in frame.spine():
        start, end = frame.local_midnight(ps), frame.local_midnight(pe)
        snap = frame.local_midnight(min(pe, frame.as_of))
        if name == "carryover_rate":
            pop = [w for w in live if cat_at(w, start) == "in_progress"]
        else:
            want = "todo" if name == "backlog_age_days" else "in_progress"
            pop = [w for w in live if cat_at(w, snap) == want]
        for (entity, _), g in _group([(w, ps) for w in pop], grain, facts).items():
            n = len(g)
            if name == "carryover_rate":
                out[(entity, ps)] = _ratio(sum(cat_at(w, end) != "done" for w in g), n)
            elif name == "wip_count":
                out[(entity, ps)] = _count(g)
            else:
                ages = [(snap - w.created_at).total_seconds() / 86400.0 for w in g]
                out[(entity, ps)] = (statistics.median(ages), None, float(n), n)
    return out


def _epic_metric(grain: str, facts: Facts, frame: Frame) -> dict[Key, OracleRow]:
    by_id = {w.record_id: w for w in facts.items}
    sums: dict[Key, list[float]] = {}
    for epic, ps in _anchored([w for w in facts.items if w.type == "epic"], _done, frame):
        start = epic.first_in_progress_at or epic.created_at
        committed = [
            w
            for w in facts.items
            if w.record_id != epic.record_id
            and epic.record_id in _item_ancestors(w, by_id)
            and w.type in DELIVERY_TYPES
            and w.created_at <= start
        ]
        if not committed:
            continue
        pts = sum(_points(w) for w in committed)
        done = sum(_points(w) for w in committed if w.done_at and w.done_at <= _done(epic))
        for entity in _entities(epic, grain, facts):
            acc = sums.setdefault((entity, ps), [0.0, 0.0, 0.0])
            acc[0] += done
            acc[1] += pts
            acc[2] += 1
    return {k: (d / c if c else None, d, c, int(n)) for k, (d, c, n) in sums.items()}


def _points(item: WorkItem) -> float:
    return 1.0 if item.story_points is None else item.story_points


def _done(item: WorkItem) -> datetime.datetime | None:
    return item.done_at


def _delivery_metric(name: str, grain: str, facts: Facts, frame: Frame) -> dict[Key, OracleRow]:
    if name in {"carryover_rate", "backlog_age_days", "wip_count"}:
        return _spine_metric(name, grain, facts, frame)
    if name == "epic_predictability":
        return _epic_metric(grain, facts, frame)
    rows = _done_items(facts, frame)
    if name == "cycle_time_days":
        rows = [(w, ps) for w, ps in rows if w.cycle_days is not None]
    out: dict[Key, OracleRow] = {}
    for key, g in _group(rows, grain, facts).items():
        n = len(g)
        if name == "throughput":
            out[key] = _count(g)
        elif name == "unplanned_work_ratio":
            out[key] = _ratio(sum(w.is_unplanned for w in g), n)
        else:
            cycles = [w.cycle_days for w in g if w.cycle_days is not None]
            out[key] = (statistics.median(cycles), None, float(n), n)
    return out


_INCIDENT: Final = frozenset(
    {"incident_count", "p1p2_count", "mttr_hours", "mttr_p50_hours", "repeat_incident_rate"}
    | {"reopen_rate", "reassignment_rate", "sla_breach_rate", "toil_hours_est"}
)
_CHANGE: Final = frozenset(
    {"change_count", "change_failure_rate", "change_lead_time_hours", "emergency_change_ratio"}
)
_DELIVERY: Final = frozenset(
    {"throughput", "cycle_time_days", "carryover_rate", "backlog_age_days", "wip_count"}
    | {"unplanned_work_ratio", "epic_predictability"}
)
COVERED: Final = _INCIDENT | _CHANGE | _DELIVERY


def oracle(metric: str, grain: str, facts: Facts, frame: Frame) -> dict[Key, OracleRow]:
    """Reference rows of `metric` at `grain` for `frame`, keyed by (entity_id, period_start)."""
    if metric in _INCIDENT:
        return _incident_metric(metric, grain, facts, frame)
    if metric in _CHANGE:
        return _change_metric(metric, grain, facts, frame)
    if metric in _DELIVERY:
        return _delivery_metric(metric, grain, facts, frame)
    msg = f"metric {metric} not covered by the oracle"
    raise ValueError(msg)
