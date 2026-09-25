"""Record purge and partition retention; the only rewrite/delete path over the lake (§3.3, R-57)."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import errno
import os
import re
import shutil
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from herness.core.errors import ConfigError, HernessError, SchemaViolation, StoreBusy
from herness.core.ids import new_ulid
from herness.core.logging import get_logger
from herness.store.lake import _BUSY_ERRNOS, _BUSY_WINERRORS, _PARQUET_OPTIONS, _fsync
from herness.store.lake import validate_name as _validate_name
from herness.store.layout import data_layout

PURGE_BATCH_MAX: Final = 10_000
_RECORD_ID_PARTS: Final = 3
_RETENTION_MIN_AGE_DAYS: Final = 30
_DT_RE: Final = re.compile(r"^dt=(\d{4})-(\d{2})-(\d{2})$")
_log = get_logger("store.lake_purge")


@dataclasses.dataclass(frozen=True, slots=True)
class LakePurgeResult:
    """Counts from ``purge_record_ids``; ``files_rewritten + files_deleted <= files_scanned``."""

    files_scanned: int
    files_rewritten: int
    files_deleted: int
    rows_removed: int


@dataclasses.dataclass(frozen=True, slots=True)
class LakeRetentionResult:
    """Counts from ``purge_partitions_before``."""

    partitions_deleted: int
    bytes_freed: int
    skipped_unparsable: int
    deferred_locked: int


def _group_ids(record_ids: Collection[str]) -> dict[tuple[str, str], set[str]]:
    groups: dict[tuple[str, str], set[str]] = {}
    for record_id in record_ids:
        parts = record_id.split(":", 2)
        if len(parts) != _RECORD_ID_PARTS or not parts[2]:
            msg = f"invalid lake record id (length {len(record_id)})"
            raise ConfigError(msg)
        key = (_validate_name("source", parts[0]), _validate_name("entity", parts[1]))
        groups.setdefault(key, set()).add(record_id)
    return groups


def _purge_os_error(exc: OSError, file: Path) -> HernessError:
    where = f"{file.parent.name}: {errno.errorcode.get(exc.errno or 0, 'unknown')}"
    winerror = getattr(exc, "winerror", None)
    if exc.errno in _BUSY_ERRNOS or winerror in _BUSY_WINERRORS:
        return StoreBusy(f"lake purge busy for {where}")
    return SchemaViolation(f"lake purge failed for {where}")


def _replace_with(file: Path, table: pa.Table) -> None:
    temp = file.with_name(f".{file.name}.tmp-{new_ulid()}")
    try:
        pq.write_table(table, temp, **_PARQUET_OPTIONS)
        _fsync(temp, os.O_RDWR | getattr(os, "O_BINARY", 0))
        os.replace(temp, file)
    except OSError:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise


def _rewrite_or_delete(file: Path, mask: pa.Array) -> bool:
    """Drop matching rows from ``file``; return ``True`` if it was deleted, else rewritten."""
    table = pq.read_table(file)
    kept = table.filter(pc.invert(mask))
    try:
        if kept.num_rows == 0:
            file.unlink()
            return True
        _replace_with(file, kept)
    except OSError as exc:
        raise _purge_os_error(exc, file) from exc
    return False


def _purge_group(base: Path, source: str, entity: str, ids: set[str]) -> tuple[int, int, int, int]:
    directory = base / source / entity
    if not directory.is_dir():
        return 0, 0, 0, 0
    id_array = pa.array(sorted(ids), pa.string())
    scanned = rewritten = deleted = removed = 0
    for file in sorted(directory.glob("dt=*/part-*.parquet")):
        scanned += 1
        ids_col = pq.read_table(file, columns=["_record_id"]).column("_record_id")
        mask = pc.is_in(ids_col, id_array)
        matched = int(pc.sum(mask).as_py() or 0)
        if matched == 0:
            continue
        removed += matched
        if _rewrite_or_delete(file, mask):
            deleted += 1
        else:
            rewritten += 1
    return scanned, rewritten, deleted, removed


def purge_record_ids(record_ids: Collection[str], *, root: Path | None = None) -> LakePurgeResult:
    """Remove every row whose ``_record_id`` is in ``record_ids`` from committed lake files."""
    count = len(record_ids)
    if not 1 <= count <= PURGE_BATCH_MAX:
        msg = f"purge record id count out of range (got {count})"
        raise ConfigError(msg)
    groups = _group_ids(record_ids)
    base = (root if root is not None else data_layout().raw).resolve(strict=False)
    scanned = rewritten = deleted = removed = 0
    for (source, entity), ids in groups.items():
        g_scanned, g_rewritten, g_deleted, g_removed = _purge_group(base, source, entity, ids)
        scanned += g_scanned
        rewritten += g_rewritten
        deleted += g_deleted
        removed += g_removed
    _log.info(
        "store.lake.purged",
        files_scanned=scanned,
        files_rewritten=rewritten,
        files_deleted=deleted,
        rows_removed=removed,
        id_count=count,
    )
    return LakePurgeResult(scanned, rewritten, deleted, removed)


def _parse_partition_date(name: str) -> datetime.date | None:
    match = _DT_RE.fullmatch(name)
    if match is None:
        return None
    try:
        return datetime.date(*(int(group) for group in match.groups()))
    except ValueError:
        return None


def _iter_partition_dirs(base: Path) -> Iterator[Path]:
    if not base.is_dir():
        return
    for source_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for entity_dir in sorted(p for p in source_dir.iterdir() if p.is_dir()):
            yield from sorted(p for p in entity_dir.iterdir() if p.is_dir())


def purge_partitions_before(
    cutoff: datetime.date, *, today: datetime.date, root: Path | None = None
) -> LakeRetentionResult:
    """Delete whole ``dt=`` partitions with ``dt < cutoff`` (spec 10 ``raw_lake_months``)."""
    if cutoff > today - datetime.timedelta(days=_RETENTION_MIN_AGE_DAYS):
        msg = "retention cutoff must be at least 30 days before today"
        raise ConfigError(msg)
    base = (root if root is not None else data_layout().raw).resolve(strict=False)
    deleted = freed = unparsable = deferred = 0
    for part_dir in _iter_partition_dirs(base):
        part_date = _parse_partition_date(part_dir.name)
        if part_date is None:
            unparsable += 1
            rel = part_dir.relative_to(base)
            _log.warning("store.lake.partition_unparsable", path=rel.as_posix())
            continue
        if part_date >= cutoff or not part_dir.resolve(strict=False).is_relative_to(base):
            continue  # containment (TH02-01) is defensive; unreachable via _iter_partition_dirs
        size = sum(f.stat().st_size for f in part_dir.rglob("*") if f.is_file())
        try:
            shutil.rmtree(part_dir)
        except PermissionError:
            deferred += 1
            continue
        deleted += 1
        freed += size
    _log.info(
        "store.lake.retention_applied",
        partitions_deleted=deleted,
        bytes_freed=freed,
        skipped_unparsable=unparsable,
        deferred_locked=deferred,
    )
    return LakeRetentionResult(deleted, freed, unparsable, deferred)
