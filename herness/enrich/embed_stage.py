"""Embed stage: anti-join, upsert, orphan delete and index upkeep (impl 03 U03-33 ... U03-35).

Design 03 §5.2, F03-03: keeps LanceDB `ticket_embedding` in step with `enrich.text_redacted`;
each new hash is encoded once, stored hashes reuse their vector. Only redacted text reaches
the encoder (TH03-13); logs carry counts only. The caller holds the GPU class (R-43).
"""

from __future__ import annotations

import functools
import math
import re
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from lancedb.index import IvfPq
from lancedb.table import Table

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import SchemaViolation
from herness.core.jobs import JobContext
from herness.core.logging import get_logger
from herness.enrich.embed import Encoder, embed_texts
from herness.enrich.gpu import YieldRequested
from herness.store.vectors import EMBEDDING_DIM, TICKET_EMBEDDING_SCHEMA, VectorStore
from herness.store.vectors import _store_error as store_error

if TYPE_CHECKING:
    from herness.enrich.pipeline import StageReport as _Report

__all__ = ["lance_filter_in", "maintain_index", "run_embed_stage"]

type IndexAction = Literal["rebuilt", "optimized", "skipped"]

FILTER_MAX: Final = 1_000
FLUSH_BATCHES: Final = 20
INDEX_MIN_ROWS: Final = 10_000
INDEX_GROWTH: Final = 1.2
INDEX_SUB_VECTORS: Final = 64
# Pinned lancedb 0.39.0 has no table-level schema-metadata update without pylance (OI-12):
# the key lives in the field metadata of `vector`, which is part of the LanceDB schema.
INDEX_ROWS_KEY: Final = "herness.index_rows"
_TABLE: Final = "ticket_embedding"
_STAGE: Final = "embed"
_SHAPE: Final = "embedding shape or dtype does not match the vector table"
_ALLOW: Final[dict[str, re.Pattern[str]]] = {
    "record_id": re.compile(r"[a-z0-9_]{1,32}:[a-z0-9_]{1,64}:[A-Za-z0-9._-]{1,128}"),
    "content_hash": re.compile(r"[0-9a-f]{32}"),
}
_LANCE_ERRORS: Final = (OSError, RuntimeError, ValueError)
_META: Final = ["record_id", "entity", "service_id", "opened_at", "content_hash"]
_HAVE: Final = "_embed_have"  # registered Arrow view of the table's key columns
_CORE: Final = """core_rows AS (
    SELECT record_id, 'incident' AS entity, service_id, opened_at FROM core.incident
    UNION ALL
    SELECT record_id, 'change', service_id, coalesce(opened_at, planned_start, actual_start)
    FROM core.change
    UNION ALL
    SELECT record_id, 'problem', service_id, opened_at FROM core.problem)"""
_TODO_SQL: Final = f"""WITH {_CORE}
    SELECT r.record_id, r.entity, c.service_id, CAST(c.opened_at AS TIMESTAMPTZ) AS opened_at,
        r.content_hash, r.text,
        r.content_hash IN (SELECT content_hash FROM {_HAVE} WHERE model = $model) AS reuse
    FROM enrich.text_redacted AS r
    JOIN core_rows AS c ON c.record_id = r.record_id AND c.entity = r.entity
    WHERE NOT EXISTS (SELECT 1 FROM {_HAVE} AS e WHERE e.record_id = r.record_id
        AND e.content_hash = r.content_hash AND e.model = $model)
    ORDER BY r.record_id"""  # noqa: S608 - fixed view name
_ORPHAN_SQL: Final = f"""WITH {_CORE}
    SELECT DISTINCT e.record_id FROM {_HAVE} AS e
    WHERE NOT EXISTS (SELECT 1 FROM core_rows AS c WHERE c.record_id = e.record_id)
    ORDER BY 1"""  # noqa: S608 - fixed view name

_log = get_logger("enrich.embed")


# --- U03-33 --------------------------------------------------------------------------------------


def _allowed(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def lance_filter_in(column: Literal["record_id", "content_hash"], values: Sequence[str]) -> str:
    """`<column> IN ('v1', ...)` for 1-1,000 allowlisted values (U03-33, TH03-07).

    LanceDB filters take no bound parameters: each value must match its column's allowlist,
    which admits no quote. SchemaViolation otherwise; the value is never echoed.
    """
    pattern = _ALLOW.get(column)
    if pattern is None:
        msg = "invalid column for vector filter"
        raise SchemaViolation(msg)
    valid = not isinstance(values, str) and 1 <= len(values) <= FILTER_MAX
    if not valid or not all(_allowed(pattern, value) for value in values):
        msg = f"invalid {column} for vector filter"
        raise SchemaViolation(msg)
    return f"{column} IN ({', '.join(f"'{value}'" for value in values)})"


# --- U03-34 --------------------------------------------------------------------------------------


def _lance[T](action: str, call: Callable[[], T]) -> T:
    """Run a LanceDB call; conflicts and locks -> StoreBusy, other failures SchemaViolation."""
    try:
        return call()
    except _LANCE_ERRORS as exc:
        raise store_error(exc, _TABLE, action) from exc


def _chunks[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    return (items[i : i + size] for i in range(0, len(items), size))


class _Sink(NamedTuple):
    table: Table
    model: str
    report: _Report
    ctx: JobContext


def _read(table: Table, columns: list[str], where: str | None = None) -> pa.Table:
    query = table.search() if where is None else table.search().where(where)
    return _lance("read", query.select(columns).limit(None).to_arrow)


class _Buffer:
    """Rows of `meta` awaiting their upsert by `record_id`, flushed at `limit` (20 x batch)
    rows: <= 20 x 512 x 4 KiB = 40 MiB of vectors (10 MiB at batch 128) plus 5 key columns."""

    def __init__(self, meta: pa.Table, sink: _Sink, limit: int) -> None:
        self.meta, self.sink, self.limit = meta, sink, limit
        self.index: list[int] = []
        self.vectors: list[np.ndarray] = []

    def add(self, positions: Sequence[int], vector: np.ndarray) -> None:
        for position in positions:
            self.index.append(position)
            self.vectors.append(vector)
            if len(self.index) >= self.limit:
                self.flush()

    def flush(self) -> None:
        if not self.index:
            return
        rows = self.meta.take(self.index).select(_META)
        flat = pa.array(np.stack(self.vectors).reshape(-1), pa.float32())
        rows = rows.append_column("model", pa.array([self.sink.model] * rows.num_rows))
        rows = rows.append_column("vector", pa.FixedSizeListArray.from_arrays(flat, EMBEDDING_DIM))
        _lance("upsert", functools.partial(self._merge, rows.cast(TICKET_EMBEDDING_SCHEMA)))
        self.sink.report.rows += rows.num_rows
        self.index, self.vectors = [], []
        _log.info("enrich.embed.batch_flushed", rows=rows.num_rows)

    def _merge(self, rows: pa.Table) -> None:
        builder = self.sink.table.merge_insert("record_id").when_matched_update_all()
        builder.when_not_matched_insert_all().execute(rows)


def _encode(encoder: Encoder, texts: Sequence[str], batch_size: int) -> np.ndarray:
    """`embed_texts` rows aligned with `texts`; each batch is checked before any use (M2)."""
    received = 0

    def check(_index: int, rows: np.ndarray) -> None:
        nonlocal received
        received += len(rows)
        matrix = rows.ndim == 2 and rows.shape[1] == EMBEDDING_DIM  # noqa: PLR2004
        if not (matrix and np.issubdtype(rows.dtype, np.floating) and received <= len(texts)):
            raise SchemaViolation(_SHAPE)

    result = embed_texts(encoder, texts, batch_size=batch_size, on_batch=check)
    if received != len(texts) or result.shape != (len(texts), EMBEDDING_DIM):
        raise SchemaViolation(_SHAPE)
    return result


def _embed_new(new: pa.Table, encoder: Encoder, sink: _Sink, batch_size: int) -> None:
    """Encode each new hash once, shortest first, one `embed_texts` call (whose result is
    input-aligned) per 20-batch window; checkpoint per window: flush, heartbeat, yield."""
    first: dict[str, int] = {}
    groups: dict[str, list[int]] = {}
    for position, digest in enumerate(new.column("content_hash").to_pylist()):
        first.setdefault(digest, position)
        groups.setdefault(digest, []).append(position)
    texts = new.column("text").to_pylist()
    hashes = sorted(first, key=lambda digest: len(texts[first[digest]]))
    window = FLUSH_BATCHES * batch_size
    buffer = _Buffer(new, sink, window)
    for chunk in _chunks(hashes, window):
        vectors = _encode(encoder, [texts[first[digest]] for digest in chunk], batch_size)
        for digest, vector in zip(chunk, vectors, strict=True):
            buffer.add(groups[digest], vector)
        sink.report.embedded += len(chunk)
        if len(chunk) == window:
            buffer.flush()
            sink.ctx.heartbeat(_STAGE)
            if sink.ctx.should_yield():
                raise YieldRequested(_STAGE)
    buffer.flush()


def _read_vectors(sink: _Sink, hashes: Sequence[str]) -> dict[str, np.ndarray]:
    """Stored vectors of the encoder's model by hash, in chunks of 1,000 hashes."""
    found: dict[str, np.ndarray] = {}
    for chunk in _chunks(hashes, FILTER_MAX):
        where = lance_filter_in("content_hash", chunk)
        part = _read(sink.table, ["content_hash", "model", "vector"], where)
        part = part.filter(pc.equal(part.column("model"), sink.model))
        matrix = part.column("vector").combine_chunks().flatten().to_numpy()
        rows = matrix.reshape(-1, EMBEDDING_DIM).copy()
        found.update(zip(part.column("content_hash").to_pylist(), rows, strict=True))
    return found


def _plan(wh: duckdb.DuckDBPyConnection, have: pa.Table, model: str) -> tuple[pa.Table, list[str]]:
    """Rows to write and orphan ids with U03-33-allowlisted `record_id`s only: others could
    never be deleted by filter (spec note: make_record_id is wider); WARNING with counts."""
    wh.register(_HAVE, have)
    try:
        todo = wh.execute(_TODO_SQL, {"model": model}).to_arrow_table()
        orphans = wh.execute(_ORPHAN_SQL).to_arrow_table().column("record_id").to_pylist()
    except duckdb.Error as exc:  # the class only: messages may quote row values
        msg = f"embed stage: {type(exc).__name__}"
        raise SchemaViolation(msg) from None
    finally:
        wh.unregister(_HAVE)
    ids = _ALLOW["record_id"]
    mask = [_allowed(ids, record_id) for record_id in todo.column("record_id").to_pylist()]
    ok = todo.filter(pa.array(mask, pa.bool_()))
    kept = [record_id for record_id in orphans if _allowed(ids, record_id)]
    records, others = todo.num_rows - ok.num_rows, len(orphans) - len(kept)
    if records or others:
        _log.warning("enrich.embed.ids_skipped", records=records, orphans=others)
    return ok, kept


def run_embed_stage(
    wh: duckdb.DuckDBPyConnection, *, encoder: Encoder, ctx: JobContext, report: _Report
) -> None:
    """Embed new hashes, reuse stored ones, delete orphans, maintain the index (U03-34).

    Caller inside `ctx.gpu_scope("decider")` (R-43). `YieldRequested("embed")` after a
    checkpoint flush; StoreBusy on a LanceDB conflict or lock (retry policy `embed_batch`).
    """
    started = clock.monotonic()
    store = VectorStore()
    store.ensure_tables()
    table = store.table(_TABLE)
    have = _read(table, ["record_id", "content_hash", "model"])
    sink = _Sink(table, encoder.model_id, report, ctx)
    todo, orphans = _plan(wh, have, sink.model)
    # Step 5 before any write: a hash whose stored row is gone by now is encoded instead.
    flagged = todo.column("reuse")
    stored = _read_vectors(sink, pc.unique(todo.filter(flagged)["content_hash"]).to_pylist())
    known = pc.is_in(todo.column("content_hash"), pa.array(list(stored), pa.string()))
    ready = pc.and_(flagged, known)
    batch_size = get_config().decisions.embedding.batch_size
    _embed_new(todo.filter(pc.invert(ready)), encoder, sink, batch_size)
    reuse = todo.filter(ready)
    buffer = _Buffer(reuse, sink, FLUSH_BATCHES * batch_size)
    for position, digest in enumerate(reuse.column("content_hash").to_pylist()):
        buffer.add((position,), stored[digest])
    buffer.flush()
    report.cache_hits += reuse.num_rows
    for chunk in _chunks(orphans, FILTER_MAX):
        _lance("delete", functools.partial(table.delete, lance_filter_in("record_id", chunk)))
    action = maintain_index(table)
    # T08-05: herness_enrich_embeddings_total += report.embedded
    _log.info(
        "enrich.embed.completed",
        embedded=report.embedded,
        cache_hits=report.cache_hits,
        rows=report.rows,
        deleted=len(orphans),
        index_rebuilt=int(action == "rebuilt"),
        duration_ms=round((clock.monotonic() - started) * 1000),
    )


# --- U03-35 --------------------------------------------------------------------------------------


def _index_rows(table: Table) -> int | None:
    """Row count at the last index build (field metadata of `vector`); None: never built."""
    value = (table.schema.field("vector").metadata or {}).get(INDEX_ROWS_KEY.encode(), b"")
    return int(value) if value.isdigit() else None


def maintain_index(table: Table) -> IndexAction:
    """Keep the IVF_PQ cosine index fresh (U03-35); StoreBusy / SchemaViolation on errors."""
    rows = _lance("count", table.count_rows)
    built = _lance("index", lambda: _index_rows(table))
    action: IndexAction = "skipped"
    if rows >= INDEX_MIN_ROWS and (built is None or rows > INDEX_GROWTH * built):
        config = IvfPq(
            distance_type="cosine",
            num_partitions=round(math.sqrt(rows)),
            num_sub_vectors=INDEX_SUB_VECTORS,
        )
        _lance("index", lambda: table.create_index("vector", config=config, replace=True))
        update = {"path": "vector", "metadata": {INDEX_ROWS_KEY: str(rows)}}
        _lance("index", lambda: table.update_field_metadata(update))
        action = "rebuilt"
    elif rows >= INDEX_MIN_ROWS:
        _lance("optimize", table.optimize)
        action = "optimized"
    _log.info("enrich.embed.index_maintained", action=action, rows=rows)
    return action
