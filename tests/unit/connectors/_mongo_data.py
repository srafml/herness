"""Shared data of the MongoDB connector tests (T01-22): settings, a recording mongomock client
factory, a fault-injecting `find`, a spy breaker and the URI secret writer (the fixtures
`mongo_breaker` and `mongo_uri` live in this folder's conftest). No socket is opened:
`mongomock` is in-memory."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any, cast

import keyring
import mongomock
import pymongo
import pytest
from pymongo.errors import PyMongoError
from tests.unit.connectors._settings_data import mongodb

from herness.connectors.mongodb import MongoConnector
from herness.connectors.settings import MongoSettings
from herness.core import secrets

UTC = datetime.UTC
URI = "mongodb://db0.example.com:27017/?tls=true"
T0 = datetime.datetime(2026, 1, 1, tzinfo=UTC)
FIXED_NOW = datetime.datetime(2026, 3, 1, tzinfo=UTC)

type Client = pymongo.MongoClient[dict[str, Any]]


def fixed_now() -> datetime.datetime:
    return FIXED_NOW


def settings(**extra: Any) -> MongoSettings:
    """A valid `sources.mongodb` section (entity `orders`, `updated_field` ts, `fields` [a])."""
    return MongoSettings.model_validate(mongodb(**extra))


class Factory:
    """`client_factory` stand-in: records every URI it is called with, returns one mongomock
    client (tz-aware, like the production client)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.client: Any = mongomock.MongoClient(tz_aware=True)

    def __call__(self, uri: str) -> Client:
        self.calls.append(uri)
        return cast("Client", self.client)

    def collection(self, name: str = "orders") -> Any:
        return self.client["ops"][name]


def connector(factory: Factory, **extra: Any) -> MongoConnector:
    return MongoConnector(settings(**extra), clock=fixed_now, client_factory=factory)


class FlakyFind:
    """Wrap `mongomock` `Collection.find`: raise `errors[n]` on call number `n` (1-based), then
    delegate; records every query."""

    def __init__(self, original: Callable[..., Any], errors: dict[int, PyMongoError]) -> None:
        self.original = original
        self.errors = errors
        self.queries: list[dict[str, Any]] = []

    def __call__(self, coll: Any, query: dict[str, Any], *a: Any, **kw: Any) -> Any:
        self.queries.append(query)
        error = self.errors.pop(len(self.queries), None)
        if error is not None:
            raise error
        return self.original(coll, query, *a, **kw)


def flaky_find(monkeypatch: pytest.MonkeyPatch, errors: dict[int, PyMongoError]) -> FlakyFind:
    original = mongomock.collection.Collection.find
    flaky = FlakyFind(original, errors)

    def find(coll: Any, query: dict[str, Any], *a: Any, **kw: Any) -> Any:
        return flaky(coll, query, *a, **kw)

    monkeypatch.setattr(mongomock.collection.Collection, "find", find)
    return flaky


class SpyBreaker:
    def __init__(self) -> None:
        self.failures: list[Exception] = []
        self.successes = 0

    def record_failure(self, err: Exception) -> None:
        self.failures.append(err)

    def record_success(self) -> None:
        self.successes += 1


def store_uri(value: str) -> None:
    """Store `value` as the `secret:mongo_uri` secret (in-memory keyring expected)."""
    keyring.set_password(secrets._SERVICE, "mongo_uri", value)
