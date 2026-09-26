"""Plant T3: change-caused incident cluster on C3 with control changes (U11-12).

Split out of `tools.synth.plants_ops` for the module line budget; `tools.synth.plants_ops`
re-exports `plant_t3` and `planned_counts_t3`. The 80 changes, their follow-up counts and
the 30 % `caused_by` flags come from the catalog's change schedule (drawn from the
`plants:t3` stream at build time), so every shard agrees without replaying that stream.
Changes and follow-up incidents are produced by the shard of their change's `work_end`
month; a follow-up may open up to 2 h into the next month.
"""

import bisect
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Final

import numpy as np

from tools.synth.catalog_plan import plant_range
from tools.synth.catalog_rows import Catalog, ChangeSlot, CiRow, MonthKey, ServiceRow
from tools.synth.params import SynthParams, SynthUsageError
from tools.synth.servicenow import (
    WINDOW_H,
    WORK_SHIFT_MIN,
    ChangeDraw,
    change_context,
    change_record,
)
from tools.synth.servicenow_common import (
    Record,
    number,
    pair,
    parse_ts,
    ref,
    service_index,
    span_end,
    span_start,
)
from tools.synth.servicenow_incidents import IncidentSpec, make_incident, retime
from tools.synth.shards import PlantOutput, Shard
from tools.synth.text import TemplateBank

SOURCE: Final = "servicenow"
_WINDOW_S: Final = 2 * 3600  # follow-ups open in (t, t + 2 h]
_WINDOW: Final = timedelta(seconds=_WINDOW_S)
_CONTROL_SHIFT: Final = timedelta(hours=3)


def _month(slot: ChangeSlot) -> date:
    return slot.work_end.date().replace(day=1)


def planned_counts_t3(cat: Catalog, params: SynthParams) -> dict[MonthKey, int]:
    """The 80 C3 changes and their follow-up incidents, by `work_end` month."""
    counts: Counter[MonthKey] = Counter()
    for slot in cat.change_schedule:
        counts[SOURCE, "change_request", _month(slot)] += 1
        if slot.follow_up_incidents:
            counts[SOURCE, "incident", _month(slot)] += slot.follow_up_incidents
    return dict(counts)


def _change_numbers(cat: Catalog) -> dict[str, str]:
    """`CHG` number of every scheduled change: its month's plant range, in `seq` order."""
    out: dict[str, str] = {}
    offsets: Counter[date] = Counter()
    for slot in cat.change_schedule:
        month = _month(slot)
        first, _ = plant_range(cat, planned_counts_t3, (SOURCE, "change_request", month))
        out[slot.sys_id] = number("CHG", first + offsets[month])
        offsets[month] += 1
    return out


def _targets(cat: Catalog) -> tuple[ServiceRow, CiRow]:
    service = next(s for s in cat.services if s.sys_id == cat.plants.s3)
    return service, next(ci for ci in cat.cis if ci.sys_id == cat.plants.c3)


def _changes(
    shard: Shard, cat: Catalog, params: SynthParams, rng: np.random.Generator, bank: TemplateBank
) -> list[Record]:
    """The shard month's C3 changes: `work_end` from the schedule, the window before it."""
    ctx, (service, c3) = change_context(cat, params, bank), _targets(cat)
    first = span_start(params)
    numbers = _change_numbers(cat)
    rows = []
    for slot in (s for s in cat.change_schedule if _month(s) == shard.month):
        hours = float(rng.uniform(*WINDOW_H))
        work_start = max(slot.work_end - timedelta(hours=hours), first)
        shift = rng.uniform(-WORK_SHIFT_MIN, WORK_SHIFT_MIN, 2)
        planned_start = max(work_start + timedelta(minutes=float(shift[0])), first)
        planned = (planned_start, slot.work_end + timedelta(minutes=float(shift[1])))
        kind = "emergency" if slot.emergency else "normal"
        seq = int(numbers[slot.sys_id][3:])
        draw = ChangeDraw(service, seq, kind, planned, (work_start, slot.work_end), c3)
        record = change_record(ctx, rng, draw)
        record["sys_id"] = pair(slot.sys_id)  # the catalog id, so incidents can name it
        rows.append(record)
    return rows


def _incidents(
    shard: Shard, cat: Catalog, params: SynthParams, rng: np.random.Generator, bank: TemplateBank
) -> PlantOutput:
    """Follow-up incidents of the shard month's emergency changes, numbered from the plan."""
    index, (service, c3) = service_index(cat), _targets(cat)
    seq, _ = plant_range(cat, planned_counts_t3, (SOURCE, "incident", shard.month))
    numbers = _change_numbers(cat)
    out = PlantOutput()
    for slot in (s for s in cat.change_schedule if _month(s) == shard.month):
        offsets = rng.integers(1, _WINDOW_S + 1, slot.follow_up_incidents)
        for offset, caused in zip(offsets, slot.caused_by, strict=True):
            opened = slot.work_end + timedelta(seconds=int(offset))
            spec = IncidentSpec(service, opened, seq, ci=c3, change_flavored=True)
            record, labels = make_incident(params, rng, bank, index, spec)
            seq += 1
            if caused:
                record["caused_by"] = ref(slot.sys_id, numbers[slot.sys_id])
            record_id = f"servicenow:incident:{record['sys_id']['value']}"
            out.records.append(record)
            out.labels.extend(labels)
            out.members.append(record_id)
            out.links.append({
                "incident_record_id": record_id,
                "change_record_id": f"servicenow:change_request:{slot.sys_id}",
            })  # fmt: skip
    return out


def _in_window(at: datetime, ends: Sequence[datetime]) -> bool:
    """True when `at` lies in (t, t + 2 h] of some `t` in the sorted `ends`."""
    k = bisect.bisect_left(ends, at)
    return k > 0 and at <= ends[k - 1] + _WINDOW


def _free_time(opened: datetime, ends: Sequence[datetime], end: datetime) -> datetime:
    """`opened` moved by +3 h steps out of every control window; backwards by -3 h steps
    when that would pass the span end."""
    at = opened + _CONTROL_SHIFT
    while _in_window(at, ends):
        at += _CONTROL_SHIFT
    if at < end:
        return at
    at = opened - _CONTROL_SHIFT
    while _in_window(at, ends):
        at -= _CONTROL_SHIFT
    return at


def _clear_controls(
    background: list[Record], cat: Catalog, params: SynthParams, rng: np.random.Generator
) -> None:
    """Shift background incidents on C3 out of the 2 h after each control change."""
    ends = sorted(s.work_end for s in cat.change_schedule if not s.emergency)
    end = span_end(params)
    for record in background:
        opened = parse_ts(record["opened_at"])
        if record["cmdb_ci"]["value"] != cat.plants.c3 or opened is None:
            continue
        if not _in_window(opened, ends):
            continue
        resolved = parse_ts(record["resolved_at"])
        mttr = None if resolved is None else resolved - opened
        retime(record, params, rng, opened=_free_time(opened, ends, end), mttr=mttr)


def plant_t3(
    shard: Shard,
    cat: Catalog,
    params: SynthParams,
    rng: np.random.Generator,
    bank: TemplateBank,
    background: list[Record],
) -> PlantOutput:
    """T3 for a ServiceNow shard: a `change_request` shard gets the month's C3 changes; an
    `incident` shard gets the follow-up incidents (links = planted pairs) and has its
    background C3 incidents moved out of the control windows (mutates `background`)."""
    if shard.source != SOURCE or shard.entity not in ("incident", "change_request"):
        msg = "T3 plants only ServiceNow incident and change_request shards"
        raise SynthUsageError(msg, key="shard")
    if shard.entity == "change_request":
        return PlantOutput(records=_changes(shard, cat, params, rng, bank))
    _clear_controls(background, cat, params, rng)
    return _incidents(shard, cat, params, rng, bank)


__all__ = ["planned_counts_t3", "plant_t3"]
