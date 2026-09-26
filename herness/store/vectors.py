"""LanceDB vector store: table schemas, safe deletes, history purge and health.

Implements impl 02 U02-63 … U02-70. Owners (spec 03 tickets, spec 07 memory) upsert and
search on the table objects ``VectorStore.table`` returns; this module owns the schemas,
deletion by validated key (TH02-08) and removal of old table versions (TH02-09).
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Collection
from pathlib import Path
from typing import Final, Literal

import lancedb
import pyarrow as pa
from lancedb.table import Table

from herness.core.errors import ConfigError, HernessError, SchemaViolation, StoreBusy
from herness.core.logging import get_logger
from herness.store.errors import NotFoundError
from herness.store.layout import data_layout

__all__ = [
    "EMBEDDING_DIM",
    "MEMORY_EMBEDDING_SCHEMA",
    "TICKET_EMBEDDING_SCHEMA",
    "HealthResult",
    "TableName",
    "VectorStore",
]

type TableName = Literal["ticket_embedding", "memory_embedding"]
type HealthResult = tuple[Literal["ok", "degraded", "down"], str]

EMBEDDING_DIM: Final = 1024
"""Embedding width; matches bge-m3 (spec 03 §5.2)."""

_VECTOR: Final = pa.list_(pa.float32(), EMBEDDING_DIM)
_UTC_US: Final = pa.timestamp("us", tz="UTC")

TICKET_EMBEDDING_SCHEMA: Final = pa.schema(
    [
        pa.field("record_id", pa.string(), nullable=False),
        pa.field("entity", pa.string(), nullable=False),
        pa.field("service_id", pa.string(), nullable=True),
        pa.field("opened_at", _UTC_US, nullable=True),
        pa.field("content_hash", pa.string(), nullable=False),
        pa.field("model", pa.string(), nullable=False),
        pa.field("vector", _VECTOR, nullable=False),
    ]
)
MEMORY_EMBEDDING_SCHEMA: Final = pa.schema(
    [
        pa.field("memory_id", pa.string(), nullable=False),
        pa.field("layer", pa.string(), nullable=False),
        pa.field("kind", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("content_hash", pa.string(), nullable=False),
        pa.field("model", pa.string(), nullable=False),
        pa.field("vector", _VECTOR, nullable=False),
    ]
)

_SCHEMAS: Final[dict[str, pa.Schema]] = {
    "ticket_embedding": TICKET_EMBEDDING_SCHEMA,
    "memory_embedding": MEMORY_EMBEDDING_SCHEMA,
}
_DELETE_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "ticket_embedding": frozenset({"record_id", "content_hash"}),
    "memory_embedding": frozenset({"memory_id", "content_hash"}),
}
# The pattern excludes quotes, so an id cannot break out of the filter literal (TH02-08).
_ID_RE: Final = re.compile(r"[A-Za-z0-9_:.|-]{1,256}")
_MAX_IDS: Final = 100_000
_DELETE_CHUNK: Final = 500
_LANCE_ERRORS: Final = (OSError, RuntimeError, ValueError)
# LanceDB 0.39.0 raises no typed conflict error; retryable failures are told by message.
_BUSY_MARKERS: Final = ("conflict", "busy", "lock")

_log = get_logger("store.vectors")


def _check_name(name: str) -> None:
    if name not in _SCHEMAS:
        msg = "unknown vector table"
        raise ConfigError(msg, table=name[:64])


def _valid_id(key: object) -> bool:
    return isinstance(key, str) and _ID_RE.fullmatch(key) is not None


def _validated_ids(ids: Collection[str]) -> list[str]:
    """Return the distinct ids in order; ConfigError before any deletion (U02-67)."""
    if isinstance(ids, str) or not 1 <= len(ids) <= _MAX_IDS:
        msg = f"vector delete needs 1 to {_MAX_IDS} ids"
        raise ConfigError(msg)
    for index, key in enumerate(ids):
        if not _valid_id(key):
            # The id itself is never echoed: it may be a crafted filter fragment.
            msg = "invalid vector id"
            raise ConfigError(msg, index=index)
    return list(dict.fromkeys(ids))


def _in_filters(column: str, keys: list[str]) -> list[str]:
    chunks = (keys[i : i + _DELETE_CHUNK] for i in range(0, len(keys), _DELETE_CHUNK))
    return [f"{column} IN ({', '.join(f"'{key}'" for key in chunk)})" for chunk in chunks]


def _store_error(exc: BaseException, name: str, action: str) -> HernessError:
    """Map a LanceDB failure: commit conflicts and busy locks are retryable, the rest fatal."""
    error_type = type(exc).__name__
    text = str(exc).lower()
    if any(marker in text for marker in _BUSY_MARKERS):
        msg = f"vector table {name} busy (commit conflict or lock) during {action}"
        return StoreBusy(msg, table=name, error_type=error_type)
    msg = f"vector {action} failed on {name}"
    return SchemaViolation(msg, table=name, error_type=error_type)


class VectorStore:
    """Thin wrapper over one LanceDB database directory (U02-64)."""

    def __init__(self, path: Path | None = None) -> None:
        """Connect to ``path`` (default ``data_layout().vectors``); creates no table."""
        target = data_layout().vectors if path is None else path
        try:
            target.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(target))
        except _LANCE_ERRORS as exc:
            msg = "cannot open vector store"
            raise SchemaViolation(msg) from exc
        self.path = target

    def _table_names(self) -> set[str]:
        # list_tables replaces the deprecated table_names() in the pinned lancedb (OI-12).
        names: set[str] = set()
        token: str | None = None
        while True:
            page = self._db.list_tables(page_token=token)
            names.update(page.tables)
            token = page.page_token
            if not token:
                return names

    def ensure_tables(self) -> None:
        """Create missing tables with the declared schemas and verify existing ones (U02-65)."""
        existing = self._table_names()
        for name, schema in _SCHEMAS.items():
            if name not in existing:
                self._create(name, schema)
            self._verify(name, schema)

    def _create(self, name: str, schema: pa.Schema) -> None:
        try:
            self._db.create_table(name, schema=schema)
        except _LANCE_ERRORS as exc:
            # A concurrent creator in another process wins the race: re-list and verify.
            if "already exists" in str(exc) and name in self._table_names():
                return
            msg = f"cannot create vector table {name}"
            raise SchemaViolation(msg, error_type=type(exc).__name__) from exc
        _log.info("store.vectors.table_created", table=name)

    def _verify(self, name: str, schema: pa.Schema) -> None:
        actual = self._open(name).schema
        expected = {field.name: field.type for field in schema}
        found = {field.name: field.type for field in actual}
        for field_name in [*schema.names, *actual.names]:
            if expected.get(field_name) != found.get(field_name):
                msg = f"vector table {name} schema mismatch: {field_name[:64]}"
                raise SchemaViolation(msg)

    def table(self, name: TableName) -> Table:
        """Open an allowlisted table; ConfigError on unknown name, NotFoundError if absent."""
        _check_name(name)
        return self._open(name)

    def _open(self, name: str) -> Table:
        try:
            return self._db.open_table(name)
        except _LANCE_ERRORS as exc:
            if isinstance(exc, ValueError) and "not found" in str(exc).lower():
                msg = f"vector table {name} does not exist"
                raise NotFoundError(msg, kind="vector_table", key=name) from exc
            raise _store_error(exc, name, "open") from exc

    def delete_ids(self, name: TableName, column: str, ids: Collection[str]) -> int:
        """Delete rows whose allowlisted ``column`` is in ``ids``; return rows deleted (U02-67)."""
        _check_name(name)
        if column not in _DELETE_COLUMNS[name]:
            msg = f"column {column[:64]} not allowed for deletes on {name}"
            raise ConfigError(msg, table=name)
        filters = _in_filters(column, _validated_ids(ids))
        table = self._open(name)
        try:
            rows = sum(table.count_rows(where) for where in filters)
            if rows:
                for where in filters:
                    table.delete(where)
        except _LANCE_ERRORS as exc:
            raise _store_error(exc, name, "delete") from exc
        _log.info("store.vectors.deleted", table=name, rows=rows)
        return rows

    def purge_history(self, name: TableName) -> None:
        """Compact and drop every old version so deleted rows cannot be read back (U02-68)."""
        _check_name(name)
        table = self._open(name)
        try:
            # OI-12: pinned lancedb 0.39.0 needs pylance for compact_files() and deprecates
            # it; optimize() compacts and cleans up in one call, as the spec allows.
            table.optimize(cleanup_older_than=datetime.timedelta(0), delete_unverified=True)
        except _LANCE_ERRORS as exc:
            raise _store_error(exc, name, "history purge") from exc
        _log.info("store.vectors.history_purged", table=name)

    def count(self, name: TableName) -> int:
        """Row count of a table; NotFoundError if absent (U02-69)."""
        _check_name(name)
        table = self._open(name)
        try:
            return table.count_rows()
        except _LANCE_ERRORS as exc:
            raise _store_error(exc, name, "count") from exc

    def health(self) -> HealthResult:
        """Health for ``herness doctor``: down, degraded (a table missing) or ok (U02-70)."""
        try:
            names = self._table_names()
        except Exception as exc:  # noqa: BLE001 - health never raises; the class name is the reason
            return ("down", type(exc).__name__)
        missing = [name for name in _SCHEMAS if name not in names]
        if missing:
            return ("degraded", "missing vector tables: " + ", ".join(missing))
        return ("ok", "vector tables present")
