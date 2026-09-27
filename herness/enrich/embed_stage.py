"""Embed stage: anti-join, upsert, orphan delete and index upkeep (impl 03 U03-33 ... U03-35).

Design 03 §5.2, flow F03-03. Keeps LanceDB `ticket_embedding` in step with
`enrich.text_redacted`: a record whose `(record_id, content_hash, model)` row is missing is
upserted by `record_id`; a hash already stored for the encoder's model reuses its vector, and
each new hash is encoded once. Rows of records gone from `core.*` are deleted and the IVF_PQ
index is kept fresh. Only `enrich.text_redacted.text` reaches the encoder (TH03-13); logs
carry counts only. The caller holds the GPU class (`ctx.gpu_scope("decider")`, R-43): this
module takes no GPU lock.
"""

from __future__ import annotations

import functools
import math
import re
from collections.abc import Callable, Iterator, Sequence
from typing import Final, Literal, Protocol

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


class _Report(Protocol):
    """Counters the stage mutates.

    T03-28 pipeline: retype to StageReport (U03-142, herness.enrich.pipeline).
    """

    embedded: int
    cache_hits: int
    rows: int


# --- U03-33 --------------------------------------------------------------------------------------


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
    if not valid or not all(pattern.fullmatch(value) for value in values):
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


class _Sink:
    """Upserts rows by `record_id` (`merge_insert`), counts them; holds the job context."""

    def __init__(self, table: Table, model: str, report: _Report, ctx: JobContext) -> None:
        self.table, self.model, self.report, self.ctx = table, model, report, ctx

    def upsert(self, meta: pa.Table, vectors: np.ndarray) -> int:
        """Write `meta` rows (`_META` columns) with `vectors`; returns the rows written."""
        flat = pa.array(vectors.reshape(-1), pa.float32())
        rows = meta.select(_META).append_column(
            "model", pa.array([self.model] * meta.num_rows, pa.string())
        )
        rows = rows.append_column("vector", pa.FixedSizeListArray.from_arrays(flat, EMBEDDING_DIM))
        rows = rows.cast(TICKET_EMBEDDING_SCHEMA)
        _lance("upsert", lambda: self._merge(rows))
        count: int = rows.num_rows
        self.report.rows += count
        return count

    def _merge(self, rows: pa.Table) -> None:
        builder = self.table.merge_insert("record_id").when_matched_update_all()
        builder.when_not_matched_insert_all().execute(rows)


class _NewRows:
    """`embed_texts` callback: maps each vector to every record sharing its hash.

    Hashes are fed shortest first, as `embed_texts` orders them (a stable sort, so its own
    sort keeps this order); the cursor walks them batch by batch. Every 20 batches it
    checkpoints: flush, heartbeat, then `YieldRequested` when the job must yield. The buffer
    holds at most 20 x batch_size rows: <= 20 x 512 x 4 KiB = 40 MiB of vectors (10 MiB at
    the default batch 128), plus their five key columns.
    """

    def __init__(self, meta: pa.Table, groups: list[list[int]], sink: _Sink, limit: int) -> None:
        self.meta, self.groups, self.sink, self.limit = meta, groups, sink, limit
        self.cursor = 0
        self.batches = 0
        self.index: list[int] = []
        self.vectors: list[np.ndarray] = []

    def on_batch(self, _index: int, rows: np.ndarray) -> None:
        """Check the batch's shape before buffering its rows; flush at the row bound."""
        remaining = len(self.groups) - self.cursor
        if not (
            rows.ndim == 2  # noqa: PLR2004 - a matrix
            and 0 < rows.shape[0] <= remaining
            and rows.shape[1] == EMBEDDING_DIM
            and np.issubdtype(rows.dtype, np.floating)
        ):
            msg = "embedding shape or dtype does not match the vector table"
            raise SchemaViolation(msg)
        for vector in rows:
            for position in self.groups[self.cursor]:
                self.index.append(position)
                self.vectors.append(vector)
                if len(self.index) >= self.limit:
                    self.flush()
            self.cursor += 1
        self.sink.report.embedded += rows.shape[0]
        self.batches += 1
        if self.batches % FLUSH_BATCHES == 0:
            self.flush()
            self.sink.ctx.heartbeat(_STAGE)
            if self.sink.ctx.should_yield():
                raise YieldRequested(_STAGE)

    def flush(self) -> None:
        if self.index:
            rows = self.sink.upsert(self.meta.take(self.index), np.stack(self.vectors))
            self.index, self.vectors = [], []
            _log.info("enrich.embed.batch_flushed", rows=rows)


def _embed_new(new: pa.Table, encoder: Encoder, sink: _Sink) -> None:
    """Encode each distinct new hash once, shortest first, then flush the rest."""
    first: dict[str, int] = {}
    groups: dict[str, list[int]] = {}
    for position, digest in enumerate(new.column("content_hash").to_pylist()):
        first.setdefault(digest, position)
        groups.setdefault(digest, []).append(position)
    texts = new.column("text").to_pylist()
    hashes = sorted(first, key=lambda digest: len(texts[first[digest]]))
    batch_size = get_config().decisions.embedding.batch_size
    window = FLUSH_BATCHES * batch_size
    state = _NewRows(new, [groups[digest] for digest in hashes], sink, window)
    # One embed_texts call per 20-batch window keeps its result matrix bounded as well.
    for chunk in _chunks(hashes, window):
        chunk_texts = [texts[first[digest]] for digest in chunk]
        embed_texts(encoder, chunk_texts, batch_size=batch_size, on_batch=state.on_batch)
    state.flush()


def _upsert_reuse(reuse: pa.Table, sink: _Sink) -> None:
    """Upsert records whose hash is stored for the model, with the stored vector."""
    hashes = list(dict.fromkeys(reuse.column("content_hash").to_pylist()))
    wanted = reuse.column("content_hash")
    for chunk in _chunks(hashes, FILTER_MAX):
        query = sink.table.search().where(lance_filter_in("content_hash", chunk))
        found = _lance(
            "read", query.select(["content_hash", "model", "vector"]).limit(None).to_arrow
        )
        found = found.filter(pc.equal(found.column("model"), sink.model))
        matrix = found.column("vector").combine_chunks().flatten().to_numpy()
        matrix = matrix.reshape(-1, EMBEDDING_DIM)
        row_of = {digest: i for i, digest in enumerate(found.column("content_hash").to_pylist())}
        rows = reuse.filter(pc.is_in(wanted, pa.array(chunk, pa.string())))
        picks = [row_of[digest] for digest in rows.column("content_hash").to_pylist()]
        sink.report.cache_hits += sink.upsert(rows, matrix[picks])


def _query(wh: duckdb.DuckDBPyConnection, sql: str, model: str | None = None) -> pa.Table:
    try:
        return wh.execute(sql, None if model is None else {"model": model}).to_arrow_table()
    except duckdb.Error as exc:  # the class only: messages may quote row values
        msg = f"embed stage: {type(exc).__name__}"
        raise SchemaViolation(msg) from None


def run_embed_stage(
    wh: duckdb.DuckDBPyConnection, *, encoder: Encoder, ctx: JobContext, report: _Report
) -> None:
    """Embed new hashes, reuse stored ones, delete orphans, maintain the index (U03-34).

    Precondition: the caller is inside `ctx.gpu_scope("decider")` (R-43) with the encoder
    loaded. Raises `YieldRequested("embed")` after a checkpoint flush when `ctx` asks to
    yield; StoreBusy on a LanceDB conflict or lock (the caller retries, policy `embed_batch`).
    """
    started = clock.monotonic()
    store = VectorStore()
    store.ensure_tables()
    table = store.table(_TABLE)
    have = _lance(
        "read", table.search().select(["record_id", "content_hash", "model"]).limit(None).to_arrow
    )
    sink = _Sink(table, encoder.model_id, report, ctx)
    wh.register(_HAVE, have)
    try:
        todo = _query(wh, _TODO_SQL, sink.model)
        orphans = _query(wh, _ORPHAN_SQL).column("record_id").to_pylist()
    finally:
        wh.unregister(_HAVE)
    is_reuse = todo.column("reuse")
    before = (report.embedded, report.cache_hits, report.rows)
    _embed_new(todo.filter(pc.invert(is_reuse)), encoder, sink)
    _upsert_reuse(todo.filter(is_reuse), sink)
    for chunk in _chunks(orphans, FILTER_MAX):
        _lance("delete", functools.partial(table.delete, lance_filter_in("record_id", chunk)))
    action = maintain_index(table)
    # T08-05: herness_enrich_embeddings_total += report.embedded - before[0]
    _log.info(
        "enrich.embed.completed",
        embedded=report.embedded - before[0],
        cache_hits=report.cache_hits - before[1],
        rows=report.rows - before[2],
        deleted=len(orphans),
        index_rebuilt=int(action == "rebuilt"),
        duration_ms=round((clock.monotonic() - started) * 1000),
    )


# --- U03-35 --------------------------------------------------------------------------------------


def _index_rows(table: Table) -> int | None:
    """Row count at the last index build (field metadata of `vector`); None: never built."""
    meta = table.schema.field("vector").metadata or {}
    value = meta.get(INDEX_ROWS_KEY.encode())
    return int(value) if value is not None and value.isdigit() else None


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
