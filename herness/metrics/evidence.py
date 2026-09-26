"""Shared ``result_hash``, samples, tolerance compare, parameter canonicalization and recorded
execution into ``meta.evidence`` (impl 04 §3).

``result_hash``, ``rows_equivalent`` and ``iter_batch_rows`` are defined only here (R-15); the
harness imports them. ``canonical_params`` pre-converts values and then uses the single
``herness.core.ids.canonical_json`` (R-14). ``run_recorded`` keeps its DuckDB mechanics in the
private sibling ``_recorded``.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import json
import math
import os
import re
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, Final, Literal, cast

import duckdb
import pyarrow as pa

from herness.core import ids
from herness.core import time as clock
from herness.core.errors import ConfigError, HernessError, SchemaViolation
from herness.core.logging import get_logger
from herness.core.resilience.metrics import record_counter, record_histogram
from herness.core.types import Evidence
from herness.metrics import _recorded as rec
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
_QUERY_ID_RE: Final = re.compile(r"q_[0-9a-f]{16}")
_log: Final = get_logger("metrics")

type Producer = Literal["facts", "metrics", "score"]


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
    if not (math.isfinite(x) and math.isfinite(y)):
        # Non-finite values match only themselves (NaN matches NaN); no tolerance applies.
        return x == y or (math.isnan(x) and math.isnan(y))
    return x == y or abs(x - y) <= _ABS_TOL + _REL_TOL * max(abs(x), abs(y))


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


# --- U04-10 … U04-12 recorded execution ------------------------------------------------------

_EVIDENCE_FIELDS: Final = (
    "query_id", "build_id", "sql", "params", "result_hash", "row_count", "result_sample",
    "executed_at", "duration_ms",
)  # fmt: skip


@dataclasses.dataclass(frozen=True, slots=True)
class RecordedQuery:
    """Everything a caller needs to persist evidence and use a result (U04-10).

    ``rows`` is None for a materialized result, else ``len(rows) == row_count``.
    """

    query_id: str
    sql: str
    params: dict[str, object]
    build_id: str
    result_hash: str
    row_count: int
    result_sample: list[dict[str, object]]
    columns: tuple[tuple[str, str], ...]
    rows: list[tuple[object, ...]] | None
    executed_at: datetime.datetime
    duration_ms: int

    def __post_init__(self) -> None:
        if self.rows is not None and len(self.rows) != self.row_count:
            msg = "recorded rows do not match row_count"
            raise SchemaViolation(msg)

    def to_evidence(self, run_id: str | None) -> Evidence:
        """The ops ``Evidence``: fields copied 1:1 plus ``run_id`` (design 05 §4.6)."""
        fields = {name: getattr(self, name) for name in _EVIDENCE_FIELDS}
        return Evidence.model_validate({**fields, "run_id": run_id})


@dataclasses.dataclass(frozen=True, slots=True)
class IntoSpec:
    """How a recorded SELECT is materialized into a stored table (U04-11, DD04-08, TH04-09).

    Raises ConfigError for a table outside ``WRITABLE_TABLES``, a bad mode or ID column, or
    upstream IDs that are malformed or given with ``id_column == "query_id"``.
    """

    table: str
    mode: Literal["replace", "append"]
    id_column: Literal["query_id", "query_ids"]
    upstream_query_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.table not in WRITABLE_TABLES:
            msg = f"table {str(self.table)[:80]} is not writable by metrics"
            raise ConfigError(msg)
        if self.mode not in ("replace", "append"):
            msg = "bad into mode"
            raise ConfigError(msg)
        if self.id_column not in ("query_id", "query_ids"):
            msg = "bad into id column"
            raise ConfigError(msg)
        ups: tuple[object, ...] = self.upstream_query_ids  # runtime check of the declared type
        well_formed = all(isinstance(u, str) and _QUERY_ID_RE.fullmatch(u) for u in ups)
        if not well_formed or (ups and self.id_column == "query_id"):
            msg = "bad upstream query id"
            raise ConfigError(msg)


def _execute(
    con: duckdb.DuckDBPyConnection,
    norm: str,
    bind: Mapping[str, object],
    qid: str,
    into: IntoSpec | None,
    max_rows: int,
) -> tuple[tuple[tuple[str, str], ...], list[tuple[object, ...]] | None, HashAccumulator]:
    """U04-12 step 6: (columns, rows or None, filled accumulator)."""
    if into is None:
        return rec.collect_rows(
            con, norm, bind, max_rows=max_rows, sample_limit=RESULT_SAMPLE_LIMIT, qid=qid
        )
    filt = rec.materialize(con, norm, bind, into, qid)
    columns, acc = rec.read_back(
        con,
        into,
        filt,
        sample_limit=RESULT_SAMPLE_LIMIT,
        parallel_min=HASH_PARALLEL_MIN_ROWS,
        workers=HASH_WORKERS,
    )
    return columns, None, acc


def run_recorded(  # noqa: PLR0913 - U04-12 signature (DD04-01): four keyword-only options
    con: duckdb.DuckDBPyConnection,
    sql: str,
    params: Mapping[str, object],
    producer: Producer | None,
    *,
    build_id: str | None = None,
    into: IntoSpec | None = None,
    timeout_s: float | None = None,
    max_rows: int = MAX_RESULT_ROWS,
) -> RecordedQuery:
    """Execute one SELECT with bound params, hash it and optionally store it and its evidence.

    U04-12. With ``producer`` exactly one ``meta.evidence`` row holds the query_id. Raises
    ConfigError (preconditions, comments, params size), SchemaViolation (``meta.build``,
    nondeterministic result, unencodable value) and QueryError (DuckDB error, timeout, too many
    rows; with ``query_id``).
    """
    start = time.perf_counter()
    p, params_text, norm = rec.prepare(sql, params, producer, has_into=into is not None)
    build = build_id if build_id is not None else rec.read_build_id(con)
    qid = ids.query_id(norm, p, build)
    template = cast("Mapping[str, object]", p["template"]).get("name")
    executed_at = clock.now()
    fired = threading.Event()
    try:
        try:
            with rec.interrupt_after(con, timeout_s, fired):
                bind = cast("Mapping[str, object]", p["bind"])
                columns, rows, acc = _execute(con, norm, bind, qid, into, max_rows)
            digest, count, sample = acc.finish()
            if producer is not None:
                values = (qid, norm, params_text, digest, count, sample, executed_at, producer)
                rec.record_evidence(con, values, build)
        except duckdb.Error as err:
            raise rec.query_error(err, fired.is_set(), timeout_s, qid) from err
    except HernessError as err:
        _log.error(
            "metrics.query.failed", query_id=qid, template=template, error_class=type(err).__name__
        )
        raise
    elapsed = time.perf_counter() - start
    duration_ms = int(elapsed * 1000)
    labels = {"producer": producer or "none"}
    _log.debug(
        "metrics.query.recorded",
        query_id=qid,
        producer=producer,
        template=template,
        row_count=count,
        duration_ms=duration_ms,
        build_id=build,
    )
    record_histogram(
        "herness_metrics_query_duration_seconds", elapsed, component="metrics", labels=labels
    )
    record_counter("herness_metrics_query_rows_total", count, component="metrics", labels=labels)
    return RecordedQuery(
        query_id=qid,
        sql=norm,
        params=p,
        build_id=build,
        result_hash=digest,
        row_count=count,
        result_sample=sample,
        columns=columns,
        rows=rows,
        executed_at=executed_at,
        duration_ms=duration_ms,
    )
