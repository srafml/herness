"""Files connector: inbox listing, SHA-256 fingerprint and DuckDB readers (impl 01 §3.10).

Units U01-46 to U01-49. Reads regular files dropped in ``inbox_root/<entity>/`` (design 01
§5.10) and never moves, renames or deletes them. Symlinks, junctions, files outside the
entity folder and files over ``MAX_INBOX_FILE_BYTES`` are skipped before any byte is read
(TH01-08, TH01-09); size and ``mtime_ns`` are re-checked after hashing and after reading
(TH01-10). The ingest flow that records fingerprints is ``files_ingest`` (T01-10).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import duckdb
import pyarrow as pa
import pyarrow.compute as pc

from herness.connectors.base import KEY_SCHEMA, METADATA_SCHEMA, record_id
from herness.connectors.rows import parse_arrow_timestamps, to_snake
from herness.connectors.settings import FilesSettings
from herness.connectors.settings_entities import FilesEntity
from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation, SourceUnavailable
from herness.core.logging import get_logger
from herness.core.registry import register

__all__ = [
    "FILE_READ_MEMORY_LIMIT",
    "FINGERPRINT_CHUNK_BYTES",
    "MAX_INBOX_FILE_BYTES",
    "FilesConnector",
    "InboxFile",
    "InboxFileChanged",
    "fingerprint_file",
]

MAX_INBOX_FILE_BYTES: Final = 1_073_741_824  # 1 GiB
FILE_READ_MEMORY_LIMIT: Final = "1GB"
FINGERPRINT_CHUNK_BYTES: Final = 1_048_576

_SOURCE: Final = "files"
_UTC_US: Final = pa.timestamp("us", tz="UTC")
_EPOCH: Final = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
_IGNORED_PREFIXES: Final = (".", "~$")  # hidden files and Office lock files
_CSV_SQL: Final = "SELECT * FROM read_csv($path, all_varchar = true, header = true)"
_PARQUET_SQL: Final = "SELECT * FROM read_parquet($path)"
_XLSX_SQL: Final = "SELECT * FROM read_xlsx($path, all_varchar = true)"
_XLSX_SHEET_SQL: Final = "SELECT * FROM read_xlsx($path, all_varchar = true, sheet = $sheet)"

_log = get_logger("connectors.files")


class InboxFileChanged(SchemaViolation):
    """The file changed while it was fingerprinted or read (TH01-10); retried next run."""

    def __init__(self, entity: str, rel_path: str) -> None:
        super().__init__("inbox file changed while read", entity=entity, file=rel_path)


@dataclass(frozen=True, slots=True)
class InboxFile:
    """One eligible inbox file (U01-47); ``rel_path`` is POSIX, relative to the inbox root."""

    path: Path
    rel_path: str
    size_bytes: int
    mtime: datetime.datetime
    mtime_ns: int


def _mtime(mtime_ns: int) -> datetime.datetime:
    """Aware UTC datetime of an ``st_mtime_ns`` value, truncated to microseconds."""
    return _EPOCH + datetime.timedelta(microseconds=mtime_ns // 1000)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _entity_of(f: InboxFile) -> str:
    return f.rel_path.split("/", 1)[0]


def fingerprint_file(f: InboxFile) -> str:
    """Return the SHA-256 (64 lower-case hex) of the file bytes (U01-48).

    Raises ``InboxFileChanged`` when the bytes read or ``mtime_ns`` differ from ``f`` and
    ``SourceUnavailable`` on ``OSError``. Constant memory.
    """
    digest = hashlib.sha256()
    total = 0
    try:
        with open(f.path, "rb") as fh:
            while chunk := fh.read(FINGERPRINT_CHUNK_BYTES):
                digest.update(chunk)
                total += len(chunk)
        mtime_ns = os.stat(f.path).st_mtime_ns
    except OSError as exc:
        msg = "inbox file unreadable"
        raise SourceUnavailable(msg, source=_SOURCE, file=f.rel_path) from exc
    if total != f.size_bytes or mtime_ns != f.mtime_ns:
        raise InboxFileChanged(_entity_of(f), f.rel_path)
    return digest.hexdigest()


def _query(path: Path, sheet: str | None) -> tuple[bool, str, dict[str, str]]:
    """Return ``(needs_excel, sql, params)`` for the file suffix; the path is bound."""
    params = {"path": str(path)}
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return False, _CSV_SQL, params
    if suffix == ".parquet":
        return False, _PARQUET_SQL, params
    if suffix == ".xlsx":
        if sheet is None:
            return True, _XLSX_SQL, params
        return True, _XLSX_SHEET_SQL, params | {"sheet": sheet}
    msg = "unsupported inbox file type"
    raise SchemaViolation(msg, source=_SOURCE)


def _raw_batches(
    con: duckdb.DuckDBPyConnection, entity: str, f: InboxFile, sheet: str | None, rows: int
) -> Iterator[pa.RecordBatch]:
    """DuckDB record batches of ``f``; engine errors become ``SchemaViolation``."""
    needs_excel, sql, params = _query(f.path, sheet)
    try:
        if needs_excel:
            con.execute("LOAD excel")
        yield from con.execute(sql, params).to_arrow_reader(rows)
    except (duckdb.Error, pa.ArrowException) as exc:
        msg = f"unreadable inbox file {f.rel_path}"
        raise SchemaViolation(msg, source=_SOURCE, entity=entity) from exc


def _column(columns: Mapping[str, pa.Array], name: str, what: str, entity: str) -> pa.Array:
    column = columns.get(to_snake(name))
    if column is None:
        msg = f"{what} missing"
        raise SchemaViolation(msg, source=_SOURCE, entity=entity)
    return column


def _source_keys(keys: list[pa.Array], f: InboxFile, offset: int) -> pa.Array:
    """Single key cast to string, or the parts joined with ``|``; nulls fail with the row."""
    parts = [pc.cast(k, pa.string()) for k in keys]
    joined = parts[0] if len(parts) == 1 else pc.binary_join_element_wise(*parts, "|")
    if joined.null_count:
        row = offset + pc.index(pc.is_null(joined), True).as_py() + 1  # 1-based data row
        msg = f"null key in {f.rel_path} at row {row}"
        raise SchemaViolation(msg, source=_SOURCE)
    return joined


def _payloads(raw: pa.RecordBatch) -> pa.Array:
    names = raw.schema.names
    values = [column.to_pylist() for column in raw.columns]
    return pa.array(
        [
            json.dumps(
                dict(zip(names, row, strict=True)),
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            for row in zip(*values, strict=True)
        ],
        pa.string(),
    )


@register("connector", "files")
class FilesConnector:
    """Reads inbox drops (U01-46); one instance per thread."""

    name: str = _SOURCE

    def __init__(
        self,
        settings: FilesSettings,
        *,
        inbox_root: Path,
        clock: Callable[[], datetime.datetime] = clock.now,
    ) -> None:
        self._settings = settings
        self._root = inbox_root
        self._clock = clock
        self._settle = datetime.timedelta(seconds=settings.settle_seconds)
        self.entities: tuple[str, ...] = tuple(settings.entities)

    def _entity(self, entity: str) -> FilesEntity:
        cfg = self._settings.entities.get(entity)
        if cfg is None:
            msg = f"unknown files entity {entity[:64]}"
            raise ConfigError(msg, source=_SOURCE)
        return cfg

    def check(self) -> None:
        """Validate the inbox root; warn about missing entity folders. Raises ConfigError."""
        if _is_link(self._root) or not self._root.is_dir():
            msg = "inbox missing or not a directory"
            raise ConfigError(msg, source=_SOURCE)
        for entity in self.entities:
            if not (self._root / entity).is_dir():
                _log.warning("connectors.files.entity_folder_missing", entity=entity)

    def watermark_field(self, entity: str) -> str:
        """The entity ``updated_field``, else ``"mtime"`` (design 01 §5.3)."""
        return self._entity(entity).updated_field or "mtime"

    def _reject(self, entity: str, rel_path: str, reason: str) -> None:
        _log.warning("connectors.files.rejected", entity=entity, file=rel_path, reason=reason)

    def _reason(self, p: Path, root: Path) -> tuple[str | None, os.stat_result | None]:
        """Why ``p`` is skipped (``None`` when eligible) and its ``lstat``."""
        if _is_link(p):
            return "symlink", None
        st = p.lstat()
        if not stat.S_ISREG(st.st_mode):
            return "not_regular", st
        if not p.resolve(strict=True).is_relative_to(root):
            return "outside_root", st
        if self._clock() - _mtime(st.st_mtime_ns) < self._settle:
            return "settling", st
        if st.st_size > MAX_INBOX_FILE_BYTES:
            return "too_large", st
        return None, st

    def _eligible(self, entity: str, p: Path, root: Path) -> InboxFile | None:
        rel_path = p.relative_to(self._root).as_posix()
        try:
            reason, st = self._reason(p, root)
        except OSError:
            reason, st = "unreadable", None
        if reason == "settling":
            _log.debug("connectors.files.rejected", entity=entity, file=rel_path, reason=reason)
            return None
        if reason is not None or st is None:
            self._reject(entity, rel_path, reason or "unreadable")
            return None
        return InboxFile(p.resolve(), rel_path, st.st_size, _mtime(st.st_mtime_ns), st.st_mtime_ns)

    def candidates(self, entity: str) -> list[InboxFile]:
        """Eligible files of the entity folder sorted by ``(mtime, name)`` (U01-47).

        Raises ``SourceUnavailable`` when the folder cannot be listed.
        """
        cfg = self._entity(entity)
        folder = self._root / entity
        if _is_link(folder):  # a linked entity folder could point anywhere (TH01-08)
            self._reject(entity, entity, "symlink")
            return []
        if not folder.is_dir():
            return []
        try:
            root = folder.resolve(strict=True)
            paths = sorted(folder.glob(cfg.pattern))  # non-recursive: pattern has no "/"
        except OSError as exc:
            msg = "inbox unreadable"
            raise SourceUnavailable(msg, source=_SOURCE, entity=entity) from exc
        paths = [p for p in paths if not p.name.startswith(_IGNORED_PREFIXES)]  # not logged
        found = [f for p in paths if (f := self._eligible(entity, p, root)) is not None]
        return sorted(found, key=lambda f: (f.mtime, f.path.name))

    def sync(
        self,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime | None = None,
    ) -> Iterator[pa.RecordBatch]:
        """Read every candidate with ``since <= mtime < until`` in ``(mtime, name)`` order."""
        for f in self.candidates(entity):
            if (since is None or since <= f.mtime) and (until is None or f.mtime < until):
                yield from self.read_file(entity, f)

    def list_keys(self, entity: str) -> Iterator[pa.RecordBatch]:
        """Keys of the newest snapshot file as ``KEY_SCHEMA`` batches. Raises ConfigError."""
        if self._entity(entity).mode != "snapshot":
            msg = f"files entity {entity} is not in snapshot mode"
            raise ConfigError(msg, source=_SOURCE, entity=entity)
        found = self.candidates(entity)
        if not found:
            msg = "no snapshot file"
            raise ConfigError(msg, source=_SOURCE, entity=entity)
        for batch in self.read_file(entity, found[-1]):
            yield pa.RecordBatch.from_arrays([batch.column("_source_key")], schema=KEY_SCHEMA)

    def read_file(self, entity: str, f: InboxFile) -> Iterator[pa.RecordBatch]:
        """Read one inbox file into lake batches with its own capped DuckDB (U01-49).

        Raises ``SchemaViolation`` (unreadable file, key or timestamp errors) and
        ``InboxFileChanged`` when the file changed while it was read.
        """
        cfg = self._entity(entity)
        con = duckdb.connect(
            ":memory:",
            config={
                "memory_limit": FILE_READ_MEMORY_LIMIT,
                "threads": 2,
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
            },
        )
        try:
            offset = 0
            for raw in _raw_batches(con, entity, f, cfg.sheet, self._settings.batch_rows):
                if raw.num_rows:
                    yield self._lake_batch(entity, cfg, f, raw, offset)
                    offset += raw.num_rows
            try:
                st = os.stat(f.path)
            except OSError:
                st = None
            if st is None or (st.st_size, st.st_mtime_ns) != (f.size_bytes, f.mtime_ns):
                raise InboxFileChanged(entity, f.rel_path)
        finally:
            con.close()

    def _lake_batch(
        self, entity: str, cfg: FilesEntity, f: InboxFile, raw: pa.RecordBatch, offset: int
    ) -> pa.RecordBatch:
        names = [to_snake(n) for n in raw.schema.names]
        if len(set(names)) != len(names):
            msg = "column collision after snake_case renaming"
            raise SchemaViolation(msg, source=_SOURCE, entity=entity)
        columns = dict(zip(names, raw.columns, strict=True))
        keys = _source_keys(
            [_column(columns, k, "key field", entity) for k in cfg.key_field], f, offset
        )
        n = raw.num_rows
        if cfg.updated_field is None:
            updated = pa.repeat(pa.scalar(f.mtime, _UTC_US), n)
        else:
            column = _column(columns, cfg.updated_field, "updated field", entity)
            updated = parse_arrow_timestamps(column, field=cfg.updated_field)
        meta = [
            pa.array([record_id(_SOURCE, entity, k) for k in keys.to_pylist()], pa.string()),
            pa.repeat(pa.scalar(_SOURCE, pa.string()), n),
            pa.repeat(pa.scalar(entity, pa.string()), n),
            keys,
            updated,
            pa.repeat(pa.scalar(self._clock(), _UTC_US), n),
            pa.repeat(pa.scalar(False), n),
            _payloads(raw),
        ]
        fields = [*METADATA_SCHEMA, *(pa.field(c, a.type) for c, a in columns.items())]
        return pa.RecordBatch.from_arrays([*meta, *columns.values()], schema=pa.schema(fields))
