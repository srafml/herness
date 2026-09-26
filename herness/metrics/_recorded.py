"""DuckDB mechanics of recorded execution (impl 04 U04-12 steps 3, 5, 6, 8); private.

``herness.metrics.evidence.run_recorded`` orchestrates these helpers; limits and the allowlist
come from ``evidence`` as arguments, so this module never imports it at import time.
Table names reach SQL text only after ``IntoSpec`` checked them against ``WRITABLE_TABLES``.
"""

from __future__ import annotations

import collections
import json
import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from typing import Final, Protocol

import duckdb
import pyarrow as pa

from herness.core import ids
from herness.core.errors import ConfigError, QueryError, SchemaViolation
from herness.core.logging import get_logger
from herness.metrics._encode import HashAccumulator, hash_arrow_batch

BATCH_ROWS: Final[int] = 10_000
_IN_FLIGHT_PER_WORKER: Final[int] = 2
_MAX_PARAMS_BYTES: Final[int] = 65_536
_SQL_FORBIDDEN: Final = ("--", "/*", ";")
_log: Final = get_logger("metrics")

type Columns = tuple[tuple[str, str], ...]
type Row = tuple[object, ...]


class IntoLike(Protocol):
    """The ``IntoSpec`` fields the materialization reads."""

    @property
    def table(self) -> str: ...
    @property
    def mode(self) -> str: ...
    @property
    def id_column(self) -> str: ...
    @property
    def upstream_query_ids(self) -> tuple[str, ...]: ...


def prepare(
    sql: str, params: Mapping[str, object], producer: str | None, *, has_into: bool
) -> tuple[dict[str, object], str, str]:
    """Preconditions and U04-12 steps 1-2: (canonical params, their JSON text, normalized SQL)."""
    from herness.metrics.evidence import canonical_params  # noqa: PLC0415 - owner, cycle

    if has_into and producer is None:
        msg = "into requires a producer: stored results are always recorded"
        raise ConfigError(msg)
    bind, template = params.get("bind"), params.get("template")
    if set(params) != {"bind", "template"} or not (
        isinstance(bind, Mapping) and isinstance(template, Mapping)
    ):
        msg = "params must be exactly the mappings bind and template"
        raise ConfigError(msg)
    p = canonical_params(bind, template)
    text = ids.canonical_json(p)
    if len(text.encode("utf-8")) > _MAX_PARAMS_BYTES:
        msg = "params too large"
        raise ConfigError(msg)
    norm = ids.normalize_sql(sql)
    if any(token in norm for token in _SQL_FORBIDDEN):
        msg = "recorded SQL must not contain comments or semicolons"
        raise ConfigError(msg)
    return p, text, norm


def query_error(
    err: duckdb.Error, timed_out: bool, timeout_s: float | None, qid: str
) -> QueryError:
    """QueryError for a DuckDB error: the timeout text when the timer fired, else its message."""
    msg = f"timeout after {timeout_s}s" if timed_out else str(err)
    return QueryError(msg, query_id=qid)


def read_build_id(con: duckdb.DuckDBPyConnection) -> str:
    """The single ``meta.build`` row's build_id. Raises SchemaViolation otherwise."""
    msg = "meta.build must hold one row"
    try:
        rows = con.execute("SELECT build_id FROM meta.build").fetchall()
    except duckdb.Error as err:
        raise SchemaViolation(msg) from err
    if len(rows) != 1 or not isinstance(rows[0][0], str):
        raise SchemaViolation(msg)
    return rows[0][0]


@contextmanager
def interrupt_after(
    con: duckdb.DuckDBPyConnection, timeout_s: float | None, fired: threading.Event
) -> Iterator[None]:
    """Interrupt ``con`` after ``timeout_s`` seconds (None: no timer), setting ``fired`` first."""
    if timeout_s is None:
        yield
        return

    def fire() -> None:
        fired.set()
        con.interrupt()

    timer = threading.Timer(timeout_s, fire)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def _columns(res: duckdb.DuckDBPyConnection) -> Columns:
    return tuple((str(d[0]), str(d[1])) for d in res.description or ())


def collect_rows(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    bind: Mapping[str, object],
    *,
    max_rows: int,
    sample_limit: int,
    qid: str,
) -> tuple[Columns, list[Row], HashAccumulator]:
    """Step 6a: run ``sql``, keep the rows and feed the accumulator batch by batch."""
    from herness.metrics.evidence import iter_batch_rows  # noqa: PLC0415 - owner (R-15), cycle

    res = con.execute(sql, dict(bind))
    columns = _columns(res)
    acc = HashAccumulator(columns, sample_limit=sample_limit)
    rows: list[Row] = []
    for batch in res.to_arrow_reader(BATCH_ROWS):
        batch_rows = list(iter_batch_rows([batch]))
        if len(rows) + len(batch_rows) > max_rows:
            msg = f"result too large: more than {max_rows} rows"
            raise QueryError(msg, query_id=qid)
        rows.extend(batch_rows)
        acc.add_rows(batch_rows)
    return columns, rows, acc


def _id_expr(id_column: str) -> str:
    if id_column == "query_id":
        return "CAST($__query_id AS VARCHAR) AS query_id"
    return (
        "list_concat([CAST($__query_id AS VARCHAR)], CAST($__upstream AS VARCHAR[])) AS query_ids"
    )


def _filter(into: IntoLike) -> str:
    if into.mode == "replace":
        return ""
    if into.id_column == "query_id":
        return " WHERE query_id = $__query_id"
    return " WHERE query_ids[1] = $__query_id"


def materialize(
    con: duckdb.DuckDBPyConnection, sql: str, bind: Mapping[str, object], into: IntoLike, qid: str
) -> tuple[str, dict[str, object]]:
    """Step 6b write: store the SELECT with its ID column; return (read-back filter, its binds).

    DuckDB accepts named parameters in CREATE TABLE AS and INSERT … SELECT (VI04-01).
    """
    binds = {**bind, "__query_id": qid}
    if into.id_column == "query_ids":
        binds["__upstream"] = list(into.upstream_query_ids)
    select = f"SELECT s.*, {_id_expr(into.id_column)} FROM ({sql}) s"  # noqa: S608 - allowlisted
    if into.mode == "replace":
        con.execute(f"CREATE OR REPLACE TABLE {into.table} AS {select}", binds)
    else:
        con.execute(f"INSERT INTO {into.table} {select}", binds)
    where = _filter(into)
    return where, ({"__query_id": qid} if where else {})


def _ipc_bytes(batch: pa.RecordBatch) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, batch.schema) as writer:
        writer.write_batch(batch)
    return bytes(sink.getvalue().to_pybytes())


def hash_in_workers(
    reader: pa.RecordBatchReader, columns: Columns, acc: HashAccumulator, workers: int, limit: int
) -> None:
    """Hash each batch as IPC bytes in a process pool (U04-04) and merge in batch order."""
    window: collections.deque[Future[tuple[list[bytes], list[tuple[bytes, str]]]]]
    window = collections.deque()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for batch in reader:
            window.append(pool.submit(hash_arrow_batch, columns, _ipc_bytes(batch), limit))
            if len(window) >= workers * _IN_FLIGHT_PER_WORKER:
                acc.add_digests(*window.popleft().result())
        while window:
            acc.add_digests(*window.popleft().result())


def read_back(
    con: duckdb.DuckDBPyConnection,
    into: IntoLike,
    filt: tuple[str, dict[str, object]],
    *,
    sample_limit: int,
    parallel_min: int,
    workers: int,
) -> tuple[Columns, HashAccumulator]:
    """Step 6b hash: the stored rows without the ID column, in workers from ``parallel_min``."""
    from herness.metrics.evidence import iter_batch_rows  # noqa: PLC0415 - owner (R-15), cycle

    where, binds = filt
    count_row = con.execute(f"SELECT count(*) FROM {into.table}{where}", binds).fetchone()  # noqa: S608
    count = int(count_row[0]) if count_row else 0
    res = con.execute(f"SELECT * EXCLUDE ({into.id_column}) FROM {into.table}{where}", binds)  # noqa: S608
    columns = _columns(res)
    acc = HashAccumulator(columns, sample_limit=sample_limit)
    reader = res.to_arrow_reader(BATCH_ROWS)
    if count >= parallel_min:
        hash_in_workers(reader, columns, acc, workers, sample_limit)
    else:
        acc.add_rows(iter_batch_rows(reader))
    return columns, acc


def record_evidence(
    con: duckdb.DuckDBPyConnection,
    values: tuple[str, str, str, str, int, list[dict[str, object]], datetime, str],
    build_id: str,
) -> None:
    """Step 8: insert the ``meta.evidence`` row unless present; a different stored hash raises
    SchemaViolation "nondeterministic result for <qid>" (TH04-06)."""
    qid, sql, params_text, digest, count, sample, executed_at, producer = values
    sample_text = json.dumps(sample, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    con.execute(
        "INSERT INTO meta.evidence (query_id, sql, params, result_hash, row_count,"
        " result_sample, executed_at, producer) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (query_id) DO NOTHING",
        [qid, sql, params_text, digest, count, sample_text, executed_at, producer],
    )
    stored = con.execute("SELECT result_hash FROM meta.evidence WHERE query_id = ?", [qid])
    row = stored.fetchone()
    if row is None or row[0] != digest:
        _log.error("metrics.evidence.hash_conflict", query_id=qid, build_id=build_id)
        msg = f"nondeterministic result for {qid}"
        raise SchemaViolation(msg, query_id=qid)
