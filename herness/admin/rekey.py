"""Redaction key rotation job (impl 10 U10-101 to U10-103; design 10 §5.3 steps 2-4, §6).

``run_rekey`` is the ``maintenance`` action ``rekey``. It maps every record's old
``content_hash`` to the hash under the staged key, re-keys the human, gold and teacher labels,
then swaps ``redact.hmac_key.next`` into ``redact.hmac_key``. Rewritten label parts are staged
as verified, dot-prefixed files (``.part-<x>.parquet.rekey-<new key id>``, skipped by every label
reader, ``herness.admin._rekey_labels``) and swapped in with ``os.replace`` only after the key
swap, so a job killed before the swap leaves the old key active and every label unchanged
(FT10-02). Every run before the swap redoes the label step from the live parts; a job killed
after the swap finishes on its rerun, refusing a live part changed since staging. Key values
stay in the keyring and in process memory: logs, errors, saved state, the marker, the map and
the audit line carry 8-hex key ids only.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Final

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import JsonValue

from herness.admin._rekey_labels import (
    commit_labels,
    os_errors,
    rekey_labels,
    unlink,
    verify_failed,
    write_json,
)
from herness.core import time as clock
from herness.core.audit import audit
from herness.core.config import HernessConfig
from herness.core.errors import FatalError, SchemaViolation
from herness.core.jobs.ports import JobContext
from herness.core.jobs.scheduler import NEXT_KEY_REF as NEXT_KEY
from herness.core.logging import get_logger
from herness.core.redact import RedactionFailed, Redactor, reset_redactor
from herness.core.redact_directory import NameDirectory
from herness.core.secrets import SecretRef, delete_secret, exists, resolve, set_secret
from herness.core.types import JobOutcome
from herness.enrich.text import compose_text, content_hash
from herness.store.layout import data_layout
from herness.store.warehouse import open_readonly

__all__ = ["build_rekey_map", "commit_labels", "rekey_labels", "run_rekey"]

BATCH_ROWS: Final = 20_000
MAP_SCHEMA: Final = pa.schema(
    [(name, pa.string()) for name in ("record_id", "old_content_hash", "new_content_hash")]
)
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
        with os_errors(out_path.name):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            rows, failed = _stream_map(con, new_redactor, tmp, ctx)
            if pq.read_metadata(tmp).num_rows != rows:
                raise verify_failed(out_path.name)
            os.replace(tmp, out_path)
    finally:
        con.close()
        unlink(tmp)
    if failed:
        _log.warning("rekey.map.redaction_failed", failed=failed, rows=rows)
    _log.info("rekey.map.written", rows=rows, key_id=out_path.stem)
    return rows


# --- U10-101 run_rekey -------------------------------------------------------------------------


def _new_redactor(cfg: HernessConfig, value: str) -> Redactor:
    """A redactor over the staged key with the same rules and name directory as the active one."""
    red = cfg.security.redaction
    display = Path(cfg.paths.data) / "cache/redact/display_names.txt"
    directory = NameDirectory.from_files(red.directory_file, red.extra_names, display)
    return Redactor(red, bytes.fromhex(value), directory)


def _map_path(cfg: HernessConfig, key_id: str) -> Path:
    return Path(cfg.paths.data) / "cache" / "rekey" / f"{key_id}.parquet"


def _finish(
    cfg: HernessConfig, ids: dict[str, str], counts: dict[str, int], *, now: datetime
) -> JobOutcome:
    """Labels swap-in and steps 4b-4e after the key swap; also the resume point after 4a."""
    data = Path(cfg.paths.data)
    moved = commit_labels(data / "labels", _map_path(cfg, ids["new_key_id"]))
    delete_secret(NEXT_KEY, actor="system")  # 4b
    body: dict[str, object] = {"swapped_at": clock.format_utc(now), **ids}
    write_json(data / "cache" / "rekey" / "SWAPPED", body)  # 4c
    detail = "old=" + ids["old_key_id"]
    audit("admin_action", "system", action="redact_rekey", target=ids["new_key_id"], detail=detail)
    reset_redactor()  # 4e
    _log.info("rekey.swapped", labels_swapped=moved, **ids)
    result: dict[str, JsonValue] = {**ids, "counts": dict(counts)}
    return JobOutcome(status="done", result=result)


def _resume(cfg: HernessConfig, state: dict[str, JsonValue], *, now: datetime) -> JobOutcome:
    """The swap began (active id is the saved ``new_key_id``): finish from step 4b."""
    old, new = state.get("old_key_id"), state.get("new_key_id")
    labels_staged = state.get("step") == 3  # noqa: PLR2004 - spec step 3 (labels staged)
    if not (isinstance(old, str) and isinstance(new, str) and labels_staged):
        msg = "rekey state incomplete after the key swap"
        raise FatalError(msg)
    counts = {
        k: v for k in ("rekeyed", "kept_old", "ambiguous") if isinstance(v := state.get(k), int)
    }
    return _finish(cfg, {"new_key_id": new, "old_key_id": old}, counts, now=now)


def run_rekey(cfg: HernessConfig, ctx: JobContext, *, now: datetime) -> JobOutcome:
    """The planned redaction key rotation (U10-101): map, labels, then the key swap.

    State (``step``, ``new_key_id``, ``old_key_id``, counts) is saved after steps 2 and 3. A
    rerun before the swap reuses the map of the same staged key but always redoes the label
    step from the live parts. A failure before 4a leaves the old key and ``.next`` in place
    (design 10 §6).
    """
    state = ctx.load_state()
    active_name = SecretRef.parse(cfg.security.redaction.key).name
    active_id = _key_id(_key(active_name))
    if state.get("new_key_id") == active_id:
        return _resume(cfg, state, now=now)  # swap began: go to 4b
    if not exists(NEXT_KEY):
        msg = "no staged key"
        raise FatalError(msg)
    value = _key(NEXT_KEY)
    ids = {"new_key_id": _key_id(value), "old_key_id": active_id}
    if ids["new_key_id"] == active_id:
        msg = "staged key equals the active key"
        raise FatalError(msg)
    map_path = _map_path(cfg, ids["new_key_id"])
    same_key = state.get("new_key_id") == ids["new_key_id"]
    if not (same_key and state.get("step") in (2, 3) and map_path.exists()):
        build_rekey_map(cfg, _new_redactor(cfg, value), out_path=map_path, ctx=ctx)
        ctx.save_state({"step": 2, **ids})
    counts = rekey_labels(Path(cfg.paths.data) / "labels", map_path)  # always, before 4a
    ctx.save_state({"step": 3, **ids, **counts})
    set_secret(active_name, value, actor="system")  # 4a
    return _finish(cfg, ids, counts, now=now)
