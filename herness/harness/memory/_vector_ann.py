"""ANN index upkeep and indexed search of ``memory_embedding`` (impl 07 U07-49, T07-27).

Private sibling of ``store.py`` (§2 row); only ``store.py`` imports it, and the maintenance job
reaches it through ``VectorIndex.refresh_index`` (U07-96 step 8), never a request path. The
index policy is spec 03's (U03-35, ``herness.enrich.embed_stage.maintain_index``): IVF_PQ,
cosine, ``round(sqrt(rows))`` partitions, 64 sub-vectors; an index is never built below 10,000
rows (flat search); a build when there is none and a rebuild when the table holds more than 1.2x
the rows recorded at the last build (field metadata of ``vector``). Every other refresh, at any
row count (also below the threshold, and after an indexed table shrank below it), runs
``optimize()``: it folds newer rows into an existing index, compacts deleted rows and prunes
table versions older than LanceDB's default 7 days, so purged vectors leave the files (TH07-21).
The constants are mirrored, not imported: importing ``embed_stage``
would load ``herness.enrich.embed`` with the store (UT07-22); UT07-91 pins them equal.

Search probes at least ``NPROBES`` partitions and more, up to all of them, when a narrow
layer/status prefilter leaves fewer than ``limit`` rows there; ``REFINE_FACTOR`` x ``limit``
candidates are re-ranked on exact cosine distances, so the returned distances are exact (the
near-duplicate thresholds of U07-50 compare them). Rows written after the last build are
searched flat next to the index. Recall guard: recall@10 >= 0.90 against flat search (UT07-92).
"""

from __future__ import annotations

import math
import threading
from typing import Final, Literal, TypedDict, cast

import numpy as np
from lancedb.index import IvfPq
from lancedb.query import LanceVectorQueryBuilder
from lancedb.table import Table

from herness.core import time as clock
from herness.core.logging import get_logger

__all__ = ["IndexAction", "IndexRefresh", "nearest", "refresh"]

MIN_ROWS: Final = 10_000  # spec 03 INDEX_MIN_ROWS: below it every search is flat
GROWTH: Final = 1.2  # spec 03 INDEX_GROWTH: rebuild past 1.2x the rows of the last build
SUB_VECTORS: Final = 64  # spec 03 INDEX_SUB_VECTORS
ROWS_KEY: Final = "herness.index_rows"  # spec 03 INDEX_ROWS_KEY (field metadata of `vector`)
NPROBES: Final = 20  # partitions always probed (of round(sqrt(rows)))
REFINE_FACTOR: Final = 10  # exact re-rank of 10 x limit PQ candidates

type IndexAction = Literal["built", "rebuilt", "optimized"]
_log = get_logger("memory")


class IndexRefresh(TypedDict):
    """What ``refresh`` did and the table's row count."""

    action: IndexAction
    rows: int


def nearest(table: Table, query: np.ndarray, where: str, limit: int) -> list[tuple[str, float]]:
    """(memory_id, cosine distance) nearest first among the rows matching ``where``."""
    builder = cast("LanceVectorQueryBuilder", table.search(query)).distance_type("cosine")
    builder = builder.where(where, prefilter=True).minimum_nprobes(NPROBES).maximum_nprobes(0)
    builder = builder.refine_factor(REFINE_FACTOR).select(["memory_id", "_distance"])
    found = builder.limit(limit).to_arrow()
    pairs = zip(found["memory_id"].to_pylist(), found["_distance"].to_pylist(), strict=True)
    return [(str(i), float(d)) for i, d in pairs]


def _built_rows(table: Table) -> int | None:
    """Rows recorded at the last build; None: no index on ``vector``; 0: none recorded."""
    if not any("vector" in index.columns for index in table.list_indices()):
        return None
    value = (table.schema.field("vector").metadata or {}).get(ROWS_KEY.encode(), b"")
    return int(value) if value.isdigit() else 0


def refresh(table: Table, write_lock: threading.Lock) -> IndexRefresh:
    """Build or rebuild the index per the spec 03 policy, else optimize (maintenance step 8).

    Training runs outside ``write_lock`` (it reads one table version; upserts may go on and
    are searched flat until the next refresh); the metadata stamp and ``optimize()``, which
    rewrite table files, hold it. LanceDB errors propagate to the caller's guard.
    """
    started = clock.monotonic()
    rows = table.count_rows()
    built = _built_rows(table)
    if rows < MIN_ROWS or (built is not None and rows <= GROWTH * built):
        with write_lock:  # version cleanup at any row count (TH07-21); never a build below MIN
            table.optimize()
        return {"action": "optimized", "rows": rows}
    config = IvfPq(
        distance_type="cosine",
        num_partitions=round(math.sqrt(rows)),
        num_sub_vectors=SUB_VECTORS,
    )
    table.create_index("vector", config=config, replace=True)
    with write_lock:
        table.update_field_metadata({"path": "vector", "metadata": {ROWS_KEY: str(rows)}})
    action: IndexAction = "built" if built is None else "rebuilt"
    duration_ms = round((clock.monotonic() - started) * 1000)
    _log.info("memory.vector.index_built", action=action, rows=rows, duration_ms=duration_ms)
    return {"action": action, "rows": rows}
