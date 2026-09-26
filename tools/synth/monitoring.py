"""Monitoring events and daily metrics of the synthetic lake (U11-09, design §5.1.2).

Rows use the impl 01 `EVENT_COLUMNS` and `METRIC_COLUMNS` names plus `_source_key`:
`<source_tool>:<event_key>` for events and `<source_tool>|<metric_name>|<service>|<date>`
for metrics. `service` is the catalog service name (the tool's tag), `ts`/`end_ts` are
ISO-8601 UTC with `Z`, `date` an ISO date, metric `value` a float. Plants (T4 events,
T5 request counts) are added or overridden by their own units.
"""

import hashlib
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Final

import numpy as np
import numpy.typing as npt

from tools.synth.catalog_rows import Catalog, ServiceRow
from tools.synth.params import SynthParams, SynthUsageError
from tools.synth.servicenow_common import _day_weight, _shard_days, arrival_times, span_end
from tools.synth.shards import IncidentTimeIndex, Shard
from tools.synth.text_vocab import SYMPTOMS

Row = dict[str, object]

SOURCE: Final = "monitoring"
TOOLS: Final = ("prometheus", "datadog", "splunk")
_EVENT_KEY_FORMATS: Final = {"prometheus": "{:016x}", "datadog": "{}", "splunk": "evt-{:010d}"}
_NEAR_S: Final = 30 * 60  # near-incident window: +/- 30 min
_DURATION_MEDIAN_MIN: Final = 20.0
_DURATION_SIGMA: Final = 0.8  # the spec gives only the median
_SERVER_CLASS: Final = "cmdb_ci_server"
_REQUESTS_BASE: Final = 10_000.0
_REQUEST_JITTER: Final = (0.95, 1.05)
_P1_DIP: Final = (0.5, 3.0)
_ERROR_RATE: Final = (0.001, 0.02)
_P95_MEDIAN_MS: Final = 250.0
_P95_SIGMA: Final = 0.3  # the spec gives only the median
_UNITS: Final = {
    "availability_pct": "percent",
    "error_rate": "ratio",
    "p95_latency_ms": "ms",
    "request_count": "count",
}


def _check_shard(shard: Shard, entity: str) -> None:
    if shard.source != SOURCE or shard.entity != entity:
        msg = f"shard must be {SOURCE}/{entity}"
        raise SynthUsageError(msg, key="shard")


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hosts(cat: Catalog) -> dict[str, tuple[str, ...]]:
    """Server CI names per service sys_id; a service without servers uses its own name."""
    out: dict[str, list[str]] = {s.sys_id: [] for s in cat.services}
    for ci in cat.cis:
        if ci.sys_class_name == _SERVER_CLASS:
            out[ci.service_sys_id].append(ci.name)
    names = {s.sys_id: s.name for s in cat.services}
    return {sid: tuple(hosts) or (names[sid],) for sid, hosts in out.items()}


def _near(
    index: IncidentTimeIndex, rng: np.random.Generator, service: ServiceRow
) -> tuple[datetime, str] | None:
    """A uniform point within +/- 30 min of a random incident of `service`, and its number."""
    opened = index.opened_at.get(service.sys_id)
    if opened is None or not len(opened):
        return None
    k = int(rng.integers(len(opened)))
    offset = int(rng.integers(-_NEAR_S, _NEAR_S + 1))
    return datetime.fromtimestamp(int(opened[k]) + offset, UTC), index.numbers[service.sys_id][k]


def _placement(
    params: SynthParams,
    rng: np.random.Generator,
    index: IncidentTimeIndex,
    service: ServiceRow,
    at: datetime,
) -> tuple[datetime, str | None]:
    """Event time and `incident_ref` for a non-info event (DD11-05 shares)."""
    ev = params.event
    if rng.random() >= ev.near_incident_share:
        return at, None
    near = _near(index, rng, service)
    if near is None:
        return at, None
    ts, incident = near
    return ts, incident if rng.random() < ev.near_incident_ref_share else None


def _event(
    params: SynthParams,
    rng: np.random.Generator,
    index: IncidentTimeIndex,
    hosts: tuple[str, ...],
    draw: tuple[int, datetime, ServiceRow, str, str],
    end: datetime,
) -> Row:
    seq, at, service, tool, severity = draw
    ref = None
    if severity != "info":
        at, ref = _placement(params, rng, index, service, at)
    minutes = float(rng.lognormal(math.log(_DURATION_MEDIAN_MIN), _DURATION_SIGMA))
    stop = at + timedelta(minutes=minutes)
    resolved = stop <= end
    symptom = SYMPTOMS[int(rng.integers(len(SYMPTOMS)))]
    title = f"{symptom[:1].upper()}{symptom[1:]} on {service.name}"
    dedup = hashlib.sha256(f"{tool}\x1f{service.name}\x1f{title}".encode()).hexdigest()[:16]
    event_key = _EVENT_KEY_FORMATS[tool].format(seq)
    return {
        "source_tool": tool,
        "event_key": event_key,
        "ts": _iso(at),
        "service": service.name,
        "host": hosts[int(rng.integers(len(hosts)))],
        "severity_raw": severity,
        "title": title,
        "status": "resolved" if resolved else "firing",
        "dedup_key": dedup,
        "end_ts": _iso(stop) if resolved else None,
        "incident_ref": ref,
        "_source_key": f"{tool}:{event_key}",
    }


def _event_services(cat: Catalog) -> tuple[tuple[ServiceRow, ...], npt.NDArray[np.float64]]:
    weights = np.array([s.event_weight for s in cat.services], dtype=np.float64)
    if not len(weights) or weights.sum() <= 0.0:
        msg = "catalog has no service with event weight"
        raise SynthUsageError(msg, key="catalog")
    return cat.services, weights / weights.sum()


def gen_events(
    cat: Catalog,
    params: SynthParams,
    shard: Shard,
    rng: np.random.Generator,
    incident_index: IncidentTimeIndex,
) -> list[Row]:
    """`shard.n_records` background events keyed from `shard.seq_start`: services by event
    weight, tools uniform, severities by `event.severities`; non-info events move near a
    same-service incident of `incident_index` with the DD11-05 shares."""
    _check_shard(shard, "event")
    services, p = _event_services(cat)
    n = shard.n_records
    times = arrival_times(params, shard, rng, n)
    picks = rng.choice(len(services), size=n, p=p)
    tools = rng.integers(len(TOOLS), size=n)
    levels = sorted(params.event.severities)
    severities = rng.choice(len(levels), size=n, p=[params.event.severities[s] for s in levels])
    hosts, end = _hosts(cat), span_end(params)
    rows = []
    for i, at in enumerate(times):
        service = services[int(picks[i])]
        draw = (shard.seq_start + i, at, service, TOOLS[int(tools[i])], levels[int(severities[i])])
        rows.append(_event(params, rng, incident_index, hosts[service.sys_id], draw, end))
    return rows


def _day_values(
    params: SynthParams, rng: np.random.Generator, share: float, day: date, p1: bool
) -> dict[str, float]:
    md = params.metric_daily
    availability = float(rng.uniform(md.availability_min, md.availability_max))
    if p1:
        availability -= float(rng.uniform(*_P1_DIP))
    jitter = float(rng.uniform(*_REQUEST_JITTER))
    return {
        "availability_pct": availability,
        "error_rate": float(rng.uniform(*_ERROR_RATE)),
        "p95_latency_ms": float(rng.lognormal(math.log(_P95_MEDIAN_MS), _P95_SIGMA)),
        "request_count": float(
            round(_REQUESTS_BASE * share * _day_weight(params.arrival, day) * jitter)
        ),
    }


def gen_metric_daily(
    cat: Catalog,
    params: SynthParams,
    shard: Shard,
    rng: np.random.Generator,
    p1_days: frozenset[tuple[str, date]],
    volume_by_day: Mapping[tuple[str, date], int],
) -> list[Row]:
    """Four metric rows per day of the shard month for the first `preset.metric_services`
    catalog services; `p1_days` holds `(service sys_id, day)` pairs with an availability
    dip. `volume_by_day` is accepted for the U11-09 signature; the background algorithm
    does not use it (T5 overrides `request_count` itself)."""
    _check_shard(shard, "metric_daily")
    del volume_by_day
    total = sum(s.event_weight for s in cat.services)
    rows: list[Row] = []
    for k, service in enumerate(cat.services[: params.preset.metric_services]):
        tool = TOOLS[k % len(TOOLS)]
        share = service.event_weight / total if total > 0 else 0.0
        for day in _shard_days(params, shard.month):
            p1 = (service.sys_id, day) in p1_days
            for name, value in _day_values(params, rng, share, day, p1).items():
                rows.append({
                    "source_tool": tool,
                    "date": day.isoformat(),
                    "service": service.name,
                    "metric_name": name,
                    "value": value,
                    "unit": _UNITS[name],
                    "_source_key": f"{tool}|{name}|{service.name}|{day.isoformat()}",
                })  # fmt: skip
    return rows


__all__ = ["TOOLS", "gen_events", "gen_metric_daily"]
