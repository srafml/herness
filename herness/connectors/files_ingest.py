"""Runner path for inbox files: fingerprint dedupe, write, commit, record, reconcile.

Impl 01 U01-50 (design 01 §5.10, flow F01-04). Each eligible inbox file of the files
connector is fingerprinted (SHA-256); a known fingerprint is skipped even when the file was
renamed. A new file goes through `SyncRunner._write_stream` (deletion filter before every
write, TH01-11) and is recorded in `file_ingest` only after its writer committed, so a crash
between the two re-ingests the file on the next run (TH01-12). A file that changed while it
was fingerprinted or read is skipped and retried next run (TH01-10). A snapshot-mode entity
reconciles its keys against the lake right after each file (design 01 §5.4); the keys are
held in memory up to `MAX_SNAPSHOT_KEYS`. Files never use the `watermark` table.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import pyarrow as pa
import pyarrow.compute as pc

from herness.connectors._write_loop import StreamOutcome, metric
from herness.connectors.base import KEY_SCHEMA
from herness.connectors.deletion import DeletionFilter
from herness.connectors.files import FilesConnector, InboxFile, InboxFileChanged, fingerprint_file
from herness.connectors.reconcile import reconcile_entity
from herness.connectors.runner import SyncResult
from herness.connectors.settings_entities import FilesEntity
from herness.core.errors import SchemaViolation
from herness.core.logging import get_logger
from herness.core.resilience import fault_point
from herness.store.ops import (
    FileIngestRow,
    get_file_ingest,
    record_file_ingest,
    record_metric_samples,
)

if TYPE_CHECKING:
    from herness.connectors.runner import SyncRunner

__all__ = ["MAX_SNAPSHOT_KEYS", "ingest_files"]

MAX_SNAPSHOT_KEYS: Final = 5_000_000  # ≈ 250 MB of keys held for one snapshot reconcile

_SOURCE: Final = "files"
_FP_LOG_HEX: Final = 12  # fingerprint prefix written to logs (§8.1)

_log = get_logger("connectors.files")


class _SnapshotTooLarge(SchemaViolation):
    """A snapshot file holds more than `MAX_SNAPSHOT_KEYS` keys (U01-50 limits)."""

    def __init__(self, entity: str) -> None:
        super().__init__("snapshot too large", source=_SOURCE, entity=entity)


@dataclass(slots=True)
class _Totals:
    """Running sums of one `ingest_files` call."""

    rows: int = 0
    tombstones: int = 0
    skipped: int = 0
    files: list[Path] = field(default_factory=list)

    def add(self, rows: int, tombstones: int, skipped: int, files: tuple[Path, ...]) -> None:
        self.rows += rows
        self.tombstones += tombstones
        self.skipped += skipped
        self.files.extend(files)


class _KeyCollector:
    """Step 2c: in snapshot mode, keep each batch's `_source_key` column, bounded."""

    def __init__(self, entity: str, *, snapshot: bool) -> None:
        self.entity, self.snapshot = entity, snapshot
        self.keys: list[pa.Array] = []
        self.count = 0

    def wrap(self, batches: Iterator[pa.RecordBatch]) -> Iterator[pa.RecordBatch]:
        """Yield `batches` unchanged; the key cap is checked before a batch is written."""
        for batch in batches:
            if self.snapshot and batch.num_rows:
                self.count += batch.num_rows
                if self.count > MAX_SNAPSHOT_KEYS:
                    raise _SnapshotTooLarge(self.entity)
                self.keys.append(batch.column("_source_key"))
            yield batch

    def batches(self, size: int) -> Iterator[pa.RecordBatch]:
        """The distinct keys as `KEY_SCHEMA` batches of at most `size` rows."""
        if not self.keys:
            return
        unique = pc.unique(pa.chunked_array(self.keys, pa.string())).cast(pa.string())
        for start in range(0, len(unique), size):
            yield pa.RecordBatch.from_arrays([unique.slice(start, size)], schema=KEY_SCHEMA)


def _reject(entity: str, f: InboxFile, reason: str) -> None:
    _log.warning("connectors.files.rejected", entity=entity, file=f.rel_path, reason=reason)


def _fingerprint(entity: str, f: InboxFile) -> str | None:
    """Step 2a: the file's SHA-256, or None (logged) when it changed while hashed."""
    try:
        return fingerprint_file(f)
    except InboxFileChanged:
        _reject(entity, f, "changed")
        return None


def _write(
    runner: SyncRunner, entity: str, f: InboxFile, deletion: DeletionFilter, keys: _KeyCollector
) -> StreamOutcome | None:
    """Steps 2c-2d: stream the file into one writer; None (logged) when it changed.

    Any error aborts the writer inside `_write_stream`; the file is not recorded.
    """
    conn = cast("FilesConnector", runner.connector)
    try:
        return runner._write_stream(
            entity,
            keys.wrap(conn.read_file(entity, f)),
            key=_SOURCE,
            deletion=deletion,
            ordered=False,
            field=conn.watermark_field(entity),
            cap=f.mtime,
            advance_watermark=False,
        )
    except InboxFileChanged:
        _reject(entity, f, "changed")
        return None
    except _SnapshotTooLarge:
        _reject(entity, f, "too_large")
        raise
    except SchemaViolation:
        _reject(entity, f, "unreadable")
        raise


def _record(runner: SyncRunner, entity: str, f: InboxFile, fp: str, out: StreamOutcome) -> None:
    """Step 2f: the `file_ingest` row (after the commit), the INFO log and the metric."""
    root = runner.data_root
    files = tuple(path.relative_to(root).as_posix() for path in out.files)
    now = runner.clock()
    row = FileIngestRow(
        fp, _SOURCE, entity, f.rel_path, f.size_bytes, f.mtime, out.rows, files, now
    )
    record_file_ingest(row)
    short = fp[:_FP_LOG_HEX]
    _log.info(
        "connectors.files.ingested",
        entity=entity,
        fingerprint=short,
        rows=out.rows,
        files=len(files),
    )
    sample = metric("counter", "files_ingested_total", 1, {"entity": entity}, now)
    record_metric_samples([sample])


def _ingest_one(
    runner: SyncRunner, entity: str, f: InboxFile, deletion: DeletionFilter, totals: _Totals
) -> None:
    """Step 2 for one candidate: dedupe, write, commit, record, snapshot reconcile."""
    fp = _fingerprint(entity, f)
    if fp is None:
        return
    if get_file_ingest(fp) is not None:
        _log.info("connectors.files.skipped_known", entity=entity, fingerprint=fp[:_FP_LOG_HEX])
        return
    cfg = runner.cfg.entity(entity)
    snapshot = isinstance(cfg, FilesEntity) and cfg.mode == "snapshot"
    keys = _KeyCollector(entity, snapshot=snapshot)
    out = _write(runner, entity, f, deletion, keys)
    if out is None:
        return
    fault_point("connector.before_watermark", source=_SOURCE)
    _record(runner, entity, f, fp, out)
    totals.add(out.rows, out.tombstones, out.skipped_deleted, out.files)
    if snapshot:
        batches = keys.batches(runner.cfg.batch_rows)
        done = reconcile_entity(runner, entity, keys=batches, deleted_at=f.mtime)
        totals.add(done.rows, done.tombstones, done.skipped_deleted, done.files)


def ingest_files(runner: SyncRunner, entity: str) -> SyncResult:
    """Ingest the new inbox files of `entity` (U01-50, design 01 §5.10).

    Each new fingerprint is written to the lake and then recorded in `file_ingest`; known
    fingerprints are skipped; a file that changed while read is skipped (retried next run).
    Returns mode `incremental` with both watermarks `None`. Raises `SchemaViolation` from
    reading (file not recorded) or from the reconcile valve (file already recorded),
    `SourceUnavailable` and `StoreBusy`.
    """
    conn = cast("FilesConnector", runner.connector)
    deletion = DeletionFilter(_SOURCE, entity)
    deletion.reload()
    totals = _Totals()
    for f in conn.candidates(entity):
        _ingest_one(runner, entity, f, deletion, totals)
    counts = (totals.rows, totals.tombstones, totals.skipped, tuple(totals.files))
    return SyncResult(_SOURCE, entity, "incremental", *counts, None, None)
