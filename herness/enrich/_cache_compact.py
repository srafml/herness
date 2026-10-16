"""Bounded, chunked merge of decision cache parts (U03-39, U03-40; T03-39).

Strings are held as 64-bit-offset ``large_string`` in memory, so no array in a merge passes the
2 GiB limit of 32-bit offsets. Pass 1 reads only the key columns of every source part and keeps
the latest ``decided_at`` per key across all parts; pass 2 reads one part at a time, keeps its
winning rows and writes the output in chunks of at most ``OUTPUT_ROWS`` rows or ``_OUTPUT_BYTES``
bytes. The on-disk schema stays ``CACHE_SCHEMA``: ``write_part`` casts each chunk back.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final

import pyarrow as pa

from herness.enrich.cache import CACHE_SCHEMA, write_part

OUTPUT_ROWS: int = 500_000
_OUTPUT_BYTES: Final = 512 * 2**20
_KEY: Final = ("content_hash", "question", "question_fingerprint")

type Reader = Callable[[Path, list[str]], pa.Table]


# Map keys stay ``string``: they are short labels (a chunk holds far less than 2 GiB of them), and
# pyarrow aborts the process when a ``map<large_string, double>`` is cast back after ``take``.
LARGE_SCHEMA: Final = pa.schema(
    [(f.name, pa.large_string() if f.type == pa.string() else f.type) for f in CACHE_SCHEMA]
)


def _winners(sources: list[Path], read: Reader) -> list[list[int]]:
    """Row indices to keep per source: the latest ``decided_at`` of each key over all sources."""
    best: dict[tuple[object, ...], tuple[int, int, int]] = {}
    for number, source in enumerate(sources):
        table = read(source, [*_KEY, "decided_at"])
        stamps = table.column("decided_at").cast(pa.int64()).to_pylist()
        keys = zip(*(table.column(name).to_pylist() for name in _KEY), strict=True)
        for row, (key, stamp) in enumerate(zip(keys, stamps, strict=True)):
            held = best.get(key)
            if held is None or stamp > held[0]:  # a tie keeps the earlier part and row
                best[key] = (stamp, number, row)
    keep: list[list[int]] = [[] for _ in sources]
    for _, number, row in best.values():
        keep[number].append(row)
    return [sorted(rows) for rows in keep]


def merge_parts(sources: list[Path], target: Path, read: Reader, *, decider: str) -> int:
    """Write the deduplicated rows of ``sources`` into chunk parts under ``target``.

    ``read(part, columns)`` returns the (filtered) rows of a part, restricted to ``columns``.
    Returns the rows written; the caller removes the sources after the last chunk is written.
    """
    pending: list[pa.Table] = []
    rows = size = written = 0

    def flush(*, final: bool) -> None:
        nonlocal pending, rows, size, written
        if not pending:
            return
        table = pa.concat_tables(pending)
        start = 0
        while rows - start >= OUTPUT_ROWS or ((final or size >= _OUTPUT_BYTES) and start < rows):
            chunk = table.slice(start, OUTPUT_ROWS)
            write_part(target, chunk, decider=decider)
            written += chunk.num_rows
            start += chunk.num_rows
        rest = table.slice(start)
        pending, rows, size = ([rest], rest.num_rows, rest.nbytes) if rest.num_rows else ([], 0, 0)

    for source, keep in zip(sources, _winners(sources, read), strict=True):
        if not keep:
            continue
        part = read(source, CACHE_SCHEMA.names).cast(LARGE_SCHEMA).take(pa.array(keep, pa.int64()))
        pending.append(part)
        rows += part.num_rows
        size += part.nbytes
        flush(final=False)
    flush(final=True)
    return written
