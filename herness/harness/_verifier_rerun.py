"""Re-run machinery of the Verifier: stored evidence, the re-run cache and the streaming scan.

Private sibling of ``herness.harness.verifier`` (impl 05 U05-63, design §5.6 steps 5a-5e),
split out so ``verifier.py`` stays within its 400-line module budget; only the Verifier
imports it. Every public name here is an implementation detail of ``Verifier``.

Re-runs use the same read-only warehouse connection and SQL guard as the agent tools
(TH05-04, TH05-12). ``result_hash``, ``rows_equivalent`` and ``iter_batch_rows`` come from
``herness.metrics.evidence`` and are never reimplemented (R-15). ``json_safe`` is a private
stand-in for the U05-35 step 6 JSON-safe conversion (``herness.harness.tools`` is not built
yet); replace it with that conversion when U05-35 lands.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from typing import Final, Literal

import duckdb
from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import ConfigError, QueryError, SchemaViolation
from herness.core.ids import query_id as compute_query_id
from herness.core.types import Evidence
from herness.harness.llm.settings import SqlSettings, VerifierSettings
from herness.harness.sql_guard import SqlGuard
from herness.harness.warehouse import DuckWarehouse, WarehousePool
from herness.metrics.evidence import iter_batch_rows, result_hash, rows_equivalent

VERIFIER_CACHE_ROWS: Final = 10_000
BATCH_ROWS: Final = 10_000
SAMPLE_ROWS: Final = 50
_ERROR_CHARS: Final = 200
_GUARD_PREFIX: Final = "SQL guard: "
_META_SQL: Final = (
    "SELECT sql, params, result_hash, row_count, result_sample FROM meta.evidence"
    " WHERE query_id = $q"
)
_NUMERIC_TYPES: Final = frozenset(
    {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "FLOAT", "REAL", "DOUBLE"}
)

type RowKey = Mapping[str, str | int | float | bool | None] | None
type Row = tuple[object, ...]
type Matcher = Callable[[Mapping[str, object], Mapping[str, str | int | float | bool | None]], bool]
type HashKind = Literal["equal", "equivalent", "values_only"]


def json_safe(value: object) -> JsonValue:
    """U05-35 step 6 JSON-safe cell: Decimal -> str, date -> ISO, datetime -> ISO UTC with Z.

    Bytes become ``None`` here (U05-35 omits them from the sample). # U05-35: replace.
    """
    if value is None or isinstance(value, bool | int | float | str):
        safe: JsonValue = value
    elif isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        safe = clock.format_utc(aware)
    elif isinstance(value, date | time):
        safe = value.isoformat()
    elif isinstance(value, bytes | bytearray | memoryview):
        safe = None
    elif isinstance(value, list | tuple):
        safe = [json_safe(member) for member in value]
    elif isinstance(value, dict):
        safe = {str(key): json_safe(member) for key, member in value.items()}
    else:  # Decimal -> str, and any other scalar as its text
        safe = str(value)
    return safe


def sample_rows(columns: Sequence[str], rows: Iterable[Row]) -> list[dict[str, JsonValue]]:
    """The first ``SAMPLE_ROWS`` rows as JSON-safe dicts; bytes cells are omitted (U05-35)."""
    sample: list[dict[str, JsonValue]] = []
    for row in rows:
        if len(sample) == SAMPLE_ROWS:
            break
        sample.append(
            {
                name: json_safe(cell)
                for name, cell in zip(columns, row, strict=True)
                if not isinstance(cell, bytes | bytearray | memoryview)
            }
        )
    return sample


def key_token(row_key: RowKey) -> str:
    """A stable text key for one ``row_key`` (``None`` means "the only row")."""
    return json.dumps(None if row_key is None else dict(row_key), sort_keys=True, default=str)


@dataclass(frozen=True, slots=True)
class StoredEvidence:
    """The recorded query a cited number points at, from ops or ``meta.evidence``."""

    sql: str
    bind: Mapping[str, JsonValue]
    result_hash: str
    row_count: int
    sample: Sequence[Mapping[str, JsonValue]]
    guard: bool


def from_ops(ev: Evidence) -> StoredEvidence:
    """Ops evidence (tamper-checked by ``get_evidence``, U05-71); re-run under the guard."""
    return StoredEvidence(ev.sql, ev.params, ev.result_hash, ev.row_count, ev.result_sample, True)


def _bind_of(params: object) -> Mapping[str, JsonValue]:
    # impl 04 records {"bind": ..., "template": ...} and executes with the bind part (U04-09).
    if isinstance(params, dict):
        if set(params) == {"bind", "template"} and isinstance(params["bind"], dict):
            return dict(params["bind"])
        return dict(params)
    return {}


def load_meta(pool: WarehousePool, query_id: str, build_id: str) -> StoredEvidence | None:
    """The build's ``meta.evidence`` row, or ``None`` when absent, unreadable or tampered."""
    try:
        cur = pool.get(build_id).cursor()
        row = cur.execute(_META_SQL, {"q": query_id}).fetchone()
    except (FileNotFoundError, ConfigError, QueryError, duckdb.Error):
        return None
    if row is None:
        return None
    sql_text, params_text, stored_hash, row_count, sample_text = row
    usable = isinstance(sql_text, str) and isinstance(stored_hash, str)
    if not usable or not isinstance(row_count, int) or isinstance(row_count, bool):
        return None  # a NULL sql, hash or row_count: the row cannot be re-run or compared
    try:
        params = json.loads(params_text) if isinstance(params_text, str) else params_text
        sample = json.loads(sample_text) if isinstance(sample_text, str) else sample_text
        recomputed = compute_query_id(sql_text, params, build_id)
    except (ValueError, TypeError, SchemaViolation):
        return None
    if recomputed != query_id:
        return None  # step 5b: tampered meta.evidence row
    rows = sample if isinstance(sample, list) else []
    return StoredEvidence(sql_text, _bind_of(params), stored_hash, row_count, rows, False)


@dataclass(slots=True)
class RefMatch:
    """Rows of one re-run matching one ``row_key``: count and the first matching row."""

    count: int = 0
    first: Row | None = None


@dataclass(frozen=True, slots=True)
class Rerun:
    """One cached re-run (U05-63 ``_Rerun``) plus the per-``row_key`` matches."""

    columns: tuple[str, ...]
    types: tuple[str, ...]
    rows: list[Row] | None
    result_hash: str
    row_count: int
    error: str | None
    matches: Mapping[str, RefMatch] = field(default_factory=dict)
    keys: Mapping[str, RowKey] = field(default_factory=dict)
    reason: str | None = None  # loggable category of `error`: never DuckDB message text


def failed(error: str, keys: Mapping[str, RowKey], reason: str | None = None) -> Rerun:
    """A failed re-run; ``reason`` defaults to ``error`` (a fixed category text)."""
    return Rerun((), (), None, "", 0, error, {}, dict(keys), reason or error)


class _TooLargeError(Exception):
    """The re-run exceeded ``sql.scan_rows``."""


class _Scan:
    """One streaming pass: counts rows, keeps up to ``VERIFIER_CACHE_ROWS``, records matches."""

    def __init__(
        self, columns: Sequence[str], keys: Mapping[str, RowKey], limit: int, matcher: Matcher
    ) -> None:
        self.count = 0
        self.rows: list[Row] | None = []
        self.matches = {token: RefMatch() for token in keys}
        self._columns = tuple(columns)
        self._limit = limit
        self._matcher = matcher
        self._keyed = [(token, key) for token, key in keys.items() if key is not None]
        self._unkeyed = [token for token, key in keys.items() if key is None]

    def feed(self, rows: Iterable[Row]) -> Iterator[Row]:
        for row in rows:
            self.count += 1
            if self.count > self._limit:
                raise _TooLargeError
            if self.rows is not None:
                self.rows = self.rows if self.count <= VERIFIER_CACHE_ROWS else None
            if self.rows is not None:
                self.rows.append(row)
            self._match(row)
            yield row

    def _match(self, row: Row) -> None:
        hits = list(self._unkeyed)
        if self._keyed:
            mapping = dict(zip(self._columns, row, strict=True))
            hits.extend(token for token, key in self._keyed if self._matcher(mapping, key))
        for token in hits:
            match = self.matches[token]
            match.count += 1
            if match.first is None:
                match.first = row


def _is_numeric(duckdb_type: str) -> bool:
    norm = duckdb_type.upper().strip()
    return norm.lstrip("U") in _NUMERIC_TYPES or norm.startswith("DECIMAL")


def hash_kind(stored: StoredEvidence, rerun: Rerun) -> HashKind | None:
    """Cell tolerance of design 04 §4.4 (R-15); ``None`` means result drift."""
    if rerun.result_hash == stored.result_hash:
        return "equal"
    if stored.row_count > SAMPLE_ROWS:
        return "values_only"
    if rerun.rows is None:
        return None
    # Both sides are JSON-safe: non-numeric cells compare as their JSON-safe text.
    columns = [
        (name, kind if _is_numeric(kind) else "VARCHAR")
        for name, kind in zip(rerun.columns, rerun.types, strict=True)
    ]
    stored_rows = [tuple(row.get(name) for name in rerun.columns) for row in stored.sample]
    rerun_rows = [tuple(json_safe(cell) for cell in row) for row in rerun.rows]
    try:
        return "equivalent" if rows_equivalent(columns, stored_rows, rerun_rows) else None
    except SchemaViolation:
        return None


class RerunCache:
    """``(query_id, build_id)`` -> ``Rerun``, lock-protected (U05-63 invariants)."""

    def __init__(
        self, pool: WarehousePool, cfg: VerifierSettings, sql: SqlSettings, matcher: Matcher
    ) -> None:
        self._pool = pool
        self._cfg = cfg
        self._sql = sql
        self._matcher = matcher
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], Rerun] = {}

    def peek(self, query_id: str, build_id: str) -> Rerun | None:
        with self._lock:
            return self._entries.get((query_id, build_id))

    def get(
        self, query_id: str, stored: StoredEvidence, build_id: str, keys: Mapping[str, RowKey]
    ) -> Rerun:
        """Step (1): reuse a hit; compute missing matches from kept rows; else re-run."""
        hit = self.peek(query_id, build_id)
        if hit is not None:
            missing = {token: key for token, key in keys.items() if token not in hit.keys}
            if not missing or hit.error is not None:
                return hit
            if hit.rows is not None:
                scan = _Scan(hit.columns, missing, len(hit.rows), self._matcher)
                for _ in scan.feed(hit.rows):
                    pass
                rerun = replace(
                    hit, matches={**hit.matches, **scan.matches}, keys={**hit.keys, **missing}
                )
                return self._store(query_id, build_id, rerun)
            keys = {**hit.keys, **missing}
        return self._store(query_id, build_id, self._compute(stored, build_id, keys))

    def _store(self, query_id: str, build_id: str, rerun: Rerun) -> Rerun:
        with self._lock:
            self._entries[(query_id, build_id)] = rerun
        return rerun

    def _compute(self, stored: StoredEvidence, build_id: str, keys: Mapping[str, RowKey]) -> Rerun:
        try:
            wh = self._pool.get(build_id)
            # A schema without tables allows nothing; sqlglot rejects empty mappings.
            schema = {name: tables for name, tables in wh.schema().items() if tables}
        except (FileNotFoundError, ConfigError, QueryError, duckdb.Error):
            return failed("build unavailable", keys)  # incl. a handle closed by another thread
        if stored.guard:
            try:
                SqlGuard(schema, self._sql.blocked_columns).check(stored.sql, allow_catalog=True)
            except QueryError as exc:
                return failed(f"guard: {exc.message.removeprefix(_GUARD_PREFIX)}", keys)
        return self._execute(wh, stored, keys)

    def _execute(
        self, wh: DuckWarehouse, stored: StoredEvidence, keys: Mapping[str, RowKey]
    ) -> Rerun:
        try:
            cur = wh.cursor()
        except duckdb.Error:
            return failed("build unavailable", keys)  # the pool closed the handle meanwhile
        timer = threading.Timer(self._cfg.rerun_timeout_s, cur.interrupt)
        timer.daemon = True
        timer.start()
        try:
            cur.execute(stored.sql, dict(stored.bind))
            description = cur.description or []
            columns = tuple(str(entry[0]) for entry in description)
            types = tuple(str(entry[1]) for entry in description)
            scan = _Scan(columns, keys, self._sql.scan_rows, self._matcher)
            batches = iter_batch_rows(cur.to_arrow_reader(BATCH_ROWS))
            digest = result_hash(list(zip(columns, types, strict=True)), scan.feed(batches))
        except _TooLargeError:
            return failed("too large", keys)
        except (duckdb.Error, SchemaViolation) as exc:
            reason = f"duckdb: {type(exc).__name__}"
            return failed((str(exc) or type(exc).__name__)[:_ERROR_CHARS], keys, reason)
        finally:
            timer.cancel()
        return Rerun(columns, types, scan.rows, digest, scan.count, None, scan.matches, dict(keys))
