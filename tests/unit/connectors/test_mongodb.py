"""Tests for herness.connectors.mongodb.MongoConnector (impl 01 U01-86, U01-87; T01-22).

`mongomock` stands in for the server through the `client_factory` parameter; the URI comes
from the in-memory keyring. `retry_page` runs for real with a spy mongo_breaker and no-op sleeps.
"""

from __future__ import annotations

import datetime
import json
import random
from typing import Any

import pyarrow as pa
import pytest
from bson import ObjectId
from pymongo import errors as me
from structlog.testing import capture_logs
from tests.unit.connectors._mongo_data import (
    T0,
    URI,
    Factory,
    SpyBreaker,
    connector,
    flaky_find,
    settings,
)

import herness.connectors.mongodb as mongo_module
from herness.connectors.base import KEY_SCHEMA, METADATA_SCHEMA, Connector, SupportsKeyListing
from herness.connectors.mongodb import MongoConnector, _map_mongo_error
from herness.core.errors import AuthError, ConfigError, SchemaViolation, SourceUnavailable

pytestmark = pytest.mark.unit

UTC = datetime.UTC
_N = 2500


def _seed(factory: Factory, n: int = _N) -> list[dict[str, Any]]:
    """`n` docs in shuffled insertion order; timestamps repeat (7 docs per second)."""
    docs = [
        {"ts": T0 + datetime.timedelta(seconds=i // 7), "a": i, "b": {"x": i}, "skip": "no"}
        for i in range(n)
    ]
    random.Random(7).shuffle(docs)
    factory.collection().insert_many(docs)
    return docs


def _rows(batches: list[pa.RecordBatch]) -> list[dict[str, Any]]:
    return [row for batch in batches for row in batch.to_pylist()]


def test_ut01_84_sync_pages_ascending_after_injected_reconnect(
    mongo_breaker: SpyBreaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-84 2,500 docs, page 1,000, `AutoReconnect` once on page 2 → 2,500 rows ascending
    by (updated, key), no duplicates, `_source_key = str(_id)`, relaxed JSON payload."""
    factory = Factory()
    _seed(factory)
    flaky = flaky_find(monkeypatch, {2: me.AutoReconnect("gone")})
    conn = connector(factory, page_size=1000, batch_rows=1000)
    batches = list(conn.sync("orders", None))
    rows = _rows(batches)
    assert len(rows) == _N
    assert len({r["_source_key"] for r in rows}) == _N
    order = [(r["_source_updated_at"], ObjectId(r["_source_key"])) for r in rows]
    assert order == sorted(order)
    assert len(flaky.queries) == 4  # pages 1, 2 (failed), 2 (retried), 3 (500 docs)
    assert flaky.queries[1] == flaky.queries[2]  # the retry resumes after the same pair
    assert "$and" in flaky.queries[1]
    assert [type(e) for e in mongo_breaker.failures] == [SourceUnavailable]
    for batch in batches:
        assert batch.schema.names[:8] == METADATA_SCHEMA.names
    for row in rows[:50]:
        payload = json.loads(row["_payload"])
        assert payload["_id"] == {"$oid": row["_source_key"]}
        assert payload["ts"] == {"$date": row["_source_updated_at"].isoformat()[:19] + "Z"}
        assert set(payload) == {"_id", "ts", "a"}  # projection: fields + updated + key
        assert row["f_id"] == json.dumps(payload["_id"], separators=(",", ":"))
        assert row["a"] == str(payload["a"])
        assert row["_record_id"] == f"mongodb:orders:{row['_source_key']}"
        assert row["_fetched_at"] == datetime.datetime(2026, 3, 1, tzinfo=UTC)
        assert row["_deleted"] is False


def test_ut01_84_sync_window_filter_and_final_short_page(
    mongo_breaker: SpyBreaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-84 `since` inclusive, `until` exclusive, the entity filter applies; an exact
    multiple of the page size ends with one empty page."""
    del mongo_breaker
    factory = Factory()
    docs = [{"ts": T0 + datetime.timedelta(minutes=i), "a": i, "k": i % 2} for i in range(10)]
    factory.collection().insert_many(docs)
    flaky = flaky_find(monkeypatch, {})
    entity = {"collection": "orders", "updated_field": "ts", "fields": ["a"], "filter": {"k": 0}}
    conn = connector(factory, page_size=2, entities={"orders": entity})
    since, until = T0 + datetime.timedelta(minutes=2), T0 + datetime.timedelta(minutes=8)
    rows = _rows(list(conn.sync("orders", since, until)))
    assert [r["a"] for r in rows] == ["2", "4", "6"]
    assert flaky.queries[0] == {"k": 0, "ts": {"$gte": since, "$lt": until}}
    only_until = _rows(list(conn.sync("orders", None, T0 + datetime.timedelta(minutes=4))))
    assert [r["a"] for r in only_until] == ["0", "2"]
    assert len(flaky.queries) == 4  # [2, 4], [6]; then [0, 2], [] (an exact page is re-asked)


def test_ut01_84_empty_collection_yields_nothing(mongo_breaker: SpyBreaker) -> None:
    """UT01-84 no documents → no batch; the client is built once, lazily."""
    del mongo_breaker
    factory = Factory()
    conn = connector(factory)
    assert factory.calls == []
    assert list(conn.sync("orders", T0)) == []
    assert factory.calls == [URI]


def test_ut01_84_string_timestamps_and_dotted_fields(mongo_breaker: SpyBreaker) -> None:
    """UT01-84 a text `updated_field` is parsed; dotted key/updated/fields read nested values."""
    del mongo_breaker
    factory = Factory()
    factory.collection().insert_many(
        [
            {"m": {"id": "k2", "at": "2026-01-02T00:00:00Z"}, "x": {"y": 2}},
            {"m": {"id": "k1", "at": "2026-01-01T00:00:00Z"}, "x": {"y": 1}},
        ]
    )
    entity = {
        "collection": "orders",
        "key_field": "m.id",
        "updated_field": "m.at",
        "fields": ["x.y"],
    }
    conn = connector(factory, entities={"orders": entity})
    rows = _rows(list(conn.sync("orders", None)))
    assert [r["_source_key"] for r in rows] == ["k1", "k2"]
    assert [r["x_y"] for r in rows] == ["1", "2"]
    assert rows[0]["m_id"] == "k1"
    assert rows[0]["_source_updated_at"] == datetime.datetime(2026, 1, 1, tzinfo=UTC)


def test_ut01_84_missing_key_or_timestamp_is_schema_violation(mongo_breaker: SpyBreaker) -> None:
    """UT01-84 a doc without the key field → SchemaViolation("missing key field"); an
    unparseable watermark → SchemaViolation."""
    del mongo_breaker
    factory = Factory()
    factory.collection().insert_one({"ts": T0, "a": 1})
    entity = {"collection": "orders", "key_field": "code", "updated_field": "ts", "fields": ["a"]}
    conn = connector(factory, entities={"orders": entity})
    with pytest.raises(SchemaViolation, match="missing key field"):
        list(conn.sync("orders", None))
    with pytest.raises(SchemaViolation, match="missing key field"):
        list(conn.list_keys("orders"))
    factory.collection().insert_one({"ts": "yesterday", "a": 2, "code": "c"})
    other = connector(factory)
    with pytest.raises(SchemaViolation, match="unparseable timestamp in ts"):
        list(other.sync("orders", None))


def test_ut01_84_list_keys_pages_by_key(
    mongo_breaker: SpyBreaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-84 `list_keys` yields every key of the filtered collection as `KEY_SCHEMA` batches,
    key-set paged over `key_field` ascending."""
    del mongo_breaker
    factory = Factory()
    factory.collection().insert_many([{"ts": T0, "a": i, "k": i % 3} for i in range(7)])
    expected = sorted(str(d["_id"]) for d in factory.collection().find({"k": 1}))
    flaky = flaky_find(monkeypatch, {})
    entity = {"collection": "orders", "updated_field": "ts", "fields": ["a"], "filter": {"k": 1}}
    conn = connector(factory, page_size=1, entities={"orders": entity})
    batches = list(conn.list_keys("orders"))
    assert all(b.schema == KEY_SCHEMA for b in batches)
    keys = [k for b in batches for k in b.column("_source_key").to_pylist()]
    assert keys == expected
    assert flaky.queries[0] == {"k": 1}
    assert flaky.queries[1] == {"$and": [{"k": 1}, {"_id": {"$gt": ObjectId(expected[0])}}]}
    assert len(flaky.queries) == 3  # 1 + 1 + empty page


def test_ut01_84_protocols_names_and_watermark(mongo_uri: Any) -> None:
    """UT01-84 name, entities, watermark field; Connector and SupportsKeyListing."""
    del mongo_uri
    conn = connector(Factory())
    assert conn.name == "mongodb"
    assert conn.entities == ("orders",)
    assert conn.watermark_field("orders") == "ts"
    assert isinstance(conn, SupportsKeyListing)
    typed: Connector = conn
    assert typed is conn
    with pytest.raises(ConfigError, match="unknown mongodb entity"):
        conn.watermark_field("nope")


def test_ut01_85_check_warns_on_missing_index(mongo_uri: Any) -> None:
    """UT01-85 `check` pings; a collection with no index starting with `updated_field` logs
    WARNING `connectors.mongodb.index_missing` with entity and field."""
    del mongo_uri
    factory = Factory()
    factory.collection().insert_one({"ts": T0})
    factory.collection("jobs").create_index([("ts", 1), ("a", 1)])
    entities = {
        "orders": {"collection": "orders", "updated_field": "ts", "fields": ["a"]},
        "jobs": {"collection": "jobs", "updated_field": "ts", "fields": ["a"]},
        "tail": {"collection": "tail", "updated_field": "ts", "fields": ["a"]},
    }
    factory.collection("tail").create_index([("a", 1), ("ts", 1)])
    with capture_logs() as logs:
        connector(factory, entities=entities).check()
    missing = [e for e in logs if e["event"] == "connectors.mongodb.index_missing"]
    got = [(e["entity"], e["field"], e["log_level"]) for e in missing]
    assert got == [("orders", "ts", "warning"), ("tail", "ts", "warning")]


def test_ut01_85_uri_without_tls_is_config_error(mongo_uri: Any) -> None:
    """UT01-85 a URI without TLS → ConfigError before the client is built."""
    mongo_uri("mongodb://db0.example.com:27017/ops")
    factory = Factory()
    with pytest.raises(ConfigError, match="MongoDB URI must enable verified TLS") as info:
        connector(factory).check()
    assert factory.calls == []
    assert "db0" not in str(info.value)


def test_ut01_85_auth_failure_maps_to_auth_error(
    mongo_breaker: SpyBreaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-85 `OperationFailure(code=18)` during sync → AuthError, not retried."""
    factory = Factory()
    _seed(factory, 3)
    flaky = flaky_find(monkeypatch, {1: me.OperationFailure("Authentication failed.", code=18)})
    with pytest.raises(AuthError) as info:
        list(connector(factory).sync("orders", None))
    assert len(flaky.queries) == 1
    assert "Authentication failed" not in str(info.value)
    assert [type(e) for e in mongo_breaker.failures] == [AuthError]


def test_ut01_85_ping_failure_is_source_unavailable(
    mongo_uri: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-85 a failing `ping` → SourceUnavailable without the driver's text."""
    del mongo_uri
    factory = Factory()

    def down(*_a: Any, **_kw: Any) -> Any:
        msg = "db0.example.com:27017 timed out"
        raise me.ServerSelectionTimeoutError(msg)

    monkeypatch.setattr(factory.client["ops"], "command", down)
    with pytest.raises(SourceUnavailable) as info:
        connector(factory).check()
    assert "db0" not in str(info.value)
    assert info.value.__cause__ is None


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (me.AutoReconnect("x"), SourceUnavailable),
        (me.NetworkTimeout("x"), SourceUnavailable),
        (me.ServerSelectionTimeoutError("x"), SourceUnavailable),
        (me.ConnectionFailure("x"), SourceUnavailable),
        (me.ExecutionTimeout("x", code=50), SourceUnavailable),
        (me.OperationFailure("x", code=13), AuthError),
        (me.OperationFailure("x", code=18), AuthError),
        (me.OperationFailure("x", code=2), SchemaViolation),
        (me.ConfigurationError("x"), ConfigError),
        (me.InvalidURI("x"), ConfigError),
        (me.PyMongoError("x"), SourceUnavailable),
    ],
)
def test_ut01_85_error_mapping(exc: me.PyMongoError, expected: type[Exception]) -> None:
    """UT01-85 `_map_mongo_error` maps each driver error class (U01-86 Algorithm)."""
    mapped = _map_mongo_error(exc)
    assert type(mapped) is expected
    assert "x" not in mapped.message.split()
    if expected is SchemaViolation:
        assert mapped.message == "mongodb operation failed"
        assert mapped.context["code"] == 2


def test_ut01_85_client_factory_error_is_mapped(mongo_uri: Any) -> None:
    """UT01-85 a driver error from the client factory (bad URI option) → ConfigError."""
    del mongo_uri

    def bad(_uri: str) -> Any:
        msg = "unknown option foo"
        raise me.ConfigurationError(msg)

    conn = MongoConnector(settings(), client_factory=bad)
    with pytest.raises(ConfigError, match="mongodb client configuration rejected"):
        conn.check()


def test_ut01_85_default_client_options(mongo_uri: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-85 the default factory builds `pymongo.MongoClient` with the U01-86 options."""
    del mongo_uri
    seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    factory = Factory()

    def fake_client(*a: Any, **kw: Any) -> Any:
        seen.append((a, kw))
        return factory.client

    monkeypatch.setattr(mongo_module.pymongo, "MongoClient", fake_client)
    MongoConnector(settings(timeout_s=30)).check()
    assert seen == [
        (
            (URI,),
            {
                "tz_aware": True,
                "tzinfo": UTC,
                "readPreference": "secondaryPreferred",
                "connectTimeoutMS": 10000,
                "serverSelectionTimeoutMS": 30000,
                "socketTimeoutMS": 30000,
                "appname": "herness",
                "retryReads": False,
            },
        )
    ]


def test_ut01_85_missing_credentials_is_config_error(mongo_uri: Any) -> None:
    """UT01-85 a section without `auth.credentials` (only reachable around validation) →
    ConfigError before any secret lookup or client build."""
    del mongo_uri
    factory = Factory()
    section = settings().model_copy(update={"auth": None})
    with pytest.raises(ConfigError, match=r"mongodb auth\.credentials is required"):
        MongoConnector(section, client_factory=factory).check()
    assert factory.calls == []
