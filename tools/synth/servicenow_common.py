"""Shared helpers of the ServiceNow generators (U11-07).

Split out of `tools.synth.servicenow` for the module line budget. Records are
`{field: {"value", "display_value"}}` dicts as the Table API returns them with
`sysparm_display_value=all`; timestamps use the internal UTC format `YYYY-MM-DD HH:MM:SS`.
"""

import calendar
import dataclasses
import hashlib
import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

import numpy as np
import numpy.typing as npt

from herness.core import time as clock
from tools.synth.catalog_rows import Catalog, CiRow, ServiceRow, TeamRow
from tools.synth.param_groups import ArrivalParams, TextParams
from tools.synth.params import SynthParams, SynthUsageError
from tools.synth.rng import shard_key_hash
from tools.synth.shards import Shard
from tools.synth.text import TemplateBank, render_incident_text

Pair = dict[str, str]
Record = dict[str, Pair]

SOURCE: Final = "servicenow"
_TS_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
_UPDATE_LAG_S: Final = 2 * 3600.0  # sys_updated_on = latest timestamp + U(0, 2 h)
_SATURDAY, _SUNDAY = 5, 6
_DAYS_PER_YEAR: Final = 365.0
_CLUSTER_KEY: Final = "servicenow_cluster"


def pair(value: str, display: str | None = None) -> Pair:
    """Return a fresh `{value, display_value}` pair; the display defaults to the value."""
    return {"value": value, "display_value": value if display is None else display}


def ts_pair(at: datetime | None) -> Pair:
    """Timestamp pair in the internal UTC format; empty for `None`."""
    return pair("" if at is None else at.astimezone(UTC).strftime(_TS_FORMAT))


def stable_id(kind: str, name: str) -> str:
    """32-hex reference id for a record this generator does not write (user, rel type)."""
    return hashlib.sha256(f"{kind}:{name}".encode()).hexdigest()[:32]


def new_sys_id(rng: np.random.Generator) -> str:
    return rng.bytes(16).hex()


def number(prefix: str, seq: int) -> str:
    return f"{prefix}{seq:07d}"


def check_shard(shard: Shard, entity: str) -> None:
    """Raise `SynthUsageError` unless `shard` is a ServiceNow shard of `entity`."""
    if shard.source != SOURCE or shard.entity != entity:
        msg = f"shard must be {SOURCE}/{entity}"
        raise SynthUsageError(msg, key="shard")


def span_start(params: SynthParams) -> datetime:
    return datetime.combine(params.start, time(), UTC)


def span_end(params: SynthParams) -> datetime:
    """Exclusive end of the generated span: midnight UTC after `params.end`."""
    return datetime.combine(params.end + timedelta(days=1), time(), UTC)


def updated_on(rng: np.random.Generator, stamps: Sequence[datetime | None], end: datetime) -> Pair:
    """`sys_updated_on`: the latest timestamp up to `end` (else the first) plus U(0, 2 h)."""
    present = [s for s in stamps if s is not None]
    latest = max([s for s in present if s <= end] or present[:1])
    return ts_pair(latest + timedelta(seconds=float(rng.uniform(0.0, _UPDATE_LAG_S))))


def _shard_days(params: SynthParams, month: date) -> list[date]:
    last = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    first, last = max(month, params.start), min(last, params.end)
    return [first + timedelta(days=k) for k in range((last - first).days + 1)]


def _day_weight(arrival: ArrivalParams, day: date) -> float:
    weekday = day.weekday()
    weight = {_SATURDAY: arrival.saturday_factor, _SUNDAY: arrival.sunday_factor}.get(weekday, 1.0)
    if day.strftime("%m-%d") in arrival.holidays:
        weight *= arrival.holiday_factor
    doy = day.timetuple().tm_yday
    return weight * (
        1.0 + arrival.annual_amplitude * math.sin(2.0 * math.pi * doy / _DAYS_PER_YEAR)
    )


def arrival_times(
    params: SynthParams, shard: Shard, rng: np.random.Generator, n: int
) -> list[datetime]:
    """`n` sorted UTC times: weighted day of the shard month, hour curve in
    `business_timezone`, then uniform minute, second and microsecond (U11-07 step 1)."""
    days = _shard_days(params, shard.month)
    if n and not days:
        msg = "shard month lies outside the generated span"
        raise SynthUsageError(msg, key="shard")
    if not n:
        return []
    weights = np.array([_day_weight(params.arrival, d) for d in days])
    curve = np.array(params.arrival.hour_curve)
    picks = rng.choice(len(days), size=n, p=weights / weights.sum())
    hours = rng.choice(24, size=n, p=curve / curve.sum())
    minutes, seconds = rng.integers(0, 60, n), rng.integers(0, 60, n)
    micros = rng.integers(0, 1_000_000, n)
    zone = clock.zone(params.business_timezone)
    local = zip(picks, hours, minutes, seconds, micros, strict=True)
    times = [
        datetime.combine(days[d], time(int(h), int(m), int(s), int(us)), zone).astimezone(UTC)
        for d, h, m, s, us in local
    ]
    return sorted(times)


@dataclasses.dataclass(frozen=True, slots=True)
class ServiceIndex:
    """Lookups over a catalog: services with incident-weight probabilities, teams, CIs."""

    services: tuple[ServiceRow, ...]
    p: npt.NDArray[np.float64]
    teams: dict[str, TeamRow]
    cis: dict[str, tuple[CiRow, ...]]  # per service: its CIs other than the service CI

    def draw(self, rng: np.random.Generator, n: int) -> list[ServiceRow]:
        return [self.services[int(i)] for i in rng.choice(len(self.services), size=n, p=self.p)]

    def ci(self, rng: np.random.Generator, service: ServiceRow) -> CiRow:
        options = self.cis[service.sys_id]
        return options[int(rng.integers(len(options)))]

    def support(self, service: ServiceRow) -> TeamRow:
        return self.teams[service.support_team_sys_id]


def service_index(cat: Catalog) -> ServiceIndex:
    weights = np.array([s.incident_weight for s in cat.services], dtype=np.float64)
    if not len(weights) or weights.sum() <= 0.0:
        msg = "catalog has no service with incident weight"
        raise SynthUsageError(msg, key="catalog")
    cis: dict[str, list[CiRow]] = {s.sys_id: [] for s in cat.services}
    for ci in cat.cis:
        if ci.sys_id != ci.service_sys_id:
            cis[ci.service_sys_id].append(ci)
    by_service = {sid: tuple(rows) for sid, rows in cis.items()}
    teams = {t.sys_id: t for t in cat.teams}
    return ServiceIndex(cat.services, weights / weights.sum(), teams, by_service)


def cluster(bank: TemplateBank, service_sys_id: str) -> tuple[str, dict[str, str]]:
    """The recurring problem cluster of a service: its template family and slot set.

    Derived from the service's sys_id alone, so incident and problem shards agree.
    """
    rng = np.random.default_rng(shard_key_hash((_CLUSTER_KEY, service_sys_id)))
    rendered = render_incident_text(
        bank,
        rng,
        family=None,
        slots=None,
        change_flavored=False,
        repeat_flavored=False,
        impact_level=0,
        component="",
        text=TextParams(),
    )
    return rendered.family, rendered.slots


def ref(sys_id: str, display: str) -> Pair:
    """Reference field: the target's sys_id with its display name."""
    return pair(sys_id, display)


__all__ = [
    "SOURCE",
    "Pair",
    "Record",
    "ServiceIndex",
    "arrival_times",
    "check_shard",
    "cluster",
    "new_sys_id",
    "number",
    "pair",
    "ref",
    "service_index",
    "span_end",
    "span_start",
    "stable_id",
    "ts_pair",
    "updated_on",
]
