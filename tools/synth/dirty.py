"""Dirty-data defects at exact, counted rates (U11-16, design §5.1.6).

`apply_dirty` mutates one shard's source-shaped records in place and returns the extra
re-emitted records (duplicates, later versions, tombstone markers); `DirtyCounters` counts
every defect it applies, so the truth file holds exact numbers. Each record draws one
uniform per defect type in the fixed order of `DEFECTS`; a defect that does not fit the
record (an open incident has no `resolved_at` to move) is skipped and not counted.
Timestamp defects never touch `sys_updated_on` (the watermark) and `bad_timestamp` never
overwrites a field another defect of the same record just set, so every counted defect
stays observable. Tombstone markers are `{"__tombstone__": True, "key", "deleted_at"}`
with the source key (ServiceNow `sys_id`, Jira `id`) and a source-format timestamp.
"""

import copy
import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from tools.synth.jira_changelog import CATEGORIES, DONE, IN_PROGRESS, STATUS_IDS, TODO, jira_ts
from tools.synth.params import SynthParams
from tools.synth.servicenow_common import pair, parse_ts, ts_pair

__all__ = [
    "COST_FIELD",
    "DEFECTS",
    "FUTURE_SHIFT",
    "DirtyCounters",
    "apply_dirty",
    "effective_rates",
]

Rec = dict[str, Any]
Draws = npt.NDArray[np.float64]

DEFECTS: Final = (
    "bad_timestamp",
    "future_ts",
    "resolved_before_opened",
    "missing_service",
    "unknown_enum",
    "duplicate_rows",
    "later_versions",
    "tombstones",
)
COST_FIELD: Final = "customfield_10050"  # synth Jira cost estimate (U11-08)
FUTURE_SHIFT: Final = timedelta(days=730)
_DRIFT_IMPACT_MONTH: Final = 24  # month 25+: incidents gain u_business_impact
_DRIFT_COST_MONTH: Final = 29  # the first month >= 29: one Jira shard drops the cost field
_TS_FIELDS: Final = {
    "incident": ("opened_at", "u_acknowledged_at", "resolved_at", "closed_at"),
    "change_request": ("opened_at", "start_date", "end_date", "work_start", "work_end"),
}
_BAD_DATE: Final = "31/02/2024"
_ENUM_FIELDS: Final = {"incident": ("priority", "P2-ish"), "change_request": ("type", "Emergency ")}
_BACKDATE_H: Final = (1.0, 48.0)
_LATER_S: Final = (3_600.0, 10 * 86_400.0)  # later version / deletion: + U(1 h, 10 d)
_NEXT_STATE: Final = {"1": ("2", "In Progress"), "2": ("6", "Resolved"), "6": ("7", "Closed")}
_NEXT_STATUS: Final = {TODO: IN_PROGRESS, IN_PROGRESS: DONE}
_IMPACT: Final = {"1": ("1", "1 - High"), "2": ("1", "1 - High"), "3": ("2", "2 - Medium")}
_IMPACT_LOW: Final = ("3", "3 - Low")
_JIRA_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%f%z"
_VERSIONED: Final = frozenset({"incident", "issue"})


@dataclasses.dataclass(slots=True)
class DirtyCounters:
    """Applied defects per type (the truth `dirty` keys); per shard, summed by the parent."""

    bad_timestamp: int = 0
    future_ts: int = 0
    resolved_before_opened: int = 0
    missing_service: int = 0
    duplicate_rows: int = 0
    later_versions: int = 0
    tombstones: int = 0
    unknown_enum: int = 0

    def add(self, other: "DirtyCounters") -> None:
        """Add `other` field by field."""
        for name in DEFECTS:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def as_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


def effective_rates(params: SynthParams) -> dict[str, float]:
    """Per-defect rates: 0 at `none`, the defaults, or `heavy_multiplier` x default capped at
    1.0 at `heavy`."""
    rates = params.dirty_rates
    factor = {"none": 0.0, "default": 1.0, "heavy": rates.heavy_multiplier}[params.dirty]
    return {name: min(1.0, float(getattr(rates, name)) * factor) for name in DEFECTS}


def _jira_time(text: str) -> datetime:
    return datetime.strptime(text, _JIRA_FORMAT).astimezone(UTC)


def _later(rng: np.random.Generator, at: datetime) -> datetime:
    return at + timedelta(seconds=float(rng.uniform(*_LATER_S)))


def _updated(entity: str, record: Rec) -> datetime | None:
    if entity == "issue":
        return _jira_time(record["fields"]["updated"])
    return parse_ts(record["sys_updated_on"])


def _source_key(entity: str, record: Rec) -> str:
    return str(record["id"]) if entity == "issue" else str(record["sys_id"]["value"])


def _drift(entity: str, records: list[Rec], month_index: int) -> None:
    if entity == "incident" and month_index >= _DRIFT_IMPACT_MONTH:
        for rec in records:
            level = _IMPACT.get(rec.get("priority", {}).get("value", ""), _IMPACT_LOW)
            rec["u_business_impact"] = pair(*level)
    if entity == "issue" and month_index == _DRIFT_COST_MONTH:
        for rec in records:
            rec["fields"].pop(COST_FIELD, None)


def _future(rec: Rec, touched: set[str]) -> bool:
    opened = parse_ts(rec["opened_at"])
    if opened is None:
        return False
    rec["opened_at"] = ts_pair(opened + FUTURE_SHIFT)
    touched.add("opened_at")
    return True


def _backdate(rec: Rec, rng: np.random.Generator, touched: set[str]) -> bool:
    opened = parse_ts(rec["opened_at"])
    if opened is None or not rec["resolved_at"]["value"]:
        return False
    rec["resolved_at"] = ts_pair(opened - timedelta(hours=float(rng.uniform(*_BACKDATE_H))))
    touched.add("resolved_at")
    return True


def _bad_timestamp(entity: str, rec: Rec, rng: np.random.Generator, touched: set[str]) -> bool:
    names = [f for f in _TS_FIELDS[entity] if f not in touched and rec[f]["value"]]
    if not names:
        return False
    name = names[int(rng.integers(len(names)))]
    at = parse_ts(rec[name])
    epoch_ms = "" if at is None else str(int(at.timestamp() * 1000))
    rec[name] = pair((_BAD_DATE, epoch_ms, "")[int(rng.integers(3))])
    return True


def _missing_service(rec: Rec) -> bool:
    if not (rec["business_service"]["value"] or rec["cmdb_ci"]["value"]):
        return False
    rec["business_service"], rec["cmdb_ci"] = pair(""), pair("")
    return True


def _mutate(
    entity: str, rec: Rec, u: Draws, r: dict[str, float], rng: np.random.Generator
) -> list[str]:
    """In-place defects of one record; returns the names of the defects applied."""
    hits: list[str] = []
    touched: set[str] = set()
    if entity == "incident":
        if u[1] < r["future_ts"] and _future(rec, touched):
            hits.append("future_ts")
        if u[2] < r["resolved_before_opened"] and _backdate(rec, rng, touched):
            hits.append("resolved_before_opened")
        if u[3] < r["missing_service"] and _missing_service(rec):
            hits.append("missing_service")
    if entity in _TS_FIELDS:
        if u[0] < r["bad_timestamp"] and _bad_timestamp(entity, rec, rng, touched):
            hits.append("bad_timestamp")
        if u[4] < r["unknown_enum"]:
            field, odd = _ENUM_FIELDS[entity]
            rec[field] = pair(odd)
            hits.append("unknown_enum")
    return hits


def _later_version(entity: str, rec: Rec, rng: np.random.Generator) -> Rec:
    out = copy.deepcopy(rec)
    if entity == "issue":
        fields = out["fields"]
        fields["updated"] = jira_ts(_later(rng, _jira_time(fields["updated"])))
        name = _NEXT_STATUS.get(fields["status"]["name"], fields["status"]["name"])
        fields["status"] = {"name": name, "id": STATUS_IDS[name]}
        fields["status"]["statusCategory"] = {"key": CATEGORIES[name]}
        return out
    at = parse_ts(out["sys_updated_on"])
    out["sys_updated_on"] = ts_pair(_later(rng, at) if at else None)
    state = out["state"]["value"]
    out["state"] = pair(*_NEXT_STATE[state]) if state in _NEXT_STATE else out["state"]
    return out


def _tombstone(entity: str, rec: Rec, rng: np.random.Generator) -> Rec:
    at = _updated(entity, rec)
    deleted = _later(rng, at) if at else None
    if entity == "issue":
        stamp = jira_ts(deleted) if deleted else ""
    else:
        stamp = ts_pair(deleted)["value"]
    return {"__tombstone__": True, "key": _source_key(entity, rec), "deleted_at": stamp}


def _reemits(
    entity: str, rec: Rec, u: Draws, r: dict[str, float], rng: np.random.Generator
) -> list[tuple[str, Rec]]:
    out: list[tuple[str, Rec]] = []
    if u[5] < r["duplicate_rows"]:
        out.append(("duplicate_rows", copy.deepcopy(rec)))
    if entity in _VERSIONED and u[6] < r["later_versions"]:
        out.append(("later_versions", _later_version(entity, rec, rng)))
    if entity in _VERSIONED and u[7] < r["tombstones"]:
        out.append(("tombstones", _tombstone(entity, rec, rng)))
    return out


def apply_dirty(
    entity: str,
    records: list[Rec],
    month_index: int,
    params: SynthParams,
    rng: np.random.Generator,
    counters: DirtyCounters,
) -> list[Rec]:
    """Inject the §5.1.6 defects into `records` (in place) and return the re-emits.

    `month_index` is the shard month's offset from the span start (0 = first month); it
    drives the schema drift. Draws only from `rng`; for `none` nothing changes."""
    if params.dirty == "none":
        return []
    _drift(entity, records, month_index)
    rates = effective_rates(params)
    draws = rng.random((len(records), len(DEFECTS)))
    extra: list[Rec] = []
    for rec, u in zip(records, draws, strict=True):
        for name in _mutate(entity, rec, u, rates, rng):
            setattr(counters, name, getattr(counters, name) + 1)
        for name, copy_ in _reemits(entity, rec, u, rates, rng):
            setattr(counters, name, getattr(counters, name) + 1)
            extra.append(copy_)
    return extra
