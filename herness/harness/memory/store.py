"""Memory embeddings and the LanceDB ``memory_embedding`` adapter (impl 07 U07-48, U07-49).

Filters use allowlisted values only (TH07-09); memory text and vectors are never logged."""

import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import batched
from typing import Final, cast, get_args

import numpy as np
import pyarrow as pa
from lancedb.query import LanceVectorQueryBuilder
from lancedb.table import Table

from herness import enrich
from herness.core import errors as herr
from herness.core.errors import ConfigError, ModelUnavailable, ToolInputError
from herness.core.ids import sha256_hex
from herness.core.types import Kind, Layer, Status
from herness.harness.memory.types import MEMORY_ID_RE
from herness.store.vectors import EMBEDDING_DIM, MEMORY_EMBEDDING_SCHEMA, VectorStore

__all__ = ["Embedder", "VectorIndex", "VectorMeta", "VectorRow"]

type _EmbedFn = Callable[[str], np.ndarray]
# Allowlist patterns (fullmatch) for every filter value and every string column (TH07-09).
_KINDS, _LAYERS, _STATUSES = (re.compile("|".join(get_args(t))) for t in (Kind, Layer, Status))
_TEXT: Final = re.compile(r".{1,256}", re.DOTALL)
_ROW_COLUMNS: Final = MEMORY_EMBEDDING_SCHEMA.names[:6]  # the string columns, in order
_ROW_RULES: Final = (MEMORY_ID_RE, _LAYERS, _KINDS, _STATUSES, _TEXT, _TEXT)
_TABLE: Final = "memory_embedding"
_CHUNK: Final = 200  # ids per filter, and the largest search limit
_LIST_MAX: Final = 1000  # maintenance pages ids 1,000 at a time (U07-96 step 5)
# lancedb/pyarrow errors and VectorStore's mapping of them: "vector path unavailable" (U07-49)
_LANCE_ERRORS: Final = (OSError, ValueError, RuntimeError)
_STORE_ERRORS: Final = (*_LANCE_ERRORS, herr.SchemaViolation, herr.StoreBusy, herr.NotFound)
_BAD_FILTER: Final = "invalid id in vector filter"


class Embedder:
    """Memory and query embeddings with an LRU cache keyed by the text's SHA-256 (U07-48)."""

    def __init__(
        self, embed_fn: _EmbedFn | None = None, *, model_name: str, cache_size: int = 2048
    ) -> None:
        if cache_size < 1:
            msg = "memory embedding cache_size must be at least 1"
            raise ConfigError(msg)
        self._embed_fn, self.model_name, self._cache_size = embed_fn, model_name, cache_size
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()

    def embed(self, text: str) -> np.ndarray:
        """Return the float32 (1024,) L2-normalized, read-only embedding of ``text``."""
        key = sha256_hex(text)
        with self._lock:
            if (hit := self._cache.get(key)) is not None:
                self._cache.move_to_end(key)
                return hit
        try:  # outside the lock; embed_fn None: herness.enrich.embed_query (lazy facade)
            raw: object = (self._embed_fn or enrich.embed_query)(text)
        except (OSError, RuntimeError, ValueError, MemoryError) as exc:
            msg = "memory embedding failed"
            raise ModelUnavailable(msg, error_type=type(exc).__name__) from exc
        if (vector := _unit_vector(raw)) is None:
            msg = "memory embedding invalid output"
            raise ModelUnavailable(msg)
        with self._lock:
            self._cache[key] = vector
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return vector

    def embed_item(self, kind: Kind, content: str) -> np.ndarray:
        """Embed a memory item as ``kind + ": " + content``."""
        if not _ok(kind, _KINDS):
            msg = "unknown memory kind"
            raise ToolInputError(msg)
        return self.embed(f"{kind}: {content}")


def _unit_vector(raw: object) -> np.ndarray | None:
    # A read-only float32 unit copy of a finite non-zero float (1024,) array, else None.
    if not isinstance(raw, np.ndarray) or raw.shape != (EMBEDDING_DIM,) or raw.dtype.kind != "f":
        return None
    wide = raw.astype(np.float64)
    peak = float(np.abs(wide).max()) if np.isfinite(wide).all() else 0.0
    if peak == 0.0:
        return None
    vector = (wide / peak / np.linalg.norm(wide / peak)).astype(np.float32)  # peak: no overflow
    vector.flags.writeable = False
    return vector


@dataclass(frozen=True, slots=True)
class VectorRow:
    """One row of ``memory_embedding``; ``vector`` is a (1024,) finite non-zero float array."""

    memory_id: str
    layer: Layer
    kind: Kind
    status: Status
    content_hash: str
    model: str
    vector: np.ndarray


@dataclass(frozen=True, slots=True)
class VectorMeta:
    """The non-vector columns maintenance compares against SQLite."""

    memory_id: str
    status: Status
    content_hash: str
    model: str


def _ok(value: object, allowed: re.Pattern[str] = MEMORY_ID_RE) -> bool:
    return isinstance(value, str) and allowed.fullmatch(value) is not None


def _filter_values(values: Sequence[str], allowed: re.Pattern[str] = MEMORY_ID_RE) -> list[str]:
    """Distinct values in order; ToolInputError before LanceDB on any value outside the set."""
    if isinstance(values, str) or not all(_ok(v, allowed) for v in values):
        raise ToolInputError(_BAD_FILTER)
    return list(dict.fromkeys(values))


def _in(column: str, values: Sequence[str]) -> str:
    # Values passed _filter_values: memory ids or literal-set members, never a quote.
    return f"{column} IN ({', '.join(f"'{v}'" for v in values)})"


def _check_limit(limit: int, maximum: int) -> None:
    if type(limit) is not int or not 1 <= limit <= maximum:  # bool is rejected too
        msg = f"vector limit must be an integer from 1 to {maximum}"
        raise ToolInputError(msg)


def _rows_table(rows: Sequence[VectorRow]) -> pa.Table:
    for row in cast("Sequence[object]", rows):
        valid = isinstance(row, VectorRow) and _unit_vector(row.vector) is not None
        rules = zip(_ROW_COLUMNS, _ROW_RULES, strict=True)
        if not valid or not all(_ok(getattr(row, c), a) for c, a in rules):
            msg = "invalid vector row"
            raise ToolInputError(msg)
    latest = list({r.memory_id: r for r in rows}.values())  # last row per id wins
    columns = {c: pa.array([getattr(r, c) for r in latest], pa.string()) for c in _ROW_COLUMNS}
    flat = pa.array(np.concatenate([np.asarray(r.vector, dtype=np.float32) for r in latest]))
    columns["vector"] = pa.FixedSizeListArray.from_arrays(flat, EMBEDDING_DIM)
    return pa.Table.from_pydict(columns, schema=MEMORY_EMBEDDING_SCHEMA)


@contextmanager
def _guard(op: str) -> Iterator[None]:
    try:
        yield
    except _STORE_ERRORS as exc:
        msg = f"memory vector store unavailable: {op}"
        raise ModelUnavailable(msg, error_type=type(exc).__name__) from exc


class VectorIndex:
    """The only accessor of the LanceDB ``memory_embedding`` table (U07-49), opened on first use."""

    def __init__(self, store_factory: Callable[[], VectorStore] = VectorStore) -> None:
        self._factory = store_factory
        self._store: VectorStore | None = None
        self._table: Table | None = None
        self._write_lock = threading.Lock()  # reads take no lock; they check out the latest version

    def ensure_table(self) -> None:
        """Create or verify ``memory_embedding`` and keep the table handle."""
        with _guard("ensure_table"):
            store = self._store if self._store is not None else self._factory()
            store.ensure_tables()
            self._store, self._table = store, store.table(_TABLE)

    def _tbl(self, *, latest: bool = False) -> Table:
        if self._table is None:
            self.ensure_table()
        elif latest:
            self._table.checkout_latest()  # type: ignore[no-untyped-call]
        return cast("Table", self._table)

    def upsert(self, rows: Sequence[VectorRow]) -> None:
        """Insert or replace rows by ``memory_id`` (merge_insert); every row is validated first."""
        if rows:
            data, table = _rows_table(rows), self._tbl()
            with self._write_lock, _guard("upsert"):
                merge = table.merge_insert("memory_id")
                merge.when_matched_update_all().when_not_matched_insert_all().execute(data)

    def set_status(self, memory_ids: Sequence[str], status: Status) -> None:
        """Mirror ``status`` onto the given ids, 200 ids per update."""
        if not _ok(status, _STATUSES):
            raise ToolInputError(_BAD_FILTER)
        if ids := _filter_values(memory_ids):
            table = self._tbl()
            with self._write_lock, _guard("set_status"):
                for chunk in batched(ids, _CHUNK):
                    table.update(where=_in("memory_id", chunk), values={"status": status})

    def delete(self, memory_ids: Sequence[str]) -> None:
        """Delete the given ids through ``VectorStore.delete_ids``, 200 ids per call."""
        if ids := _filter_values(memory_ids):
            self._tbl()
            with self._write_lock, _guard("delete"):
                for chunk in batched(ids, _CHUNK):
                    cast("VectorStore", self._store).delete_ids(_TABLE, "memory_id", chunk)

    def search(
        self, vector: np.ndarray, layers: Sequence[Layer], statuses: Sequence[Status], limit: int
    ) -> list[tuple[str, float]]:
        """(memory_id, cosine distance) nearest first, among rows of the given layers/statuses."""
        _check_limit(limit, _CHUNK)
        if (query := _unit_vector(vector)) is None:
            msg = "invalid query vector"
            raise ToolInputError(msg)
        layer_values = _filter_values(layers, _LAYERS)
        status_values = _filter_values(statuses, _STATUSES)
        if not layer_values or not status_values:
            return []
        where = f"{_in('layer', layer_values)} AND {_in('status', status_values)}"
        with _guard("search"):  # distance_type is the current name of the spec's metric()
            builder = cast("LanceVectorQueryBuilder", self._tbl(latest=True).search(query))
            builder = builder.distance_type("cosine").where(where, prefilter=True)
            found = builder.select(["memory_id", "_distance"]).limit(limit).to_arrow()
            pairs = zip(found["memory_id"].to_pylist(), found["_distance"].to_pylist(), strict=True)
        return [(str(i), float(d)) for i, d in pairs]

    def vectors(self, memory_ids: Sequence[str]) -> dict[str, np.ndarray]:
        """Read-only float32 vectors of the ids present in the table (200 ids per scan)."""
        ids, out = _filter_values(memory_ids), dict[str, np.ndarray]()
        with _guard("vectors"):
            for index, chunk in enumerate(batched(ids, _CHUNK)):
                query = self._tbl(latest=index == 0).search().where(_in("memory_id", chunk))
                found = query.select(["memory_id", "vector"]).limit(len(chunk)).to_arrow()
                flat = found["vector"].combine_chunks().flatten().to_numpy(zero_copy_only=False)
                matrix = np.array(flat, dtype=np.float32).reshape(-1, EMBEDDING_DIM)
                matrix.flags.writeable = False
                out.update(zip(map(str, found["memory_id"].to_pylist()), matrix, strict=True))
        return out

    def list_ids(self, after: str, limit: int) -> list[VectorMeta]:
        """Up to ``limit`` rows with ``memory_id > after`` in id order; ``""`` starts the scan."""
        _check_limit(limit, _LIST_MAX)
        if after != "" and not _ok(after):
            raise ToolInputError(_BAD_FILTER)
        columns = ["memory_id", "status", "content_hash", "model"]
        with _guard("list_ids"):  # every memory_id is > '' so "" needs no special case
            query = self._tbl(latest=True).search().where(f"memory_id > '{after}'")
            rows = query.select(columns).limit(None).to_arrow().to_pylist()
        rows.sort(key=lambda r: str(r["memory_id"]))
        return [VectorMeta(*(r[c] for c in columns)) for r in rows[:limit]]
