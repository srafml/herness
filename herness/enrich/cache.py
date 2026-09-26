"""Persistent decision cache: schema, reader and writer (U03-36 ... U03-38; design 03 §4.3).

Layout (impl 03 §4.2): ``<qsv>/decider=<name>/decider_version=<url-quoted v>/part-<ulid>.parquet``,
written as ``.part-<ulid>.parquet.tmp`` then renamed. Readers see only ``part-*.parquet`` files
and reject any part whose schema is not exactly ``CACHE_SCHEMA`` (TH03-18).
"""

from __future__ import annotations

import errno
import os
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import duckdb
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from herness.core import time as clock
from herness.core.errors import FatalError, SchemaViolation, StoreBusy
from herness.core.ids import new_ulid
from herness.core.logging import get_logger
from herness.core.types import DecisionOutput, Question, QuestionSet
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import question_fingerprint

CACHE_SCHEMA: Final = pa.schema(
    [
        ("content_hash", pa.string()),
        ("question", pa.string()),
        ("question_fingerprint", pa.string()),
        ("answer", pa.string()),
        ("probability", pa.float64()),
        ("distribution", pa.map_(pa.string(), pa.float64())),
        ("backend_confidence", pa.float64()),
        ("samples", pa.int16()),
        ("decided_at", pa.timestamp("us", tz="UTC")),
    ]
)
_PARTITION_SCHEMA: Final = pa.schema([("decider", pa.string()), ("decider_version", pa.string())])
_FULL_SCHEMA: Final = pa.schema([*CACHE_SCHEMA, *_PARTITION_SCHEMA])
_PART_GLOB: Final = "decider=*/decider_version=*/part-*.parquet"
_BUSY_ERRNOS: Final = frozenset({errno.EACCES, errno.EBUSY})

_log = get_logger("enrich.cache")

type CacheKey = tuple[str, str, str]


def _fault_point(name: str) -> None:
    """No-op hook for a named fault point.

    T08-08: replaced by herness.core.resilience.fault_point when that lands (R-40).
    """


def _fingerprint(q: Question) -> str:
    return q.fingerprint or question_fingerprint(q)


class DecisionCache:
    """Read access to the cache of one question set version and factory for writers (U03-37)."""

    def __init__(self, paths: EnrichPaths, qsv: str) -> None:
        self._paths = paths
        self._qsv = qsv
        self._dir = paths.cache_dir(qsv)

    def _part_files(self) -> list[Path]:
        if not self._dir.is_dir():
            return []
        return sorted(self._dir.glob(_PART_GLOB))

    def dataset(self) -> ds.Dataset | None:
        """Dataset over every visible part, or None when there is none.

        Only ``part-*.parquet`` files under the Hive partition directories are read, so
        ``.tmp``, dot- or underscore-prefixed files and ``questions.json`` are never seen.
        Partition values are URL-decoded by pyarrow (``segment_encoding="uri"``).
        Raises SchemaViolation when any part's schema differs from ``CACHE_SCHEMA``.
        """
        files = self._part_files()
        if not files:
            return None
        partitioning = ds.HivePartitioning(_PARTITION_SCHEMA, segment_encoding="uri")
        dataset = ds.dataset(
            [f.as_posix() for f in files],
            format="parquet",
            partitioning=partitioning,
            partition_base_dir=self._dir.as_posix(),
            ignore_prefixes=[".", "_"],
            schema=_FULL_SCHEMA,
        )
        for fragment in dataset.get_fragments():
            if not fragment.physical_schema.equals(CACHE_SCHEMA, check_metadata=False):
                msg = f"cache part schema mismatch: {Path(fragment.path).name}"
                raise SchemaViolation(msg, question_set_version=self._qsv)
        return dataset

    def register(self, con: duckdb.DuckDBPyConnection, view: str) -> None:
        """Register the cache as ``view`` on ``con``; an empty table when there is no part."""
        dataset = self.dataset()
        con.register(view, dataset if dataset is not None else _FULL_SCHEMA.empty_table())

    def existing_keys(
        self, decider: str, decider_version: str, questions: QuestionSet
    ) -> set[tuple[str, str]]:
        """``(content_hash, question)`` pairs cached for this partition and these fingerprints."""
        dataset = self.dataset()
        if dataset is None:
            return set()
        fingerprints = sorted({_fingerprint(q) for q in questions.questions})
        condition = (
            (ds.field("decider") == decider)
            & (ds.field("decider_version") == decider_version)
            & ds.field("question_fingerprint").isin(fingerprints)
        )
        table = dataset.to_table(columns=["content_hash", "question"], filter=condition)
        hashes = table.column("content_hash").to_pylist()
        qids = table.column("question").to_pylist()
        return set(zip(hashes, qids, strict=True))

    def writer(
        self, decider: str, decider_version: str, *, questions: QuestionSet, flush_rows: int
    ) -> CacheWriter:
        """A buffered writer of the partition ``(decider, decider_version)``."""
        partition = self._paths.cache_partition(self._qsv, decider, decider_version)
        return CacheWriter(
            partition, decider, decider_version, questions=questions, flush_rows=flush_rows
        )


class CacheWriter:
    """Buffered, atomic, idempotent writer of one cache partition (U03-38).

    One writer per partition per process; not thread-safe.
    """

    def __init__(
        self,
        partition: Path,
        decider: str,
        decider_version: str,
        *,
        questions: QuestionSet,
        flush_rows: int,
    ) -> None:
        if flush_rows < 1:
            msg = "flush_rows must be at least 1"
            raise SchemaViolation(msg, flush_rows=flush_rows)
        self._partition = partition
        self._decider = decider
        self._decider_version = decider_version
        self._fingerprints = {q.id: _fingerprint(q) for q in questions.questions}
        self._flush_rows = flush_rows
        self._buffer: list[dict[str, object]] = []
        self._written: set[CacheKey] = set()

    def _check_decider(self, output: DecisionOutput) -> None:
        if output.decider != self._decider or output.decider_version != self._decider_version:
            msg = "decision output does not belong to this cache writer's decider"
            raise SchemaViolation(msg, decider=output.decider, expected=self._decider)

    def add(self, outputs: Sequence[DecisionOutput], *, samples: int | None) -> int:
        """Buffer the rows of ``outputs``; return the number of rows added.

        Error outputs, question ids outside the set and keys already written are skipped.
        Flushes whenever the buffer reaches ``flush_rows``.
        """
        added = 0
        for output in outputs:
            self._check_decider(output)
            if output.error is not None:
                continue
            for qid, answer in output.answers.items():
                fingerprint = self._fingerprints.get(qid)
                key = (output.content_hash, qid, fingerprint or "")
                if fingerprint is None or key in self._written:
                    continue
                self._written.add(key)
                self._buffer.append(
                    {
                        "content_hash": output.content_hash,
                        "question": qid,
                        "question_fingerprint": fingerprint,
                        "answer": answer.answer,
                        "probability": answer.probability,
                        "distribution": list(answer.distribution.items()),
                        "backend_confidence": answer.backend_confidence,
                        "samples": samples,
                        "decided_at": clock.now(),
                    }
                )
                added += 1
                if len(self._buffer) >= self._flush_rows:
                    self.flush()
        return added

    def flush(self) -> Path | None:
        """Write the buffered rows as one new ``part-<ulid>.parquet``; None when empty."""
        if not self._buffer:
            return None
        table = pa.Table.from_pylist(self._buffer, schema=CACHE_SCHEMA)
        ulid = new_ulid()
        tmp = self._partition / f".part-{ulid}.parquet.tmp"
        target = self._partition / f"part-{ulid}.parquet"
        try:
            self._partition.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, tmp, compression="zstd")
            with tmp.open("rb+") as handle:
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            msg = f"cannot write cache part: {target.name}"
            error_class = StoreBusy if exc.errno in _BUSY_ERRNOS else FatalError
            raise error_class(msg, decider=self._decider) from exc
        _fault_point("enrich.after_batch_write")
        _log.debug("enrich.cache.flushed", decider=self._decider, rows=table.num_rows)
        self._buffer.clear()
        return target

    def close(self) -> None:
        """Flush the remaining rows."""
        self.flush()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.flush()
