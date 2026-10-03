"""Jira Cloud and Data Center connector: search, key listing, field discovery and the raw
column contract of the ``issue`` row (impl 01 U01-71 to U01-75, U01-93; R-59; design 01
§4.2, §5.8).

Requests go through ``herness.connectors.http.SourceHttp`` (egress client, page retry, error
mapping); pages follow the body ``nextPageToken`` (Cloud, under ``CursorGuard``) or
``startAt`` (DC), never a server-supplied URL. JQL is built only from the validated
``jql_scope`` and the run's bounds (TH01-07). The HTTP layer and the row helpers (``base``
re-exports ``http_client``) are imported lazily: importing ``flatten_issue`` (T11-12) loads
no ``httpx`` code. Complete changelogs and remote links (U01-76, U01-77) come from
``jira_changelog`` per search page before any row of the page is batched, so a shape error
or an incomplete changelog raises before a batch is yielded (no lake write, no watermark
move; TH01-05).
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from herness.connectors import jira_changelog as changelogs_api
from herness.connectors.jira_changelog import project_history, project_remote_link
from herness.connectors.settings import JiraSettings
from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.registry import register

if TYPE_CHECKING:
    import pyarrow as pa

    from herness.connectors.http import JsonPage, SourceHttp

__all__ = [
    "JIRA_FIELDS", "JIRA_ISSUE_COLUMNS", "FieldCandidate", "JiraConnector", "build_jql",
    "flatten_issue",
]  # fmt: skip

JIRA_FIELDS: Final = (
    "issuetype", "parent", "project", "components", "labels", "status", "created",
    "resolutiondate", "summary", "description", "updated", "issuelinks",
)  # fmt: skip
JIRA_ISSUE_COLUMNS: Final = ("id", "key", *JIRA_FIELDS, "changelog", "remotelinks")

type SuggestedKey = Literal[
    "jira.story_points", "jira.team", "jira.epic_link", "jira.estimate_cost_usd"
]
type _Issue = Mapping[str, Any]
type _ByIssue = Mapping[str, Sequence[Mapping[str, object]]]
type _Row = tuple[str, datetime.datetime, str, dict[str, str | None]]

_SOURCE: Final = "jira"
_ENTITY: Final = "issue"
_ID_RE: Final = re.compile(r"[0-9]+")
_KEY_RE: Final = re.compile(r"[A-Z][A-Z0-9_]{0,31}-\d{1,10}", re.ASCII)
_CUSTOM_RE: Final = re.compile(r"customfield_\d{1,10}", re.ASCII)
_JQL_TIME: Final = "%Y/%m/%d %H:%M"
_TIME_ORDER: Final = "ORDER BY updated ASC, id ASC"
_CLOUD_SEARCH: Final = "/rest/api/3/search/jql"
_DC_SEARCH: Final = "/rest/api/2/search"
_BAD_PAGE: Final = "bad search page"
_BAD_FIELDS: Final = "bad field list"
_NAMED: Final[Mapping[str, SuggestedKey]] = {
    "story points": "jira.story_points",
    "story point estimate": "jira.story_points",
    "team": "jira.team",
    "epic link": "jira.epic_link",
}
_COST_RE: Final = re.compile(r"(?i)cost|usd|budget")


@dataclass(frozen=True, slots=True)
class FieldCandidate:
    """A custom field ``--discover-fields`` suggests for ``mappings.yaml`` (U01-75)."""

    id: str
    name: str
    schema_type: str | None
    suggested_key: SuggestedKey


def _jql_time(ts: datetime.datetime) -> str:
    if ts.utcoffset() is None:
        msg = "JQL bounds must be timezone-aware"
        raise ConfigError(msg, source=_SOURCE)
    return ts.astimezone(datetime.UTC).strftime(_JQL_TIME)


def build_jql(
    *,
    since: datetime.datetime | None,
    until: datetime.datetime | None,
    scope: str | None,
    order: Literal["time", "key"] = "time",
) -> str:
    """The search JQL of design 01 §5.8 (U01-72): bounds in UTC floored to the minute."""
    where = f"({scope})" if scope else None
    if order == "key":
        return f"{where} ORDER BY id ASC" if where else "ORDER BY id ASC"
    clauses = [f'updated >= "{_jql_time(since)}"'] if since is not None else []
    if until is not None:
        clauses.append(f'updated < "{_jql_time(until)}"')
    if where:
        clauses.append(where)
    prefix = " AND ".join(clauses)
    return f"{prefix} {_TIME_ORDER}" if prefix else _TIME_ORDER


def _check_custom_ids(ids: Sequence[str]) -> tuple[str, ...]:
    for cid in ids:
        if not _CUSTOM_RE.fullmatch(cid):
            msg = f"bad custom field id {cid[:64]}"
            raise ConfigError(msg, source=_SOURCE)
    return tuple(ids)


def _issue_id(issue: object) -> str:
    """The numeric issue ``id`` string; ``SchemaViolation("bad issue id")`` otherwise."""
    value = issue.get("id") if isinstance(issue, Mapping) else None
    if not (isinstance(value, str) and _ID_RE.fullmatch(value)):
        msg = "bad issue id"
        raise SchemaViolation(msg, source=_SOURCE)
    return value


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _history_order(history: Mapping[str, object]) -> tuple[str, str]:
    return str(history["created"]), str(history["id"])


def flatten_issue(
    issue: Mapping[str, object],
    *,
    changelog: Sequence[Mapping[str, object]],
    remotelinks: Sequence[Mapping[str, object]] | None,
    custom_field_ids: Sequence[str] = (),
) -> dict[str, str | None]:
    """The Jira raw column contract (U01-93, R-59): keys exactly ``JIRA_ISSUE_COLUMNS`` then
    ``custom_field_ids``; objects as compact JSON, absent fields ``None``; ``changelog``
    projected and sorted by ``(created, id)``; ``remotelinks`` projected or ``None``."""
    from herness.connectors.rows import flatten_record  # noqa: PLC0415 - lazy: no HTTP layer

    issue_id = _issue_id(issue)
    key = issue.get("key")
    if not (isinstance(key, str) and _KEY_RE.fullmatch(key)):
        msg = "bad issue key"
        raise SchemaViolation(msg, source=_SOURCE, issue_id=issue_id)
    fields = issue.get("fields")
    if not isinstance(fields, Mapping):
        msg = "issue fields missing"
        raise SchemaViolation(msg, source=_SOURCE, issue_id=issue_id)
    _check_custom_ids(custom_field_ids)
    flat = flatten_record(fields, fields=[*JIRA_FIELDS, *custom_field_ids])
    out: dict[str, str | None] = {"id": issue_id, "key": key}
    out.update((name, flat[name]) for name in JIRA_FIELDS)
    out["changelog"] = _compact(sorted(map(project_history, changelog), key=_history_order))
    links = None if remotelinks is None else [project_remote_link(r) for r in remotelinks]
    out["remotelinks"] = None if links is None else _compact(links)
    out.update((cid, flat[cid]) for cid in custom_field_ids)
    return out


def _body(page: JsonPage) -> Mapping[str, Any]:
    if not isinstance(page.body, dict):
        raise SchemaViolation(_BAD_PAGE, source=_SOURCE)
    return page.body


def _issues(body: Mapping[str, Any]) -> list[_Issue]:
    issues = body.get("issues")
    if not (isinstance(issues, list) and all(isinstance(i, Mapping) for i in issues)):
        raise SchemaViolation(_BAD_PAGE, source=_SOURCE)
    return issues


def _candidate(field: object) -> FieldCandidate | None:
    """The suggestion for one ``/field`` entry, ``None`` when it matches no rule (U01-75)."""
    if not isinstance(field, Mapping):
        raise SchemaViolation(_BAD_FIELDS, source=_SOURCE)
    fid, name, schema = field.get("id"), field.get("name"), field.get("schema")
    if not (isinstance(fid, str) and isinstance(name, str)):
        raise SchemaViolation(_BAD_FIELDS, source=_SOURCE)
    kind = schema.get("type") if isinstance(schema, Mapping) else None
    kind = kind if isinstance(kind, str) else None
    key = _NAMED.get(name.casefold())
    cost = field.get("custom") is True and kind == "number" and _COST_RE.search(name)
    if key is None and cost:
        key = "jira.estimate_cost_usd"
    return None if key is None else FieldCandidate(fid, name, kind, key)


@register("connector", "jira")
class JiraConnector:
    """Jira Cloud (REST v3) or Data Center (REST v2) source (U01-71); one instance per thread.

    The ``SourceHttp`` is built on first use when not given, so construction makes no call
    and resolves no secret."""

    name: str = _SOURCE

    def __init__(
        self,
        settings: JiraSettings,
        *,
        custom_field_ids: Sequence[str] = (),
        http: SourceHttp | None = None,
        clock: Callable[[], datetime.datetime] = clock.now,  # default: module; body: parameter
    ) -> None:
        self._settings = settings
        # one column per id even when two mapped custom fields name the same field
        self._custom = tuple(dict.fromkeys(_check_custom_ids(custom_field_ids)))
        self._http = http
        self._clock = clock
        self._version: Literal["2", "3"] = "3" if settings.flavor == "cloud" else "2"
        self._cl_state = changelogs_api.ChangelogState()  # bulk availability (U01-76)
        self.entities: tuple[str, ...] = tuple(settings.entities)

    def _source(self) -> SourceHttp:
        """The page fetcher; the egress client and auth are built on first use."""
        if self._http is None:
            from herness.connectors.auth import build_auth  # noqa: PLC0415 - lazy (module doc)
            from herness.connectors.http import SourceHttp, http_client  # noqa: PLC0415

            s = self._settings
            if s.auth is None or s.base_url is None:
                msg = "jira base_url and auth are required"
                raise ConfigError(msg, source=_SOURCE)
            client = http_client(s, source=_SOURCE, max_concurrency=s.max_concurrency)
            auth = build_auth(
                s.auth, source=_SOURCE, base_url=s.base_url, token_client=client, clock=self._clock
            )
            self._http = SourceHttp(client, breaker_key=_SOURCE, auth=auth, clock=self._clock)
        return self._http

    def _entity(self, entity: str) -> None:
        if entity != _ENTITY:
            msg = f"unknown jira entity {entity[:64]}"
            raise ConfigError(msg, source=_SOURCE)

    def watermark_field(self, entity: str) -> str:
        """``updated`` for the ``issue`` entity."""
        self._entity(entity)
        return "updated"

    def check(self) -> None:
        """One authenticated ``GET /rest/api/{v}/myself``."""
        self._source().get_json(f"/rest/api/{self._version}/myself")

    def sync(
        self,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime | None = None,
    ) -> Iterator[pa.RecordBatch]:
        """Issues with ``since <= updated < until`` ascending by ``(updated, id)``, each with
        its complete changelog and remote links (U01-73)."""
        from herness.connectors.rows import RowBatcher  # noqa: PLC0415 - lazy (module doc)

        self._entity(entity)
        jql = build_jql(since=since, until=until, scope=self._settings.jql_scope)
        columns, rows = [*JIRA_ISSUE_COLUMNS, *self._custom], self._settings.batch_rows
        batcher = RowBatcher(_SOURCE, _ENTITY, batch_rows=rows, columns=columns, clock=self._clock)
        for issues in self._pages(jql, [*JIRA_FIELDS, *self._custom], expand=True):
            for row in self._rows(issues):
                batch = batcher.add(*row)
                if batch is not None:
                    yield batch
        tail = batcher.flush()
        if tail is not None:
            yield tail

    def _rows(self, issues: list[_Issue]) -> list[_Row]:
        """One search page as ``RowBatcher.add`` arguments, with changelogs and links."""
        from herness.connectors.rows import parse_source_timestamp  # noqa: PLC0415 - lazy

        http, ids = self._source(), [_issue_id(i) for i in issues]
        flavor, state = self._settings.flavor, self._cl_state
        changelogs = changelogs_api.fetch_changelogs(
            http, flavor=flavor, issues=issues, state=state
        )
        links: _ByIssue | None = None
        if self._settings.fetch_remote_links:
            links = changelogs_api.fetch_remote_links(http, version=self._version, issue_ids=ids)
        rows: list[_Row] = []
        for issue_id, issue in zip(ids, issues, strict=True):
            history = changelogs.get(issue_id)
            remote = None if links is None else links.get(issue_id)
            if history is None or (links is not None and remote is None):
                msg = "incomplete changelog or remote links"
                raise SchemaViolation(msg, source=_SOURCE, issue_id=issue_id)
            cols = flatten_issue(
                issue, changelog=history, remotelinks=remote, custom_field_ids=self._custom
            )
            ts = parse_source_timestamp(issue["fields"].get("updated"), field="updated")
            rows.append((issue_id, ts, _compact(issue), cols))
        return rows

    def list_keys(self, entity: str) -> Iterator[pa.RecordBatch]:
        """Every issue id in scope as ``KEY_SCHEMA`` batches, one per page (U01-74)."""
        import pyarrow as pa  # noqa: PLC0415 - lazy (module doc)

        from herness.connectors.base import KEY_SCHEMA  # noqa: PLC0415 - lazy (module doc)

        self._entity(entity)
        jql = build_jql(since=None, until=None, scope=self._settings.jql_scope, order="key")
        for issues in self._pages(jql, ["id"], expand=False):
            ids = pa.array([_issue_id(i) for i in issues], pa.string())
            yield pa.RecordBatch.from_arrays([ids], schema=KEY_SCHEMA)

    def discover_fields(self) -> list[FieldCandidate]:
        """Custom field candidates from ``GET /rest/api/{v}/field`` sorted by
        ``(suggested_key, name)``; writes nothing (U01-75)."""
        listing = self._source().get_json(f"/rest/api/{self._version}/field").body
        if not isinstance(listing, list):
            raise SchemaViolation(_BAD_FIELDS, source=_SOURCE)
        found = [c for f in listing if (c := _candidate(f)) is not None]
        return sorted(found, key=lambda c: (c.suggested_key, c.name))

    def _pages(self, jql: str, fields: list[str], *, expand: bool) -> Iterator[list[_Issue]]:
        """Non-empty ``issues`` lists of the search pages, each validated before it is
        yielded."""
        size = self._settings.page_size_for(_ENTITY)
        body: dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": size}
        if self._settings.flavor == "cloud":
            yield from self._cloud_pages(body)
        else:
            yield from self._dc_pages(body | ({"expand": ["changelog"]} if expand else {}))

    def _cloud_pages(self, body: dict[str, Any]) -> Iterator[list[_Issue]]:
        """``POST /rest/api/3/search/jql`` with the body ``nextPageToken`` (U01-73 step 2)."""
        from herness.connectors.http import CursorGuard  # noqa: PLC0415 - lazy (module doc)

        http, guard = self._source(), CursorGuard()
        while True:
            page = _body(http.post_json(_CLOUD_SEARCH, json_body=body))
            issues, token, is_last = _issues(page), page.get("nextPageToken"), page.get("isLast")
            if token is not None and not isinstance(token, str):
                raise SchemaViolation(_BAD_PAGE, source=_SOURCE)
            last = is_last is True or (is_last is None and token is None)
            if not last:
                if token is None:
                    msg = "missing nextPageToken"
                    raise SchemaViolation(msg, source=_SOURCE)
                guard.step(token)
            if issues:
                yield issues
            if last:
                return
            body = body | {"nextPageToken": token}

    def _dc_pages(self, body: dict[str, Any]) -> Iterator[list[_Issue]]:
        """``POST /rest/api/2/search`` with ``startAt += len(issues)`` (U01-73 step 3)."""
        from herness.connectors.http import CursorGuard  # noqa: PLC0415 - lazy (module doc)

        http, guard, start = self._source(), CursorGuard(), 0
        while True:
            guard.step(str(start))
            page = _body(http.post_json(_DC_SEARCH, json_body=body | {"startAt": start}))
            issues, total = _issues(page), page.get("total")
            if not isinstance(total, int) or isinstance(total, bool):
                raise SchemaViolation(_BAD_PAGE, source=_SOURCE)
            if not issues:
                return
            yield issues
            start += len(issues)
            if start >= total:
                return
