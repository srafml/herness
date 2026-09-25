"""Lake contract validation and ``LakeWriter``; files appear on commit (impl 02 §3.2)."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import errno
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal, NoReturn, Self

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from herness.core import time as clock
from herness.core.errors import ConfigError, HernessError, SchemaViolation, StoreBusy
from herness.core.ids import new_ulid
from herness.core.logging import get_logger
from herness.store.errors import LakeContractError, LakeStateError
from herness.store.layout import data_layout

BUFFER_ROWS: Final = 131_072
BUFFER_BYTES: Final = 64 * 2**20
ZSTD_LEVEL: Final = 3
PARQUET_VERSION: Final = "2.6"
LAKE_FILE_PATTERN: Final = "[!.]*.parquet"  # OI-06: DuckDB honours [!.] (ST02-02)
NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
COLUMN_RE: Final = re.compile(r"^[a-z_][a-z0-9_]{0,127}$")

_UTC_US: Final = pa.timestamp("us", tz="UTC")
META_COLUMNS: Final[tuple[tuple[str, pa.DataType, bool], ...]] = (
    ("_record_id", pa.string(), False),
    ("_source", pa.string(), False),
    ("_entity", pa.string(), False),
    ("_source_key", pa.string(), False),
    ("_source_updated_at", _UTC_US, False),
    ("_fetched_at", _UTC_US, False),
    ("_deleted", pa.bool_(), False),
    ("_payload", pa.string(), True),
)
_TIME_COLUMNS: Final = ("_source_updated_at", "_fetched_at")
_US_RANGE: Final = (-62_135_596_800_000_000, 253_402_300_799_999_999)  # datetime min/max, us
_META_NAMES: Final = frozenset(name for name, _, _ in META_COLUMNS)
_TARGET_RANGE: Final = (2**20, 2**30)
_MAX_OPEN_RANGE: Final = (1, 86_400)
_BUSY_ERRNOS: Final = frozenset({errno.EACCES, errno.EBUSY})
_BUSY_WINERRORS: Final = frozenset({32, 33})

_PARQUET_OPTIONS: Final = {
    "compression": "zstd",
    "compression_level": ZSTD_LEVEL,
    "version": PARQUET_VERSION,
    "use_dictionary": True,
    "write_statistics": True,
}
_log = get_logger("store.lake")

type _State = Literal["open", "committed", "aborted"]


def validate_name(kind: Literal["source", "entity"], value: str) -> str:
    """Return ``value`` if it full-matches ``NAME_RE``; the value is never echoed (TH02-01)."""
    if NAME_RE.fullmatch(value) is None:
        msg = f"invalid lake {kind} name (length {len(value)})"
        raise ConfigError(msg)
    return value


def partition_dir(raw_root: Path, source: str, entity: str, dt: datetime.date) -> Path:
    """Return ``raw_root/<source>/<entity>/dt=YYYY-MM-DD``, contained in ``raw_root``."""
    path = raw_root / validate_name("source", source) / validate_name("entity", entity)
    path = path / f"dt={dt.isoformat()}"
    if not path.resolve(strict=False).is_relative_to(raw_root.resolve(strict=False)):
        msg = "lake partition resolves outside the raw root"
        raise ConfigError(msg)
    return path


def lake_glob(raw_root: Path, source: str, entity: str) -> str:
    """Return the staging glob of one entity; it never matches dot-prefixed temp files."""
    names = f"{validate_name('source', source)}/{validate_name('entity', entity)}"
    return f"{raw_root.as_posix()}/{names}/**/{LAKE_FILE_PATTERN}"


@dataclasses.dataclass(frozen=True, slots=True)
class LakeFileSet:
    """Result of one ``commit()``: final paths sorted by POSIX string, rows, max update time."""

    files: tuple[Path, ...]
    rows: int
    max_source_updated_at: datetime.datetime | None


def _fail(source: str, entity: str, rule: str, column: str, bad_rows: int = 0) -> NoReturn:
    raise LakeContractError(source, entity, rule, column, bad_rows)


def _count_true(mask: pa.Array) -> int:
    return int(pc.sum(mask).as_py() or 0)


def _type_ok(name: str, dtype: pa.DataType) -> bool:
    if name in _TIME_COLUMNS:
        return bool(pa.types.is_timestamp(dtype) and dtype.tz == "UTC")
    if name == "_deleted":
        return bool(pa.types.is_boolean(dtype))
    return bool(pa.types.is_string(dtype) or pa.types.is_large_string(dtype))


def _cast_time(name: str, array: pa.Array, where: tuple[str, str]) -> pa.Array:
    """Cast to ``timestamp[us, UTC]``; values outside the datetime range fail ``type``."""
    factor = {"s": 10**6, "ms": 10**3, "us": 1}.get(array.type.unit)  # ns always fits
    if factor is not None:
        low, high = -(-_US_RANGE[0] // factor), _US_RANGE[1] // factor
        ints = pc.cast(array, pa.int64())
        bad = _count_true(pc.or_(pc.greater(ints, high), pc.less(ints, low)))
        if bad:
            _fail(*where, "type", name, bad)
    return pc.cast(array, options=pc.CastOptions(_UTC_US, allow_time_truncate=True))


def _meta_arrays(batch: pa.RecordBatch, source: str, entity: str) -> dict[str, pa.Array]:
    """Rules (1) to (3): presence, types, nulls; returns the normalised metadata arrays."""
    names: list[str] = batch.schema.names
    for name, _, _ in META_COLUMNS:
        if name not in names:
            _fail(source, entity, "missing_column", name)
    raw = {name: batch.column(names.index(name)) for name, _, _ in META_COLUMNS}
    for name, _, _ in META_COLUMNS:
        if not _type_ok(name, raw[name].type):
            _fail(source, entity, "type", name)
    for name, _, nullable in META_COLUMNS:
        if not nullable and raw[name].null_count:
            _fail(source, entity, "null", name, raw[name].null_count)
    strings = {n for n, dtype, _ in META_COLUMNS if dtype == pa.string()}
    return {n: pc.cast(a, pa.string()) if n in strings else a for n, a in raw.items()}


def _check_values(meta: dict[str, pa.Array], source: str, entity: str) -> None:
    """Rules (4) to (6): source, entity, record id format, payload of live rows."""
    for column, expected in (("_source", source), ("_entity", entity)):
        bad = _count_true(pc.not_equal(meta[column], pa.scalar(expected, pa.string())))
        if bad:
            _fail(source, entity, f"{column[1:]}_mismatch", column, bad)
    joined = pc.binary_join_element_wise(meta["_source"], meta["_entity"], meta["_source_key"], ":")
    bad = _count_true(pc.not_equal(meta["_record_id"], joined))
    if bad:
        _fail(source, entity, "record_id_format", "_record_id", bad)
    live_null = pc.and_(pc.invert(meta["_deleted"]), pc.is_null(meta["_payload"]))
    bad = _count_true(live_null)
    if bad:
        _fail(source, entity, "payload_null", "_payload", bad)


def _check_names(names: list[str], source: str, entity: str) -> None:
    """Rules (7) and (8): entity column names and uniqueness."""
    for name in names:
        if name not in _META_NAMES and COLUMN_RE.fullmatch(name) is None:
            _fail(source, entity, "column_name", name)
    seen: set[str] = set()
    for name in names:
        if name in seen:
            _fail(source, entity, "duplicate_column", name)
        seen.add(name)


def _validate_batch(batch: pa.RecordBatch, source: str, entity: str) -> pa.RecordBatch:
    """Enforce the lake contract; return metadata first with exact types, then the rest."""
    meta = _meta_arrays(batch, source, entity)
    _check_values(meta, source, entity)
    names: list[str] = batch.schema.names
    _check_names(names, source, entity)
    for name in _TIME_COLUMNS:
        meta[name] = _cast_time(name, meta[name], (source, entity))
    fields = [pa.field(name, dtype, nullable) for name, dtype, nullable in META_COLUMNS]
    arrays = [meta[name] for name, _, _ in META_COLUMNS]
    for index, name in enumerate(names):
        if name not in _META_NAMES:
            fields.append(batch.schema.field(index))
            arrays.append(batch.column(index))
    return pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields))


def _fsync(path: Path, flags: int) -> None:
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LakeWriter:
    """Buffer, validate and write one entity's batches; commit renames. Not thread-safe."""

    def __init__(
        self,
        source: str,
        entity: str,
        *,
        target_bytes: int = 128 * 2**20,
        max_open_s: int = 600,
        root: Path | None = None,
        clock: Callable[[], float] = clock.monotonic,
    ) -> None:
        self._source = validate_name("source", source)
        self._entity = validate_name("entity", entity)
        self._ids = {"source": self._source, "entity": self._entity}
        for name, value, (low, high) in (
            ("target_bytes", target_bytes, _TARGET_RANGE),
            ("max_open_s", max_open_s, _MAX_OPEN_RANGE),
        ):
            if not low <= value <= high:
                msg = f"lake {name} must lie in [{low}, {high}]"
                raise ConfigError(msg)
        self._target_bytes, self._max_open_s, self._clock = target_bytes, max_open_s, clock
        self._root = (root if root is not None else data_layout().raw).resolve(strict=False)
        self._state: _State = "open"
        self.rows = 0
        self.max_source_updated_at: datetime.datetime | None = None
        self._temp_files: list[tuple[Path, Path]] = []
        self._discard: list[Path] = []  # temps of failed opens: deleted, never renamed
        self._buffer: list[pa.RecordBatch] = []
        self._buffer_rows = self._buffer_bytes = 0
        self._key: tuple[datetime.date, pa.Schema] | None = None  # dt and schema of buffer/file
        self._pq: pq.ParquetWriter | None = None
        self._file_rows = 0
        self._opened_at: float | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> Literal[False]:
        if self._state == "open":
            if exc_type is None:
                _log.warning("store.lake.uncommitted_exit", **self._ids)
            self.abort()
        return False

    def _require_open(self, method: str) -> None:
        if self._state == "open":
            return
        raise LakeStateError(self._source, self._entity, self._state, method)

    def _os_error(self, exc: OSError) -> HernessError:
        where = f"{self._source}/{self._entity}: {errno.errorcode.get(exc.errno or 0, 'unknown')}"
        winerror = getattr(exc, "winerror", None)
        if exc.errno in _BUSY_ERRNOS or winerror in _BUSY_WINERRORS:
            return StoreBusy(f"lake file busy for {where}")
        return SchemaViolation(f"lake write failed for {where}")

    def write(self, batch: pa.RecordBatch) -> None:
        """Validate ``batch``, split it by ``dt``, buffer it and rotate files at the limits."""
        self._require_open("write")
        if batch.num_rows == 0:
            return
        try:
            self._write(batch)
        except OSError as exc:
            raise self._os_error(exc) from exc

    def _write(self, batch: pa.RecordBatch) -> None:
        opened_at = self._opened_at
        if opened_at is not None and self._clock() - opened_at >= self._max_open_s:
            self._flush()
            self._close("age")
        norm = _validate_batch(batch, self._source, self._entity)
        dates = pc.cast(norm.column(5), pa.date32())
        for day in pc.unique(dates).to_pylist():
            group = norm.filter(pc.equal(dates, pa.scalar(day, pa.date32())))
            self._append(day, group)

    def _append(self, day: datetime.date, group: pa.RecordBatch) -> None:
        key = self._key
        active = bool(self._buffer) or self._pq is not None
        if active and key is not None and (key[0] != day or not key[1].equals(group.schema)):
            self._flush()
            self._close("dt_change" if key[0] != day else "schema_change")
        self._key = (day, group.schema)
        self._buffer.append(group)
        self._buffer_rows += group.num_rows
        self.rows += group.num_rows  # counted once buffered, so a failed flush stays consistent
        group_max = pc.max(group.column(4)).as_py().astimezone(datetime.UTC)
        current = self.max_source_updated_at
        self.max_source_updated_at = group_max if current is None else max(current, group_max)
        self._buffer_bytes += int(group.nbytes)
        if self._buffer_rows >= BUFFER_ROWS or self._buffer_bytes >= BUFFER_BYTES:
            self._flush()

    def _open_file(self, day: datetime.date, schema: pa.Schema) -> pq.ParquetWriter:
        directory = partition_dir(self._root, self._source, self._entity, day)
        directory.mkdir(parents=True, exist_ok=True)
        name = f"part-{new_ulid()}.parquet"
        temp = directory / f".{name}.tmp-{new_ulid()}"
        try:
            writer = pq.ParquetWriter(temp, schema, **_PARQUET_OPTIONS)
        except BaseException:
            try:
                temp.unlink(missing_ok=True)
            except OSError:  # never published; abort and commit retry the unlink
                self._discard.append(temp)
            raise
        self._temp_files.append((temp, directory / name))
        self._opened_at, self._file_rows = self._clock(), 0
        return writer

    def _flush(self) -> None:
        if not self._buffer or self._key is None:
            return
        day, schema = self._key
        if self._pq is None:
            self._pq = self._open_file(day, schema)
        table = pa.Table.from_batches(self._buffer, schema=schema)
        self._pq.write_table(table, row_group_size=table.num_rows)
        self._file_rows += table.num_rows
        self._buffer, self._buffer_rows, self._buffer_bytes = [], 0, 0
        if self._temp_files[-1][0].stat().st_size >= self._target_bytes:
            self._close("size")

    def _close(self, reason: str) -> None:
        if self._pq is None:
            return
        self._pq.close()
        self._pq, self._opened_at = None, None
        temp = self._temp_files[-1][0]
        fields = {"dt": temp.parent.name[3:], "rows": self._file_rows, "reason": reason}
        _log.debug("store.lake.file_rotated", **self._ids, **fields, bytes=temp.stat().st_size)

    def commit(self) -> LakeFileSet:
        """Flush, close and rename every temp file to its final name; return the file set."""
        self._require_open("commit")
        started = clock.monotonic()
        try:
            self._flush()
            self._close("commit")
            for temp, final in self._temp_files:
                if not temp.exists() and final.exists():
                    continue
                _fsync(temp, os.O_RDWR | getattr(os, "O_BINARY", 0))
                os.replace(temp, final)
            for temp in self._discard:
                with contextlib.suppress(OSError):
                    temp.unlink(missing_ok=True)
            if os.name != "nt":  # directory fsync is POSIX-only
                for directory in {final.parent for _, final in self._temp_files}:
                    _fsync(directory, os.O_RDONLY)
            files = tuple(sorted((f for _, f in self._temp_files), key=lambda p: p.as_posix()))
            size = sum(f.stat().st_size for f in files)
        except OSError as exc:
            raise self._os_error(exc) from exc
        self._state = "committed"
        fields = {"files": len(files), "rows": self.rows, "bytes": size}
        duration_ms = round((clock.monotonic() - started) * 1000)
        _log.info("store.lake.committed", **self._ids, **fields, duration_ms=duration_ms)
        rows, max_at = self.rows, self.max_source_updated_at
        return LakeFileSet(files=files, rows=rows, max_source_updated_at=max_at)

    def abort(self) -> None:
        """Discard everything not committed; idempotent, a no-op after ``commit()``."""
        if self._state == "committed":
            return
        if self._pq is not None:
            with contextlib.suppress(OSError, pa.ArrowException):
                self._pq.close()
            self._pq, self._opened_at = None, None
        leftover = 0
        for temp in [*(t for t, _ in self._temp_files), *self._discard]:
            try:
                temp.unlink(missing_ok=True)
            except OSError:  # abort never raises (U02-17); spec 01 sweeps old temps
                leftover += 1
        if leftover:
            _log.warning("store.lake.abort_leftover", **self._ids, count=leftover)
        self._buffer, self._buffer_rows, self._buffer_bytes = [], 0, 0
        self._state = "aborted"
