"""MongoDB connector: key-set paged reads and key listing (impl 01 U01-86, U01-87).

Design 01 §5.9 and §6. The connection URI is a secret resolved when the client is first
needed; it is checked for verified TLS and for hosts listed in ``sources.mongodb.hosts``
(R-06, TH01-18) before ``client_factory`` is called, and it is never logged, stored beyond
the client or put in an error (TH01-03). SRV targets are enforced by the socket guard.
Driver errors are mapped by ``_map_mongo_error`` without their text, which can name hosts.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Final
from urllib.parse import parse_qsl, unquote

import pyarrow as pa
import pymongo
from bson import json_util
from pymongo import errors as me
from pymongo.collection import Collection as _Collection
from pymongo.database import Database

from herness.connectors._auth_breaker import open_on_auth
from herness.connectors.base import KEY_SCHEMA
from herness.connectors.rows import RowBatcher, flatten_record, parse_source_timestamp, to_snake
from herness.connectors.settings import MongoSettings
from herness.connectors.settings_entities import MongoEntity
from herness.core import time as clock
from herness.core.errors import (
    AuthError,
    ConfigError,
    HernessError,
    SchemaViolation,
    SourceUnavailable,
)
from herness.core.logging import get_logger
from herness.core.registry import register
from herness.core.resilience import fault_point, retry_page
from herness.core.secrets import resolve

__all__ = ["MongoConnector"]

type Doc = Mapping[str, Any]
type Client = pymongo.MongoClient[dict[str, Any]]
type Collection = _Collection[dict[str, Any]]

_SOURCE: Final = "mongodb"
_TLS_MSG: Final = "MongoDB URI must enable verified TLS"
_HOST_MSG: Final = "MongoDB URI names a host not listed in sources.mongodb.hosts"
_PLAIN: Final = "mongodb://"
_SRV: Final = "mongodb+srv://"
_TLS_KEYS: Final = frozenset({"tls", "ssl"})
_INSECURE_KEYS: Final = frozenset(
    {"tlsinsecure", "tlsallowinvalidcertificates", "tlsallowinvalidhostnames"}
)
_AUTH_CODES: Final = frozenset({13, 18})  # Unauthorized, AuthenticationFailed
# ConnectionFailure covers AutoReconnect, NetworkTimeout and ServerSelectionTimeoutError.
_UNAVAILABLE: Final = (me.ConnectionFailure, me.ExecutionTimeout)
_CONNECT_TIMEOUT_MS: Final = 10_000

_log = get_logger("connectors.mongodb")


def _map_mongo_error(exc: me.PyMongoError) -> HernessError:
    """The Herness error for a driver error (U01-86 Algorithm); the driver text is dropped."""
    kind = type(exc).__name__
    if isinstance(exc, _UNAVAILABLE):  # before OperationFailure: ExecutionTimeout is one
        return SourceUnavailable("mongodb unavailable", source=_SOURCE, error_type=kind)
    if isinstance(exc, me.OperationFailure):
        if exc.code in _AUTH_CODES:
            return AuthError("mongodb authentication failed", source=_SOURCE, code=exc.code)
        return SchemaViolation("mongodb operation failed", source=_SOURCE, code=exc.code)
    if isinstance(exc, me.ConfigurationError):  # InvalidURI is a ConfigurationError
        return ConfigError("mongodb client configuration rejected", source=_SOURCE)
    return SourceUnavailable("mongodb unavailable", source=_SOURCE, error_type=kind)


def _driver[T](fn: Callable[[], T]) -> T:
    """Run one driver call; a driver error becomes its mapped error with no chained cause."""
    try:
        return fn()
    except me.PyMongoError as exc:
        raise open_on_auth(_SOURCE, _map_mongo_error(exc)) from None


def _uri_parts(uri: str) -> tuple[bool, list[str], list[tuple[str, str]]]:
    """``(srv, hosts, options)`` of a MongoDB URI; hosts and option keys/values lower-cased."""
    srv = uri.startswith(_SRV)
    if not (srv or uri.startswith(_PLAIN)):
        raise ConfigError(_TLS_MSG, source=_SOURCE)
    head, _, query = uri.split("://", 1)[1].partition("?")
    netloc = head.partition("/")[0].rpartition("@")[2]  # no user info, no database
    hosts = [_host(seed) for seed in netloc.split(",")]
    query = query.replace(";", "&")  # the legacy option separator
    options = [(k.lower(), v.lower()) for k, v in parse_qsl(query, keep_blank_values=True)]
    return srv, hosts, options


def _host(seed: str) -> str:
    """Host of one ``host[:port]`` seed; an IPv6 literal loses its brackets."""
    seed = unquote(seed).lower()
    if seed.startswith("["):
        return seed[1:].partition("]")[0]
    return seed.rpartition(":")[0] if ":" in seed else seed


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_uri(uri: str, allowed: tuple[str, ...]) -> None:
    """Raise ``ConfigError`` unless the URI enables verified TLS and names listed hosts only."""
    srv, hosts, options = _uri_parts(uri)
    tls = [value for key, value in options if key in _TLS_KEYS]
    insecure = any(key in _INSECURE_KEYS and value != "false" for key, value in options)
    if srv:
        tls_on = all(value == "true" for value in tls)  # TLS is on by default for +srv
    else:
        tls_on = bool(tls) and all(value == "true" for value in tls)
        tls_on = tls_on or all(_loopback(host) for host in hosts)
    if insecure or not tls_on:
        raise ConfigError(_TLS_MSG, source=_SOURCE)
    # Loopback needs no listing (the settings reject such entries; the socket guard allows it).
    if not all(host in allowed or _loopback(host) for host in hosts):
        raise ConfigError(_HOST_MSG, source=_SOURCE)


def _dig(doc: Doc, path: str) -> Any:  # noqa: ANN401 - a BSON value of any type
    """Value at a top-level or dotted field path; ``None`` when absent."""
    if path in doc:
        return doc[path]
    node: Any = doc
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _key(doc: Doc, cfg: MongoEntity, entity: str) -> Any:  # noqa: ANN401 - a BSON key value
    value = _dig(doc, cfg.key_field)
    if value is None:
        msg = "missing key field"
        raise SchemaViolation(msg, source=_SOURCE, entity=entity)
    return value


@register("connector", "mongodb")
class MongoConnector:
    """MongoDB source (U01-86); one instance per thread (``MongoClient`` is thread-safe)."""

    name: str = _SOURCE

    def __init__(
        self,
        settings: MongoSettings,
        *,
        clock: Callable[[], datetime.datetime] = clock.now,  # default: module; body: parameter
        client_factory: Callable[[str], Client] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._factory = client_factory or self._default_client
        self._client: Client | None = None
        self.entities: tuple[str, ...] = tuple(settings.entities)

    def _default_client(self, uri: str) -> Client:
        timeout_ms = int(self._settings.timeout_s * 1000)
        return pymongo.MongoClient(
            uri,
            tz_aware=True,
            tzinfo=datetime.UTC,
            readPreference="secondaryPreferred",
            connectTimeoutMS=_CONNECT_TIMEOUT_MS,
            serverSelectionTimeoutMS=timeout_ms,
            socketTimeoutMS=timeout_ms,
            appname="herness",
            retryReads=False,
        )

    def _entity(self, entity: str) -> MongoEntity:
        cfg = self._settings.entities.get(entity)
        if cfg is None:
            msg = f"unknown mongodb entity {entity[:64]}"
            raise ConfigError(msg, source=_SOURCE)
        return cfg

    def _database(self) -> Database[dict[str, Any]]:
        """The configured database; the client is built on first use (lazy)."""
        if self._client is None:
            auth = self._settings.auth
            if auth is None or auth.credentials is None:
                msg = "mongodb auth.credentials is required"
                raise ConfigError(msg, source=_SOURCE)
            uri = resolve(auth.credentials).get_secret_value()
            _check_uri(uri, self._settings.hosts)
            self._client = _driver(lambda: self._factory(uri))
        return self._client[self._settings.database]

    def watermark_field(self, entity: str) -> str:
        """The entity ``updated_field``."""
        return self._entity(entity).updated_field

    def check(self) -> None:
        """Ping; warn per entity whose collection has no index led by ``updated_field``."""
        db = self._database()
        _driver(lambda: db.command("ping"))
        for entity in self.entities:
            cfg = self._entity(entity)
            if not self._indexed(db[cfg.collection], cfg.updated_field):
                _log.warning(
                    "connectors.mongodb.index_missing", entity=entity, field=cfg.updated_field
                )

    @staticmethod
    def _indexed(coll: Collection, field: str) -> bool:
        """Whether some index of ``coll`` has ``field`` as its first key."""
        indexes = _driver(lambda: list(coll.list_indexes()))
        return any(next(iter(ix["key"]), None) == field for ix in indexes)

    def _page(
        self,
        coll: Collection,
        query: Mapping[str, Any],
        projection: Mapping[str, int],
        order: list[tuple[str, int]],
        size: int,
    ) -> list[dict[str, Any]]:
        """One page under ``retry_page``; the query carries the last pair, so a retry resumes."""
        max_time_ms = self._settings.max_time_ms

        def read() -> list[dict[str, Any]]:
            return list(
                coll.find(query, projection).sort(order).limit(size).max_time_ms(max_time_ms)
            )

        def fetch() -> list[dict[str, Any]]:
            fault_point("http.page", source=_SOURCE)
            return _driver(read)

        return retry_page(fetch, source=_SOURCE)

    def sync(
        self,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime | None = None,
    ) -> Iterator[pa.RecordBatch]:
        """Rows with ``since <= updated < until`` ascending by ``(updated_field, key_field)``."""
        cfg = self._entity(entity)
        updated, key = cfg.updated_field, cfg.key_field
        bounds = {op: v for op, v in (("$gte", since), ("$lt", until)) if v is not None}
        base: dict[str, Any] = dict(cfg.filter) | ({updated: bounds} if bounds else {})
        columns = list(dict.fromkeys([*cfg.fields, updated, key]))
        projection = dict.fromkeys(columns, 1)
        order = [(updated, pymongo.ASCENDING), (key, pymongo.ASCENDING)]
        size = self._settings.page_size_for(entity)
        batcher = RowBatcher(
            _SOURCE,
            entity,
            batch_rows=self._settings.batch_rows,
            columns=[to_snake(c) for c in columns],
            clock=self._clock,
        )
        coll = self._database()[cfg.collection]
        query: Mapping[str, Any] = base
        while True:
            docs = self._page(coll, query, projection, order, size)
            for doc in docs:
                batch = batcher.add(*self._row(doc, cfg, entity, columns))
                if batch is not None:
                    yield batch
            if len(docs) < size:
                break
            last_ts, last_key = _dig(docs[-1], updated), _dig(docs[-1], key)
            after = [{updated: {"$gt": last_ts}}, {updated: last_ts, key: {"$gt": last_key}}]
            query = {"$and": [base, {"$or": after}]}
        tail = batcher.flush()
        if tail is not None:
            yield tail

    def _row(
        self, doc: Doc, cfg: MongoEntity, entity: str, columns: list[str]
    ) -> tuple[str, datetime.datetime, str, dict[str, str | None]]:
        """``(source_key, updated_at, payload, fields)`` of one document."""
        source_key = str(_key(doc, cfg, entity))
        ts = _dig(doc, cfg.updated_field)  # an aware datetime is taken as it is (U01-87 step 3)
        aware = isinstance(ts, datetime.datetime) and ts.utcoffset() is not None
        updated_at = ts if aware else parse_source_timestamp(ts, field=cfg.updated_field)
        payload = json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS)
        record = json.loads(payload)
        fields = flatten_record({c: _dig(record, c) for c in columns})
        return source_key, updated_at, payload, fields

    def list_keys(self, entity: str) -> Iterator[pa.RecordBatch]:
        """Every current key of the filtered collection as ``KEY_SCHEMA`` batches, key-set
        paged over ``key_field`` ascending."""
        cfg = self._entity(entity)
        key = cfg.key_field
        size = self._settings.page_size_for(entity)
        coll = self._database()[cfg.collection]
        base: dict[str, Any] = dict(cfg.filter)
        query: Mapping[str, Any] = base
        while True:
            docs = self._page(coll, query, {key: 1}, [(key, pymongo.ASCENDING)], size)
            keys = [_key(doc, cfg, entity) for doc in docs]
            if keys:
                column = pa.array([str(k) for k in keys], pa.string())
                yield pa.RecordBatch.from_arrays([column], schema=KEY_SCHEMA)
            if len(docs) < size:
                return
            query = {"$and": [base, {key: {"$gt": keys[-1]}}]}
