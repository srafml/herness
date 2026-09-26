"""Chunking and the worker process pool of ``redact_table`` (U10-47, size-forced sibling).

Chunks cross the process boundary as built-in lists of strings only (ENG exception X-2):
the IDs and text columns of one chunk go out, the joined texts and failed IDs come back.
Workers use the ``spawn`` start method on every OS; ``_init_worker`` loads the parent's
profile from its config dir and builds the worker's own redactor.
"""

from __future__ import annotations

import multiprocessing
import os
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path
from typing import Any, Final

import pyarrow as pa

from herness.core import config as _config
from herness.core import redact as _redact
from herness.core.config import ProfileName
from herness.core.errors import SchemaViolation

__all__ = ["CHUNK_ROWS", "Chunk", "RowsOut", "redact_chunks", "redact_rows", "spawn_args"]
__all__ += ["string_column", "worker_count"]

CHUNK_ROWS: Final = 20_000
_JOIN: Final = "\n\n"

Chunk = tuple[list[str | None], list[list[str | None]]]  # IDs; one list per text column
RowsOut = tuple[list[str | None], list[str | None]]  # joined texts; IDs of failed rows


def redact_rows(redactor: _redact.Redactor, chunk: Chunk) -> RowsOut:
    """Redact each column value, join non-null results; a raising row is NULL and failed."""
    ids, columns = chunk
    out: list[str | None] = []
    failed: list[str | None] = []
    for row, record_id in enumerate(ids):
        try:
            cells = (redactor.redact(column[row]) for column in columns)
            parts = [result.text for result in cells if result is not None]
        except Exception:  # noqa: BLE001 - any row that raises fails closed (U10-47 step 4)
            failed.append(record_id)
            out.append(None)  # fail closed: raw text is never substituted
            continue
        out.append(_JOIN.join(parts) if parts else None)
    return out, failed


def spawn_args() -> tuple[ProfileName, tuple[str, ...], str]:
    """``(profile, overrides, config_dir)`` of the parent's cached config for ``init_config``.

    ``herness.core.config`` keeps neither the ``--set`` overrides nor the config dir: the dir
    is taken as ``<cached root>/config`` and overrides as none. ``security.*`` is file-only,
    so the worker's redaction settings and key reference equal the parent's.
    """
    cfg = _config.get_config()
    root = _config._Cache.root or Path.cwd()
    return cfg.profile, (), str(root / "config")  # built-in types only (ENG X-2)


def string_column(tbl: pa.Table, name: str) -> Any:  # noqa: ANN401 - pyarrow is untyped
    """Column ``name`` if the table has exactly one of that name and it holds strings."""
    index = tbl.schema.get_field_index(name)  # -1 when missing or duplicated
    kind = tbl.schema.field(index).type if index >= 0 else None
    if kind is None or not (pa.types.is_string(kind) or pa.types.is_large_string(kind)):
        msg = f"redact_table: missing column {name[:100]}"
        raise SchemaViolation(msg)
    return tbl.column(index)


def worker_count(workers: int | None) -> int:
    """``workers``, else ``sources.build.threads``, else the CPU count; at least 1."""
    if workers is None:
        threads = _config.get_config().sources.build.threads
        workers = (os.cpu_count() or 1) if threads is None else threads
    return max(1, workers)


def _init_worker(profile: ProfileName, overrides: Sequence[str], config_dir: str) -> None:
    """Worker initializer: load the parent's config, then build this process's redactor."""
    _config.init_config(profile, overrides, Path(config_dir))
    _redact.get_redactor()


def _redact_chunk(chunk: Chunk) -> RowsOut:
    return redact_rows(_redact.get_redactor(), chunk)


def redact_chunks(load: Callable[[int], Chunk], count: int, workers: int) -> list[RowsOut]:
    """Redact chunks ``load(0) .. load(count - 1)`` in order: in-process for one worker or one
    chunk, else in a spawn pool holding at most ``2 * workers`` chunks in flight.

    A worker that dies raises ``BrokenProcessPool`` out of this call; nothing is returned.
    """
    if workers == 1 or count <= 1:
        redactor = _redact.get_redactor()
        return [redact_rows(redactor, load(index)) for index in range(count)]
    out: list[RowsOut] = []
    pending: deque[Future[RowsOut]] = deque()
    pool = ProcessPoolExecutor(
        max_workers=min(workers, count),
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_init_worker,
        initargs=spawn_args(),
    )
    try:
        for index in range(count):
            if len(pending) >= 2 * workers:
                out.append(pending.popleft().result())
            pending.append(pool.submit(_redact_chunk, load(index)))
        out.extend(future.result() for future in pending)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return out
