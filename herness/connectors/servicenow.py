"""ServiceNow Table API connector (impl 01 U01-66 to U01-70; design 01 §4.2, §5.4, §5.7).

Every request goes through ``SourceHttp`` (egress source client, page retry, size caps, error
mapping; T01-14) with the auth object of ``build_auth`` (T01-15), both built on first use so
construction makes no network call and resolves no secret. The table name is the configured
entity name; ``sysparm_query`` is built only from validated settings and formatted
timestamps (TH01-07). Next links are followed only on the ``base_url`` host and port
(``check_next_url``, TH01-02). Any shape, key or timestamp error raises ``SchemaViolation``
before the batch that holds it is yielded, so the runner commits nothing of it and the
watermark stays put (TH01-05). Errors and logs carry no record value.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, Final, Literal

import pyarrow as pa

from herness.connectors.auth import build_auth
from herness.connectors.base import KEY_SCHEMA, split_range
from herness.connectors.http import CursorGuard, SourceHttp, http_client
from herness.connectors.rows import RowBatcher, flatten_record, parse_source_timestamp
from herness.connectors.settings import ServiceNowEntity, ServiceNowSettings
from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.logging import get_logger
from herness.core.registry import register

__all__ = ["ServiceNowConnector", "build_sn_query", "merge_by_time"]

_SOURCE: Final = "servicenow"
_WATERMARK: Final = "sys_updated_on"
_AUDIT_TABLE: Final = "sys_audit_delete"
_AUDIT_FIELDS: Final = ("documentkey", "sys_created_on")
_MAX_DELETES: Final = 100_000
_TIME_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
_FORBIDDEN: Final = frozenset({403})
_ORDER: Final = {"time": "ORDERBYsys_updated_on^ORDERBYsys_id", "key": "ORDERBYsys_id"}

type _Clock = Callable[[], datetime.datetime]
type _Record = dict[str, Any]

_log = get_logger("connectors.servicenow")
# The time module is referenced only here; in the class `clock` is the injected callable.
_DEFAULT_CLOCK: Final[_Clock] = clock.now


def _sn_time(value: datetime.datetime) -> str:
    """``value`` in UTC, floored to the whole second, as ``YYYY-MM-DD HH:MM:SS``."""
    return value.astimezone(datetime.UTC).replace(microsecond=0).strftime(_TIME_FORMAT)


def build_sn_query(
    *,
    since: datetime.datetime | None,
    until: datetime.datetime | None,
    classes: Sequence[str] | None = None,
    filter: str | None = None,  # noqa: A002 - the U01-67 parameter name
    order: Literal["time", "key"] = "time",
) -> str:
    """The ``sysparm_query`` value of design 01 §5.7 (U01-67); absent parts are omitted.

    Clauses joined with ``^``: ``sys_updated_on>=<since>``, ``sys_updated_on<<until>``,
    ``sys_class_nameIN<classes>``, ``<filter>``, then the ``order`` clause. Timestamps are
    floored to whole seconds: ``since`` stays inclusive; the last partial second before
    ``until`` is re-read by the next run's overlap.
    """
    parts: list[str] = []
    if since is not None:
        parts.append(f"sys_updated_on>={_sn_time(since)}")
    if until is not None:
        parts.append(f"sys_updated_on<{_sn_time(until)}")
    if classes:
        parts.append("sys_class_nameIN" + ",".join(classes))
    if filter:
        parts.append(filter)
    parts.append(_ORDER[order])
    return "^".join(parts)


def _ascending[I: tuple[Any, ...]](items: Iterator[I]) -> Iterator[I]:
    """``items`` unchanged; ``SchemaViolation`` when ``(ts, key)`` ever goes backwards."""
    last: tuple[datetime.datetime, str] | None = None
    for item in items:
        key = (item[0], item[1])
        if last is not None and key < last:
            msg = "source order violated"
            raise SchemaViolation(msg, source=_SOURCE)
        last = key
        yield item


def merge_by_time[T](
    records: Iterator[tuple[datetime.datetime, str, T]],
    deletes: Sequence[tuple[datetime.datetime, str]],
) -> Iterator[tuple[datetime.datetime, str, T | None]]:
    """Two-way merge ascending by ``(ts, key)``; ``None`` marks a delete (U01-69).

    On an equal ``(ts, key)`` the record comes first. An input out of order raises
    ``SchemaViolation("source order violated")``.
    """
    recs, dels = _ascending(records), _ascending(iter(deletes))
    rec, gone = next(recs, None), next(dels, None)
    while True:
        if rec is not None and (gone is None or (rec[0], rec[1]) <= gone):
            yield rec
            rec = next(recs, None)
        elif gone is not None:
            yield gone[0], gone[1], None
            gone = next(dels, None)
        else:
            return


def _value(record: Mapping[str, Any], name: str) -> object:
    """The raw value of ``name``: ``value`` of a ``{value, display_value}`` pair, else as is."""
    field = record.get(name)
    return field.get("value") if isinstance(field, dict) else field


def _checked(record: object, name: str = "sys_id") -> tuple[_Record, str]:
    """``(record, key)``: a record object and its non-empty string ``name``, else
    ``SchemaViolation``."""
    if not isinstance(record, dict):
        msg = "unexpected record shape"
        raise SchemaViolation(msg, source=_SOURCE)
    key = _value(record, name)
    if not isinstance(key, str) or not key:
        msg = f"missing {name}"
        raise SchemaViolation(msg, source=_SOURCE)
    return record, key


@register("connector", "servicenow")
class ServiceNowConnector:
    """ServiceNow Table API source (U01-66); one instance per thread.

    The integration user's timezone must be UTC (design 01 §5.7; checked manually, V-1).
    """

    name: str = _SOURCE

    def __init__(
        self,
        settings: ServiceNowSettings,
        *,
        http: SourceHttp | None = None,
        clock: _Clock = _DEFAULT_CLOCK,
    ) -> None:
        self._settings, self._http, self._clock = settings, http, clock
        self.entities: tuple[str, ...] = tuple(settings.entities)
        self._audit_readable: bool | None = None  # probed on the first delete lookup

    # --- members of the Connector protocol (U01-16) -----------------------------------------

    def check(self) -> None:
        """One cheap authenticated read of the first entity's table; errors propagate."""
        params = {"sysparm_limit": 1, "sysparm_fields": "sys_id"}
        self._source().get_json(_table_url(self.entities[0]), params=params)

    def watermark_field(self, entity: str) -> str:
        """``sys_updated_on`` for every table."""
        del entity
        return _WATERMARK

    def sync(
        self,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime | None = None,
    ) -> Iterator[pa.RecordBatch]:
        """Rows of ``entity`` updated in ``[since, until)`` with merged delete tombstones,
        ascending by ``(_source_updated_at, _source_key)``, in windows of ``window_hours``
        (U01-68). ``since`` ``None`` starts at the entity's backfill start."""
        ent = self._entity(entity)
        until = until or self._clock()
        since = since or self._settings.backfill_for(entity).resolve_start(until)
        fields = _fetch_fields(entity, ent)
        batcher = RowBatcher(
            _SOURCE,
            entity,
            batch_rows=self._settings.batch_rows,
            columns=[col for f in fields for col in (f, f"{f}_display")],
            clock=self._clock,
        )
        step = datetime.timedelta(hours=ent.window_hours)
        for w0, w1 in split_range(since, until, step):
            deletes = self._audit_deletes(entity, w0, w1)
            query = build_sn_query(since=w0, until=w1, classes=ent.classes, filter=ent.filter)
            pages = self._pages(entity, query, fields=fields, display=True)
            for ts, key, record in merge_by_time(_timed(pages), deletes):
                if record is None:
                    full = batcher.add_tombstone(key, ts)
                else:
                    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    flat = flatten_record(record, fields=fields, display_pairs=True)
                    full = batcher.add(key, ts, payload, flat)
                if full is not None:
                    yield full
        last = batcher.flush()
        if last is not None:
            yield last

    # --- SupportsKeyListing (U01-17) ----------------------------------------------------------

    def list_keys(self, entity: str) -> Iterator[pa.RecordBatch]:
        """Every current ``sys_id`` matching the entity's ``classes`` and ``filter``, as
        ``KEY_SCHEMA`` batches of at most ``batch_rows`` (U01-70)."""
        ent = self._entity(entity)
        query = build_sn_query(
            since=None, until=None, classes=ent.classes, filter=ent.filter, order="key"
        )
        size, keys = self._settings.batch_rows, []
        for record in self._pages(entity, query, fields=("sys_id",), display=False):
            keys.append(_checked(record)[1])
            if len(keys) >= size:
                yield _key_batch(keys)
                keys = []
        if keys:
            yield _key_batch(keys)

    # --- internals ----------------------------------------------------------------------------

    def _entity(self, entity: str) -> ServiceNowEntity:
        found = self._settings.entity(entity)  # ConfigError for an unknown entity
        if not isinstance(found, ServiceNowEntity):
            msg = f"entity {entity} is not a ServiceNow entity"
            raise ConfigError(msg, source=_SOURCE, entity=entity)
        return found

    def _source(self) -> SourceHttp:
        """The page fetcher, built on first use: egress client, ``build_auth`` auth (U01-66)."""
        if self._http is None:
            cfg = self._settings
            if cfg.auth is None or cfg.base_url is None:
                msg = "servicenow needs base_url and auth"
                raise ConfigError(msg, source=_SOURCE)
            client = http_client(cfg, source=_SOURCE, max_concurrency=cfg.max_concurrency)
            auth = build_auth(
                cfg.auth,
                source=_SOURCE,
                base_url=cfg.base_url,
                token_client=client,
                clock=self._clock,
            )
            self._http = SourceHttp(client, breaker_key=_SOURCE, auth=auth, clock=self._clock)
        return self._http

    def _pages(
        self, entity: str, query: str, *, fields: Sequence[str], display: bool, table: str = ""
    ) -> Iterator[object]:
        """Records of ``table`` (default: ``entity``) matching ``query``, page by page with the
        entity's ``page_size``: offset paging, or the response's same-host ``next`` link."""
        http, size = self._source(), self._settings.page_size_for(entity)
        url = base = _table_url(table or entity)
        first: dict[str, Any] = {
            "sysparm_query": query,
            "sysparm_fields": ",".join(fields),
            "sysparm_limit": size,
            "sysparm_offset": 0,
            "sysparm_display_value": "all" if display else "false",
            "sysparm_exclude_reference_link": "true",
            "sysparm_no_count": "true",
        }
        params: dict[str, Any] | None = first
        cursor, offset = CursorGuard(), 0
        while True:
            page = http.get_json(url, params=params)
            body = page.body
            result = body.get("result") if isinstance(body, dict) else None
            if not isinstance(result, list):
                msg = "unexpected response shape: result is not a list"
                raise SchemaViolation(msg, source=_SOURCE, entity=entity)
            yield from result
            if len(result) < size:
                return
            offset += len(result)
            link = page.links.get("next")
            if link:  # the link carries its own offset: a repeated link is a repeated cursor
                url, params = http.check_next_url(link), None
                cursor.step(url)
            else:
                url, params = base, first | {"sysparm_offset": offset}
                cursor.step(f"{url}|{offset}")

    def _audit_deletes(
        self, entity: str, w0: datetime.datetime, w1: datetime.datetime
    ) -> list[tuple[datetime.datetime, str]]:
        """``(sys_created_on, documentkey)`` of the table's deletes in ``[w0, w1)``, ascending;
        ``[]`` when ``sys_audit_delete`` is not readable (403 on the first probe)."""
        if self._audit_readable is None:
            params = {"sysparm_limit": 1, "sysparm_fields": "sys_id"}
            probe = self._source().get_json(
                _table_url(_AUDIT_TABLE), params=params, allow_status=_FORBIDDEN
            )
            self._audit_readable = probe.status not in _FORBIDDEN
            if not self._audit_readable:
                _log.info("connectors.servicenow.audit_delete_unreadable", source=_SOURCE)
        if not self._audit_readable:
            return []
        query = (
            f"tablename={entity}^sys_created_on>={_sn_time(w0)}^sys_created_on<{_sn_time(w1)}"
            "^ORDERBYsys_created_on^ORDERBYdocumentkey"
        )
        found: list[tuple[datetime.datetime, str]] = []
        records = self._pages(
            entity, query, fields=_AUDIT_FIELDS, display=False, table=_AUDIT_TABLE
        )
        for raw in records:
            record, key = _checked(raw, "documentkey")
            at = parse_source_timestamp(_value(record, "sys_created_on"), field="sys_created_on")
            found.append((at, key))
            if len(found) > _MAX_DELETES:
                msg = "too many deletes in window; lower window_hours"
                raise SchemaViolation(msg, source=_SOURCE, entity=entity)
        return found


def _table_url(table: str) -> str:
    return f"/api/now/table/{table}"


def _fetch_fields(entity: str, ent: ServiceNowEntity) -> tuple[str, ...]:
    """``sys_id``, ``sys_updated_on``, ``sys_class_name`` (``cmdb_ci``), then the configured
    fields, without duplicates."""
    head = ["sys_id", _WATERMARK] + (["sys_class_name"] if entity == "cmdb_ci" else [])
    return tuple(dict.fromkeys([*head, *ent.fields]))


def _timed(records: Iterator[object]) -> Iterator[tuple[datetime.datetime, str, _Record]]:
    """``(sys_updated_on, sys_id, record)`` of each record; shape errors raise."""
    for raw in records:
        record, key = _checked(raw)
        ts = parse_source_timestamp(_value(record, _WATERMARK), field=_WATERMARK)
        yield ts, key, record


def _key_batch(keys: list[str]) -> pa.RecordBatch:
    return pa.RecordBatch.from_arrays([pa.array(keys, pa.string())], schema=KEY_SCHEMA)
