"""Snowflake connector: bounded ordered SELECT with a scan guard (impl 01 U01-88, U01-89).

Design 01 §5.9. The key-pair credential is resolved when the connection is first needed and
handed to ``connect`` only (TH01-03); the driver derives the account host, which the settings
require in ``sources.snowflake.hosts`` and the socket guard enforces (R-06, TH01-18).
Identifiers come from validated config only, upper-cased and double-quoted; values are bound
parameters (TH01-07). Every query first runs ``EXPLAIN USING JSON`` and is refused above
``max_scan_gb`` (TH01-17). Driver errors are mapped by ``_map_sf_error`` without their text.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Iterator
from typing import Any, Final, Protocol, Self

import pyarrow as pa
import pyarrow.compute as pc
import snowflake.connector
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from snowflake.connector import errors as sfe

from herness.connectors.base import KEY_SCHEMA, METADATA_SCHEMA
from herness.connectors.rows import parse_arrow_timestamps, to_snake
from herness.connectors.settings import SnowflakeSettings
from herness.connectors.settings_entities import SnowflakeEntity
from herness.core import time as clock
from herness.core.errors import (
    AuthError,
    ConfigError,
    HernessError,
    RateLimited,
    SchemaViolation,
    SourceUnavailable,
)
from herness.core.registry import register
from herness.core.resilience import fault_point, retry_page
from herness.core.secrets import resolve_json

__all__ = ["SnowflakeConnector"]

type Params = dict[str, Any]

_SOURCE: Final = "snowflake"
_AUTH_STATE: Final = "28000"
_CANCELED_STATE: Final = "57014"  # statement canceled, including a queue timeout
_GIB: Final = 2**30
_NO_MONITOR: Final = frozenset({"", "null"})
_KEY_RE2: Final = r"^[^\x00-\x1f]{1,512}$"  # record_id key rule, vectorised (RE2)
_UTC_US: Final = pa.timestamp("us", tz="UTC")


class _Cursor(Protocol):
    """The DB-API cursor surface the connector uses (``SnowflakeCursor``)."""

    description: Any

    def __enter__(self) -> Self: ...
    def __exit__(self, *exc: object) -> None: ...
    def execute(self, sql: str, params: Params) -> object: ...
    def fetchone(self) -> Any: ...  # noqa: ANN401 - a driver row
    def fetchall(self) -> list[Any]: ...
    def fetch_arrow_batches(self) -> Iterator[pa.Table]: ...


class _Connection(Protocol):
    def cursor(self) -> _Cursor: ...


def _map_sf_error(exc: sfe.Error) -> HernessError:
    """The Herness error for a driver error (U01-88 step 3); the driver text is dropped."""
    state, kind = exc.sqlstate, type(exc).__name__
    if isinstance(exc, sfe.DatabaseError) and state == _AUTH_STATE:
        return AuthError("snowflake authentication failed", source=_SOURCE)
    if isinstance(exc, sfe.OperationalError | sfe.InterfaceError):
        return SourceUnavailable("snowflake unavailable", source=_SOURCE, error_type=kind)
    if isinstance(exc, sfe.ProgrammingError):
        if state == _CANCELED_STATE:
            return RateLimited("snowflake statement canceled", retry_after=None, source=_SOURCE)
        return ConfigError("snowflake query rejected", source=_SOURCE)  # §13 V-6
    return SourceUnavailable("snowflake unavailable", source=_SOURCE, error_type=kind)


def _driver[T](fn: Callable[[], T]) -> T:
    """Run one driver call; a driver error becomes its mapped error with no chained cause."""
    try:
        return fn()
    except sfe.Error as exc:
        raise _map_sf_error(exc) from None


def _quote(name: str) -> str:
    """A validated identifier, upper-cased and double-quoted (ENG §3.5)."""
    return '"' + name.upper() + '"'


def _der_key(pem: str, passphrase: str | None) -> bytes:
    """The PEM private key as unencrypted DER PKCS#8 bytes; any failure → ``ConfigError``."""
    try:
        key = serialization.load_pem_private_key(
            pem.encode(), password=passphrase.encode() if passphrase else None
        )
        der = key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    except (ValueError, TypeError, UnsupportedAlgorithm):
        msg = "snowflake private key cannot be read"
        raise ConfigError(msg, source=_SOURCE) from None
    return der


def _naive_utc(value: datetime.datetime) -> datetime.datetime:
    """A bound window value: naive UTC (the session runs with ``TIMEZONE = 'UTC'``)."""
    return value.astimezone(datetime.UTC).replace(tzinfo=None)


def _source_keys(column: pa.Array | pa.ChunkedArray, entity: str) -> pa.Array:
    """The key column as strings; a null or unusable key → ``SchemaViolation``."""
    if column.null_count:
        msg = "null key in result"
        raise SchemaViolation(msg, source=_SOURCE, entity=entity)
    keys = pc.cast(column, pa.string())
    if len(keys) and not pc.all(pc.match_substring_regex(keys, _KEY_RE2)).as_py():
        msg = "invalid source key in result"
        raise SchemaViolation(msg, source=_SOURCE, entity=entity)
    return keys


@register("connector", "snowflake")
class SnowflakeConnector:
    """Snowflake source (U01-88); one connection per instance, one instance per thread."""

    name: str = _SOURCE

    def __init__(
        self,
        settings: SnowflakeSettings,
        *,
        clock: Callable[[], datetime.datetime] = clock.now,  # default: module; body: parameter
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._connect: Callable[..., Any] = connect or snowflake.connector.connect
        self._conn: _Connection | None = None
        self.entities: tuple[str, ...] = tuple(settings.entities)

    def _entity(self, entity: str) -> SnowflakeEntity:
        cfg = self._settings.entities.get(entity)
        if cfg is None:
            msg = f"unknown snowflake entity {entity[:64]}"
            raise ConfigError(msg, source=_SOURCE)
        return cfg

    def _connection(self) -> _Connection:
        """The session; built on first use from the key-pair credential (lazy)."""
        if self._conn is None:
            s = self._settings
            if s.auth is None or s.auth.credentials is None:
                msg = "snowflake auth.credentials is required"
                raise ConfigError(msg, source=_SOURCE)
            cred = {k: v.get_secret_value() for k, v in resolve_json(s.auth.credentials).items()}
            if not (cred.get("user") and cred.get("private_key")):
                msg = "snowflake credential must hold user and private_key"
                raise ConfigError(msg, source=_SOURCE)
            der = _der_key(cred["private_key"], cred.get("passphrase"))
            session = {"STATEMENT_TIMEOUT_IN_SECONDS": s.statement_timeout_s, "TIMEZONE": "UTC"}
            self._conn = _driver(
                lambda: self._connect(
                    account=s.account,
                    user=cred["user"],
                    private_key=der,
                    warehouse=s.warehouse,
                    role=s.role,
                    session_parameters=session,
                    login_timeout=s.timeout_s,
                    network_timeout=s.timeout_s,
                    application="herness",
                    ocsp_fail_open=True,
                )
            )
        return self._conn

    def watermark_field(self, entity: str) -> str:
        """The entity ``updated_field``."""
        return self._entity(entity).updated_field

    def check(self) -> None:
        """The configured warehouse must have a resource monitor (TH01-17)."""
        warehouse = self._settings.warehouse
        with self._connection().cursor() as cur:
            _driver(lambda: cur.execute("SHOW WAREHOUSES LIKE %(wh)s", {"wh": warehouse}))
            names = [str(col[0]).lower() for col in cur.description or ()]
            rows = [dict(zip(names, row, strict=False)) for row in _driver(cur.fetchall)]
        # LIKE treats `_` as a wildcard: only the row naming this warehouse counts.
        mine = [row for row in rows if str(row.get("name", "")).upper() == warehouse.upper()]
        monitor = mine[0].get("resource_monitor") if mine else None
        if monitor is None or str(monitor).strip().lower() in _NO_MONITOR:
            msg = "warehouse has no resource monitor"
            raise ConfigError(msg, source=_SOURCE)

    def _scan_guard(self, cur: _Cursor, sql: str, params: Params) -> None:
        """``EXPLAIN USING JSON`` the query; refuse it above ``max_scan_gb`` (U01-88 step 2)."""
        _driver(lambda: cur.execute("EXPLAIN USING JSON " + sql, params))
        row = _driver(cur.fetchone)
        try:
            stats = json.loads(row[0])["GlobalStats"]
            scanned = int(stats["bytesAssigned"])
        except (TypeError, KeyError, IndexError, ValueError):
            msg = "EXPLAIN plan has no GlobalStats.bytesAssigned"
            raise SchemaViolation(msg, source=_SOURCE) from None
        gb = scanned / _GIB
        if gb > self._settings.max_scan_gb:
            msg = f"scan guard: {gb:.1f} GB exceeds max_scan_gb"
            raise ConfigError(msg, source=_SOURCE)

    def _tables(self, entity: str, sql: str, params: Params) -> Iterator[pa.Table]:
        """Tag the session, guard the scan, run the query under ``retry_page`` and stream its
        Arrow tables; an error while reading is mapped (the job retry restarts the query)."""
        with self._connection().cursor() as cur:
            tag = {"tag": f"herness:{entity}"}
            _driver(lambda: cur.execute("ALTER SESSION SET QUERY_TAG = %(tag)s", tag))
            self._scan_guard(cur, sql, params)

            def run() -> object:
                fault_point("http.page", source=_SOURCE)
                return _driver(lambda: cur.execute(sql, params))

            retry_page(run, source=_SOURCE)
            tables = _driver(cur.fetch_arrow_batches)
            while (table := _driver(lambda: next(tables, None))) is not None:
                yield table

    def _where(self, cfg: SnowflakeEntity, bounds: list[str]) -> str:
        parts = [*bounds, f"({cfg.filter})"] if cfg.filter else bounds
        return f" WHERE {' AND '.join(parts)}" if parts else ""

    def sync(
        self,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime | None = None,
    ) -> Iterator[pa.RecordBatch]:
        """Rows with ``since <= updated < until`` ascending by ``(updated_field, key_field)``;
        field columns keep their Arrow types (U01-89)."""
        cfg = self._entity(entity)
        upd, key = _quote(cfg.updated_field), _quote(cfg.key_field)
        params: Params = {}
        bounds: list[str] = []
        for name, op, value in (("since", ">=", since), ("until", "<", until)):
            if value is not None:
                params[name] = _naive_utc(value)
                bounds.append(f"{upd} {op} %({name})s")
        columns = ", ".join(_quote(c) for c in cfg.columns)
        # identifiers: validated config, upper-cased and quoted; values: bound
        sql = (
            f"SELECT {columns} FROM {self._table(cfg)}{self._where(cfg, bounds)}"  # noqa: S608 - see above
            f" ORDER BY {upd}, {key}"
        )
        at_key = [c.upper() for c in cfg.columns].index(cfg.key_field.upper())
        at_upd = [c.upper() for c in cfg.columns].index(cfg.updated_field.upper())
        for table in self._tables(entity, sql, params):
            for batch in table.to_batches(max_chunksize=self._settings.batch_rows):
                yield self._batch(batch, entity, cfg, (at_key, at_upd))

    @staticmethod
    def _table(cfg: SnowflakeEntity) -> str:
        return ".".join(_quote(part) for part in cfg.table.split("."))

    def _batch(
        self,
        batch: pa.RecordBatch,
        entity: str,
        cfg: SnowflakeEntity,
        at: tuple[int, int],
    ) -> pa.RecordBatch:
        """Metadata columns first, then the result columns renamed with ``to_snake``."""
        n = batch.num_rows
        keys = _source_keys(batch.column(at[0]), entity)
        updated = parse_arrow_timestamps(batch.column(at[1]), field=cfg.updated_field)
        payload = [json.dumps(row, default=str) for row in batch.to_pylist()]
        fetched = self._clock().astimezone(datetime.UTC)
        arrays: list[pa.Array] = [
            pc.binary_join_element_wise(pa.scalar(f"{_SOURCE}:{entity}:"), keys, ""),
            pa.repeat(pa.scalar(_SOURCE, pa.string()), n),
            pa.repeat(pa.scalar(entity, pa.string()), n),
            keys,
            updated,
            pa.repeat(pa.scalar(fetched, _UTC_US), n),
            pa.repeat(pa.scalar(False), n),
            pa.array(payload, pa.string()),
        ]
        fields = list(METADATA_SCHEMA)
        names = [to_snake(name) for name in batch.schema.names]
        if len(set(names)) < len(names):
            msg = "column collision in result"
            raise SchemaViolation(msg, source=_SOURCE, entity=entity)
        for name, field, column in zip(names, batch.schema, batch.columns, strict=True):
            fields.append(pa.field(name, field.type))
            arrays.append(column)
        return pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields))

    def list_keys(self, entity: str) -> Iterator[pa.RecordBatch]:
        """Every current key of the filtered table as ``KEY_SCHEMA`` batches, ordered by key."""
        cfg = self._entity(entity)
        key = _quote(cfg.key_field)
        where = self._where(cfg, [])
        sql = f"SELECT {key} FROM {self._table(cfg)}{where} ORDER BY {key}"  # noqa: S608
        for table in self._tables(entity, sql, {}):
            for batch in table.to_batches(max_chunksize=self._settings.batch_rows):
                keys = _source_keys(batch.column(0), entity)
                yield pa.RecordBatch.from_arrays([keys], schema=KEY_SCHEMA)
