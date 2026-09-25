"""Shared ``result_hash``, samples, tolerance compare and parameter canonicalization (impl 04 §3).

``result_hash``, ``rows_equivalent`` and ``iter_batch_rows`` are defined only here (R-15); the
harness imports them. ``canonical_params`` pre-converts values and then uses the single
``herness.core.ids.canonical_json`` (R-14).
"""

from __future__ import annotations

import datetime
import decimal
import json
import math
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, Final, cast

import pyarrow as pa

from herness.core import ids
from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.metrics._encode import (
    FLOAT_TYPES,
    INTEGER_TYPES,
    HashAccumulator,
    encode_cell,
    normalize_type,
)

RESULT_SAMPLE_LIMIT: Final[int] = 50
MAX_RESULT_ROWS: Final[int] = 1_000_000
HASH_PARALLEL_MIN_ROWS: Final[int] = 200_000
HASH_WORKERS: Final[int] = min(8, os.cpu_count() or 1)
WRITABLE_TABLES: Final[frozenset[str]] = frozenset(
    {
        "metrics.incident_fact",
        "metrics.change_fact",
        "metrics.work_item_fact",
        "metrics.org_closure",
        "metrics.work_item_closure",
        "metrics.metric_value",
        "score.funding",
        "score.funding_attribution",
        "score.org",
        "score.action_lever",
        "score.portfolio",
    }
)

_NUMERIC_TYPES: Final = INTEGER_TYPES | FLOAT_TYPES
_ABS_TOL: Final[float] = 1e-9
_REL_TOL: Final[float] = 1e-6
_BIND_NAME_RE: Final = re.compile(r"[a-z_][a-z0-9_]{0,62}")
# Beyond canonical_json's depth limit values are passed through so it raises its own error.
_MAX_CONVERT_DEPTH: Final[int] = 64


def result_hash(columns: Sequence[tuple[str, str]], rows: Iterable[Sequence[Any]]) -> str:
    """Design 00 §5.1 ``result_hash`` with the pinned canonical rules (U04-05, R-15).

    Independent of row order. Raises SchemaViolation for unencodable cells or width mismatch.
    """
    acc = HashAccumulator(columns, sample_limit=0)
    acc.add_rows(rows)
    return acc.finish()[0]


def result_sample(
    columns: Sequence[tuple[str, str]],
    rows: Iterable[Sequence[Any]],
    *,
    limit: int = RESULT_SAMPLE_LIMIT,
) -> list[dict[str, object]]:
    """The ``limit`` rows with the smallest row digests, ascending, as JSON-ready dicts (U04-06).

    Raises SchemaViolation for a limit outside 0..1000 or as ``result_hash``.
    """
    acc = HashAccumulator(columns, sample_limit=limit)
    acc.add_rows(rows)
    return acc.finish()[2]


def _is_numeric(duckdb_type: str) -> bool:
    norm = normalize_type(duckdb_type)
    return norm in _NUMERIC_TYPES or norm.startswith("DECIMAL(")


def _sort_key(
    row: Sequence[Any], types: Sequence[str], numeric: Sequence[bool]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if len(row) != len(types):
        msg = f"row width {len(row)} != column count {len(types)}"
        raise SchemaViolation(msg)
    exact = tuple(encode_cell(v, t) for v, t in zip(row, types, strict=True))
    coarse = tuple(
        ("null" if v is None else format(float(v), ".6g")) if is_num else text
        for v, is_num, text in zip(row, numeric, exact, strict=True)
    )
    # The exact texts only break ties between rows with equal coarse keys.
    return coarse, exact


def _numbers_match(a: object, b: object) -> bool:
    if a is None or b is None:
        return a is None and b is None
    x, y = float(cast("float", a)), float(cast("float", b))
    if x == y or (math.isnan(x) and math.isnan(y)):
        return True
    return abs(x - y) <= _ABS_TOL + _REL_TOL * max(abs(x), abs(y))


def _rows_match(
    a: Sequence[Any], b: Sequence[Any], types: Sequence[str], numeric: Sequence[bool]
) -> bool:
    for va, vb, kind, is_num in zip(a, b, types, numeric, strict=True):
        if is_num:
            if not _numbers_match(va, vb):
                return False
        elif encode_cell(va, kind) != encode_cell(vb, kind):
            return False
    return True


def rows_equivalent(
    columns: Sequence[tuple[str, str]],
    rows_a: Sequence[Sequence[Any]],
    rows_b: Sequence[Sequence[Any]],
) -> bool:
    """Design 04 §4.4 Verifier cell tolerance between two results of one query (U04-07).

    Raises SchemaViolation when a cell cannot be encoded or a row width differs.
    """
    if len(rows_a) != len(rows_b):
        return False
    types = [kind for _, kind in columns]
    numeric = [_is_numeric(kind) for kind in types]
    sorted_a = sorted(rows_a, key=lambda r: _sort_key(r, types, numeric))
    sorted_b = sorted(rows_b, key=lambda r: _sort_key(r, types, numeric))
    return all(_rows_match(a, b, types, numeric) for a, b in zip(sorted_a, sorted_b, strict=True))


def iter_batch_rows(batches: Iterable[pa.RecordBatch]) -> Iterator[tuple[object, ...]]:
    """Rows of Arrow record batches in batch order, values as ``to_pylist`` gives them (U04-08)."""
    for batch in batches:
        cols = [c.to_pylist() for c in batch.columns]
        for i in range(batch.num_rows):
            yield tuple(col[i] for col in cols)


def _unsupported(key: str) -> ConfigError:
    msg = f"unsupported bind value for {key}"
    return ConfigError(msg)


def _param_leaf(value: object, key: str) -> object:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _unsupported(key)
        return value
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise _unsupported(key)
        return format(value, "f")
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise _unsupported(key)
        return clock.format_utc(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    raise _unsupported(key)


def _param_value(value: object, key: str, depth: int) -> object:
    if depth > _MAX_CONVERT_DEPTH:
        return value
    if isinstance(value, Mapping):
        return {k: _param_value(v, key, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_param_value(v, key, depth + 1) for v in value]
    return _param_leaf(value, key)


def canonical_params(
    bind: Mapping[str, object], template: Mapping[str, object]
) -> dict[str, object]:
    """JSON-round-tripped ``{"bind": ..., "template": ...}`` for ``query_id`` and binding (U04-09).

    Raises ConfigError for bad bind names, unsupported values or non-canonicalisable params.
    """
    names: Iterable[object] = bind.keys()
    for key in names:
        if not isinstance(key, str) or _BIND_NAME_RE.fullmatch(key) is None:
            msg = f"bad bind name {key}"
            raise ConfigError(msg)
    b = {k: _param_value(v, k, 1) for k, v in bind.items()}
    t = {k: _param_value(v, str(k), 1) for k, v in template.items()}
    try:
        text = ids.canonical_json({"bind": b, "template": t})
    except SchemaViolation as err:
        msg = "params not canonicalisable"
        raise ConfigError(msg) from err
    return cast("dict[str, object]", json.loads(text))
