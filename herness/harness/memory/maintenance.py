"""The `memory_maintenance` job handler (impl 07 U07-96, T07-22; design 07 §5.12).

Seven idempotent steps, each followed by a saved step, a heartbeat and a yield check; a resumed
job starts at `ctx.load_state()["step"]`. Steps 5 (vector consistency, TH07-13: SQLite is the
source of truth) and 6 (backfill) live in the private sibling `_maintenance_vectors`.
Collaborators come from the one module seam `configure_maintenance` (wired by the T07-23
composition root). Logs carry ids and counts only, never memory text.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Final, cast, get_args

import duckdb
from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.jobs import JobContext
from herness.core.logging import get_logger
from herness.core.types import JobOutcome, MetricSample, Status
from herness.harness.memory import _maintenance_vectors as mv
from herness.harness.memory.lifecycle import MemoryLifecycle
from herness.harness.memory.procedural import ProceduralDeps, promote_procedural, validate_templates
from herness.harness.memory.store import Embedder, VectorIndex
from herness.store import warehouse
from herness.store.errors import NotFoundError
from herness.store.ops import closed_loop, core
from herness.store.ops import memory as ops
from herness.store.ops.metrics import record_metric_samples
from herness.store.ops.shared import create_review_item

__all__ = [
    "MaintenanceDeps", "configure_maintenance", "maintenance_deps", "memory_maintenance_handler",
]  # fmt: skip

type Row = ops.MemoryItemRow
type Conn = sqlite3.Connection
type Counts = dict[str, JsonValue]

_REVIEW_LIMIT: Final = 10_000  # maintenance_rows maximum; the rest is due tomorrow
_RECENT: Final = timedelta(days=7)
_YEAR: Final = timedelta(days=365)
_STATUSES: Final[tuple[str, ...]] = get_args(Status)
_log = get_logger("memory")


@dataclass(frozen=True, slots=True)
class MaintenanceDeps:
    """Collaborators of the one-argument handler (T07-22 spec note); T07-23 builds them."""

    lifecycle: MemoryLifecycle  # step 1
    procedural: ProceduralDeps  # steps 2-3
    vectors: VectorIndex  # steps 5-6
    embedder: Embedder  # step 5 model check, step 6 embeddings
    open_current: Callable[[], duckdb.DuckDBPyConnection] = warehouse.open_readonly  # step 3


_SEAM: dict[str, MaintenanceDeps] = {}  # the module's only state


def configure_maintenance(deps: MaintenanceDeps | None) -> None:
    """Set the handler's collaborators (composition root, T07-23); `None` clears them."""
    if deps is None:
        _SEAM.clear()
    else:
        _SEAM["deps"] = deps


def maintenance_deps() -> MaintenanceDeps:
    """The configured collaborators; ConfigError when the composition root set none."""
    if (deps := _SEAM.get("deps")) is None:
        msg = "memory maintenance is not configured: call configure_maintenance first"
        raise ConfigError(msg)
    return deps


@dataclass(frozen=True, slots=True)
class _Run:
    deps: MaintenanceDeps
    ctx: JobContext
    now: datetime

    @property
    def stamp(self) -> str:
        return clock.format_utc(self.now)


def _expire(r: _Run) -> JsonValue:
    return r.deps.lifecycle.expire(r.now)


def _promote(r: _Run) -> JsonValue:
    runs = closed_loop.recent_done_runs(clock.format_utc(r.now - _RECENT))
    created = 0
    for run_id in runs:
        created += promote_procedural(run_id, deps=r.deps.procedural, now=r.now).templates_created
    return {"runs": len(runs), "templates_created": created}


def _templates(r: _Run) -> JsonValue:
    try:
        con = r.deps.open_current()
    except NotFoundError as exc:  # no promoted build yet: nothing to EXPLAIN against
        if exc.kind != "current":  # CURRENT names a missing build: a broken warehouse
            raise
        _log.info("memory.maintenance.templates_skipped", reason="no_current_build")
        return {"skipped": "no_current_build"}
    try:
        validated, expired = validate_templates(con=con, deps=r.deps.procedural, now=r.now)
    finally:
        con.close()
    return {"validated": validated, "expired": expired}


def _fts(r: _Run) -> JsonValue:
    del r
    return {"rebuilt": ops.fts_check_and_rebuild()}


def _vectors(r: _Run) -> JsonValue:
    return mv.check_vectors(r.deps.vectors, r.deps.embedder.model_name, r.stamp)


def _backfill(r: _Run) -> JsonValue:
    return mv.backfill(r.deps.vectors, r.deps.embedder, r.stamp, r.ctx.heartbeat)


def _review_tx(row: Row, now: datetime, conn: Conn) -> bool:
    """One yearly review item plus `data.last_review_requested_at`, if still due."""
    cur = ops.get_memory_items([row["memory_id"]], conn=conn)
    last = "last_review_requested_at"
    if not cur or cur[0]["status"] != "active" or cur[0]["data"].get(last) != row["data"].get(last):
        return False
    data = cur[0]["data"]
    payload = {
        "memory_id": row["memory_id"], "layer": cur[0]["layer"], "kind": cur[0]["kind"],
        "content": cur[0]["content"], "numbers": data.get("numbers", []),
        "entities": data.get("entities", []), "provenance": cur[0]["provenance"],
        "flags": ["yearly_review"], "conflicts_with": data.get("conflicts_with", []),
    }  # fmt: skip
    create_review_item("memory_write", payload, now=now, conn=conn)
    stamped: dict[str, JsonValue] = {**data, last: clock.format_utc(now)}
    ops.update_memory_item(row["memory_id"], data=stamped, conn=conn)
    return True


def _review(r: _Run) -> JsonValue:
    cutoff = clock.format_utc(r.now - _YEAR)
    rows = ops.maintenance_rows(selector="business_rule_review_due", now=r.stamp,
                                limit=_REVIEW_LIMIT, review_cutoff=cutoff)  # fmt: skip
    return sum(core.run_write(partial(_review_tx, row, r.now), op="memory_yearly_review")
               for row in cast("list[Row]", rows))  # fmt: skip


_STEPS: Final[tuple[tuple[str, Callable[[_Run], JsonValue]], ...]] = (
    ("expire", _expire), ("promote", _promote), ("templates", _templates), ("fts", _fts),
    ("vectors", _vectors), ("backfill", _backfill), ("review", _review),
)  # fmt: skip


def _finish(r: _Run, counts: Counts) -> JobOutcome:
    """Gauges through record_metric_samples (R-12), the completion log, the done outcome."""
    vec = counts.get("vectors")
    tally = vec.get("statuses") if isinstance(vec, dict) else None
    tally = tally if isinstance(tally, dict) else {}

    def gauge(name: str, value: float, **labels: str) -> MetricSample:
        return MetricSample(ts=r.now, name=name, kind="gauge", value=value, labels=labels,
                            component="memory")  # fmt: skip

    samples = [gauge("herness_memory_items_total", float(cast("int", tally.get(s, 0))), status=s)
               for s in _STATUSES]  # fmt: skip
    samples.append(gauge("herness_memory_embedding_pending_total",
                         float(ops.pending_embedding_count())))  # fmt: skip
    record_metric_samples(samples)
    _log.info("memory.maintenance.completed", job_id=r.ctx.job_id, counts=counts)
    return JobOutcome(status="done", result=counts)


def memory_maintenance_handler(ctx: JobContext) -> JobOutcome:
    """Daily memory upkeep (U07-96): steps 1-7 with saved progress, then gauges and a log."""
    r = _Run(maintenance_deps(), ctx, clock.now())
    state = ctx.load_state()
    start, saved = state.get("step"), state.get("counts")
    if not (isinstance(start, int) and 0 <= start <= len(_STEPS) and isinstance(saved, dict)):
        start, saved = 0, {}
    counts: Counts = dict(saved)
    for index in range(start, len(_STEPS)):
        name, step = _STEPS[index]
        counts[name] = step(r)
        ctx.save_state({"step": index + 1, "counts": counts})
        ctx.heartbeat(f"memory_maintenance step {index + 1}")
        if ctx.should_yield():
            return JobOutcome(status="yield", result=counts)
    return _finish(r, counts)
