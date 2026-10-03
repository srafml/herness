"""Steps 5 and 6 of the `memory_maintenance` job (U07-96): vector consistency and backfill.

Size-forced private sibling of `maintenance.py` (T07-22 spec note); only `maintenance.py`
imports it. SQLite is the source of truth (TH07-13): step 5 deletes only *vectors* whose row is
gone, mirrors the row's status onto its vector and flags items for re-embedding; it never
changes an item's status or deletes a row, and every batch re-reads SQLite before it acts.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from functools import partial
from itertools import batched
from typing import Final, cast

from pydantic import JsonValue

from herness.core.errors import ModelUnavailable
from herness.core.logging import get_logger
from herness.core.types import Status
from herness.harness.memory._write_steps import stored_flags
from herness.harness.memory.store import Embedder, VectorIndex, VectorMeta, VectorRow
from herness.store.ops import core
from herness.store.ops import memory as ops

__all__ = ["BACKFILL_MAX", "BEAT_EVERY", "PAGE", "backfill", "check_vectors"]

type Row = ops.MemoryItemRow
type Conn = sqlite3.Connection
type Pair = tuple[str, VectorMeta | None]

PAGE: Final = 1_000  # step 5 page size of vectors.list_ids and all_ids_status
BACKFILL_MAX: Final = 5_000  # step 6 bound per run
BEAT_EVERY: Final = 256  # step 6 heartbeat interval (items)
_CHECK: Final = 500  # ids per SQLite re-read (the get_memory_items C2 limit)
_log = get_logger("memory")


def _sql_ids(stamp: str, tally: dict[str, int]) -> Iterator[str]:
    """Every SQLite memory_id in order, counting statuses into `tally`."""
    after = ""
    while page := cast(
        "list[tuple[str, str]]",
        ops.maintenance_rows(selector="all_ids_status", now=stamp, limit=PAGE, after=after),
    ):
        for memory_id, status in page:
            tally[status] = tally.get(status, 0) + 1
            yield memory_id
        after = page[-1][0]


def _vector_ids(vectors: VectorIndex) -> Iterator[VectorMeta]:
    """Every vector row's metadata in memory_id order."""
    after = ""
    while page := vectors.list_ids(after, PAGE):
        yield from page
        after = page[-1].memory_id


def _union(ids: Iterator[str], metas: Iterator[VectorMeta]) -> Iterator[Pair]:
    """Merge the two ordered streams: each id once, with its vector metadata when it has one."""
    sid, meta = next(ids, None), next(metas, None)
    while sid is not None or meta is not None:
        if meta is not None and (sid is None or meta.memory_id <= sid):
            if meta.memory_id == sid:
                sid = next(ids, None)
            yield meta.memory_id, meta
            meta = next(metas, None)
        else:
            yield cast("str", sid), None
            sid = next(ids, None)


def _needs_embedding(row: Row, meta: VectorMeta | None, model: str) -> bool:
    """Missing vector, other content hash or other model (LLM03); rejected items never."""
    data = row["data"]
    if row["status"] == "rejected" or data.get("embedding_pending") is True:
        return False
    return meta is None or meta.content_hash != str(data.get("content_hash")) or meta.model != model


def _set_pending(row: Row, conn: Conn, *, value: bool) -> None:
    data = dict(row["data"])
    flags: list[JsonValue] = [f for f in stored_flags(data) if f != "embedding_pending"]
    data["flags"] = [*flags, "embedding_pending"] if value else flags
    data["embedding_pending"] = value
    ops.update_memory_item(row["memory_id"], data=data, conn=conn)


def _flag_tx(ids: Sequence[str], conn: Conn) -> None:
    for row in ops.get_memory_items(ids, conn=conn):
        if row["data"].get("embedding_pending") is not True:
            _set_pending(row, conn, value=True)


def _repair(vectors: VectorIndex, model: str, batch: Sequence[Pair], out: dict[str, int]) -> None:
    """Act on one batch after re-reading SQLite (TH07-13: vectors follow the rows)."""
    rows = {row["memory_id"]: row for row in ops.get_memory_items([i for i, _ in batch])}
    orphans: list[str] = []
    moves: dict[str, list[str]] = {}
    stale: list[str] = []
    for memory_id, meta in batch:
        if (row := rows.get(memory_id)) is None:
            orphans.extend([memory_id] if meta is not None else [])
            continue
        if meta is not None and meta.status != row["status"]:
            moves.setdefault(row["status"], []).append(memory_id)
        if _needs_embedding(row, meta, model):
            stale.append(memory_id)
    vectors.delete(orphans)
    for status, ids in sorted(moves.items()):
        vectors.set_status(ids, cast("Status", status))
    if stale:
        core.run_write(partial(_flag_tx, stale), op="memory_maintenance_flag")
    out["deleted"] += len(orphans)
    out["restatused"] += sum(map(len, moves.values()))
    out["flagged"] += len(stale)


def check_vectors(vectors: VectorIndex, model: str, stamp: str) -> dict[str, JsonValue]:
    """Step 5: delete orphan vectors, repair vector statuses, flag stale or missing ones."""
    tally: dict[str, int] = {}
    out = {"deleted": 0, "restatused": 0, "flagged": 0}
    for batch in batched(_union(_sql_ids(stamp, tally), _vector_ids(vectors)), _CHECK):
        _repair(vectors, model, batch, out)
    return {**out, "statuses": dict(sorted(tally.items()))}


def _clear_tx(hashes: dict[str, str], conn: Conn) -> None:
    """Clear the flag of rows whose content is still the embedded one."""
    for row in ops.get_memory_items(list(hashes), conn=conn):
        if str(row["data"].get("content_hash")) == hashes[row["memory_id"]]:
            _set_pending(row, conn, value=False)


def _embed_chunk(vectors: VectorIndex, embedder: Embedder, chunk: Sequence[Row]) -> int:
    """Embed and upsert a chunk: the count before the first ModelUnavailable (0: upsert)."""
    out: list[VectorRow] = []
    with suppress(ModelUnavailable):
        for row in chunk:
            vector = embedder.embed_item(row["kind"], row["content"])  # type: ignore[arg-type]
            key = str(row["data"].get("content_hash"))
            out.append(VectorRow(row["memory_id"], row["layer"], row["kind"], row["status"],  # type: ignore[arg-type]
                                 key, embedder.model_name, vector))  # fmt: skip
    try:
        vectors.upsert(out)
    except ModelUnavailable:
        return 0
    if out:
        hashes = {v.memory_id: v.content_hash for v in out}
        core.run_write(partial(_clear_tx, hashes), op="memory_maintenance_backfill")
    return len(out)


def backfill(
    vectors: VectorIndex, embedder: Embedder, stamp: str, beat: Callable[[str], None]
) -> dict[str, JsonValue]:
    """Step 6: embed up to 5,000 pending items, beating every 256; ModelUnavailable stops it."""
    found = ops.maintenance_rows(selector="embedding_pending", now=stamp, limit=BACKFILL_MAX)
    embedded = 0
    for chunk in batched(cast("list[Row]", found)[:BACKFILL_MAX], BEAT_EVERY):
        done = _embed_chunk(vectors, embedder, chunk)
        embedded += done
        beat(f"memory_maintenance backfill {embedded}")
        if done < len(chunk):  # model or vector store down: retried tomorrow
            _log.warning("memory.maintenance.backfill_stopped", embedded=embedded)
            return {"embedded": embedded, "stopped": True}
    return {"embedded": embedded, "stopped": False}
