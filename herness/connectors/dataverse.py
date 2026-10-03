"""Dataverse Web API connector: OData paged reads and key listing (impl 01 U01-90, U01-91).

Design 01 §5.9 and §6. Queries are built only from validated settings values (OData
identifiers) and formatted timestamps; ``$skip`` and ``$top`` are never sent. Each next page
is the response's ``@odata.nextLink`` followed unmodified after ``check_next_url`` (same
origin as ``base_url``, TH01-02) with the ``Prefer`` header repeated. The default HTTP layer
is built on first use: the egress source client and the MSAL token provider of
``build_auth`` (scope ``f"{base_url}/.default"``, TH01-16). Change tracking (Q4) is not used;
deletes come from weekly reconciliation through ``list_keys``. Errors carry no row text,
token or host (TH01-03).
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Final

import pyarrow as pa

from herness.connectors.auth import build_auth
from herness.connectors.base import KEY_SCHEMA
from herness.connectors.http import CursorGuard, SourceHttp, http_client
from herness.connectors.rows import RowBatcher, parse_source_timestamp
from herness.connectors.rows import _text as text
from herness.connectors.settings import DataverseSettings
from herness.connectors.settings_entities import DataverseEntity
from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.registry import register

__all__ = ["DataverseConnector"]

type Row = Mapping[str, Any]
type Params = dict[str, str] | None

_SOURCE: Final = "dataverse"
_API: Final = "/api/data/v9.2"
_FORMATTED: Final = "@OData.Community.Display.V1.FormattedValue"
_NEXT: Final = "@odata.nextLink"
_TS: Final = "%Y-%m-%dT%H:%M:%SZ"  # strftime drops the fraction: floored to seconds
_VERSION: Final = {"OData-MaxVersion": "4.0", "OData-Version": "4.0"}


def _stamp(value: datetime.datetime) -> str:
    """An OData UTC literal of an aware datetime, floored to whole seconds."""
    if value.utcoffset() is None:
        msg = "dataverse sync bounds must be timezone-aware"
        raise ConfigError(msg, source=_SOURCE)
    return value.astimezone(datetime.UTC).strftime(_TS)


def _headers(page_size: int) -> dict[str, str]:
    """Headers of every page (U01-91 step 3): page size and formatted-value annotations."""
    prefer = (
        f"odata.maxpagesize={page_size},"
        'odata.include-annotations="OData.Community.Display.V1.FormattedValue"'
    )
    return {"Prefer": prefer, **_VERSION}


def _rows(body: object, entity: str) -> list[Row]:
    """The ``value`` list of a page body; any other shape is ``SchemaViolation``."""
    value = body.get("value") if isinstance(body, dict) else None
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        msg = "bad dataverse response shape"
        raise SchemaViolation(msg, source=_SOURCE, entity=entity)
    return value


def _key(row: Row, field: str, entity: str) -> str:
    """The row's key, a non-empty string; the value is never put in the message."""
    value = row.get(field)
    if not isinstance(value, str) or not value:
        msg = "missing or empty key field"
        raise SchemaViolation(msg, source=_SOURCE, entity=entity, field=field)
    return value


@register("connector", "dataverse")
class DataverseConnector:
    """Dataverse source (U01-90); ``Connector`` and ``SupportsKeyListing``; one per thread."""

    name: str = _SOURCE

    def __init__(
        self,
        settings: DataverseSettings,
        *,
        http: SourceHttp | None = None,
        clock: Callable[[], datetime.datetime] = clock.now,  # default: module; body: parameter
    ) -> None:
        self._settings = settings
        self._http = http
        self._clock = clock
        self.entities: tuple[str, ...] = tuple(settings.entities)

    def _client(self) -> SourceHttp:
        """The page fetcher; the default one is built on first use (no network before)."""
        if self._http is None:
            self._http = self._default_http()
        return self._http

    def _default_http(self) -> SourceHttp:
        settings = self._settings
        if settings.auth is None or settings.base_url is None:  # the settings require both
            msg = "dataverse base_url and auth are required"
            raise ConfigError(msg, source=_SOURCE)
        client = http_client(settings, source=_SOURCE, max_concurrency=settings.max_concurrency)
        auth = build_auth(
            settings.auth,
            source=_SOURCE,
            base_url=settings.base_url,
            token_client=client,
            clock=self._clock,
        )
        return SourceHttp(client, breaker_key=_SOURCE, auth=auth, clock=self._clock)

    def _entity(self, entity: str) -> DataverseEntity:
        cfg = self._settings.entities.get(entity)
        if cfg is None:
            msg = f"unknown dataverse entity {entity[:64]}"
            raise ConfigError(msg, source=_SOURCE)
        return cfg

    def watermark_field(self, entity: str) -> str:
        """The entity ``updated_field``."""
        return self._entity(entity).updated_field

    def check(self) -> None:
        """One authenticated read: ``GET /api/data/v9.2/WhoAmI``."""
        self._client().get_json(f"{_API}/WhoAmI", headers=_VERSION)

    def _pages(self, entity: str, params: Params) -> Iterator[list[Row]]:
        """Every page's rows of ``entity``: the first page with ``params``, then each
        ``@odata.nextLink`` unmodified (``params=None``), the same headers on every page."""
        http, guard = self._client(), CursorGuard()
        url = f"{_API}/{self._entity(entity).entityset}"
        headers = _headers(self._settings.page_size_for(entity))
        while True:
            body = http.get_json(url, params=params, headers=headers).body
            yield _rows(body, entity)
            nxt = body.get(_NEXT) if isinstance(body, dict) else None
            if nxt is None:
                return
            if not isinstance(nxt, str):
                msg = "bad dataverse next link"
                raise SchemaViolation(msg, source=_SOURCE, entity=entity)
            url, params = http.check_next_url(nxt), None
            guard.step(nxt)

    def sync(
        self,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime | None = None,
    ) -> Iterator[pa.RecordBatch]:
        """Rows with ``since <= updated < until`` ascending by ``(updated_field, key_field)``;
        each selected field ``f`` gives the columns ``f`` and ``f_display``."""
        cfg = self._entity(entity)
        upd, key = cfg.updated_field, cfg.key_field
        select = list(dict.fromkeys([key, upd, *cfg.select]))
        columns = [col for f in select for col in (f, f"{f}_display")]
        if len(set(columns)) != len(columns):
            msg = "dataverse select gives a duplicate _display column"
            raise ConfigError(msg, source=_SOURCE, entity=entity)
        bounds = [
            f"{upd} {op} {_stamp(v)}" for op, v in (("ge", since), ("lt", until)) if v is not None
        ]
        params = {"$select": ",".join(select), "$orderby": f"{upd} asc,{key} asc"}
        if bounds:
            params["$filter"] = " and ".join(bounds)
        batcher = RowBatcher(
            _SOURCE,
            entity,
            batch_rows=self._settings.batch_rows,
            columns=columns,
            clock=self._clock,
        )
        for rows in self._pages(entity, params):
            for row in rows:
                batch = batcher.add(*_record(row, cfg, entity, select))
                if batch is not None:
                    yield batch
        tail = batcher.flush()
        if tail is not None:
            yield tail

    def list_keys(self, entity: str) -> Iterator[pa.RecordBatch]:
        """Every current key as ``KEY_SCHEMA`` batches, one per page: ``$select=<key>`` only,
        no ``$filter`` or ``$orderby``; a failed page raises, the listing never ends early."""
        key = self._entity(entity).key_field
        for rows in self._pages(entity, {"$select": key}):
            if rows:
                column = pa.array([_key(row, key, entity) for row in rows], pa.string())
                yield pa.RecordBatch.from_arrays([column], schema=KEY_SCHEMA)


def _record(
    row: Row, cfg: DataverseEntity, entity: str, select: list[str]
) -> tuple[str, datetime.datetime, str, dict[str, str | None]]:
    """``(source_key, updated_at, payload, fields)`` of one row (U01-91 step 4)."""
    source_key = _key(row, cfg.key_field, entity)
    updated_at = parse_source_timestamp(row.get(cfg.updated_field), field=cfg.updated_field)
    fields: dict[str, str | None] = {}
    for f in select:
        fields[f] = text(row.get(f))
        fields[f"{f}_display"] = text(row.get(f + _FORMATTED))
    return source_key, updated_at, json.dumps(row), fields
