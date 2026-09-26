"""Key reconciliation with a DuckDB anti-join and a safety valve (impl 01 U01-44, U01-45).

Design 01 §5.4, flow F01-03. The source's key listing is streamed to a scratch Parquet file
under `data/tmp/reconcile/`, anti-joined in DuckDB against the lake's latest live keys
(minus the deletion set, TH01-11), and every live lake key missing from the source gets a
tombstone stamped with the detection time. When the missing keys exceed
`reconcile.max_delete_pct` percent of the live keys the run writes nothing and raises
(TH01-13: an empty or partial listing never tombstones an entity). The watermark is never
read for the window and never moves; the scratch file is always deleted.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from herness.connectors._write_loop import metric
from herness.connectors.base import KEY_SCHEMA, SupportsKeyListing
from herness.connectors.deletion import DeletionFilter
from herness.connectors.rows import tombstone_batch
from herness.connectors.runner import SyncResult
from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.core.ids import new_ulid
from herness.core.logging import get_logger
from herness.core.resilience import guard
from herness.store.lake import LAKE_FILE_PATTERN, LakeWriter
from herness.store.ops import get_watermark, record_metric_samples

if TYPE_CHECKING:
    from herness.connectors.runner import SyncRunner

__all__ = ["find_missing_keys", "reconcile_entity"]

_log = get_logger("connectors.reconcile")

# Latest row per record (spec 02 §4.2 staging dedupe rule), live and not under deletion.
_LIVE: Final = (
    "WITH latest AS (SELECT _record_id, _source_key, _deleted "
    "FROM read_parquet($lake, hive_partitioning = true, union_by_name = true) "
    "QUALIFY row_number() OVER (PARTITION BY _record_id "
    "ORDER BY _source_updated_at DESC, _fetched_at DESC) = 1), "
    "live AS (SELECT _source_key FROM latest "
    "WHERE NOT _deleted AND _record_id NOT IN (SELECT record_id FROM deleted_ids)) "
)
# S608: constant text only; the paths are bound parameters.
_COUNT_LIVE: Final = _LIVE + "SELECT count(*) FROM live"  # noqa: S608
_MISSING: Final = (
    _LIVE  # noqa: S608
    + "SELECT l._source_key FROM live l "
    "ANTI JOIN read_parquet($keys) k ON l._source_key = k._source_key ORDER BY 1"
)


def find_missing_keys(
    lake_dir: Path,
    keys_file: Path,
    *,
    deleted: pa.StringArray,
    temp_dir: Path,
    memory_limit: str = "1GB",
) -> tuple[pa.StringArray, int]:
    """Live lake keys missing from `keys_file`, sorted, and the live key count (U01-45).

    No committed lake file under `lake_dir` gives `(empty, 0)`. Paths are bound parameters,
    never formatted into SQL. Any `duckdb.Error` raises `SchemaViolation("reconcile query
    failed")` from the original.
    """
    empty = pa.array([], pa.string())
    if next(lake_dir.glob(f"**/{LAKE_FILE_PATTERN}"), None) is None:
        return empty, 0
    lake = f"{lake_dir.as_posix()}/**/{LAKE_FILE_PATTERN}"
    config: dict[str, str | bool | int | float | list[str]] = {
        "memory_limit": memory_limit,
        "temp_directory": str(temp_dir),
        "threads": 4,
    }
    try:
        con = duckdb.connect(":memory:", config=config)
        try:
            con.register("deleted_ids", pa.table({"record_id": deleted.cast(pa.string())}))
            row = con.execute(_COUNT_LIVE, {"lake": lake}).fetchone()
            live = 0 if row is None else int(row[0])
            params = {"lake": lake, "keys": keys_file.as_posix()}
            column = con.execute(_MISSING, params).to_arrow_table().column(0)
        finally:
            con.close()
    except duckdb.Error as exc:
        msg = "reconcile query failed"
        raise SchemaViolation(msg, entity=lake_dir.name) from exc
    missing = column.combine_chunks() if column.num_chunks else empty
    return missing.cast(pa.string()), live


def _key_batch(batch: pa.RecordBatch, source: str, entity: str) -> pa.RecordBatch:
    """`batch` cast to `KEY_SCHEMA`; another column, type or a null key → SchemaViolation."""
    column = batch.column(0) if batch.schema.names == ["_source_key"] else None
    typed = column is not None and (
        pa.types.is_string(column.type) or pa.types.is_large_string(column.type)
    )
    if column is None or not typed or column.null_count:
        msg = "invalid key batch"
        raise SchemaViolation(msg, source=source, entity=entity)
    return pa.RecordBatch.from_arrays([column.cast(pa.string())], schema=KEY_SCHEMA)


def _write_keys(keys: Iterator[pa.RecordBatch], path: Path, source: str, entity: str) -> int:
    """Step 4: stream the key batches to `path` (zstd Parquet); return the key count."""
    count = 0
    with pq.ParquetWriter(path, KEY_SCHEMA, compression="zstd") as out:
        for batch in keys:
            checked = _key_batch(batch, source, entity)
            out.write_batch(checked)
            count += checked.num_rows
    return count


def _abort(writer: LakeWriter, source: str, entity: str) -> None:
    """Abort the writer; a failing abort is logged, never raised over the original (§6)."""
    try:
        writer.abort()
    except Exception as exc:  # noqa: BLE001 - must not mask the original error (§6)
        name = type(exc).__name__
        _log.error("connectors.lake.abort_failed", source=source, entity=entity, error_class=name)


class _Tombstones:
    """Step 7: the missing keys as tombstones through the deletion filter into one writer."""

    def __init__(self, runner: SyncRunner, entity: str, deletion: DeletionFilter) -> None:
        self.runner, self.entity, self.deletion = runner, entity, deletion
        self.rows = self.skipped = 0
        self.files: tuple[Path, ...] = ()

    def write(self, missing: pa.StringArray, deleted_at: datetime | None) -> None:
        """Write `missing` in `batch_rows` chunks and commit; abort on any error."""
        if len(missing) == 0:
            return
        runner, entity = self.runner, self.entity
        source, size = runner.connector.name, runner.cfg.batch_rows
        when = deleted_at or runner.clock()
        writer = runner.writer_factory(source, entity)
        try:
            for start in range(0, len(missing), size):
                chunk = missing.slice(start, size)
                rows = tombstone_batch(
                    source, entity, chunk, deleted_at=when, fetched_at=runner.clock()
                )
                kept, dropped = self.deletion.apply(rows)
                self.skipped += dropped
                if kept.num_rows:
                    writer.write(kept)
                    self.rows += kept.num_rows
            self.files = writer.commit().files
        except BaseException:
            _abort(writer, source, entity)
            raise


def _valve(runner: SyncRunner, entity: str, missing: int, live: int) -> None:
    """Step 6: raise `SchemaViolation` when `missing` exceeds `max_delete_pct` % of `live`."""
    pct = runner.cfg.reconcile.max_delete_pct
    if live == 0 or missing * 100 <= pct * live:
        return
    source = runner.connector.name
    ids = {"source": source, "entity": entity}
    _log.error(
        "connectors.reconcile.aborted",
        **ids,
        live_keys=live,
        missing_keys=missing,
        max_delete_pct=pct,
    )
    record_metric_samples([metric("counter", "reconcile_aborted_total", 1, ids, runner.clock())])
    msg = "reconcile safety valve"
    raise SchemaViolation(msg, source=source, entity=entity)


def reconcile_entity(
    runner: SyncRunner,
    entity: str,
    *,
    keys: Iterator[pa.RecordBatch] | None = None,
    deleted_at: datetime | None = None,
) -> SyncResult:
    """Key reconciliation of one entity with the safety valve (U01-44, design 01 §5.4).

    `keys` defaults to `connector.list_keys(entity)` (the files snapshot passes the file's
    keys); `deleted_at` defaults to the detection time. Raises `CircuitOpen` (not for
    files), the connector's §6 errors and `SchemaViolation` (valve, bad key batch, query).
    The watermark never changes; no tombstone is written for a record under deletion.
    """
    source = runner.connector.name
    if source != "files":
        guard(source)
    deletion = DeletionFilter(source, entity)
    deletion.reload()
    scratch = runner.data_root / "tmp" / "reconcile"
    scratch.mkdir(parents=True, exist_ok=True)
    key_path = scratch / f"{source}-{entity}-{new_ulid()}.parquet"
    tombstones = _Tombstones(runner, entity, deletion)
    try:
        if keys is None:
            keys = cast("SupportsKeyListing", runner.connector).list_keys(entity)
        source_keys = _write_keys(keys, key_path, source, entity)
        lake_dir = runner.data_root / "raw" / source / entity
        missing, live = find_missing_keys(
            lake_dir, key_path, deleted=deletion.ids, temp_dir=scratch
        )
        _valve(runner, entity, len(missing), live)
        tombstones.write(missing, deleted_at)
    finally:
        key_path.unlink(missing_ok=True)
    n = tombstones.rows
    ids = {"source": source, "entity": entity}
    _log.info(
        "connectors.reconcile.completed",
        **ids,
        live_keys=live,
        source_keys=source_keys,
        tombstones=n,
    )
    wm = get_watermark(source, entity)
    text = None if wm is None else clock.format_utc(wm.value)
    return SyncResult(
        source=source,
        entity=entity,
        mode="reconcile",
        rows=n,
        tombstones=n,
        skipped_deleted=tombstones.skipped,
        files=tombstones.files,
        watermark_before=text,
        watermark_after=text,
    )
