"""Redaction key rotation job (impl 10 U10-101 to U10-103; design 10 §5.3 steps 2-4, §6).

``run_rekey`` is the ``maintenance`` action ``rekey``. It maps every record's old
``content_hash`` to the hash under the staged key, re-keys the human, gold and teacher labels,
then swaps ``redact.hmac_key.next`` into ``redact.hmac_key``. Rewritten label parts are staged
as verified, dot-prefixed files (``.part-<x>.parquet.rekey-<new key id>``, skipped by every label
reader) and swapped in with ``os.replace`` only after the key swap, so a job killed before the
swap leaves the old key active and every label unchanged (FT10-02); a job killed after it
finishes the swap on its rerun. Key values stay in the keyring and in process memory: logs,
errors, saved state, the marker, the map and the audit line carry 8-hex key ids only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Final

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import JsonValue

from herness.core import time as clock
from herness.core.audit import audit
from herness.core.config import HernessConfig
from herness.core.errors import FatalError, SchemaViolation, StoreBusy
from herness.core.jobs.ports import JobContext
from herness.core.logging import get_logger
from herness.core.redact import RedactionFailed, Redactor, reset_redactor
from herness.core.redact_directory import NameDirectory
from herness.core.secrets import SecretRef, delete_secret, exists, resolve, set_secret
from herness.core.types import JobOutcome
from herness.enrich.text import compose_text, content_hash
from herness.store.layout import data_layout
from herness.store.warehouse import open_readonly

__all__ = ["build_rekey_map", "commit_labels", "rekey_labels", "run_rekey"]

NEXT_KEY: Final = "redact.hmac_key.next"
BATCH_ROWS: Final = 20_000
MAP_SCHEMA: Final = pa.schema(
    [(name, pa.string()) for name in ("record_id", "old_content_hash", "new_content_hash")]
)
LABEL_DIRS: Final = ("human", "gold", "gold/_reviews", "teacher")  # design 10 §5.3 step 3
_PART_GLOB: Final = "part-*.parquet"
_KEY_HEX: Final = re.compile(r"[0-9A-Fa-f]{64}")
_SCHEMA_ERRORS: Final = (duckdb.CatalogException, duckdb.BinderException)
# (record_id, old content_hash, short description, body) of every hashed record, by record id;
# columns as in impl 03 U03-28 (`herness.enrich.text` entities).
_TEXT_SQL: Final = """
    SELECT t.record_id, t.content_hash, c.short_description, c.description
    FROM enrich.text_redacted AS t JOIN core.incident AS c ON c.record_id = t.record_id
    WHERE t.entity = 'incident'
    UNION ALL
    SELECT t.record_id, t.content_hash, c.short_description, c.description
    FROM enrich.text_redacted AS t JOIN core.change AS c ON c.record_id = t.record_id
    WHERE t.entity = 'change'
    UNION ALL
    SELECT t.record_id, t.content_hash, CAST(NULL AS VARCHAR), c.root_cause_text
    FROM enrich.text_redacted AS t JOIN core.problem AS c ON c.record_id = t.record_id
    WHERE t.entity = 'problem'
    ORDER BY 1
"""

_log = get_logger("admin.rekey")


# --- helpers -----------------------------------------------------------------------------------


def _key(name: str) -> str:
    """The 64-hex value of secret ``name``; the error names the secret, never the value."""
    value = resolve(name).get_secret_value()
    if _KEY_HEX.fullmatch(value) is None:
        msg = f"{name} must be 64 hex characters"
        raise FatalError(msg)
    return value


def _key_id(value: str) -> str:
    """First 8 hex of SHA-256 over the key bytes (U10-40 ``Redactor.key_id``)."""
    return hashlib.sha256(bytes.fromhex(value)).hexdigest()[:8]


@contextlib.contextmanager
def _os_errors(what: str) -> Iterator[None]:
    """``OSError`` -> ``StoreBusy`` naming ``what`` (a kind and a file name) only."""
    try:
        yield
    except OSError as exc:
        msg = f"rekey cannot write {what}"
        raise StoreBusy(msg) from exc


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _verify_failed(what: str) -> FatalError:
    return FatalError(f"rekey verification failed: {what}")


def _write_json(path: Path, body: dict[str, str]) -> None:
    """Write ``body`` to ``path`` atomically (``.tmp``, fsync, ``os.replace``)."""
    tmp = path.with_name(f".{path.name}.tmp")
    with _os_errors(path.name):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(body, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            _unlink(tmp)


# --- U10-102 build_rekey_map -------------------------------------------------------------------


def _map_batch(batch: pa.RecordBatch, redactor: Redactor) -> tuple[pa.RecordBatch, int]:
    """Redact one batch under the new key; a failed record gets ``NULL`` (counted)."""
    ids, olds, shorts, bodies = (batch.column(i).to_pylist() for i in range(4))
    new: list[str | None] = []
    for short, body in zip(shorts, bodies, strict=True):
        try:
            result = redactor.redact(compose_text(short, body))
        except RedactionFailed:
            result = None
        new.append(None if result is None else content_hash(result.text))
    columns = [pa.array(ids, pa.string()), pa.array(olds, pa.string()), pa.array(new, pa.string())]
    return pa.record_batch(columns, schema=MAP_SCHEMA), new.count(None)


def _stream_map(
    con: duckdb.DuckDBPyConnection, redactor: Redactor, tmp: Path, ctx: JobContext
) -> tuple[int, int]:
    rows = failed = 0
    try:
        reader = con.execute(_TEXT_SQL).to_arrow_reader(BATCH_ROWS)
        with pq.ParquetWriter(tmp, MAP_SCHEMA, compression="zstd") as writer:
            for batch in reader:
                out, bad = _map_batch(batch, redactor)
                writer.write_batch(out)
                rows, failed = rows + out.num_rows, failed + bad
                ctx.heartbeat(f"rekey map rows={rows}")
    except _SCHEMA_ERRORS as exc:
        msg = "rekey map: enrich.text_redacted or core text tables missing"
        raise SchemaViolation(msg) from exc
    except duckdb.Error as exc:
        msg = f"rekey map: {type(exc).__name__}"  # other DuckDB messages may quote row values
        raise FatalError(msg) from exc
    return rows, failed


def build_rekey_map(
    cfg: HernessConfig, new_redactor: Redactor, *, out_path: Path, ctx: JobContext
) -> int:
    """Write ``(record_id, old_content_hash, new_content_hash)`` for every record of the
    ``CURRENT`` warehouse to ``out_path`` atomically (U10-102); returns the row count.

    Raw text stays in process; only hashes are written. The written file is re-read and its
    row count checked before it replaces ``out_path``.
    """
    tmp = out_path.with_name(f".{out_path.name}.tmp")
    con = open_readonly(layout=data_layout(cfg=cfg))
    try:
        with _os_errors(out_path.name):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            rows, failed = _stream_map(con, new_redactor, tmp, ctx)
            if pq.read_metadata(tmp).num_rows != rows:
                raise _verify_failed(out_path.name)
            os.replace(tmp, out_path)
    finally:
        con.close()
        _unlink(tmp)
    if failed:
        _log.warning("rekey.map.redaction_failed", failed=failed, rows=rows)
    _log.info("rekey.map.written", rows=rows, key_id=out_path.stem)
    return rows


# --- U10-103 rekey_labels ----------------------------------------------------------------------


def _load_map(path: Path) -> dict[str, tuple[str, ...]]:
    """``old -> sorted new hashes``; NULL new hashes are ignored."""
    with _os_errors(path.name):
        table = pq.read_table(path, columns=["old_content_hash", "new_content_hash"])
    found: dict[str, set[str]] = {}
    for old, new in zip(*(table.column(i).to_pylist() for i in range(2)), strict=True):
        if old is not None and new is not None:
            found.setdefault(old, set()).add(new)
    return {old: tuple(sorted(news)) for old, news in found.items()}


def _label_parts(labels_root: Path) -> list[Path]:
    """``part-*.parquet`` of every label kind of every question set version."""
    with _os_errors("labels"):
        roots = sorted(labels_root.iterdir()) if labels_root.is_dir() else []
        folders = [v / kind for v in roots if v.is_dir() for kind in LABEL_DIRS]
        return [p for f in folders if f.is_dir() for p in sorted(f.glob(_PART_GLOB))]


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


def _stage(part: Path, table: pa.Table, hashes: list[str | None], key_id: str) -> None:
    """Write the staged part via ``.tmp``, re-read and verify it, then ``os.replace``."""
    staged = _staged_name(part, key_id)
    tmp = staged.with_name(f"{staged.name}.tmp")
    with _os_errors(f"label part {part.name}"):
        try:
            pq.write_table(table, tmp, compression="zstd")
            with tmp.open("rb+") as handle:
                os.fsync(handle.fileno())
            check = pq.read_table(tmp, columns=["content_hash"], partitioning=None)
            if check.num_rows != len(hashes) or check.column(0).to_pylist() != hashes:
                raise _verify_failed(part.name)
            os.replace(tmp, staged)
        finally:
            _unlink(tmp)


def rekey_labels(labels_root: Path, rekey_map: Path) -> dict[str, int]:
    """Re-key every human, gold and teacher label part through ``rekey_map`` (U10-103).

    Per row: exactly one new hash replaces the old one (``rekeyed``), several give one row per
    new hash (``ambiguous``), none keeps the old hash (``kept_old``, reported). A changed part
    is staged next to the live one; ``commit_labels`` swaps the staged parts in.
    """
    key_id = rekey_map.stem
    mapping = _load_map(rekey_map)
    counts = {"rekeyed": 0, "kept_old": 0, "ambiguous": 0}
    for part in _label_parts(labels_root):
        with _os_errors(f"label part {part.name}"):
            table = pq.read_table(part, partitioning=None)
        if "content_hash" not in table.column_names:
            msg = f"label part has no content_hash: {part.name}"
            raise SchemaViolation(msg)
        rekeyed = _rekey_table(table, mapping, counts)
        if rekeyed is not None:
            _stage(part, *rekeyed, key_id)
    if counts["kept_old"]:
        _log.warning("rekey.labels.kept_old", kept_old=counts["kept_old"], key_id=key_id)
    _log.info("rekey.labels.staged", key_id=key_id, **counts)
    return counts


def commit_labels(labels_root: Path, key_id: str) -> int:
    """Swap every staged part of ``key_id`` onto its live part with ``os.replace``."""
    moved = 0
    for part in _label_parts(labels_root):
        staged = _staged_name(part, key_id)
        with _os_errors(f"label part {part.name}"):
            if staged.exists():
                os.replace(staged, part)
                moved += 1
    return moved


# --- U10-101 run_rekey -------------------------------------------------------------------------


def _new_redactor(cfg: HernessConfig, value: str) -> Redactor:
    """A redactor over the staged key with the same rules and name directory as the active one."""
    red = cfg.security.redaction
    display = Path(cfg.paths.data) / "cache/redact/display_names.txt"
    directory = NameDirectory.from_files(red.directory_file, red.extra_names, display)
    return Redactor(red, bytes.fromhex(value), directory)


def _finish(
    cfg: HernessConfig, ids: dict[str, str], counts: dict[str, int], *, now: datetime
) -> JobOutcome:
    """Steps 4b-4e after the key swap; also the resume point of a rerun after 4a."""
    data = Path(cfg.paths.data)
    moved = commit_labels(data / "labels", ids["new_key_id"])
    delete_secret(NEXT_KEY, actor="system")  # 4b
    body = {"swapped_at": clock.format_utc(now), **ids}
    _write_json(data / "cache" / "rekey" / "SWAPPED", body)  # 4c
    detail = "old=" + ids["old_key_id"]
    audit("admin_action", "system", action="redact_rekey", target=ids["new_key_id"], detail=detail)
    reset_redactor()  # 4e
    _log.info("rekey.swapped", labels_swapped=moved, **ids)
    result: dict[str, JsonValue] = {**ids, "counts": dict(counts)}
    return JobOutcome(status="done", result=result)


def _saved_counts(state: dict[str, JsonValue]) -> dict[str, int]:
    return {
        k: v for k in ("rekeyed", "kept_old", "ambiguous") if isinstance(v := state.get(k), int)
    }


def run_rekey(cfg: HernessConfig, ctx: JobContext, *, now: datetime) -> JobOutcome:
    """The planned redaction key rotation (U10-101): map, labels, then the key swap.

    State (``step``, ``new_key_id``, ``old_key_id``, counts) is saved after steps 2 and 3, and
    a rerun skips finished steps of the same staged key. A failure before 4a leaves the old
    key and ``.next`` in place (design 10 §6).
    """
    state = ctx.load_state()
    active_name = SecretRef.parse(cfg.security.redaction.key).name
    active_id = _key_id(_key(active_name))
    if isinstance(saved := state.get("new_key_id"), str) and saved == active_id:
        ids = {"new_key_id": saved, "old_key_id": str(state.get("old_key_id"))}
        return _finish(cfg, ids, _saved_counts(state), now=now)  # swap began: go to 4b
    if not exists(NEXT_KEY):
        msg = "no staged key"
        raise FatalError(msg)
    value = _key(NEXT_KEY)
    ids = {"new_key_id": _key_id(value), "old_key_id": active_id}
    if ids["new_key_id"] == active_id:
        msg = "staged key equals the active key"
        raise FatalError(msg)
    raw_step = state.get("step") if saved == ids["new_key_id"] else 0
    step = raw_step if isinstance(raw_step, int) else 0
    map_path = Path(cfg.paths.data) / "cache" / "rekey" / f"{ids['new_key_id']}.parquet"
    if step < 2 or not map_path.exists():  # noqa: PLR2004 - spec step 2
        build_rekey_map(cfg, _new_redactor(cfg, value), out_path=map_path, ctx=ctx)
        ctx.save_state({"step": 2, **ids})
        step = 2
    counts = _saved_counts(state)
    if step < 3:  # noqa: PLR2004 - spec step 3
        counts = rekey_labels(Path(cfg.paths.data) / "labels", map_path)
        ctx.save_state({"step": 3, **ids, **counts})
    set_secret(active_name, value, actor="system")  # 4a
    return _finish(cfg, ids, counts, now=now)
