"""Privacy deletion of one record from enrichment's stores (U03-145; spec 10 §5.5 step 3).

Removes the record's `ticket_embedding` rows, the decision-cache and label rows of its hashes
that no other live record shares, its own label rows, and its pair index rows with the cache
and label rows of their pair hashes. Memory items are spec 07's (`MemoryStore.purge`).
"""

from __future__ import annotations

import contextlib
import functools
import re
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Final

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from lancedb.table import Table

from herness.core.config import get_config
from herness.core.errors import SchemaViolation, StoreBusy
from herness.core.logging import get_logger
from herness.enrich.cache import replace_atomic
from herness.enrich.cache_maint import purge_hashes
from herness.enrich.embed_stage import FILTER_MAX, lance_filter_in
from herness.enrich.layout import EnrichPaths
from herness.store.errors import NotFoundError
from herness.store.vectors import VectorStore
from herness.store.vectors import _store_error as store_error
from herness.store.warehouse import open_readonly

__all__ = ["purge_record"]

_TABLE: Final = "ticket_embedding"
_HASH_RE: Final = re.compile(r"[0-9a-f]{32}")
_LABEL_DIRS: Final = ("teacher", "human", "gold", "gold/_reviews")
_PART_GLOB: Final = "part-*.parquet"
_STALE_GLOB: Final = ".part-*.parquet.tmp"
_LANCE_ERRORS: Final = (OSError, RuntimeError, ValueError)
_WH_HASHES: Final = "SELECT DISTINCT content_hash FROM enrich.text_redacted WHERE record_id = ?"
_WH_SHARED: Final = """SELECT DISTINCT content_hash FROM enrich.text_redacted
    WHERE record_id <> ? AND list_contains(?, content_hash)"""

_log = get_logger("enrich.purge")


@contextlib.contextmanager
def _io(what: str) -> Iterator[None]:
    """OS errors -> StoreBusy (spec 10 retries the step); the message names no path."""
    try:
        yield
    except OSError as exc:
        msg = f"cannot purge {what}"
        raise StoreBusy(msg, error_type=type(exc).__name__) from exc
    except pa.ArrowInvalid as exc:
        msg = f"unreadable {what} part"
        raise SchemaViolation(msg) from exc


def _lance[T](action: str, call: Callable[[], T]) -> T:
    try:
        return call()
    except _LANCE_ERRORS as exc:
        raise store_error(exc, _TABLE, action) from exc


def _hashes(values: Iterable[object]) -> set[str]:
    """The values that are well-formed content hashes (only those reach a filter)."""
    return {v for v in values if isinstance(v, str) and _HASH_RE.fullmatch(v) is not None}


def _read(table: Table, where: str, columns: list[str]) -> pa.Table:
    query = table.search().where(where).select(columns).limit(None)
    return _lance("read", query.to_arrow)


@contextlib.contextmanager
def _current_warehouse() -> Iterator[duckdb.DuckDBPyConnection | None]:
    """The `CURRENT` build read-only (T02-09), or None when there is none."""
    try:
        con: duckdb.DuckDBPyConnection | None = open_readonly(None)
    except NotFoundError:
        con = None
    try:
        yield con
    except duckdb.Error as exc:
        msg = "cannot read the current warehouse for purge"
        raise StoreBusy(msg, error_type=type(exc).__name__) from exc
    finally:
        if con is not None:
            con.close()


def _query(wh: duckdb.DuckDBPyConnection | None, sql: str, params: list[object]) -> set[str]:
    if wh is None:
        return set()
    return _hashes(row[0] for row in wh.execute(sql, params).fetchall())


def _parts(folders: Iterable[Path], what: str) -> list[Path]:
    """Part files under `folders`; stale `.part-*.parquet.tmp` files are deleted (TH03-12)."""
    parts: list[Path] = []
    with _io(what):
        for folder in folders:
            if not folder.is_dir():
                continue
            for stale in folder.glob(_STALE_GLOB):
                stale.unlink()
            parts += sorted(folder.glob(_PART_GLOB))
    return parts


def _label_parts(paths: EnrichPaths) -> list[tuple[Path, bool]]:
    """`(part, is_gold)` of every label part of every version and kind."""
    root = paths.data_root / "labels"
    with _io("labels"):
        versions = sorted(d for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
    return [
        (part, kind == "gold")
        for version in versions
        for kind in _LABEL_DIRS
        for part in _parts([version / kind], "labels")
    ]


def _own_label_hashes(parts: list[tuple[Path, bool]], record_id: str) -> set[str]:
    found: set[str] = set()
    for part, _ in parts:
        with _io("labels"):
            table = pq.read_table(part, columns=["record_id", "content_hash"], partitioning=None)
        found |= _hashes(table.filter(pc.equal(table["record_id"], record_id))[1].to_pylist())
    return found


def _rewrite(part: Path, drop: Callable[[pa.Table], pa.Array], what: str) -> pa.Table:
    """Drop the rows where `drop` is true; return the dropped rows (empty when none).

    An emptied pair index part is deleted; an emptied label part is kept with no rows, so
    `LabelStore.read` still finds the kind's schema.
    """
    with _io(what):
        table = pq.read_table(part, partitioning=None)
    mask = pc.fill_null(drop(table), False)
    dropped = table.filter(mask)
    if dropped.num_rows:
        kept = table.filter(pc.invert(mask))
        if kept.num_rows or what == "labels":
            write = functools.partial(pq.write_table, kept, compression="zstd")
            replace_atomic(part, write, kind=f"{what} part")
        else:
            with _io(what):
                part.unlink()
    return dropped


def _purge_labels(parts: list[tuple[Path, bool]], targets: frozenset[str], record_id: str) -> int:
    hashes = pa.array(sorted(targets), pa.string())

    def drop(table: pa.Table) -> pa.Array:
        own = pc.equal(table["record_id"], record_id)
        return pc.or_(own, pc.is_in(table["content_hash"], value_set=hashes))

    removed = 0
    for part, is_gold in parts:
        dropped = _rewrite(part, drop, "labels")
        removed += dropped.num_rows
        if is_gold and dropped.num_rows:
            for question in sorted(set(dropped["question"].to_pylist())):
                _log.warning("enrich.purge.gold_modified", question=question)
    return removed


def _pair_drop(record_id: str) -> Callable[[pa.Table], pa.Array]:
    return lambda t: pc.or_(
        pc.equal(t["incident_id"], record_id), pc.equal(t["change_id"], record_id)
    )


def _pair_hashes(parts: list[Path], record_id: str) -> set[str]:
    found: set[str] = set()
    for part in parts:
        with _io("pair index"):
            table = pq.read_table(part, partitioning=None)
        found |= _hashes(table.filter(_pair_drop(record_id)(table))["content_hash"].to_pylist())
    return found


def _shared(
    table: Table, wh: duckdb.DuckDBPyConnection | None, record_id: str, hashes: set[str]
) -> set[str]:
    """Hashes of `hashes` that another record still has in vectors or `CURRENT` (step 5)."""
    ordered = sorted(hashes)
    shared = _query(wh, _WH_SHARED, [record_id, ordered]) if ordered else set()
    for start in range(0, len(ordered), FILTER_MAX):
        where = lance_filter_in("content_hash", ordered[start : start + FILTER_MAX])
        shared |= _hashes(_read(table, where, ["content_hash"])["content_hash"].to_pylist())
    return shared


def purge_record(record_id: str) -> dict[str, int]:
    """Delete `record_id` from vectors, cache, labels and the pair index (U03-145).

    Hashes another live record still has (vectors or `CURRENT` `text_redacted`) keep their
    cache and other label rows. Idempotent. Runs inside the exclusive `maintenance` job.
    Raises SchemaViolation for an id outside the U03-33 allowlist (before any IO) and
    StoreBusy on IO errors.
    """
    where = lance_filter_in("record_id", [record_id])  # step 1: SchemaViolation, no IO yet
    paths = EnrichPaths.from_config(get_config())
    store = VectorStore()
    store.ensure_tables()
    table = store.table(_TABLE)
    labels = _label_parts(paths)
    pairs = _parts([paths.pairs_dir()], "pair index")
    with _current_warehouse() as wh:
        hashes = _hashes(_read(table, where, ["content_hash"])["content_hash"].to_pylist())
        hashes |= _query(wh, _WH_HASHES, [record_id])
        hashes |= _own_label_hashes(labels, record_id)
        pair_hashes = _pair_hashes(pairs, record_id)
        embeddings = _lance("count", functools.partial(table.count_rows, where))
        if embeddings:
            _lance("delete", functools.partial(table.delete, where))
        shared = _shared(table, wh, record_id, hashes)
    targets = frozenset((hashes - shared) | pair_hashes)
    counts = {
        "embeddings_deleted": embeddings,
        "cache_rows_deleted": purge_hashes(paths, targets),
        "label_rows_deleted": _purge_labels(labels, targets, record_id),
        "pair_rows_deleted": sum(
            _rewrite(p, _pair_drop(record_id), "pair index").num_rows for p in pairs
        ),
        "hashes_shared": len(shared),
    }
    _log.info("enrich.purge.completed", **counts)
    return counts
