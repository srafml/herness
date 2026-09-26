"""Shared connector protocols, lake metadata constants, ``record_id`` and ``split_range``.

Units U01-16 to U01-21 of impl 01. The metadata columns are the spec 02 §3.1 contract that
``herness.store.lake.LakeWriter`` validates. The ``http_client`` re-export listed in the
module map is added by T01-14 together with ``herness.connectors.http``.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterator
from typing import Final, Protocol, runtime_checkable

import pyarrow as pa

from herness.core.errors import ConfigError, SchemaViolation

_UTC_US: Final = pa.timestamp("us", tz="UTC")

METADATA_FIELDS: Final[tuple[str, ...]] = (
    "_record_id",
    "_source",
    "_entity",
    "_source_key",
    "_source_updated_at",
    "_fetched_at",
    "_deleted",
    "_payload",
)
METADATA_SCHEMA: Final = pa.schema(
    [
        pa.field("_record_id", pa.string(), nullable=False),
        pa.field("_source", pa.string(), nullable=False),
        pa.field("_entity", pa.string(), nullable=False),
        pa.field("_source_key", pa.string(), nullable=False),
        pa.field("_source_updated_at", _UTC_US, nullable=False),
        pa.field("_fetched_at", _UTC_US, nullable=False),
        pa.field("_deleted", pa.bool_(), nullable=False),
        pa.field("_payload", pa.string(), nullable=True),
    ]
)
KEY_SCHEMA: Final = pa.schema([pa.field("_source_key", pa.string(), nullable=False)])
DEFAULT_BATCH_ROWS: Final = 10000
DEFAULT_CHECKPOINT_ROWS: Final = 500000
UNORDERED_SOURCES: Final = frozenset({"files", "monitoring"})

_SOURCE_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
_ENTITY_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_KEY_RE: Final = re.compile(r"[^\x00-\x1f]{1,512}")  # no character below U+0020
_MAX_WINDOWS: Final = 100_000


class Connector(Protocol):
    """The ingestion edge every source implements (spec 00 §6, U01-16).

    ``sync`` yields batches of at most ``batch_rows`` rows: the eight metadata columns first
    with ``METADATA_SCHEMA`` types, then flattened fields; ``since`` inclusive, ``until``
    exclusive; ascending ``(_source_updated_at, _source_key)`` unless the connector's name is
    in ``UNORDERED_SOURCES``. An unknown entity raises ``ConfigError``. Not thread-safe.
    """

    name: str
    entities: tuple[str, ...]

    def check(self) -> None:
        """One cheap authenticated read; no lake or ops writes."""
        ...

    def sync(
        self,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime | None = None,
    ) -> Iterator[pa.RecordBatch]:
        """Yield the entity's rows changed in ``[since, until)`` as record batches."""
        ...

    def watermark_field(self, entity: str) -> str:
        """Return the source field name stored in ``watermark.field``."""
        ...


@runtime_checkable
class SupportsKeyListing(Protocol):
    """Optional capability used by reconciliation (U01-17)."""

    def list_keys(self, entity: str) -> Iterator[pa.RecordBatch]:
        """Yield every current source key as ``KEY_SCHEMA`` batches; raise, never end early."""
        ...


@runtime_checkable
class SupportsToolStreams(Protocol):
    """Fan-out connector processed one tool at a time (U01-18; ``MonitoringConnector``)."""

    def tools(self) -> tuple[str, ...]:
        """Return the enabled tool names in config order."""
        ...

    def stream_key(self, tool: str) -> str:
        """Return ``f"monitoring:{tool}"``: breaker key, watermark and slice source."""
        ...

    def sync_tool(
        self,
        tool: str,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime,
    ) -> Iterator[pa.RecordBatch]:
        """The ``Connector.sync`` batch contract for one tool."""
        ...


def _matches(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def record_id(source: str, entity: str, source_key: str) -> str:
    """Return ``f"{source}:{entity}:{source_key}"`` after validating the three parts.

    Raises ``SchemaViolation`` for an invalid part; the key value is never in the message
    (TH01-05).
    """
    valid = _matches(source, _SOURCE_RE) and _matches(entity, _ENTITY_RE)
    if not (valid and _matches(source_key, _KEY_RE)):
        msg = "invalid record key"
        raise SchemaViolation(msg, source=source, entity=entity)
    return f"{source}:{entity}:{source_key}"


def _aware(value: object) -> bool:
    return isinstance(value, datetime.datetime) and value.utcoffset() is not None


def _positive(step: object) -> bool:
    return isinstance(step, datetime.timedelta) and step > datetime.timedelta(0)


def split_range(
    start: datetime.datetime, end: datetime.datetime, step: datetime.timedelta
) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """Cut ``[start, end)`` into consecutive half-open windows of at most ``step``.

    ``start >= end`` gives an empty list. Naive input, ``step <= 0`` or more than 100,000
    windows raise ``ConfigError``.
    """
    if not (_aware(start) and _aware(end)):
        msg = "split_range needs timezone-aware datetimes"
        raise ConfigError(msg)
    if not _positive(step):
        msg = "split_range step must be positive"
        raise ConfigError(msg)
    if start < end and -((start - end) // step) > _MAX_WINDOWS:
        msg = f"split_range would produce more than {_MAX_WINDOWS} windows"
        raise ConfigError(msg)
    windows: list[tuple[datetime.datetime, datetime.datetime]] = []
    cur = start
    while cur < end:
        nxt = end if end - cur <= step else cur + step  # never overflows past ``end``
        windows.append((cur, nxt))
        cur = nxt
    return windows
