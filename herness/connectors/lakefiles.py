"""Orphan temp-file cleanup and per-writer schema drift tracking (impl 01 U01-34, U01-35,
design 01 §5.2, TH01-08).

Crash leftovers named ``.<name>.parquet.tmp-<ulid>`` under ``data/raw/<source>/`` are safe
to delete once older than an hour: a live writer either commits or rotates well within its
600 s ``max_open_s`` (spec 02). `SchemaTracker` tells the sync runner when a batch's columns
no longer match the current lake file, so it can start a new one (one schema per file).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pyarrow as pa

from herness.core.logging import get_logger

_log = get_logger("connectors.lake")

# ULID charset (Crockford base32, case-insensitive-free); the count is not itself validated
# against the strict ULID grammar (TH01-08 only needs no traversal, not ULID canonicity).
_TEMP_RE: Final = re.compile(r"\.[^\\/]+\.parquet\.tmp-[0-9A-HJKMNP-TV-Z]{26}")


def _is_contained(path: Path, resolved_root: Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    return resolved == resolved_root or resolved_root in resolved.parents


def _candidate(path: Path, resolved_root: Path) -> bool:
    """True iff `path` may be deleted: not a symlink or junction, resolves under root.

    `Path.is_junction()` is always `False` on POSIX and always `False` for a regular file
    on Windows (junctions are directory reparse points), so this also protects against a
    junction directory that `os.walk` still descended into (TH01-08).
    """
    if path.is_symlink() or path.is_junction():
        return False
    return _is_contained(path, resolved_root)


def _remove_if_old(
    path: Path, *, now: datetime, max_age: timedelta, source: str, root: Path
) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC)
    if now - mtime <= max_age:
        return False
    try:
        path.unlink()
    except (PermissionError, FileNotFoundError) as exc:
        _log.warning(
            "connectors.lake.orphan_remove_failed",
            source=source,
            file=path.relative_to(root).as_posix(),
            error_class=type(exc).__name__,
        )
        return False
    return True


def cleanup_orphan_temp_files(
    raw_root: Path, source: str, *, now: datetime, max_age: timedelta = timedelta(hours=1)
) -> int:
    """Delete crash-leftover temp parquet files older than `max_age` under `raw_root/source`.

    Never follows symlinks or junctions and never deletes outside `raw_root / source`
    (TH01-08). Raises nothing: OS errors on individual files are logged and skipped.
    """
    root = raw_root / source
    if not root.is_dir():
        return 0
    resolved_root = root.resolve()
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            if _TEMP_RE.fullmatch(name) is None:
                continue
            path = Path(dirpath) / name
            if not _candidate(path, resolved_root):
                continue
            if _remove_if_old(path, now=now, max_age=max_age, source=source, root=root):
                removed += 1
    if removed > 0:
        _log.info("connectors.lake.orphans_removed", source=source, count=removed)
    return removed


@dataclass(frozen=True, slots=True)
class SchemaDrift:
    """Column differences between a batch's schema and the writer's baseline (U01-35)."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]


class SchemaTracker:
    """Detects when a batch's columns differ from the current writer's baseline (U01-35).

    The baseline is the schema of the last observed batch; column order is ignored.
    """

    def __init__(self) -> None:
        self._baseline: dict[str, pa.DataType] | None = None

    def observe(self, schema: pa.Schema) -> SchemaDrift | None:
        """Compare `schema` to the baseline, update it, and return the drift (or None)."""
        current = {field.name: field.type for field in schema}
        baseline, self._baseline = self._baseline, current
        if baseline is None or baseline == current:
            return None
        added = tuple(sorted(set(current) - set(baseline)))
        removed = tuple(sorted(set(baseline) - set(current)))
        changed = tuple(
            sorted(
                name for name in current.keys() & baseline.keys() if current[name] != baseline[name]
            )
        )
        return SchemaDrift(added=added, removed=removed, changed=changed)
