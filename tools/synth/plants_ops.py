"""Operations plants T1, T3, T4 and T5 (U11-10, U11-12, U11-13, U11-14, design §5.1.5).

Each plant has a `planned_counts_tN(cat_stub, params)` planner registered in
`tools.synth.catalog.default_planners()`; the catalog numbers plant records after the
month's background records (`tools.synth.catalog_plan.plant_range`), so a plant takes
its count and first sequence number from the catalog, never from the records it sees.
T3 lives in `tools.synth.plants_ops_t3` for the line budget and is re-exported here.
"""

import math
from datetime import date, datetime, timedelta
from typing import Final

import numpy as np
import numpy.typing as npt

from tools.synth.catalog_plan import plant_range, span_months, split_counts
from tools.synth.catalog_rows import Catalog, MonthKey, ServiceRow
from tools.synth.monitoring import (
    EVENT_KEY_FORMATS,
    TOOLS,
    event_end,
    iso_ts,
    service_hosts,
)
from tools.synth.params import SynthParams, SynthUsageError
from tools.synth.plants_ops_t3 import planned_counts_t3, plant_t3
from tools.synth.servicenow_common import (
    Record,
    arrival_times,
    pair,
    parse_ts,
    ref,
    service_index,
    shard_days,
    span_end,
    span_start,
)
from tools.synth.servicenow_incidents import IncidentSpec, make_incident, retime
from tools.synth.shards import IncidentTimeIndex, PlantOutput, Shard
from tools.synth.text import TemplateBank

Row = dict[str, object]

_T1_BASE_MULTIPLIER: Final = 2.0  # m(t) = 2.0 + elapsed fraction of the span
_T1_REASSIGN_MEAN: Final = 1.0  # extra reassignments ~ Poisson(1.0)
_T4_EVENT_SHARE: Final = 0.07  # of preset.events
_T4_SEVERITIES: Final = ("minor", "warning")  # 0.5 each
_T4_NEAR_S: Final = 30 * 60  # redraw while within +/- 30 min of an S4 incident
_T4_REDRAWS: Final = 3
T4_TITLES: Final = (
    "Health check flapping",
    "Heartbeat missed",
    "CPU usage above threshold",
    "Disk latency above threshold",
    "Queue depth above threshold",
)
_T5_FACTOR: Final = 2.8  # peak-day volume and request_count multiplier
_T5_EXTRA: Final = _T5_FACTOR - 1.0  # extra incidents per baseline incident on peak days
_REQUEST_COUNT: Final = "request_count"


def _service(cat: Catalog, sys_id: str) -> ServiceRow:
    return next(s for s in cat.services if s.sys_id == sys_id)


# --- T1 bad team ----------------------------------------------------------------------


def planned_counts_t1(cat: Catalog, params: SynthParams) -> dict[MonthKey, int]:
    """T1 adds no records."""
    return {}


def plant_t1(
    records: list[Record], cat: Catalog, params: SynthParams, rng: np.random.Generator
) -> None:
    """T1 over one shard's background incidents, in place and in input order: incidents
    on a T1 service get the T1 team, MTTR x m(t) = 2.0 + (opened_at - start)/(end - start)
    (resolution rounded to the second; `end` is the exclusive span end), reassignments
    + Poisson(1.0), and their dependent fields recomputed (`retime`)."""
    services = set(cat.plants.t1_services)
    team = next(t for t in cat.teams if t.sys_id == cat.plants.t1_team)
    start = span_start(params)
    length = (span_end(params) - start).total_seconds()
    for record in records:
        if record["business_service"]["value"] not in services:
            continue
        record["assignment_group"] = ref(team.sys_id, team.name)
        extra = int(rng.poisson(_T1_REASSIGN_MEAN))
        record["reassignment_count"] = pair(str(int(record["reassignment_count"]["value"]) + extra))
        opened = parse_ts(record["opened_at"]) or start
        resolved = parse_ts(record["resolved_at"])
        m = _T1_BASE_MULTIPLIER + (opened - start).total_seconds() / length
        mttr = None
        if resolved is not None:
            mttr = timedelta(seconds=round((resolved - opened).total_seconds() * m))
        retime(record, params, rng, opened=opened, mttr=mttr)


# --- T4 noisy alerting ----------------------------------------------------------------


def planned_counts_t4(cat: Catalog, params: SynthParams) -> dict[MonthKey, int]:
    """round(0.07 x preset.events) S4 events split across months by days in span."""
    months = span_months(params)
    total = round(_T4_EVENT_SHARE * params.preset.events)
    split = split_counts(total, [days for _, days in months])
    return {("monitoring", "event", m): n for (m, _), n in zip(months, split, strict=True)}


def _near(stamps: npt.NDArray[np.int64], at: datetime) -> bool:
    t = int(at.timestamp())
    k = int(np.searchsorted(stamps, t - _T4_NEAR_S))
    return k < len(stamps) and int(stamps[k]) <= t + _T4_NEAR_S


def plant_t4(
    shard: Shard,
    cat: Catalog,
    params: SynthParams,
    rng: np.random.Generator,
    incident_index: IncidentTimeIndex,
) -> PlantOutput:
    """The shard month's S4 events: flapping titles, minor/warning, `incident_ref` NULL,
    `dedup_key` = title slug; a time within +/- 30 min of an S4 incident of
    `incident_index` is redrawn up to 3 times."""
    if shard.source != "monitoring" or shard.entity != "event":
        msg = "T4 plants only monitoring event shards"
        raise SynthUsageError(msg, key="shard")
    first, n = plant_range(cat, planned_counts_t4, (shard.source, shard.entity, shard.month))
    s4 = _service(cat, cat.plants.s4)
    stamps = incident_index.opened_at.get(s4.sys_id, np.zeros(0, dtype=np.int64))
    times = []
    for drawn in arrival_times(params, shard, rng, n):
        at = drawn
        for _ in range(_T4_REDRAWS):
            if not _near(stamps, at):
                break
            at = arrival_times(params, shard, rng, 1)[0]
        times.append(at)
    times.sort()
    tools = rng.integers(len(TOOLS), size=n)
    titles = rng.integers(len(T4_TITLES), size=n)
    severities = rng.integers(len(_T4_SEVERITIES), size=n)
    hosts, end = service_hosts(cat)[s4.sys_id], span_end(params)
    rows: list[Row] = []
    for i, at in enumerate(times):
        tool, title = TOOLS[int(tools[i])], T4_TITLES[int(titles[i])]
        status, end_ts = event_end(rng, at, end)
        event_key = EVENT_KEY_FORMATS[tool].format(first + i)
        rows.append({
            "source_tool": tool,
            "event_key": event_key,
            "ts": iso_ts(at),
            "service": s4.name,
            "host": hosts[int(rng.integers(len(hosts)))],
            "severity_raw": _T4_SEVERITIES[int(severities[i])],
            "title": title,
            "status": status,
            "dedup_key": title.lower().replace(" ", "-"),
            "end_ts": end_ts,
            "incident_ref": None,
            "_source_key": f"{tool}:{event_key}",
        })  # fmt: skip
    return PlantOutput(records=list(rows))


# --- T5 confounder trap ---------------------------------------------------------------


def _peak_days(cat: Catalog, params: SynthParams, month: date) -> list[date]:
    return [d for d in shard_days(params, month) if cat.plants.in_peak(d)]


def planned_counts_t5(cat: Catalog, params: SynthParams) -> dict[MonthKey, int]:
    """Extra S5 incidents per month = round(1.8 x S5 baseline expected daily count x peak
    days in month); the baseline daily count is S5's incident-weight share of the month's
    background incidents per day of the month in span."""
    share = _service(cat, cat.plants.s5).incident_weight / math.fsum(
        s.incident_weight for s in cat.services
    )
    counts: dict[MonthKey, int] = {}
    for month, days in span_months(params):
        key = ("servicenow", "incident", month)
        peak = len(_peak_days(cat, params, month))
        if key in cat.month_counts and peak:
            counts[key] = round(_T5_EXTRA * share * cat.month_counts[key] / days * peak)
    return counts


def _t5_incidents(
    records: list[Record],
    shard: Shard,
    cat: Catalog,
    params: SynthParams,
    rng: np.random.Generator,
) -> PlantOutput:
    team = next(t for t in cat.teams if t.sys_id == cat.plants.t5_team)
    for record in records:  # T5 resolves every S5 incident
        if record["business_service"]["value"] == cat.plants.s5:
            record["assignment_group"] = ref(team.sys_id, team.name)
    first, n = plant_range(cat, planned_counts_t5, (shard.source, shard.entity, shard.month))
    if not n:
        return PlantOutput()
    s5, index, bank = _service(cat, cat.plants.s5), service_index(cat), TemplateBank()
    days = _peak_days(cat, params, shard.month)
    out = PlantOutput()
    for i, at in enumerate(arrival_times(params, shard, rng, n, days=days)):
        record, labels = make_incident(params, rng, bank, index, IncidentSpec(s5, at, first + i))
        out.records.append(record)
        out.labels.extend(labels)
    return out


def _t5_metrics(rows: list[Row], cat: Catalog) -> None:
    name = _service(cat, cat.plants.s5).name
    for row in rows:
        value = row["value"]
        if row["service"] != name or row["metric_name"] != _REQUEST_COUNT:
            continue
        if isinstance(value, int | float) and cat.plants.in_peak(
            date.fromisoformat(str(row["date"]))
        ):
            row["value"] = float(round(value * _T5_FACTOR))


def plant_t5(
    records: list[Record],
    metric_rows: list[Row],
    shard: Shard,
    cat: Catalog,
    params: SynthParams,
    rng: np.random.Generator,
) -> PlantOutput:
    """T5 for one shard. `servicenow/incident`: S5 background incidents get team T5 and the
    planned extra S5 incidents (clones in distribution) arrive on the month's peak days and
    are returned (not appended to `records`). `monitoring/metric_daily`: S5 `request_count`
    rows on peak days are multiplied by 2.8 in place. Other shards: nothing."""
    if shard.source == "servicenow" and shard.entity == "incident":
        return _t5_incidents(records, shard, cat, params, rng)
    if shard.source == "monitoring" and shard.entity == "metric_daily":
        _t5_metrics(metric_rows, cat)
    return PlantOutput()


__all__ = [
    "T4_TITLES",
    "planned_counts_t1",
    "planned_counts_t3",
    "planned_counts_t4",
    "planned_counts_t5",
    "plant_t1",
    "plant_t3",
    "plant_t4",
    "plant_t5",
]
