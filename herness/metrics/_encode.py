"""Cell encoding, row digests and batch hashing: the pinned ``result_hash`` rules (R-15, DD04-07).

Texts are built directly, never via ``canonical_json``, which sorts keys and loses column order.
"""

from __future__ import annotations

import base64
import datetime
import decimal
import hashlib
import heapq
import json
import math
import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from typing import Final, cast

import pyarrow as pa

from herness.core import time as clock
from herness.core.errors import SchemaViolation

MAX_SAMPLE_LIMIT: Final[int] = 1000
_SIGNED: Final = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT")
INTEGER_TYPES: Final = frozenset(_SIGNED) | {"U" + name for name in _SIGNED}
FLOAT_TYPES: Final = frozenset({"FLOAT", "REAL", "DOUBLE"})
_TS_SUFFIXES: Final = ("", "_S", "_MS", "_NS")
_TIMESTAMP_TYPES: Final = frozenset({"TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ"}) | {
    "TIMESTAMP" + suffix for suffix in _TS_SUFFIXES
}
_TEXT_TYPES: Final = frozenset({"VARCHAR", "UUID", "JSON"})
_DECIMAL_RE: Final = re.compile(r"DECIMAL\((\d+),\s*(\d+)\)")
_OPEN_SPACE_RE: Final = re.compile(r"\(\s+")
_CLOSE_SPACE_RE: Final = re.compile(r"\s+\)")
_DECIMAL_CONTEXT: Final = decimal.Context(prec=100)  # DECIMAL(38, s) quantizes exactly
_SEP: Final = (",", ":")
_CONVERSION_ERRORS: Final = (TypeError, ValueError, ArithmeticError, AttributeError)


def normalize_type(duckdb_type: str) -> str:
    """Upper-case a DuckDB type string and drop spaces directly inside parentheses."""
    return _CLOSE_SPACE_RE.sub(")", _OPEN_SPACE_RE.sub("(", duckdb_type.upper().strip()))


def _text(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _boolean(value: object) -> str:
    return "true" if bool(value) else "false"


def _integer(value: object) -> str:
    return str(int(cast("int", value)))


def _float_token(value: object) -> str:
    x = float(cast("float", value))
    if math.isnan(x):
        return '"nan"'
    if math.isinf(x):
        return '"inf"' if x > 0 else '"-inf"'
    return format(x + 0.0, ".9g")  # adding +0.0 turns -0.0 into 0.0


def _decimal_text(value: object, scale: int | None = None) -> str:
    d = value if isinstance(value, decimal.Decimal) else decimal.Decimal(str(value))
    if not d.is_finite():
        msg = "non-finite decimal"
        raise ValueError(msg)
    if scale is not None:
        d = d.quantize(decimal.Decimal(1).scaleb(-scale), decimal.ROUND_HALF_EVEN, _DECIMAL_CONTEXT)
    return json.dumps(format(d.copy_abs() if d.is_zero() else d, "f"))


def _date(value: object) -> str:
    return json.dumps(cast("datetime.date", value).isoformat())


def _timestamp(value: object) -> str:
    ts = cast("datetime.datetime", value)
    if ts.tzinfo is None or ts.utcoffset() is None:
        ts = ts.replace(tzinfo=datetime.UTC)  # naive timestamps are UTC
    return json.dumps(clock.format_utc(ts))


def _time(value: object) -> str:
    t = cast("datetime.time", value)
    return json.dumps(f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}.{t.microsecond:06d}")


def _interval(value: object) -> str:
    if isinstance(value, datetime.timedelta):
        return _float_token(value.total_seconds())
    # Arrow MonthDayNano; DuckDB's fetchall timedelta counts a month as 30 days.
    months, days, nanos = cast("tuple[int, int, int]", value)
    return _float_token(((months * 30 + days) * 86_400_000_000 + nanos // 1000) / 1_000_000)


def _blob(value: object) -> str:
    return json.dumps(base64.b64encode(memoryview(cast("bytes", value))).decode("ascii"))


_SCALAR_ENCODERS: Final[dict[str, Callable[[object], str]]] = {
    "BOOLEAN": _boolean,
    "DATE": _date,
    "TIME": _time,
    "INTERVAL": _interval,
    "BLOB": _blob,
    **dict.fromkeys(INTEGER_TYPES, _integer),
    **dict.fromkeys(FLOAT_TYPES, _float_token),
    **dict.fromkeys(_TIMESTAMP_TYPES, _timestamp),
    **dict.fromkeys(_TEXT_TYPES, _text),
}


def _unsupported(value: object, duckdb_type: str) -> SchemaViolation:
    msg = f"unsupported result value type {type(value).__name__} for column type {duckdb_type}"
    return SchemaViolation(msg)


def _dict_key(key: object, duckdb_type: str) -> str:
    text = key if isinstance(key, str) else _encode_python(key, duckdb_type)
    return json.dumps(text, ensure_ascii=False)


def _encode_python(value: object, duckdb_type: str) -> str:
    """Step 14: encode by Python type (STRUCT, MAP, UNION and any other column type)."""
    if value is None:
        return "null"
    kind = duckdb_type
    if isinstance(value, dict):
        items = (_dict_key(k, kind) + ":" + _encode_python(v, kind) for k, v in value.items())
        return "{" + ",".join(items) + "}"
    # Arrow's MonthDayNano is a named tuple with a nanoseconds field.
    if isinstance(value, datetime.timedelta) or hasattr(value, "nanoseconds"):
        return _interval(value)
    if isinstance(value, list | tuple):
        return "[" + ",".join(_encode_python(e, kind) for e in value) + "]"
    for kinds, encoder in _PYTHON_ENCODERS:
        if isinstance(value, kinds):
            return encoder(value)
    raise _unsupported(value, kind)


# Order matters: bool before int, datetime before date.
_PYTHON_ENCODERS: Final[tuple[tuple[type | tuple[type, ...], Callable[[object], str]], ...]] = (
    (bool, _boolean),
    (int, _integer),
    (float, _float_token),
    (decimal.Decimal, _decimal_text),
    ((str, uuid.UUID), _text),
    (datetime.datetime, _timestamp),
    (datetime.date, _date),
    (datetime.time, _time),
    ((bytes, bytearray, memoryview), _blob),
)


def _encode_typed(value: object, norm: str, duckdb_type: str) -> str:
    if norm.endswith("]") and "[" in norm:
        if not isinstance(value, list | tuple):
            raise _unsupported(value, duckdb_type)
        el = norm[: norm.rindex("[")]
        parts = ("null" if e is None else _encode_typed(e, el, el) for e in value)
        return "[" + ",".join(parts) + "]"
    encoder = _SCALAR_ENCODERS.get(norm)
    if encoder is not None:
        return encoder(value)
    match = _DECIMAL_RE.fullmatch(norm)
    if match is not None:
        return _decimal_text(value, int(match.group(2)))
    if norm.startswith("ENUM("):
        return _text(value)
    if norm.startswith("MAP(") and isinstance(value, list):
        # Arrow gives MAP values as (key, value) pairs; fetchall gives a dict.
        value = dict(cast("list[tuple[object, object]]", value))
    return _encode_python(value, duckdb_type)


def encode_cell(value: object, duckdb_type: str) -> str:
    """Canonical JSON token of one cell by its DuckDB type (U04-01). Raises SchemaViolation."""
    if value is None:
        return "null"
    if not duckdb_type:
        msg = "empty column type"
        raise SchemaViolation(msg)
    try:
        return _encode_typed(value, normalize_type(duckdb_type), duckdb_type)
    except _CONVERSION_ERRORS as err:
        msg = f"cannot encode value of type {type(value).__name__} for column type {duckdb_type}"
        raise SchemaViolation(msg) from err


def row_digest(
    names: Sequence[str], types: Sequence[str], row: Sequence[object]
) -> tuple[bytes, str]:
    """(SHA-256 digest, row JSON) of one row in column order (U04-02). Raises SchemaViolation."""
    if not len(names) == len(types) == len(row):
        msg = f"row width {len(row)} != column count {len(names)}"
        raise SchemaViolation(msg)
    pairs = zip(names, types, row, strict=True)
    cells = (json.dumps(n, ensure_ascii=False) + ":" + encode_cell(v, t) for n, t, v in pairs)
    text = "{" + ",".join(cells) + "}"
    return hashlib.sha256(text.encode("utf-8")).digest(), text


class HashAccumulator:
    """Row digests and smallest-digest sample of one result (U04-03); not thread-safe."""

    def __init__(self, columns: Sequence[tuple[str, str]], *, sample_limit: int = 50) -> None:
        if isinstance(sample_limit, bool) or not 0 <= sample_limit <= MAX_SAMPLE_LIMIT:
            msg = f"sample limit {sample_limit} outside 0..{MAX_SAMPLE_LIMIT}"
            raise SchemaViolation(msg)
        self._columns = tuple((name, kind) for name, kind in columns)
        self._names, self._types = [n for n, _ in self._columns], [t for _, t in self._columns]
        self._limit = sample_limit
        self._digests: list[bytes] = []
        # Bounded max-heap on digest: entries are (-digest as int, digest, row JSON).
        self._heap: list[tuple[int, bytes, str]] = []
        self._finished = False

    def _check_open(self) -> None:
        if self._finished:
            msg = "accumulator finished"
            raise SchemaViolation(msg)

    def _offer(self, digest: bytes, text: str) -> None:
        entry = (-int.from_bytes(digest, "big"), digest, text)
        if len(self._heap) < self._limit:
            heapq.heappush(self._heap, entry)
        elif self._heap and entry[0] > self._heap[0][0]:
            heapq.heapreplace(self._heap, entry)

    def add_rows(self, rows: Iterable[Sequence[object]]) -> None:
        """Digest each row and offer it to the sample."""
        self._check_open()
        for row in rows:
            digest, text = row_digest(self._names, self._types, row)
            self._digests.append(digest)
            self._offer(digest, text)

    def add_digests(self, digests: Iterable[bytes], sample: Iterable[tuple[bytes, str]]) -> None:
        """Merge digests and sample candidates computed elsewhere (U04-04)."""
        self._check_open()
        self._digests.extend(digests)
        for digest, text in sample:
            self._offer(digest, text)

    def _drain(self) -> tuple[list[bytes], list[tuple[bytes, str]]]:
        self._check_open()
        self._finished = True
        return self._digests, sorted(((d, t) for _, d, t in self._heap), key=lambda p: p[0])

    def finish(self) -> tuple[str, int, list[dict[str, object]]]:
        """(hash hex, row count, sample) of everything added; the accumulator then closes."""
        digests, pairs = self._drain()
        digests.sort()
        header = json.dumps([list(c) for c in self._columns], separators=_SEP, ensure_ascii=False)
        payload = header + "\n" + "\n".join(d.hex() for d in digests)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        sample = [cast("dict[str, object]", json.loads(text)) for _, text in pairs]
        return digest, len(digests), sample


def hash_arrow_batch(
    columns: tuple[tuple[str, str], ...], ipc_bytes: bytes, sample_limit: int
) -> tuple[list[bytes], list[tuple[bytes, str]]]:
    """Digests and smallest sample pairs of one Arrow IPC batch (U04-04). Raises SchemaViolation."""
    from herness.metrics.evidence import iter_batch_rows  # noqa: PLC0415 - owner (R-15), cycle

    acc = HashAccumulator(columns, sample_limit=sample_limit)
    reader = pa.ipc.open_stream(pa.py_buffer(ipc_bytes))
    if len(reader.schema.names) != len(columns):
        msg = f"row width {len(reader.schema.names)} != column count {len(columns)}"
        raise SchemaViolation(msg)
    acc.add_rows(iter_batch_rows(reader))
    return acc._drain()
