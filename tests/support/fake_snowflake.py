"""A fake Snowflake connection and cursor returning Arrow batches (impl 01 §11; T01-23).

`FakeSnowflake.connect` stands in for `snowflake.connector.connect` through the
`SnowflakeConnector(connect=...)` parameter. It is in-memory only: no socket is opened or
bound (loopback or otherwise), so the process socket guard never sees it. Every `execute`
lands in the call log `calls` as `(sql, params)` before any scripted error is raised, and
the connect keyword arguments land in `connects` (they hold the DER key: never print them).

Statements are told apart by their first word (and, for SELECT, by a single-column select
list, which is the key listing):

- `ALTER` (the `QUERY_TAG`) returns nothing;
- `EXPLAIN` answers `fetchone()` with one JSON text built from `bytes_assigned`
  (`None` leaves `GlobalStats.bytesAssigned` out) or with `explain_json` verbatim;
- `SHOW` answers `description` and `fetchall()` from `warehouses` (one dict per row);
- `SELECT` answers `fetch_arrow_batches()` with `tables` (or `key_tables` for the key listing);
  `fetch_error` is raised after the first table.

`errors` maps a first word to a queue of exceptions raised by the next `execute` calls of
that kind; `connect_error` is raised by `connect`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import pyarrow as pa

__all__ = ["FakeConnection", "FakeCursor", "FakeSnowflake"]

type Params = Mapping[str, Any] | None


def _kind(sql: str) -> str:
    return sql.lstrip().split(" ", 1)[0].upper()


def _key_listing(sql: str) -> bool:
    select_list = sql.split(" FROM ", 1)[0]
    return "," not in select_list


@dataclass
class FakeSnowflake:
    """Scripted server state shared by every connection and cursor it hands out."""

    tables: list[pa.Table] = field(default_factory=list)
    key_tables: list[pa.Table] = field(default_factory=list)
    bytes_assigned: int | None = 0
    explain_json: str | None = None
    warehouses: list[dict[str, Any]] = field(default_factory=list)
    errors: dict[str, list[Exception]] = field(default_factory=dict)
    fetch_error: Exception | None = None
    connect_error: Exception | None = None
    calls: list[tuple[str, Params]] = field(default_factory=list)
    connects: list[dict[str, Any]] = field(default_factory=list)
    closed: int = 0

    def connect(self, **kwargs: Any) -> FakeConnection:
        """`snowflake.connector.connect` stand-in: records the keyword arguments."""
        self.connects.append(kwargs)
        if self.connect_error is not None:
            raise self.connect_error
        return FakeConnection(self)

    def statements(self, kind: str) -> list[str]:
        """Executed SQL texts whose first word is ``kind``."""
        return [sql for sql, _ in self.calls if _kind(sql) == kind]

    def explain(self) -> str:
        if self.explain_json is not None:
            return self.explain_json
        stats: dict[str, Any] = {"partitionsTotal": 1, "partitionsAssigned": 1}
        if self.bytes_assigned is not None:
            stats["bytesAssigned"] = self.bytes_assigned
        return json.dumps({"GlobalStats": stats, "Operations": [[]]})


class FakeConnection:
    """A connection whose cursors share the server state."""

    def __init__(self, server: FakeSnowflake) -> None:
        self.server = server

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.server)

    def close(self) -> None:
        self.server.closed += 1


class FakeCursor:
    """A DB-API cursor over the scripted state; a context manager like `SnowflakeCursor`."""

    def __init__(self, server: FakeSnowflake) -> None:
        self.server = server
        self.description: list[tuple[str, ...]] | None = None
        self._last = ""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, sql: str, params: Params = None) -> Self:
        self.server.calls.append((sql, params))
        kind = _kind(sql)
        queue = self.server.errors.get(kind)
        if queue:
            raise queue.pop(0)
        self._last = sql
        if kind == "SHOW":
            names = list(self.server.warehouses[0]) if self.server.warehouses else ["name"]
            self.description = [(name,) for name in names]
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        if _kind(self._last) == "EXPLAIN":
            return (self.server.explain(),)
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [tuple(row.values()) for row in self.server.warehouses]

    def fetch_arrow_batches(self) -> Iterator[pa.Table]:
        keys = _key_listing(self._last)
        tables = self.server.key_tables if keys else self.server.tables
        for index, table in enumerate(tables):
            if index == 1 and self.server.fetch_error is not None:
                raise self.server.fetch_error
            yield table
