"""Decision cache maintenance: migrate, compact and purge (U03-39 ... U03-41; design 03 §4.3).

Every part written here goes through ``cache.write_part`` (cast to ``CACHE_SCHEMA``, tmp +
fsync + ``os.replace``), so readers keep seeing only complete ``part-*.parquet`` files.
OS errors map as in U03-38: ``StoreBusy`` for ``EACCES``/``EBUSY``, else ``FatalError``.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final
from urllib.parse import unquote

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.logging import get_logger
from herness.core.types import QuestionSet
from herness.enrich.cache import (
    CACHE_SCHEMA,
    _fingerprint,
    io_error,
    replace_atomic,
    write_part,
)
from herness.enrich.layout import EnrichPaths

_PARTITION_GLOB: Final = "decider=*/decider_version=*"
_PART_GLOB: Final = "part-*.parquet"
_KEY: Final = ("content_hash", "question", "question_fingerprint")
_BATCH_ROWS: Final = 1_000_000
_HASH_RE: Final = re.compile(r"^[0-9a-f]{32}$")
_SEP: Final = "\x00"  # never part of a question id or fingerprint

_log = get_logger("enrich.cache")

type BatchFilter = Callable[[pa.RecordBatch], pa.Array]


@contextlib.contextmanager
def _os_errors(what: str, decider: str | None = None) -> Iterator[None]:
    try:
        yield
    except OSError as exc:
        raise io_error(exc, f"cannot maintain cache {what}", decider=decider) from exc


def _partitions(version_dir: Path) -> list[Path]:
    with _os_errors(version_dir.name):
        if not version_dir.is_dir():
            return []
        return sorted(p for p in version_dir.glob(_PARTITION_GLOB) if p.is_dir())


def _parts(partition: Path) -> list[Path]:
    with _os_errors(partition.name):
        return sorted(partition.glob(_PART_GLOB))


def _decider(partition: Path) -> str:
    return partition.parent.name.removeprefix("decider=")


def _read(
    part: Path, qsv: str, *, columns: list[str] | None = None, keep: BatchFilter | None = None
) -> pa.Table:
    """Read ``part`` in batches of 1M rows, keeping rows where ``keep`` is true.

    Raises SchemaViolation when the part's schema is not exactly ``CACHE_SCHEMA`` (TH03-18).
    """
    names = columns or CACHE_SCHEMA.names
    schema = pa.schema([CACHE_SCHEMA.field(name) for name in names])
    with _os_errors(part.name, _decider(part.parent)), pq.ParquetFile(part) as handle:
        if not handle.schema_arrow.equals(CACHE_SCHEMA, check_metadata=False):
            msg = f"cache part schema mismatch: {part.name}"
            raise SchemaViolation(msg, question_set_version=qsv)
        batches = [
            batch.filter(keep(batch)) if keep is not None else batch
            for batch in handle.iter_batches(batch_size=_BATCH_ROWS, columns=names)
        ]
    return pa.Table.from_batches(batches, schema=schema)


def _dedupe(table: pa.Table) -> pa.Table:
    """One row per (content_hash, question, question_fingerprint): the latest ``decided_at``."""
    ordered = table.sort_by([("decided_at", "descending")])
    seen: set[tuple[object, ...]] = set()
    keep: list[int] = []
    columns = [ordered.column(name).to_pylist() for name in _KEY]
    for index, key in enumerate(zip(*columns, strict=True)):
        if key not in seen:
            seen.add(key)
            keep.append(index)
    return ordered.take(pa.array(keep, type=pa.int64()))


def _read_marker(marker: Path) -> int:
    with _os_errors(marker.name):
        text = marker.read_text(encoding="utf-8")
    try:
        rows = json.loads(text).get("rows")
    except (ValueError, AttributeError):
        rows = None
    if type(rows) is not int or rows < 0:
        msg = f"{marker.name} is unreadable: no row count"
        raise ConfigError(msg)
    return rows


def migrate(paths: EnrichPaths, old_qsv: str, new_qs: QuestionSet) -> int:
    """Copy unchanged questions' rows from ``old_qsv`` to ``new_qs.version`` (U03-39).

    Returns the rows copied; idempotent through ``_migrated_from_<old_qsv>.json``, written last.
    Raises ConfigError when both versions are equal or the marker is unreadable,
    SchemaViolation for a foreign part, StoreBusy or FatalError on OS errors.
    """
    if old_qsv == new_qs.version:
        msg = "migrate needs a new question set version"
        raise ConfigError(msg, question_set_version=old_qsv)
    old_dir = paths.cache_dir(old_qsv)
    marker = paths.cache_dir(new_qs.version) / f"_migrated_from_{old_qsv}.json"
    with _os_errors(marker.name):
        done = marker.is_file()
    if done:
        return _read_marker(marker)
    wanted = pa.array(sorted(f"{q.id}{_SEP}{_fingerprint(q)}" for q in new_qs.questions))

    def _unchanged(batch: pa.RecordBatch) -> pa.Array:
        pair = pc.binary_join_element_wise(
            batch.column("question"), batch.column("question_fingerprint"), _SEP
        )
        return pc.is_in(pair, value_set=wanted)

    total = 0
    for partition in _partitions(old_dir):
        decider = _decider(partition)
        version = unquote(partition.name.removeprefix("decider_version="))
        target = paths.cache_partition(new_qs.version, decider, version)
        parts = [_read(part, old_qsv, keep=_unchanged) for part in _parts(partition)]
        rows = _dedupe(pa.concat_tables(parts)) if parts else CACHE_SCHEMA.empty_table()
        if rows.num_rows:
            write_part(target, rows, decider=decider)
            total += rows.num_rows
    record = json.dumps({"rows": total, "finished_at": clock.format_utc(clock.now())})
    replace_atomic(marker, lambda tmp: tmp.write_text(record, encoding="utf-8"))
    _log.info("enrich.cache.migrated", old=old_qsv, new=new_qs.version, rows=total)
    return total


def compact(paths: EnrichPaths, qsv: str, *, small_bytes: int = 64 * 2**20) -> int:
    """Merge the parts smaller than ``small_bytes`` of every partition of ``qsv`` (U03-40).

    Returns the source parts removed. Duplicate keys keep the latest ``decided_at``. A crash
    after the merged write leaves duplicates only (readers dedupe; the next run removes them).
    """
    if small_bytes < 1:
        msg = "small_bytes must be at least 1"
        raise ConfigError(msg, small_bytes=small_bytes)
    removed = 0
    for partition in _partitions(paths.cache_dir(qsv)):
        decider = _decider(partition)
        with _os_errors(partition.name, decider):
            small = [p for p in _parts(partition) if p.stat().st_size < small_bytes]
        if len(small) < 2:  # noqa: PLR2004 - merging needs two parts
            continue
        merged = _dedupe(pa.concat_tables([_read(part, qsv) for part in small]))
        write_part(partition, merged, decider=decider)
        with _os_errors(partition.name, decider):
            for part in small:
                part.unlink()
        removed += len(small)
    _log.info("enrich.cache.compacted", qsv=qsv, parts=removed)
    return removed


def _purge_part(part: Path, qsv: str, targets: pa.Array) -> int:
    """Drop the rows of ``targets`` from ``part``; return the rows removed."""
    hashes = _read(part, qsv, columns=["content_hash"]).column("content_hash")
    hits = pc.sum(pc.is_in(hashes, value_set=targets)).as_py() or 0
    if not hits:
        return 0
    kept = _read(part, qsv, keep=lambda b: pc.invert(pc.is_in(b.column(0), value_set=targets)))
    decider = _decider(part.parent)
    if kept.num_rows:
        write_part(part.parent, kept, name=part.name, decider=decider)
    else:
        with _os_errors(part.name, decider):
            part.unlink()
    return int(hits)


def purge_hashes(paths: EnrichPaths, hashes: frozenset[str]) -> int:
    """Remove every cache row whose ``content_hash`` is in ``hashes``, in every version (U03-41).

    Returns the rows deleted; idempotent. A part left empty is deleted (TH03-12).
    Raises ConfigError for a hash not matching ``^[0-9a-f]{32}$``.
    """
    if any(_HASH_RE.fullmatch(h) is None for h in hashes):
        msg = "purge_hashes needs 32-character lowercase hex content hashes"
        raise ConfigError(msg)
    root = paths.data_root / "cache" / "decisions"
    with _os_errors("decisions"):
        versions = sorted(d for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
    if not hashes:
        versions = []
    targets = pa.array(sorted(hashes), type=pa.string())
    rows = files = 0
    for version_dir in versions:
        for partition in _partitions(version_dir):
            for part in _parts(partition):
                removed = _purge_part(part, version_dir.name, targets)
                rows += removed
                files += 1 if removed else 0
    _log.info("enrich.cache.purged", rows=rows, files=files)
    return rows
