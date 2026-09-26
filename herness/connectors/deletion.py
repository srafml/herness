"""In-memory deletion set and batch filter (impl 01 U01-33, design 01 §5.6, R-10, TH01-11).

`DeletionFilter` is reloaded before every sync run and at each checkpoint, so a record
placed under a `running`/`done` deletion request stops being re-ingested a few thousand
rows later at the worst case, never at the end of the run.
"""

from __future__ import annotations

import threading

import pyarrow as pa
import pyarrow.compute as pc

from herness.core.errors import SchemaViolation
from herness.store.ops.privacy import deleted_record_ids


class DeletionFilter:
    """Drops records under an open deletion request before `LakeWriter.write` (U01-33).

    `apply` before any `reload` raises `SchemaViolation`, so a missed reload can never let
    rows through. `reload` swaps the loaded set under a lock; `apply` reads the current
    set once per call, so concurrent `apply` calls are safe. Dropped ids are never logged,
    only counts (TH01-11).
    """

    def __init__(self, source: str, entity: str) -> None:
        self._source = source
        self._entity = entity
        self._lock = threading.Lock()
        self._ids: pa.StringArray | None = None

    def reload(self) -> int:
        """Load the current deletion set and return its size."""
        ids = pa.array(deleted_record_ids(self._source, self._entity), type=pa.string())
        with self._lock:
            self._ids = ids
        return len(ids)

    def apply(self, batch: pa.RecordBatch) -> tuple[pa.RecordBatch, int]:
        """Drop rows whose `_record_id` is in the loaded set; return (kept, dropped).

        Raises SchemaViolation when no `reload` has run yet.
        """
        with self._lock:
            ids = self._ids
        if ids is None:
            msg = "deletion set not loaded"
            raise SchemaViolation(msg, source=self._source, entity=self._entity)
        if len(ids) == 0:
            return batch, 0
        mask = pc.invert(pc.is_in(batch["_record_id"], value_set=ids))
        filtered = batch.filter(mask)
        return filtered, batch.num_rows - filtered.num_rows

    @property
    def ids(self) -> pa.StringArray:
        """The loaded deletion set (reconciliation excludes it, U01-44); empty before `reload`."""
        with self._lock:
            return pa.array([], pa.string()) if self._ids is None else self._ids

    @property
    def size(self) -> int:
        """Current deletion-set size; 0 before the first `reload`."""
        with self._lock:
            return 0 if self._ids is None else len(self._ids)
