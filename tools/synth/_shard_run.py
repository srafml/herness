"""Private sibling of `tools.synth.shards` (U11-19): one shard's generation and lake write.

Split off for the 350-line budget of `shards.py`, which imports it lazily (`run_shard`)
because the generators import `Shard` from `tools.synth.shards`. Per shard (F11-02): the
source generators in chunks of at most `MAX_ROWS` background records (an incident month is
generated whole, because the incident plants work on the month's background), the plants
(incident shards: T6 on the background first, then T1, T3, T5, then the T2/T2c clusters),
the incident time index (phase A, before the dirty defects), `apply_dirty`, the month's
`task_sla` rows from the final incident records (U11-77), `assign_fetch`, `to_lake_batch`
and `LakeWriter`; then the truth parts. The `owning_team` label follows the final
`assignment_group`, since T1 and T5 reassign incidents after the background labels are drawn.
"""

import dataclasses
import functools
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, time, timedelta
from typing import Any, Final

import numpy as np

from tools.synth import _shard_io as io
from tools.synth import plants_delivery as delivery
from tools.synth import plants_ops as ops
from tools.synth import servicenow as sn
from tools.synth.dirty import DirtyCounters, apply_dirty
from tools.synth.fetch import assign_fetch
from tools.synth.flatten import MAX_ROWS, source_updated_at
from tools.synth.jira import gen_issues
from tools.synth.jira_links import WINDOW
from tools.synth.monitoring import gen_events, gen_metric_daily
from tools.synth.rng import shard_rng
from tools.synth.servicenow_aux import gen_departments, gen_task_slas
from tools.synth.servicenow_common import span_start, ts_pair
from tools.synth.shards import Shard, ShardResult, WorkerContext

Rec = dict[str, Any]
Gen = np.random.Generator

_DRIFT_BARE_MONTH: Final = 29  # month 30+: bare priority in 50 % of the incident files
_BARE_SHARE: Final = 0.5  # per-file coin of the bare-priority drift
_NOISE: Final = frozenset({"critical", "major", "minor", "warning"})
_DAY: Final = timedelta(days=1)


@dataclasses.dataclass(slots=True)
class Generated:
    """A shard's records before dirty defects, with its truth rows."""

    records: list[Rec]
    labels: list[dict[str, str]] = dataclasses.field(default_factory=list)
    pii: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    texts: list[dict[str, str]] = dataclasses.field(default_factory=list)
    members: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    pairs: list[dict[str, str]] = dataclasses.field(default_factory=list)


def _chunks(shard: Shard) -> Iterator[Shard]:
    for first in range(0, shard.n_records, MAX_ROWS):
        n = min(MAX_ROWS, shard.n_records - first)
        yield dataclasses.replace(shard, n_records=n, seq_start=shard.seq_start + first)


def _month_start(shard: Shard) -> datetime:
    return datetime.combine(shard.month, time(), UTC)


def _next_month(shard: Shard) -> datetime:
    return datetime.combine((shard.month.replace(day=28) + 4 * _DAY).replace(day=1), time(), UTC)


def _dimension(shard: Shard, ctx: WorkerContext, rng: Gen) -> Generated:
    cat, params = ctx.cat, ctx.params
    rows: dict[str, Callable[[], list[Rec]]] = {
        "sys_user_group": lambda: sn.gen_groups(cat, params),
        "cmn_department": lambda: gen_departments(cat, params, rng),
        "cmdb_ci": lambda: sn.gen_cis(cat, params)[0],
        "cmdb_ci_service": lambda: sn.gen_cis(cat, params)[1],
        "cmdb_rel_ci": lambda: sn.gen_rels(cat, params),
    }
    records = rows[shard.entity]()
    stamp = ts_pair(span_start(params))
    for rec in records:  # CIs and relations carry no stamp; dimensions are stamped at start
        rec.setdefault("sys_updated_on", dict(stamp))
    return Generated(records)


def _sync_owning_team(records: list[Rec], labels: list[dict[str, str]]) -> None:
    team = {f"servicenow:incident:{r['sys_id']['value']}": r["assignment_group"]["value"]
            for r in records}  # fmt: skip
    for row in labels:
        if row["question"] == "owning_team" and row["record_id"] in team:
            row["answer"] = team[row["record_id"]]


def _incident(shard: Shard, ctx: WorkerContext, rng: Gen) -> Generated:
    cat, params, bank = ctx.cat, ctx.params, ctx.bank
    batch = sn.gen_incidents(cat, params, shard, rng, bank, ctx.names)
    records = batch.records
    t6 = delivery.plant_t6(records, shard, cat, params, rng)
    ops.plant_t1(records, cat, params, rng)
    t3 = ops.plant_t3(shard, cat, params, rng, bank, records)
    t5 = ops.plant_t5(records, [], shard, cat, params, rng)
    t2 = delivery.plant_t2(shard, cat, params, rng, bank)
    t2c = delivery.plant_t2c(shard, cat, params, rng, bank)
    gone = set(t6.replaced)
    out = Generated(records, [r for r in batch.labels if r["record_id"] not in gone])
    out.pii = [r for r in batch.pii if r["record_id"] not in gone]
    for plant in (t6, t3, t5, t2, t2c):
        out.records.extend(plant.records)
        out.labels.extend(plant.labels)
    _sync_owning_team(out.records, out.labels)
    out.members = {"T2": t2.members, "T2c": t2c.members, "T3": t3.members}
    out.pairs = t3.links
    out.texts = [
        {"record_id": f"servicenow:incident:{r['sys_id']['value']}",
         "short_description": r["short_description"]["value"],
         "description": r["description"]["value"]}
        for r in out.records
    ]  # fmt: skip
    io.write_index(ctx.parts_dir, shard.month, out.records)
    return out


def _change(shard: Shard, ctx: WorkerContext, rng: Gen) -> Generated:
    cat, params, bank = ctx.cat, ctx.params, ctx.bank
    records = [r for sub in _chunks(shard) for r in sn.gen_changes(cat, params, sub, rng, bank)]
    records.extend(ops.plant_t3(shard, cat, params, rng, bank, []).records)
    return Generated(records)


def _problem(shard: Shard, ctx: WorkerContext, rng: Gen) -> Generated:
    cat, params, bank = ctx.cat, ctx.params, ctx.bank
    return Generated(
        [r for s in _chunks(shard) for r in sn.gen_problems(cat, params, s, rng, bank)]
    )


def _issue(shard: Shard, ctx: WorkerContext, rng: Gen) -> Generated:
    cat, params, bank = ctx.cat, ctx.params, ctx.bank
    index, _ = io.load_index(ctx.parts_dir, _month_start(shard) - WINDOW, _next_month(shard))
    out = Generated([])
    for sub in _chunks(shard):
        batch = gen_issues(cat, params, sub, rng, bank, ctx.names, incident_index=index)
        out.records.extend(batch.records)
        out.pii.extend(batch.pii)
    out.records.extend(delivery.plant_t2(shard, cat, params, rng, bank).records)
    out.records.extend(delivery.plant_t6(out.records, shard, cat, params, rng).records)
    return out


def _event(shard: Shard, ctx: WorkerContext, rng: Gen) -> Generated:
    cat, params = ctx.cat, ctx.params
    index, _ = io.load_index(ctx.parts_dir, _month_start(shard) - _DAY, _next_month(shard) + _DAY)
    records = [r for s in _chunks(shard) for r in gen_events(cat, params, s, rng, index)]
    records.extend(ops.plant_t4(shard, cat, params, rng, index).records)
    return Generated(records)


def _metric(shard: Shard, ctx: WorkerContext, rng: Gen) -> Generated:
    cat, params = ctx.cat, ctx.params
    _, p1 = io.load_index(ctx.parts_dir, _month_start(shard), _next_month(shard))
    rows = gen_metric_daily(cat, params, shard, rng, p1, {})
    ops.plant_t5([], rows, shard, cat, params, rng)
    return Generated(rows)


_GENERATORS: Final[dict[tuple[str, str], Callable[[Shard, WorkerContext, Gen], Generated]]] = {
    ("servicenow", "incident"): _incident,
    ("servicenow", "change_request"): _change,
    ("servicenow", "problem"): _problem,
    ("jira", "issue"): _issue,
    ("monitoring", "event"): _event,
    ("monitoring", "metric_daily"): _metric,
}


def _s4_noise(shard: Shard, ctx: WorkerContext, rows: list[Rec]) -> tuple[int, int]:
    if (shard.source, shard.entity) != ("monitoring", "event"):
        return 0, 0
    s4 = next(s.name for s in ctx.cat.services if s.sys_id == ctx.cat.plants.s4)
    noisy = [r for r in rows if r.get("service") == s4 and r.get("severity_raw") in _NOISE]
    return sum(1 for r in noisy if r.get("incident_ref") is None), len(noisy)


def _month_index(shard: Shard, ctx: WorkerContext) -> int:
    start = ctx.params.start
    return (shard.month.year - start.year) * 12 + shard.month.month - start.month


def _write(shard: Shard, ctx: WorkerContext, rng: Gen, gen: Generated, counters: DirtyCounters
           ) -> io.LakeSink:  # fmt: skip
    """Dirty defects, task SLAs, fetch placement and the lake writes (not yet committed)."""
    params, source, entity = ctx.params, shard.source, shard.entity
    month_index = _month_index(shard, ctx)
    extra = apply_dirty(entity, gen.records, month_index, params, rng, counters)
    sink = io.LakeSink(ctx.raw_root)
    try:
        when = functools.partial(source_updated_at, source, entity)
        rows = assign_fetch(gen.records, extra, params, rng, updated_at_of=when)
        drift = entity == "incident" and month_index >= _DRIFT_BARE_MONTH
        sink.write(
            source, entity, rows, bare=(lambda: bool(rng.random() < _BARE_SHARE)) if drift else None
        )
        if entity == "incident":
            slas = gen_task_slas(gen.records, params, rng)
            sla_when = functools.partial(source_updated_at, source, "task_sla")
            sink.write(
                source, "task_sla", assign_fetch(slas, [], params, rng, updated_at_of=sla_when)
            )
    except BaseException:
        sink.abort()
        raise
    gen.records.extend(extra)
    return sink


def execute(shard: Shard, ctx: WorkerContext) -> ShardResult:
    """Run one shard end to end (see the module docstring)."""
    rng = shard_rng(ctx.seed, (shard.source, shard.entity, shard.month.isoformat()))
    if shard.source == "servicenow" and shard.entity not in (
        "incident",
        "change_request",
        "problem",
    ):
        gen = _dimension(shard, ctx, rng)
    else:
        gen = _GENERATORS[shard.source, shard.entity](shard, ctx, rng)
    counters = DirtyCounters()
    sink = _write(shard, ctx, rng, gen, counters)
    try:
        files = sink.commit()
    except BaseException:
        sink.abort()
        raise
    parts = ctx.parts_dir
    labels_path = io.write_part(parts, "labels", shard.index, gen.labels)
    io.write_part(parts, "texts", shard.index, gen.texts)
    io.write_part(parts, "pii", shard.index, gen.pii)
    members = [{"record_id": m, "plant": p} for p in ("T2", "T2c") for m in gen.members.get(p, [])]
    io.write_part(parts, "members", shard.index, members)
    io.write_part(parts, "pairs", shard.index, gen.pairs)
    return ShardResult(
        rows_written=sum(sink.rows.values()),
        dirty=counters,
        labels_path=labels_path,
        pii_rows=len(gen.pii),
        plant_members={k: v for k, v in gen.members.items() if v},
        plant_pairs=gen.pairs,
        files=files,
        row_counts=dict(sink.rows),
        s4_noise=_s4_noise(shard, ctx, gen.records),
    )


__all__ = ["Generated", "execute"]
