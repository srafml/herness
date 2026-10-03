"""Complete Jira changelogs and remote links per search page, and their projections
(impl 01 U01-76, U01-77, U01-94; design 01 §4.2, §5.8).

The values stored in the ``changelog`` and ``remotelinks`` columns of the Jira ``issue``
row: each history keeps ``{id, created, items[{field, from, fromString, to, toString}]}``
and each remote link ``{id, object{url, title}}``. Every other member (``author``,
``application``, ``relationship``, ...) is dropped (personal-data minimisation, TH01-05).
Both projections are pure and idempotent.

Fetches go through ``SourceHttp`` (imported lazily, so importing ``flatten_issue`` loads no
HTTP layer). Paging is by body ``nextPageToken`` (bulk, under ``CursorGuard``) or integer
``startAt``; ``self`` / ``nextPage`` / remote-link URLs are data and never requested. Issue
ids must be ASCII digits before they enter a request path. Any shape error raises
``SchemaViolation`` before the caller yields the page (no lake write, no watermark move).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from herness.core.errors import SchemaViolation
from herness.core.logging import get_logger

if TYPE_CHECKING:
    from herness.connectors.http import SourceHttp

__all__ = [
    "ChangelogState", "fetch_changelogs", "fetch_remote_links", "project_history",
    "project_remote_link",
]  # fmt: skip

type _Raw = list[Mapping[str, Any]]

_SOURCE: Final = "jira"
_ITEM_MEMBERS: Final = ("field", "from", "fromString", "to", "toString")
_ID_RE: Final = re.compile(r"[0-9]+")
_BULK: Final = "/rest/api/3/changelog/bulkfetch"
_BULK_MAX: Final = 1000
_PAGE_MAX: Final = 100
_BAD_PAGE: Final = "bad changelog page"
_BAD_LINKS: Final = "bad remote link page"
_log = get_logger("connectors.jira")


@dataclass(slots=True)
class ChangelogState:
    """Per-connector changelog state (U01-76): whether the Cloud bulk endpoint answers."""

    bulk_available: bool = True


def project_history(history: Mapping[str, object]) -> dict[str, object]:
    """``{"id", "created", "items": [...]}`` of one changelog history (U01-94).

    Missing item members become ``None``; ``items`` absent gives ``[]``; items that are not
    mappings are skipped. ``SchemaViolation("bad changelog history")`` when ``id`` or
    ``created`` is not a string or ``items`` is not a list.
    """
    hid, created, items = history.get("id"), history.get("created"), history.get("items")
    if items is None:
        items = []
    if not (isinstance(hid, str) and isinstance(created, str) and isinstance(items, list)):
        msg = "bad changelog history"
        raise SchemaViolation(msg, source=_SOURCE)
    projected = [
        {member: item.get(member) for member in _ITEM_MEMBERS}
        for item in items
        if isinstance(item, Mapping)
    ]
    return {"id": hid, "created": created, "items": projected}


def project_remote_link(link: Mapping[str, object]) -> dict[str, object]:
    """``{"id", "object": {"url", "title"}}`` of one remote link, ``id`` as a string (U01-94).

    ``SchemaViolation("bad remote link")`` when ``id`` is not a number or string or
    ``object`` is not a mapping.
    """
    lid, obj = link.get("id"), link.get("object")
    valid_id = isinstance(lid, str) or (isinstance(lid, int) and not isinstance(lid, bool))
    if not (valid_id and isinstance(obj, Mapping)):
        msg = "bad remote link"
        raise SchemaViolation(msg, source=_SOURCE)
    return {"id": str(lid), "object": {"url": obj.get("url"), "title": obj.get("title")}}


def _bad(msg: str = _BAD_PAGE, *, issue_id: str | None = None) -> SchemaViolation:
    """A data-free ``SchemaViolation``; context ``source`` and the numeric ``issue_id``."""
    if issue_id is None:
        return SchemaViolation(msg, source=_SOURCE)
    return SchemaViolation(msg, source=_SOURCE, issue_id=issue_id)


def _issue_id(value: object) -> str:
    """``value`` when it is a string of ASCII digits; ``SchemaViolation`` otherwise."""
    if not (isinstance(value, str) and _ID_RE.fullmatch(value)):
        msg = "bad issue id"
        raise _bad(msg)
    return value


def _mapping(value: object, issue_id: str | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _bad(issue_id=issue_id)
    return value


def _records(value: object, issue_id: str | None = None) -> _Raw:
    """``value`` when it is a list of mappings; ``SchemaViolation`` otherwise."""
    if not (isinstance(value, list) and all(isinstance(v, Mapping) for v in value)):
        raise _bad(issue_id=issue_id)
    return value


def _count(value: object, issue_id: str | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _bad(issue_id=issue_id)
    return value


def _ordered(raw: _Raw, issue_id: str) -> list[dict[str, object]]:
    """Projected histories ascending by ``(created, id)``; ``author`` dropped. A repeated
    history ``id`` (a server ignoring ``startAt`` or repeating a bulk page) is refused."""
    histories = [project_history(h) for h in raw]
    if len({h["id"] for h in histories}) != len(histories):
        msg = "duplicate changelog history"
        raise _bad(msg, issue_id=issue_id)
    return sorted(histories, key=lambda h: (str(h["created"]), str(h["id"])))


def fetch_changelogs(
    http: SourceHttp,
    *,
    flavor: Literal["cloud", "datacenter"],
    issues: Sequence[Mapping[str, object]],
    state: ChangelogState,
) -> dict[str, list[dict[str, object]]]:
    """The complete changelog of every issue of one search page (U01-76): issue id ->
    projected histories ascending by ``(created, id)``; every id has an entry."""
    ids = [_issue_id(issue.get("id")) for issue in issues]
    if flavor == "datacenter":
        raw = {i: _dc_histories(http, i, issue) for i, issue in zip(ids, issues, strict=True)}
    else:
        raw = _cloud_histories(http, ids, state)
    return {i: _ordered(raw[i], i) for i in ids}


def _cloud_histories(http: SourceHttp, ids: list[str], state: ChangelogState) -> dict[str, _Raw]:
    """Bulk while it answers; after its first 404 (``SourceNotFound``), per issue."""
    from herness.connectors.http import SourceNotFound  # noqa: PLC0415 - lazy (module doc)

    if state.bulk_available and ids:
        try:
            return _bulk(http, ids)
        except SourceNotFound:
            state.bulk_available = False
            _log.warning("connectors.jira.bulk_changelog_unavailable", source=_SOURCE)
    return {i: _issue_pages(http, i) for i in ids}


def _bulk(http: SourceHttp, ids: list[str]) -> dict[str, _Raw]:
    """``POST /rest/api/3/changelog/bulkfetch`` until no ``nextPageToken``; histories are
    appended to their ``issueId``, which must be one of ``ids``."""
    from herness.connectors.http import CursorGuard  # noqa: PLC0415 - lazy (module doc)

    out: dict[str, _Raw] = {i: [] for i in ids}
    body: dict[str, object] = {"issueIdsOrKeys": ids, "maxResults": _BULK_MAX}
    guard = CursorGuard()
    while True:
        page = _mapping(http.post_json(_BULK, json_body=body).body)
        for log in _records(page.get("issueChangeLogs")):
            issue_id = log.get("issueId")
            if not (isinstance(issue_id, str) and issue_id in out):
                raise _bad()
            out[issue_id].extend(_records(log.get("changeHistories"), issue_id=issue_id))
        token = page.get("nextPageToken")
        if token is None:
            return out
        if not isinstance(token, str):
            raise _bad()
        guard.step(token)
        body = body | {"nextPageToken": token}


def _issue_pages(http: SourceHttp, issue_id: str) -> _Raw:
    """``GET /rest/api/3/issue/{id}/changelog`` by ``startAt`` until ``isLast``, or
    ``startAt + len(values) >= total``, or no ``values``."""
    from herness.connectors.http import CursorGuard  # noqa: PLC0415 - lazy (module doc)

    path, start, guard = f"/rest/api/3/issue/{issue_id}/changelog", 0, CursorGuard()
    out: _Raw = []
    while True:
        guard.step(str(start))
        query = {"startAt": start, "maxResults": _PAGE_MAX}
        page = _mapping(http.get_json(path, params=query).body, issue_id=issue_id)
        values = _records(page.get("values"), issue_id=issue_id)
        total, is_last = _count(page.get("total"), issue_id=issue_id), page.get("isLast")
        if is_last is not None and not isinstance(is_last, bool):
            raise _bad(issue_id=issue_id)
        out.extend(values)
        start += len(values)
        if not values or is_last or start >= total:
            return out


def _dc_histories(http: SourceHttp, issue_id: str, issue: Mapping[str, object]) -> _Raw:
    """The search ``changelog.histories``; when ``total`` exceeds them, those of
    ``GET /rest/api/2/issue/{id}?expand=changelog&fields=id`` (at least ``total``)."""
    changelog = _mapping(issue.get("changelog"), issue_id=issue_id)
    histories = _records(changelog.get("histories"), issue_id=issue_id)
    total = _count(changelog.get("total"), issue_id=issue_id)
    if total <= len(histories):
        return histories
    query = {"expand": "changelog", "fields": "id"}
    page = http.get_json(f"/rest/api/2/issue/{issue_id}", params=query)
    body = _mapping(page.body, issue_id=issue_id)
    full = _mapping(body.get("changelog"), issue_id=issue_id)
    histories = _records(full.get("histories"), issue_id=issue_id)
    if body.get("id") != issue_id:
        raise _bad(issue_id=issue_id)
    if len(histories) < total:
        msg = "incomplete changelog"
        raise _bad(msg, issue_id=issue_id)
    return histories


def fetch_remote_links(
    http: SourceHttp, *, version: Literal["2", "3"], issue_ids: Sequence[str]
) -> dict[str, list[dict[str, object]]]:
    """Remote links of every issue, one ``GET /rest/api/{version}/issue/{id}/remotelink``
    each (U01-77); the body must be a list; each link projected (URLs never fetched)."""
    out: dict[str, list[dict[str, object]]] = {}
    for raw_id in issue_ids:
        issue_id = _issue_id(raw_id)
        body = http.get_json(f"/rest/api/{version}/issue/{issue_id}/remotelink").body
        if not isinstance(body, list):
            raise _bad(_BAD_LINKS, issue_id=issue_id)
        out[issue_id] = [project_remote_link(_link(link, issue_id)) for link in body]
    return out


def _link(link: object, issue_id: str) -> Mapping[str, object]:
    if not isinstance(link, Mapping):
        msg = "bad remote link"
        raise _bad(msg, issue_id=issue_id)
    return link
