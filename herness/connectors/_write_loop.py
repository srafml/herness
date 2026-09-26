"""Shared write loop of the sync runner (impl 01 U01-40; design 01 §3.2, §5.1, §5.2, §5.6).

Private sibling of `herness.connectors.runner`, split out so runner.py keeps its 390-line
budget with room for T01-07, T01-08 and T01-10; `SyncRunner._write_stream` is the only
caller. Deleted records are filtered before every write and the set is reloaded at each
checkpoint (TH01-11); each lake file has one schema; the watermark moves only after
`LakeWriter.commit()` returned (design 01 §2).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import pyarrow as pa
import pyarrow.compute as pc

from herness.connectors.deletion import DeletionFilter
from herness.connectors.lakefiles import SchemaDrift, SchemaTracker
from herness.core import time as clock
from herness.core.logging import get_logger
from herness.core.resilience import fault_point
from herness.core.types import MetricSample
from herness.store.lake import LakeFileSet
from herness.store.ops import record_metric_samples, set_watermark

if TYPE_CHECKING:
    from herness.connectors.runner import SyncRunner

LAKE_MAX_OPEN_S: Final = timedelta(seconds=600)  # T02-02 LakeWriter default `max_open_s`

_log = get_logger("connectors.sync")


def metric(
    kind: Literal["counter", "histogram"],
    name: str,
    value: float,
    labels: dict[str, str],
    ts: datetime,
) -> MetricSample:
    """One §8.2 sample of component `connectors`; `name` lacks the `herness_connectors_` prefix."""
    return MetricSample(
        ts=ts,
        name=f"herness_connectors_{name}",
        kind=kind,
        value=max(0.0, float(value)),
        labels=labels,
        component="connectors",
    )


@dataclass(frozen=True, slots=True)
class StreamOutcome:
    """What one `_write_stream` call committed (U01-40)."""

    rows: int
    tombstones: int
    skipped_deleted: int
    files: tuple[Path, ...]
    max_committed: datetime | None


@dataclass(frozen=True, slots=True)
class StreamSpec:
    """The U01-40 keyword parameters: stream key, order, watermark field, cap, advance."""

    key: str
    ordered: bool
    field: str
    cap: datetime
    advance: bool


class WriteLoop:
    """State of one `_write_stream` call: the open writer, counters, checkpoint window."""

    def __init__(
        self, runner: SyncRunner, entity: str, deletion: DeletionFilter, spec: StreamSpec
    ) -> None:
        self.runner, self.entity, self.deletion, self.spec = runner, entity, deletion, spec
        self.source = runner.connector.name
        self.at = {"source": spec.key, "entity": entity}
        self.writer = runner.writer_factory(self.source, entity)
        self.tracker = SchemaTracker()
        self.rows = self.tombstones = self.skipped = self.cp_rows = 0
        self.files: list[Path] = []
        self.max_committed: datetime | None = None
        self.cp_start = runner.clock()

    def run(self, batches: Iterator[pa.RecordBatch]) -> StreamOutcome:
        """Steps 2-5: any exception (also `KeyboardInterrupt`) aborts the writer and propagates."""
        try:
            for batch in batches:
                self.feed(batch)
            self.finish()
        except BaseException:
            self.abort()
            raise
        files = tuple(self.files)
        return StreamOutcome(self.rows, self.tombstones, self.skipped, files, self.max_committed)

    def feed(self, batch: pa.RecordBatch) -> None:
        """Step 2 for one batch: filter, drift split, write, checkpoint by rows or age."""
        if batch.num_rows == 0:
            return
        batch, dropped = self.deletion.apply(batch)
        self.skipped += dropped
        if batch.num_rows == 0:
            return
        drift = self.tracker.observe(batch.schema)
        if drift is not None:
            self._drift(drift)
            if self.cp_rows > 0:
                self.checkpoint()
        self.writer.write(batch)
        self.cp_rows += batch.num_rows
        self.rows += batch.num_rows
        self.tombstones += int(pc.sum(batch["_deleted"]).as_py() or 0)
        aged = self.runner.clock() - self.cp_start >= LAKE_MAX_OPEN_S
        if self.cp_rows >= self.runner.cfg.checkpoint_rows or aged:
            self.checkpoint()

    def _drift(self, drift: SchemaDrift) -> None:
        _log.warning("connectors.schema_drift.detected", **self.at, **dataclasses.asdict(drift))
        sample = metric("counter", "schema_drift_total", 1, self.at, self.runner.clock())
        record_metric_samples([sample])

    def _set_watermark(self, top: datetime) -> datetime:
        value = min(top, self.spec.cap)
        spec = self.spec
        set_watermark(spec.key, self.entity, spec.field, value, now=self.runner.clock())
        return value

    def _commit(self) -> tuple[LakeFileSet, datetime | None]:
        """Commit, fault point, then (ordered streams) move the watermark; returns its value."""
        fs = self.writer.commit()
        self.files.extend(fs.files)
        top = fs.max_source_updated_at
        if top is not None:
            self.max_committed = top if self.max_committed is None else max(self.max_committed, top)
        fault_point("connector.before_watermark", source=self.spec.key)
        if self.spec.advance and self.spec.ordered and top is not None:
            return fs, self._set_watermark(top)
        return fs, None

    def checkpoint(self) -> None:
        """Step 3: commit, watermark, reload deletions, progress note, log, new writer."""
        fs, value = self._commit()
        self.deletion.reload()
        key, entity = self.spec.key, self.entity
        if self.runner.progress is not None:
            self.runner.progress(f"{key}/{entity} rows={self.rows}")
        mark = None if value is None else clock.format_utc(value)
        ids = {"source": self.source, "entity": entity, "stream": key}
        count = len(fs.files)
        _log.info(
            "connectors.sync.checkpoint_committed",
            **ids,
            rows=self.rows,
            files=count,
            watermark=mark,
        )
        self.writer = self.runner.writer_factory(self.source, entity)
        self.cp_rows, self.cp_start = 0, self.runner.clock()

    def finish(self) -> None:
        """Step 4: final commit (also when empty); unordered streams set the watermark once."""
        self._commit()
        if self.spec.advance and not self.spec.ordered and self.max_committed is not None:
            self._set_watermark(self.max_committed)

    def abort(self) -> None:
        """Step 5: abort the writer; a failing abort is logged, never raised over the original."""
        try:
            self.writer.abort()
        except Exception as exc:  # noqa: BLE001 - must not mask the original error (§6)
            name = type(exc).__name__
            _log.error("connectors.lake.abort_failed", **self.at, error_class=name)
