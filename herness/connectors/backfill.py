"""Resumable parallel date-slice backfill (impl 01 U01-43; design 01 §5.5, flow F01-02).

`[start, end)` is cut into `slice_days` slices planned in the ops-store `sync_slice` table
(`ensure_slices`). Slices not yet `done` run in up to `max_workers` threads, each with its
own connector (when the runner has a factory), `LakeWriter` and `DeletionFilter`, so
deleted records are filtered before every write (TH01-11). A `done` slice is never rerun;
`running` and `failed` slices rerun from their start. The watermark moves once, after
every planned slice is `done`: to `min(end, max committed _source_updated_at)` of this
call, or to `end` when this call committed no rows (spec §13 D-8).
"""

from __future__ import annotations

import contextvars
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from herness.connectors._write_loop import StreamOutcome
from herness.connectors.base import UNORDERED_SOURCES, split_range
from herness.connectors.deletion import DeletionFilter
from herness.connectors.runner import FetchOf, SyncResult
from herness.core import time as clock
from herness.core.errors import FatalError
from herness.core.logging import get_logger
from herness.core.resilience import guard
from herness.store.ops import (
    SliceRow,
    ensure_slices,
    get_watermark,
    mark_slice_done,
    mark_slice_failed,
    mark_slice_running,
    set_watermark,
)

if TYPE_CHECKING:
    from herness.connectors.runner import SyncRunner

__all__ = ["run_backfill"]

_log = get_logger("connectors.backfill")


def _total(outcomes: list[StreamOutcome]) -> StreamOutcome:
    """Sum of the slice outcomes; `max_committed` is the latest over all slices."""
    tops = [o.max_committed for o in outcomes if o.max_committed is not None]
    return StreamOutcome(
        sum(o.rows for o in outcomes),
        sum(o.tombstones for o in outcomes),
        sum(o.skipped_deleted for o in outcomes),
        tuple(path for o in outcomes for path in o.files),
        max(tops, default=None),
    )


def _first_error(errors: list[BaseException]) -> BaseException:
    """Step 6: the first `FatalError` in slice order, else the first error."""
    return next((e for e in errors if isinstance(e, FatalError)), errors[0])


class _Slices:
    """One `run_backfill` call: runs the slice tasks of one stream (U01-43 steps 3-6)."""

    def __init__(self, runner: SyncRunner, entity: str, key: str, fetch_of: FetchOf) -> None:
        self.runner, self.entity, self.key, self.fetch_of = runner, entity, key, fetch_of

    def run_all(self, todo: list[SliceRow], max_workers: int) -> list[StreamOutcome | None]:
        """Steps 3-6: run every slice; `None` marks a slice skipped for a stop request.
        Raises the first fatal (else first) slice error after all slices finished."""
        if not todo:
            return []
        parallel = self.runner.connector_factory is not None
        workers = max(1, min(max_workers, len(todo))) if parallel else 1
        prefix = f"backfill-{self.key}"
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix) as pool:
            # each task gets its own copy of the caller's context (log binding such as job id)
            futures: list[Future[StreamOutcome | None]] = [
                pool.submit(contextvars.copy_context().run, self.run_slice, row) for row in todo
            ]
        errors = [exc for f in futures if (exc := f.exception()) is not None]
        if errors:
            raise _first_error(errors)
        return [f.result() for f in futures]

    def _ids(self, row: SliceRow) -> dict[str, str]:
        return {
            "source": self.runner.connector.name,
            "entity": self.entity,
            "stream": self.key,
            "slice_start": clock.format_utc(row.slice_start),
            "slice_end": clock.format_utc(row.slice_end),
        }

    def run_slice(self, row: SliceRow) -> StreamOutcome | None:
        """Step 4 for one slice; any exception marks the slice failed and propagates."""
        runner, key, entity = self.runner, self.key, self.entity
        if runner.should_stop is not None and runner.should_stop():
            return None
        try:
            out = self._write(row)
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            try:
                mark_slice_failed(key, entity, row.slice_start, error=error, now=runner.clock())
            except Exception as mark_exc:  # noqa: BLE001 - must not mask the slice error
                exc.add_note(f"mark_slice_failed also failed: {type(mark_exc).__name__}")
            _log.warning(
                "connectors.backfill.slice_failed",
                **self._ids(row),
                rows=0,
                error_class=type(exc).__name__,
                attempts=row.attempts + 1,
            )
            raise
        _log.info("connectors.backfill.slice_completed", **self._ids(row), rows=out.rows)
        return out

    def _write(self, row: SliceRow) -> StreamOutcome:
        """Steps 4b-4g: running, guard, connector, deletion set, write loop, done."""
        runner, key, entity = self.runner, self.key, self.entity
        s0, s1 = row.slice_start, row.slice_end
        mark_slice_running(key, entity, s0, now=runner.clock())
        guard(key)
        factory = runner.connector_factory
        conn = runner.connector if factory is None else factory()
        source = runner.connector.name
        deletion = DeletionFilter(source, entity)
        deletion.reload()
        out = runner._write_stream(
            entity,
            self.fetch_of(conn)(entity, s0, s1),
            key=key,
            deletion=deletion,
            ordered=source not in UNORDERED_SOURCES,
            field=runner.connector.watermark_field(entity),
            cap=s1,
            advance_watermark=False,
        )
        root = runner.data_root
        files = [path.relative_to(root).as_posix() for path in out.files]
        mark_slice_done(key, entity, s0, rows=out.rows, files=files, now=runner.clock())
        return out


def run_backfill(  # noqa: PLR0913 - the U01-43 contract; keyword-only after `end`
    runner: SyncRunner,
    entity: str,
    start: datetime,
    end: datetime,
    *,
    key: str,
    fetch_of: FetchOf,
    max_workers: int,
) -> SyncResult:
    """Resumable parallel date-slice backfill of one stream (U01-43, design 01 §5.5).

    `fetch_of` maps a connector instance to its fetch function (`c.sync`, or
    `partial(c.sync_tool, tool)`). When a slice is skipped because `runner.should_stop()`
    returned true, `runner.stopped` is set and the watermark is left unchanged. Slice
    errors are raised after all slices finished: the first `FatalError`, else the first.
    """
    started = runner.clock()
    wm = get_watermark(key, entity)
    before = None if wm is None else clock.format_utc(wm.value)
    step = timedelta(days=runner.cfg.backfill_for(entity).slice_days)
    plan = ensure_slices(key, entity, split_range(start, end, step), now=runner.clock())
    todo = [row for row in plan if row.status != "done"]
    outcomes = _Slices(runner, entity, key, fetch_of).run_all(todo, max_workers)
    total = _total([o for o in outcomes if o is not None])
    if None in outcomes:
        runner.stopped = True
        counts = (total.rows, total.tombstones, total.skipped_deleted, total.files)
        return SyncResult(runner.connector.name, entity, "backfill", *counts, before, before)
    top = end if total.max_committed is None else min(end, total.max_committed)
    field = runner.connector.watermark_field(entity)
    set_watermark(key, entity, field, top, now=runner.clock())
    return runner._finish(entity, key, "backfill", total, (before, started))
