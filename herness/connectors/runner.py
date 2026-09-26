"""Sync runner: `SyncResult`, `SyncRunner` and the incremental flow (impl 01 U01-36 to U01-40).

Design 01 §3.2, §5.1, §5.2 and §5.6. One `SyncRunner` serves one source in one job thread.
Every stream goes through `_write_stream` (loop state in the private `_write_loop` module),
which filters deleted records before each write (TH01-11), splits lake files on schema
drift, checkpoints by rows or writer age, and moves the watermark only after
`LakeWriter.commit()` returned (design 01 §2). Backfill slices run in the `backfill`
module (U01-43); reconciliation (T01-08) and the files path (T01-10) extend this module.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Literal, Protocol, cast

import pyarrow as pa

from herness.connectors._write_loop import StreamOutcome, StreamSpec, WriteLoop
from herness.connectors._write_loop import metric as _metric
from herness.connectors.base import UNORDERED_SOURCES, Connector, SupportsToolStreams
from herness.connectors.deletion import DeletionFilter
from herness.connectors.lakefiles import cleanup_orphan_temp_files
from herness.connectors.settings import MonitoringSettings
from herness.connectors.settings_base import SourceSettings
from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import CircuitOpen, ConfigError, FatalError, HernessError, RetryableError
from herness.core.logging import get_logger
from herness.core.resilience import guard
from herness.store.lake import LakeWriter
from herness.store.ops import get_watermark, record_metric_samples

__all__ = ["SyncResult", "SyncRunner"]

type Mode = Literal["incremental", "backfill", "reconcile"]
type Fetch = Callable[[str, datetime | None, datetime], Iterator[pa.RecordBatch]]
type FetchOf = Callable[[Connector], Fetch]  # connector instance -> its fetch function

_log = get_logger("connectors.sync")


class _StreamRun(Protocol):
    """One stream's flow: `_incremental_stream`, or `_backfill_stream` bound to a range."""

    def __call__(self, entity: str, *, key: str, fetch_of: FetchOf, workers: int) -> SyncResult:
        """Run stream `key` of `entity`; `workers` bounds the backfill slice threads."""
        ...


def _utc(value: datetime | None) -> str | None:
    return None if value is None else clock.format_utc(value)


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Outcome of one runner call (U01-36); immutable, counts and paths only.

    Invariants: `rows >= tombstones >= 0`; `skipped_deleted >= 0`; `watermark_after >=
    watermark_before` when both are set (fixed-width UTC text, so text order is time order).
    """

    source: str
    entity: str
    mode: Mode
    rows: int
    tombstones: int
    skipped_deleted: int
    files: tuple[Path, ...]
    watermark_before: str | None
    watermark_after: str | None

    def to_dict(self, *, data_root: Path | None = None) -> dict[str, object]:
        """JSON-safe form for `JobOutcome.result`; files as POSIX paths relative to
        `data_root` (default `paths.data`)."""
        root = get_config().paths.data if data_root is None else data_root
        data = dataclasses.asdict(self)
        data["files"] = [path.relative_to(root).as_posix() for path in self.files]
        return data


def _low(values: list[str | None]) -> str | None:
    """Minimum watermark text; `None` when any value is `None` or there are none."""
    present = [v for v in values if v is not None]
    return None if not present or len(present) < len(values) else min(present)


def _aggregate(source: str, entity: str, results: list[SyncResult]) -> SyncResult:
    """Tool results summed; files concatenated; watermarks by the U01-36 minimum rule."""
    mode: Mode = "backfill" if any(r.mode == "backfill" for r in results) else "incremental"
    counts = (sum(r.rows for r in results), sum(r.tombstones for r in results))
    skipped = sum(r.skipped_deleted for r in results)
    files = tuple(path for r in results for path in r.files)
    before = _low([r.watermark_before for r in results])
    after = _low([r.watermark_after for r in results])
    return SyncResult(source, entity, mode, *counts, skipped, files, before, after)


def _first_error(
    errors: list[HernessError], skipped: list[CircuitOpen], *, succeeded: int
) -> HernessError | None:
    """U01-38 step 3: first fatal, else first retryable, else first other kept error, else
    the first `CircuitOpen` when every tool was skipped."""
    fatal = [e for e in errors if isinstance(e, FatalError)]
    retryable = [e for e in errors if isinstance(e, RetryableError)]
    for group in (fatal, retryable, errors):
        if group:
            return group[0]
    return skipped[0] if skipped and not succeeded else None


def _default_writer(source: str, entity: str) -> LakeWriter:
    return LakeWriter(source, entity)


def _own_sync(conn: Connector) -> Fetch:
    return conn.sync


def _tool_sync(tool: str, conn: Connector) -> Fetch:
    return partial(cast("SupportsToolStreams", conn).sync_tool, tool)


class SyncRunner:
    """Runs incremental, backfill and reconcile flows for one source (U01-37).

    At most one open `LakeWriter` per stream; a writer is always committed or aborted before
    a method returns or raises; watermarks move only after `commit()` returned. After a run,
    `skipped_open` holds the stream keys skipped for an open breaker and `stopped` is true
    when a backfill left slices pending because `should_stop()` returned true.
    """

    def __init__(  # noqa: PLR0913 - the U01-37 contract; all but two are keyword-only
        self,
        connector: Connector,
        cfg: SourceSettings,
        *,
        clock: Callable[[], datetime] = clock.now,
        writer_factory: Callable[[str, str], LakeWriter] = _default_writer,
        connector_factory: Callable[[], Connector] | None = None,
        data_root: Path | None = None,
        progress: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        if connector.name != cfg.SOURCE:
            msg = "connector name does not match its settings section"
            raise ConfigError(msg, source=cfg.SOURCE)
        self.connector, self.cfg, self.clock = connector, cfg, clock
        self.writer_factory, self.connector_factory = writer_factory, connector_factory
        self.progress, self.should_stop = progress, should_stop
        self._data_root = data_root
        self.skipped_open: tuple[str, ...] = ()
        self.stopped = False
        self._cleaned = False

    @property
    def data_root(self) -> Path:
        """The `data_root` argument, else `paths.data` of the loaded config."""
        return get_config().paths.data if self._data_root is None else self._data_root

    def _prepare(self, entity: str) -> None:
        """Validate `entity` (ConfigError), reset the run attributes, clean orphans once."""
        self.cfg.entity(entity)
        self.skipped_open, self.stopped = (), False
        if not self._cleaned:
            raw = self.data_root / "raw"
            cleanup_orphan_temp_files(raw, self.connector.name, now=self.clock())
            self._cleaned = True

    def run_incremental(self, entity: str) -> SyncResult:
        """Job kind `sync` for one entity (U01-38, design 01 §5.1).

        Mode `incremental`, or `backfill` when a stream had no watermark. Raises
        `CircuitOpen` and every §6 error, after the writer is aborted.
        """
        self._prepare(entity)
        # T01-10: connector.name == "files" -> return ingest_files(self, entity) (U01-50)
        conn = self.connector
        if isinstance(conn, SupportsToolStreams):
            return self._over_tools(entity, conn, "incremental", self._incremental_stream)
        workers = self.cfg.max_concurrency
        return self._incremental_stream(entity, key=conn.name, fetch_of=_own_sync, workers=workers)

    def run_backfill(self, entity: str, start: datetime, end: datetime) -> SyncResult:
        """Job kind `sync` with `--backfill` over `[start, end)` (U01-41, design 01 §5.5).

        Needs aware `start < end <= now` and a connector other than `files`, else
        `ConfigError`. Raises as U01-43; tool streams as U01-38 step 3.
        """
        conn = self.connector
        if conn.name == "files":
            msg = "files does not backfill"
            raise ConfigError(msg, source=conn.name)
        if any(v.tzinfo is None or v.utcoffset() is None for v in (start, end)):
            msg = "backfill range needs timezone-aware datetimes"
            raise ConfigError(msg, source=conn.name)
        if not start < end <= self.clock():
            msg = "backfill range must satisfy start < end <= now"
            raise ConfigError(msg, source=conn.name)
        self._prepare(entity)
        run = partial(self._backfill_stream, start=start, end=end)
        if isinstance(conn, SupportsToolStreams):
            return self._over_tools(entity, conn, "backfill", run)
        return run(entity, key=conn.name, fetch_of=_own_sync, workers=self.cfg.max_concurrency)

    def _tool_workers(self, tool: str) -> int:
        """The adapter's `max_concurrency` of a monitoring tool, else the source's."""
        cfg = self.cfg
        adapters = cfg.adapters.items() if isinstance(cfg, MonitoringSettings) else ()
        return next((a.max_concurrency for t, a in adapters if t == tool), cfg.max_concurrency)

    def _over_tools(
        self, entity: str, conn: SupportsToolStreams, mode: Mode, run: _StreamRun
    ) -> SyncResult:
        """Call `run` per tool with its stream key; one tool's failure never stops the
        others; errors are raised after all tools (U01-38 step 3)."""
        results: list[SyncResult] = []
        errors: list[HernessError] = []
        skipped: list[CircuitOpen] = []
        source = self.connector.name
        for tool in conn.tools():
            key = conn.stream_key(tool)
            try:
                fetch_of, workers = partial(_tool_sync, tool), self._tool_workers(tool)
                results.append(run(entity, key=key, fetch_of=fetch_of, workers=workers))
            except CircuitOpen as exc:
                skipped.append(exc)
                self.skipped_open = (*self.skipped_open, key)
                retry_at = clock.format_utc(exc.retry_at)
                _log.warning(
                    "connectors.sync.skipped_open_circuit",
                    source=source,
                    stream=key,
                    retry_at=retry_at,
                )
            except HernessError as exc:
                errors.append(exc)
                ids = {"source": source, "entity": entity, "stream": key, "mode": mode}
                _log.error("connectors.sync.failed", **ids, error_class=type(exc).__name__)
        first = _first_error(errors, skipped, succeeded=len(results))
        if first is not None:
            raise first
        return _aggregate(source, entity, results)

    def _incremental_stream(
        self, entity: str, *, key: str, fetch_of: FetchOf, workers: int
    ) -> SyncResult:
        """Incremental flow for one stream (U01-39); the watermark never moves back."""
        guard(key)
        wm = get_watermark(key, entity)
        now = self.clock()
        if wm is None:
            start = self.cfg.backfill_for(entity).resolve_start(now)
            run = partial(self._backfill_stream, start=start, end=now)
            return run(entity, key=key, fetch_of=fetch_of, workers=workers)
        since = wm.value - self.cfg.overlap_for(entity)
        until = now - timedelta(seconds=self.cfg.settle_seconds)
        before = clock.format_utc(wm.value)
        if since >= until:
            name = self.connector.name
            return SyncResult(name, entity, "incremental", 0, 0, 0, (), before, before)
        out = self._stream(entity, key, fetch_of(self.connector), (since, until), "incremental")
        return self._finish(entity, key, "incremental", out, (before, now))

    def _backfill_stream(
        self,
        entity: str,
        *,
        key: str,
        fetch_of: FetchOf,
        workers: int,
        start: datetime,
        end: datetime,
    ) -> SyncResult:
        """Backfill of one stream over `[start, end)` in resumable parallel slices (U01-43)."""
        from herness.connectors.backfill import run_backfill  # noqa: PLC0415 - it imports us

        return run_backfill(
            self, entity, start, end, key=key, fetch_of=fetch_of, max_workers=workers
        )

    def _stream(
        self, entity: str, key: str, fetch: Fetch, window: tuple[datetime, datetime], mode: Mode
    ) -> StreamOutcome:
        """U01-39 steps 6-8: log the start, load the deletion set, run the write loop; only
        the incremental mode advances the watermark per commit."""
        since, until = window
        ids = {"source": self.connector.name, "entity": entity, "stream": key, "mode": mode}
        span = {"since": clock.format_utc(since), "until": clock.format_utc(until)}
        _log.info("connectors.sync.started", **ids, **span)
        deletion = DeletionFilter(self.connector.name, entity)
        deletion.reload()
        return self._write_stream(
            entity,
            fetch(entity, since, until),
            key=key,
            deletion=deletion,
            ordered=self.connector.name not in UNORDERED_SOURCES,
            field=self.connector.watermark_field(entity),
            cap=until,
            advance_watermark=mode == "incremental",
        )

    def _finish(
        self,
        entity: str,
        key: str,
        mode: Mode,
        out: StreamOutcome,
        begin: tuple[str | None, datetime],
    ) -> SyncResult:
        """U01-39 step 9: re-read the watermark, log `connectors.sync.completed`, write the
        §8.2 metrics (`source` label = stream key). `begin` is (watermark before, start time)."""
        before, started = begin
        wm = get_watermark(key, entity)
        now = self.clock()
        duration = (now - started).total_seconds()
        counts = (out.rows, out.tombstones, out.skipped_deleted, out.files)
        after = _utc(None if wm is None else wm.value)
        result = SyncResult(self.connector.name, entity, mode, *counts, before, after)
        # §8.1 gives this event no `stream` field, so `source` names the stream key.
        extra = {"source": key, "files": len(out.files), "duration_s": duration}
        _log.info("connectors.sync.completed", **(dataclasses.asdict(result) | extra))
        stream = {"source": key, "entity": entity}
        labels = stream | {"mode": mode}
        samples = [
            _metric("counter", "records_total", out.rows, labels, now),
            _metric("counter", "tombstones_total", out.tombstones, labels, now),
            _metric("counter", "skipped_deleted_total", out.skipped_deleted, stream, now),
            _metric("histogram", "sync_duration_seconds", duration, labels, now),
        ]
        if wm is not None:
            lag = (now - wm.value).total_seconds()
            samples.append(_metric("histogram", "watermark_lag_seconds", lag, stream, now))
        record_metric_samples(samples)
        return result

    def _write_stream(  # noqa: PLR0913 - the U01-40 contract, keyword-only after `batches`
        self,
        entity: str,
        batches: Iterator[pa.RecordBatch],
        *,
        key: str,
        deletion: DeletionFilter,
        ordered: bool,
        field: str,
        cap: datetime,
        advance_watermark: bool,
    ) -> StreamOutcome:
        """Filter, drift-split, write, checkpoint and advance the watermark (U01-40).

        Ordered streams set `min(max committed _source_updated_at, cap)` after each commit;
        unordered streams set `min(max over all commits, cap)` once at the end. Any exception
        (also `KeyboardInterrupt`) aborts the open writer and propagates.
        """
        spec = StreamSpec(key, ordered, field, cap, advance_watermark)
        return WriteLoop(self, entity, deletion, spec).run(batches)
