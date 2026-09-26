"""Pure helpers that turn source records into lake batches (impl 01 U01-22 to U01-26)."""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Final, Literal

import pyarrow as pa
import pyarrow.compute as pc

from herness.connectors.base import METADATA_SCHEMA, record_id
from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation

_UTC = datetime.UTC
_UTC_US: Final = pa.timestamp("us", tz="UTC")
_NAME_IN_MAX: Final = 256
_NAME_OUT_MAX: Final = 128
_NOT_ALNUM: Final = re.compile(r"[^A-Za-z0-9]")
_LOWER_UPPER: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_UPPER_UPPER_LOWER: Final = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_UNDERSCORES: Final = re.compile(r"_+")
_SN_TIME: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?"
)
_DATE_ONLY: Final = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_COMPACT_OFFSET: Final = re.compile(r"([+-])([0-9]{2})([0-9]{2})$")
_LONG_FRACTION: Final = re.compile(r"(\.[0-9]{6})[0-9]+")
_YEARS: Final = (1970, 2100)
_PER_SECOND: Final = {"s": 1, "ms": 10**3, "us": 10**6, "ns": 10**9}
_EPOCH_S_HIGH: Final = 4_133_980_800  # 2101-01-01T00:00:00Z, exclusive
_EPOCHS: Final = {"s": 1, "ms": 1000}
_MAX_TOMBSTONES: Final = 100_000
_KEY_RE2: Final = r"^[^\x00-\x1f]{1,512}$"  # record_id key rule, vectorised (RE2)


def to_snake(name: str) -> str:
    """Return the lake column name of a source field, matching ``^[a-z][a-z0-9_]{0,127}$``."""
    out = _NOT_ALNUM.sub("_", name) if len(name) <= _NAME_IN_MAX else ""  # empty: error below
    out = _UPPER_UPPER_LOWER.sub("_", _LOWER_UPPER.sub("_", out))
    out = _UNDERSCORES.sub("_", out.lower()).strip("_")
    if out and (name.startswith("_") or out[0].isdigit()):
        out = "f_" + out
    if not out or len(out) > _NAME_OUT_MAX:
        msg = "unusable field name"
        raise SchemaViolation(msg)
    return out


def _text(value: object) -> str | None:  # noqa: PLR0911 - one return per type rule (U01-23)
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return json.dumps(value)
    if isinstance(value, datetime.datetime) and value.utcoffset() is not None:
        return value.astimezone(_UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return str(value)  # Decimal and every other type


def flatten_record(
    record: Mapping[str, object],
    *,
    fields: Sequence[str] | None = None,
    display_pairs: bool = False,
) -> dict[str, str | None]:
    """One-level flattening of a JSON record into ``to_snake``-named string columns."""
    out: dict[str, str | None] = {}

    def put(col: str, value: object) -> None:
        if col in out:
            msg = f"column collision {col}"
            raise SchemaViolation(msg)
        out[col] = _text(value)

    for key in record if fields is None else fields:
        col, value = to_snake(key), record.get(key)
        if display_pairs and isinstance(value, dict) and value.keys() == {"value", "display_value"}:
            put(col, value["value"])
            put(col + "_display", value["display_value"])
        else:
            put(col, value)
    return out


def _parse_text(text: str) -> datetime.datetime:
    match = _SN_TIME.fullmatch(text)
    if match is not None:
        *parts, fraction = match.groups()
        micros = int((fraction or "0")[:6].ljust(6, "0"))
        year, month, day, hour, minute, second = (int(p) for p in parts)
        return datetime.datetime(year, month, day, hour, minute, second, micros, tzinfo=_UTC)
    match = _DATE_ONLY.fullmatch(text)
    if match is not None:
        year, month, day = (int(p) for p in match.groups())
        return datetime.datetime(year, month, day, tzinfo=_UTC)
    text = _COMPACT_OFFSET.sub(r"\1\2:\3", text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(_LONG_FRACTION.sub(r"\1", text))
    if parsed.utcoffset() is None:
        raise ValueError
    return parsed


def _parse(value: object, epoch_unit: str | None) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        if value.utcoffset() is None:
            raise ValueError
        return value
    if isinstance(value, str):
        return _parse_text(value.strip())
    if isinstance(value, int | float) and not isinstance(value, bool) and epoch_unit in _EPOCHS:
        return datetime.datetime.fromtimestamp(value / _EPOCHS[epoch_unit], _UTC)
    raise ValueError


def parse_source_timestamp(
    value: object, *, field: str, epoch_unit: Literal["s", "ms"] | None = None
) -> datetime.datetime:
    """Parse a watermark value to an aware UTC datetime in years 1970 to 2100.

    Raises ``SchemaViolation(f"unparseable timestamp in {field}")``; the value is never echoed.
    """
    try:
        parsed = _parse(value, epoch_unit).astimezone(_UTC)
    except (ValueError, OverflowError, OSError):
        parsed = None
    if parsed is None or not _YEARS[0] <= parsed.year <= _YEARS[1]:
        msg = f"unparseable timestamp in {field}"
        raise SchemaViolation(msg) from None
    return parsed


def _bad_rows(field: str, count: int) -> SchemaViolation:
    return SchemaViolation(f"unparseable timestamp in {field} ({count} bad rows)")


def _out_of_range(array: pa.Array) -> int:
    """Count values outside 1970 to 2100 in the array's native unit (no nulls)."""
    dtype = array.type
    if pa.types.is_date32(dtype):
        ints, per_second = pc.cast(pc.cast(array, pa.int32()), pa.int64()), None
    else:
        ints = pc.cast(array, pa.int64())
        per_second = 10**3 if pa.types.is_date64(dtype) else _PER_SECOND[dtype.unit]
    high = _EPOCH_S_HIGH // 86_400 if per_second is None else _EPOCH_S_HIGH * per_second
    bad = pc.or_(pc.less(ints, 0), pc.greater_equal(ints, high))
    return int(pc.sum(bad).as_py() or 0)


def parse_arrow_timestamps(array: pa.Array, *, field: str) -> pa.TimestampArray:
    """Return ``array`` as ``timestamp("us", tz="UTC")``; naive timestamps are read as UTC."""
    dtype = array.type
    if pa.types.is_string(dtype) or pa.types.is_large_string(dtype):
        values: list[datetime.datetime | None] = []
        for item in array.to_pylist():
            try:
                values.append(parse_source_timestamp(item, field=field))
            except SchemaViolation:
                values.append(None)
        bad = values.count(None)
        if bad:
            raise _bad_rows(field, bad)
        result: pa.TimestampArray = pa.array(values, type=_UTC_US)
        return result
    temporal = pa.types.is_timestamp(dtype) or pa.types.is_date32(dtype)
    if not (temporal or pa.types.is_date64(dtype)):
        msg = f"unparseable timestamp in {field}"
        raise SchemaViolation(msg)
    if array.null_count:
        raise _bad_rows(field, array.null_count)
    bad = _out_of_range(array)
    if bad:
        raise _bad_rows(field, bad)
    result = pc.cast(array, options=pc.CastOptions(_UTC_US, allow_time_truncate=True))
    return result


def _utc(value: object, source: str, entity: str) -> datetime.datetime:
    if not isinstance(value, datetime.datetime) or value.utcoffset() is None:
        msg = "naive or missing timestamp in row"
        raise SchemaViolation(msg, source=source, entity=entity)
    return value.astimezone(_UTC)


type _Row = tuple[str, str, datetime.datetime, bool, str | None, Mapping[str, str | None]]


class RowBatcher:
    """Buffer flattened rows and emit ``pa.RecordBatch`` objects with the metadata columns.

    Each batch has ``METADATA_SCHEMA`` columns first, then ``columns``, then other field
    columns in first-seen order; a column seen once stays in every later batch. Not
    thread-safe; one per stream.
    """

    def __init__(
        self,
        source: str,
        entity: str,
        *,
        batch_rows: int,
        columns: Sequence[str] = (),
        clock: Callable[[], datetime.datetime] = clock.now,
    ) -> None:
        if type(batch_rows) is not int or batch_rows < 1:
            msg = "batch_rows must be an integer of at least 1"
            raise ConfigError(msg, source=source, entity=entity)
        self._source, self._entity, self._batch_rows = source, entity, batch_rows
        self._clock = clock
        self._columns: dict[str, None] = {}
        self._buffer: list[_Row] = []
        self._rows_emitted = 0
        self._note(columns)

    @property
    def rows_emitted(self) -> int:
        """Rows in emitted batches."""
        return self._rows_emitted

    def _note(self, names: Sequence[str] | Mapping[str, object]) -> None:
        for name in names:
            if name in METADATA_SCHEMA.names:
                msg = f"column collision {name}"
                raise SchemaViolation(msg, source=self._source, entity=self._entity)
            self._columns.setdefault(name, None)

    def _push(self, row: _Row) -> pa.RecordBatch | None:
        self._note(row[5])
        self._buffer.append(row)
        return self.flush() if len(self._buffer) >= self._batch_rows else None

    def add(
        self,
        source_key: str,
        updated_at: datetime.datetime,
        payload: str | None,
        fields: Mapping[str, str | None],
    ) -> pa.RecordBatch | None:
        """Buffer one live row; return a batch when the buffer reaches ``batch_rows``."""
        rid = record_id(self._source, self._entity, source_key)
        at = _utc(updated_at, self._source, self._entity)
        return self._push((rid, source_key, at, False, payload, fields))

    def add_tombstone(
        self, source_key: str, deleted_at: datetime.datetime
    ) -> pa.RecordBatch | None:
        """Buffer one tombstone row (``_deleted`` true; ``_payload`` and fields null)."""
        rid = record_id(self._source, self._entity, source_key)
        at = _utc(deleted_at, self._source, self._entity)
        return self._push((rid, source_key, at, True, None, {}))

    def flush(self) -> pa.RecordBatch | None:
        """Emit the buffer as one batch; ``None`` when it is empty."""
        rows = self._buffer
        if not rows:
            return None
        fetched = _utc(self._clock(), self._source, self._entity)
        n = len(rows)
        arrays: list[pa.Array] = [
            pa.array([r[0] for r in rows], pa.string()),
            pa.repeat(pa.scalar(self._source, pa.string()), n),
            pa.repeat(pa.scalar(self._entity, pa.string()), n),
            pa.array([r[1] for r in rows], pa.string()),
            pa.array([r[2] for r in rows], _UTC_US),
            pa.repeat(pa.scalar(fetched, _UTC_US), n),
            pa.array([r[3] for r in rows], pa.bool_()),
            pa.array([r[4] for r in rows], pa.string()),
        ]
        fields = list(METADATA_SCHEMA)
        for name in self._columns:
            arrays.append(pa.array([r[5].get(name) for r in rows], pa.string()))
            fields.append(pa.field(name, pa.string()))
        batch = pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields))
        self._buffer = []
        self._rows_emitted += n
        return batch


def tombstone_batch(
    source: str,
    entity: str,
    keys: pa.StringArray,
    *,
    deleted_at: datetime.datetime,
    fetched_at: datetime.datetime,
) -> pa.RecordBatch:
    """Return tombstone rows (``_deleted`` true, ``_payload`` null) with ``METADATA_SCHEMA``."""
    ok = isinstance(keys, pa.Array) and pa.types.is_string(keys.type)
    ok = ok and 1 <= len(keys) <= _MAX_TOMBSTONES and keys.null_count == 0
    ok = ok and pc.all(pc.match_substring_regex(keys, _KEY_RE2)).as_py()
    if not ok:
        msg = "invalid tombstone keys"
        raise SchemaViolation(msg, source=source, entity=entity)
    record_id(source, entity, keys[0].as_py())  # validates source and entity
    n = len(keys)
    deleted = _utc(deleted_at, source, entity)
    fetched = _utc(fetched_at, source, entity)
    arrays = [
        pc.binary_join_element_wise(pa.scalar(f"{source}:{entity}:"), keys, ""),
        pa.repeat(pa.scalar(source, pa.string()), n),
        pa.repeat(pa.scalar(entity, pa.string()), n),
        keys,
        pa.repeat(pa.scalar(deleted, _UTC_US), n),
        pa.repeat(pa.scalar(fetched, _UTC_US), n),
        pa.repeat(pa.scalar(True), n),
        pa.nulls(n, pa.string()),
    ]
    return pa.RecordBatch.from_arrays(arrays, schema=METADATA_SCHEMA)
