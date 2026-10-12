"""Deterministic embeddings for the clustering rows (T03-36; BT03-07, BT03-08).

No model runs: every incident of the build gets a 1024-d unit vector that is a pure function
of its row number (rows ordered by `record_id`): one of 400 planted centres (a fixed seed)
plus per-dimension Gaussian noise of `0.3 / sqrt(1024)`, and 5 % uniform noise points, the
construction of `tests/unit/enrich/_cluster_support.planted`. The vectors are written to a
LanceDB `ticket_embedding` table in chunks, keyed like the embed stage's output
(`record_id`, `content_hash` of the stored text), so the cluster stage reads them exactly as
it reads real bge-m3 vectors. The cost measured by the rows is the cluster stage, not this
preparation.
"""

from __future__ import annotations

from typing import Final

import duckdb
import numpy as np
import pyarrow as pa

from herness.store.vectors import EMBEDDING_DIM, TICKET_EMBEDDING_SCHEMA, VectorStore

__all__ = ["MODEL_ID", "planted_vectors", "write_incident_vectors"]

MODEL_ID: Final = "bench/planted@1"
CENTRES: Final = 400
SPREAD: Final = 0.3
NOISE_PERCENT: Final = 5
CHUNK_ROWS: Final = 131_072
_SEED: Final = 20261012
_SQL: Final = """SELECT i.record_id, 'incident' AS entity, i.service_id, i.opened_at,
    t.content_hash FROM core.incident AS i JOIN enrich.text_redacted AS t
    ON t.record_id = i.record_id AND t.entity = 'incident' ORDER BY i.record_id"""


def _unit(rows: np.ndarray) -> np.ndarray:
    out: np.ndarray = rows / np.linalg.norm(rows, axis=1, keepdims=True)
    return out.astype(np.float32)


def planted_vectors(offset: int, count: int) -> np.ndarray:
    """Unit vectors of rows `offset .. offset + count` (a pure function of the row number)."""
    centres = _unit(np.random.default_rng(_SEED).standard_normal((CENTRES, EMBEDDING_DIM)))
    idx = np.arange(offset, offset + count, dtype=np.uint64)
    cid = ((idx * np.uint64(2654435761)) % np.uint64(2**32) % np.uint64(CENTRES)).astype(np.int64)
    noise = ((idx * np.uint64(40503)) % np.uint64(100)) < np.uint64(NOISE_PERCENT)
    rng = np.random.default_rng((_SEED, offset))
    scale = np.float32(SPREAD / np.sqrt(EMBEDDING_DIM))
    rows = centres[cid] + scale * rng.standard_normal((count, EMBEDDING_DIM), dtype=np.float32)
    rows[noise] = rng.standard_normal((int(noise.sum()), EMBEDDING_DIM), dtype=np.float32)
    return _unit(rows)


def write_incident_vectors(wh: duckdb.DuckDBPyConnection, store: VectorStore) -> int:
    """Write the planted vectors of every incident that has a stored text; return the count.
    The store must be empty; the caller has filled `enrich.text_redacted`."""
    store.ensure_tables()
    table = store.table("ticket_embedding")
    reader = wh.cursor().execute(_SQL).to_arrow_reader(CHUNK_ROWS)
    offset = 0
    for batch in reader:
        n = batch.num_rows
        flat = pa.array(planted_vectors(offset, n).ravel(), pa.float32())
        vector = pa.FixedSizeListArray.from_arrays(flat, EMBEDDING_DIM)
        columns = {name: batch.column(name) for name in batch.schema.names}
        columns["opened_at"] = columns["opened_at"].cast(pa.timestamp("us", tz="UTC"))
        columns["model"] = pa.array([MODEL_ID] * n, pa.string())
        columns["vector"] = vector
        rows = pa.table(columns).select(TICKET_EMBEDDING_SCHEMA.names)
        table.add(rows.cast(TICKET_EMBEDDING_SCHEMA))
        offset += n
    return offset
