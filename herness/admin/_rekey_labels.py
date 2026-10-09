"""Label re-keying for the rekey job (impl 10 U10-103; design 10 §5.3 step 3, FT10-02).

Private sibling of ``herness.admin.rekey`` (size limit). ``rekey_labels`` first deletes every
staged file left by an earlier attempt, then writes each changed label part as a verified,
dot-prefixed staged file ``.part-<x>.parquet.rekey-<new key id>`` (every label reader skips
``.``-prefixed entries) and records the staged set in the manifest ``<new key id>.staged.json``
next to the map: per part, the SHA-256 of the live part it was made from and of the staged
file. ``commit_labels`` runs after the key swap: it checks every recorded part first (live part
unchanged since staging and staged file present, or already swapped in) and raises
``FatalError`` before swapping anything when a part changed or a staged file is missing, so a
privacy purge or an append between staging and the swap is never overwritten.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from herness.core.errors import FatalError, SchemaViolation, StoreBusy
from herness.core.logging import get_logger
from herness.enrich.purge import _LABEL_DIRS, _PART_GLOB

__all__ = ["commit_labels", "manifest_path", "rekey_labels"]

_STAGED_GLOB: Final = ".part-*.parquet.rekey-*"  # any key id, and their .tmp files

_log = get_logger("admin.rekey")


@contextlib.contextmanager
def os_errors(what: str) -> Iterator[None]:
    """``OSError`` -> ``StoreBusy`` naming ``what`` (a kind and a file name) only."""
    try:
        yield
    except OSError as exc:
        msg = f"rekey cannot write {what}"
        raise StoreBusy(msg) from exc


def unlink(path: Path) -> None:
    """Remove ``path`` if present; an OS error is ignored (best-effort cleanup)."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def verify_failed(what: str) -> FatalError:
    """The error for a written file whose re-read content differs."""
    return FatalError(f"rekey verification failed: {what}")


def write_json(path: Path, body: dict[str, object]) -> None:
    """Write ``body`` to ``path`` atomically (``.tmp``, fsync, ``os.replace``)."""
    tmp = path.with_name(f".{path.name}.tmp")
    with os_errors(path.name):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(body, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            unlink(tmp)


def manifest_path(rekey_map: Path) -> Path:
    """``<new key id>.staged.json`` next to the map."""
    return rekey_map.with_name(f"{rekey_map.stem}.staged.json")


def _sha(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _load_map(path: Path) -> dict[str, tuple[str, ...]]:
    """``old -> sorted new hashes``; NULL new hashes are ignored."""
    with os_errors(path.name):
        table = pq.read_table(path, columns=["old_content_hash", "new_content_hash"])
    found: dict[str, set[str]] = {}
    for old, new in zip(*(table.column(i).to_pylist() for i in range(2)), strict=True):
        if old is not None and new is not None:
            found.setdefault(old, set()).add(new)
    return {old: tuple(sorted(news)) for old, news in found.items()}


def _label_folders(labels_root: Path) -> list[Path]:
    """Every label kind folder (``herness.enrich.purge`` set) of every question set version."""
    with os_errors("labels"):
        roots = sorted(labels_root.iterdir()) if labels_root.is_dir() else []
        return [
            v / kind for v in roots if v.is_dir() for kind in _LABEL_DIRS if (v / kind).is_dir()
        ]


def _staged_name(part: Path, key_id: str) -> Path:
    return part.with_name(f".{part.name}.rekey-{key_id}")


def _rekey_table(
    table: pa.Table, mapping: dict[str, tuple[str, ...]], counts: dict[str, int]
) -> tuple[pa.Table, list[str | None]] | None:
    """The re-keyed table and its hash column, or None when no row changes."""
    take: list[int] = []
    hashes: list[str | None] = []
    changed = False
    for i, old in enumerate(table.column("content_hash").to_pylist()):
        new = mapping.get(old, ()) if old is not None else ()
        counts["kept_old" if not new else "rekeyed" if len(new) == 1 else "ambiguous"] += 1
        changed = changed or (bool(new) and new != (old,))
        take += [i] * max(len(new), 1)
        hashes += list(new) or [old]
    if not changed:
        return None
    out = table.take(pa.array(take, pa.int64()))
    idx = out.schema.get_field_index("content_hash")
    field = out.schema.field(idx)
    return out.set_column(idx, field, pa.array(hashes, field.type)), hashes


def _stage(part: Path, table: pa.Table, hashes: list[str | None], key_id: str) -> str:
    """Write the staged part via ``.tmp``, re-read and verify it, ``os.replace``; its SHA-256."""
    staged = _staged_name(part, key_id)
    tmp = staged.with_name(f"{staged.name}.tmp")
    with os_errors(f"label part {part.name}"):
        try:
            pq.write_table(table, tmp, compression="zstd")
            with tmp.open("rb+") as handle:
                os.fsync(handle.fileno())
            check = pq.read_table(tmp, columns=["content_hash"], partitioning=None)
            if check.num_rows != len(hashes) or check.column(0).to_pylist() != hashes:
                raise verify_failed(part.name)
            os.replace(tmp, staged)
            return _sha(staged)
        finally:
            unlink(tmp)


def rekey_labels(labels_root: Path, rekey_map: Path) -> dict[str, int]:
    """Re-key every human, gold and teacher label part through ``rekey_map`` (U10-103).

    Per row: exactly one new hash replaces the old one (``rekeyed``), several give one row per
    new hash (``ambiguous``), none keeps the old hash (``kept_old``, reported). Earlier staged
    files are deleted first; a changed part is staged and recorded in the manifest.
    """
    key_id = rekey_map.stem
    mapping = _load_map(rekey_map)
    folders = _label_folders(labels_root)
    with os_errors("labels"):
        for stale in (s for folder in folders for s in folder.glob(_STAGED_GLOB)):
            stale.unlink()
        parts = [part for folder in folders for part in sorted(folder.glob(_PART_GLOB))]
    counts = {"rekeyed": 0, "kept_old": 0, "ambiguous": 0}
    staged: dict[str, dict[str, str]] = {}
    for part in parts:
        with os_errors(f"label part {part.name}"):
            live = _sha(part)
            table = pq.read_table(part, partitioning=None)
        if "content_hash" not in table.column_names:
            msg = f"label part has no content_hash: {part.name}"
            raise SchemaViolation(msg)
        rekeyed = _rekey_table(table, mapping, counts)
        if rekeyed is not None:
            done = _stage(part, *rekeyed, key_id)
            staged[part.relative_to(labels_root).as_posix()] = {"live": live, "staged": done}
    write_json(manifest_path(rekey_map), {"key_id": key_id, "parts": staged})
    if counts["kept_old"]:
        _log.warning("rekey.labels.kept_old", kept_old=counts["kept_old"], key_id=key_id)
    _log.info("rekey.labels.staged", key_id=key_id, staged=len(staged), **counts)
    return counts


def _recorded(rekey_map: Path) -> dict[str, dict[str, str]]:
    """The staged set of the manifest; FatalError when it is missing or malformed."""
    try:
        parts = json.loads(manifest_path(rekey_map).read_text("utf-8"))["parts"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        msg = f"rekey staging manifest unreadable: {manifest_path(rekey_map).name}"
        raise FatalError(msg) from exc
    if not isinstance(parts, dict):
        raise verify_failed(manifest_path(rekey_map).name)
    return parts


def commit_labels(labels_root: Path, rekey_map: Path) -> int:
    """Swap every recorded staged part onto its live part (after the key swap); returns the
    number swapped now. Parts already swapped in are skipped, so a rerun is idempotent."""
    key_id = rekey_map.stem
    todo: list[tuple[Path, Path]] = []
    for rel, sums in sorted(_recorded(rekey_map).items()):
        part = labels_root / rel
        staged = _staged_name(part, key_id)
        with os_errors(f"label part {part.name}"):
            current = _sha(part) if part.exists() else None
            pending = staged.exists()
        if pending and current == sums.get("live"):
            todo.append((staged, part))
        elif pending or current != sums.get("staged"):
            msg = f"label part changed since staging or staged file missing: {part.name}"
            raise FatalError(msg)
    for staged, part in todo:
        with os_errors(f"label part {part.name}"):
            os.replace(staged, part)
    return len(todo)
