"""Deterministic Jira REST cassettes for the T01-17 and T01-18 tests (impl 01 §11, §13 O-16).

``tools.synth.api_pages`` (T11-15) is not in the tree, so this card-local generator writes
the Jira cassettes into ``tests/fixtures/connectors/jira/``; the files are committed and a
test checks that regenerating reproduces them byte for byte. Regenerate with
``uv run python -m tests.support.jira_pages``.

A cassette is ``{"interactions": [{"request": {...}, "response": {...}}]}`` replayed in
order: ``request`` holds ``method``, ``path`` and the JSON body (``json``, POST only);
``params`` (the query, GET only, values as text); ``response`` holds ``status`` and
``json``. Hosts are synthetic (``jira.example.test``, ``*.example.test`` in remote links) and
every page token starts with ``synthetic`` (R-67). Changelog histories carry an ``author``,
which the connector drops; ``self`` / ``nextPage`` URLs in bodies are data, never followed.
The JQL and field lists are written as literals here, independent of
``herness.connectors.jira``, so the tests compare the connector's requests against an oracle
rather than against themselves.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CASSETTE_DIR", "FILES", "histories", "history", "issue", "remote_link", "render_all",
    "write_jira_pages",
]  # fmt: skip

CASSETTE_DIR: Final = Path(__file__).resolve().parents[1] / "fixtures" / "connectors" / "jira"
BASE_URL: Final = "https://jira.example.test"
SCOPE: Final = "project = SYN"
# since 2026-09-01 12:00:30+02:00 and until 2026-09-02 09:59:59.9Z: UTC, floored to the minute
SEARCH_JQL: Final = (
    'updated >= "2026/09/01 10:00" AND updated < "2026/09/02 09:59" AND (project = SYN) '
    "ORDER BY updated ASC, id ASC"
)
KEY_JQL: Final = "(project = SYN) ORDER BY id ASC"
CUSTOM_FIELD_IDS: Final = ("customfield_10016", "customfield_10014")
SEARCH_FIELDS: Final = [
    "issuetype", "parent", "project", "components", "labels", "status", "created",
    "resolutiondate", "summary", "description", "updated", "issuelinks", *CUSTOM_FIELD_IDS,
]  # fmt: skip
CLOUD_SEARCH: Final = "/rest/api/3/search/jql"
DC_SEARCH: Final = "/rest/api/2/search"
TOKEN_1: Final = "synthetic-next-page-token-1"  # noqa: S105  # pragma: allowlist secret
KEY_TOKENS: Final = ("synthetic-key-page-token-1", "synthetic-key-page-token-2")
FIRST_ID: Final = 10001
CLOUD_BULK: Final = "/rest/api/3/changelog/bulkfetch"
BULK_TOKEN: Final = "synthetic-changelog-token-1"  # noqa: S105  # pragma: allowlist secret
# The synth profile's `mappings.custom_fields.jira` (impl 11 U11-08): story_points, team,
# estimate_cost_usd in mappings order; epic_link unmapped (synthetic issues carry `parent`).
SYNTH_CUSTOM_FIELDS: Final = {
    "story_points": "customfield_10016",
    "team": "customfield_10060",
    "estimate_cost_usd": "customfield_10050",
    "epic_link": None,
}
SYNTH_FIELD_IDS: Final = ("customfield_10016", "customfield_10060", "customfield_10050")


def _post(path: str, body: dict[str, Any], response: object) -> dict[str, Any]:
    return {
        "request": {"method": "POST", "path": path, "json": body},
        "response": {"status": 200, "json": response},
    }


def _get(path: str, response: object, params: dict[str, str] | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"method": "GET", "path": path}
    if params is not None:
        request["params"] = params
    return {"request": request, "response": {"status": 200, "json": response}}


def _adf(n: int) -> dict[str, Any]:
    """A small Atlassian Document Format description (Cloud v3 returns objects)."""
    text = {"type": "text", "text": f"Synthetic description {n}"}
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [text]}]}


def issue(n: int) -> dict[str, Any]:
    """Synthetic issue ``n`` (1-based): id ``10000 + n``, key ``SYN-n``, ``updated`` ascending."""
    minute = 5 + n
    fields: dict[str, Any] = {
        "issuetype": {"id": "10002", "name": "Story"},
        "parent": {"id": "10000", "key": "SYN-0"},
        "project": {"id": "10100", "key": "SYN", "name": "Synthetic"},
        "components": [{"id": "10200", "name": "core"}],
        "labels": ["synthetic"],
        "status": {"name": "Done", "statusCategory": {"key": "done"}},
        "created": "2026-08-01T09:00:00.000+0000",
        "resolutiondate": None if n % 2 else "2026-09-01T09:30:00.000+0000",
        "summary": f"Synthetic issue {n}",
        "description": _adf(n),
        "updated": f"2026-09-01T{10 + minute // 60:02d}:{minute % 60:02d}:00.000+0000",
        "issuelinks": [],
        "customfield_10016": float(n % 8),
        "customfield_10014": "SYN-0",
    }
    return {"id": str(FIRST_ID - 1 + n), "key": f"SYN-{n}", "fields": fields}


def _cloud_search() -> dict[str, Any]:
    body = {"jql": SEARCH_JQL, "fields": SEARCH_FIELDS, "maxResults": 100}
    first: dict[str, Any] = {"issues": [issue(n) for n in range(1, 38)]}
    first |= {"nextPageToken": TOKEN_1, "isLast": False}  # 37 < maxResults, yet not last
    second = {"issues": [issue(n) for n in range(38, 41)], "isLast": True}
    return {
        "interactions": [
            _post(CLOUD_SEARCH, body, first),
            _post(CLOUD_SEARCH, body | {"nextPageToken": TOKEN_1}, second),
        ]
    }


def _cloud_search_no_token() -> dict[str, Any]:
    body = {"jql": SEARCH_JQL, "fields": SEARCH_FIELDS, "maxResults": 100}
    page = {"issues": [issue(1), issue(2)], "isLast": False}
    return {"interactions": [_post(CLOUD_SEARCH, body, page)]}


def _key(n: int) -> dict[str, str]:
    return {"id": str(FIRST_ID - 1 + n)}


def _cloud_keys() -> dict[str, Any]:
    body: dict[str, Any] = {"jql": KEY_JQL, "fields": ["id"], "maxResults": 100}
    pages: list[dict[str, Any]] = [
        {"issues": [_key(n) for n in range(1, 101)], "nextPageToken": KEY_TOKENS[0]},
        {"issues": [_key(n) for n in range(101, 151)], "nextPageToken": KEY_TOKENS[1]},
        {"issues": [_key(n) for n in range(151, 156)], "isLast": True},
    ]
    tokens = [None, *KEY_TOKENS]
    return {
        "interactions": [
            _post(CLOUD_SEARCH, body if t is None else body | {"nextPageToken": t}, page)
            for t, page in zip(tokens, pages, strict=True)
        ]
    }


def _dc_keys() -> dict[str, Any]:
    body: dict[str, Any] = {"jql": KEY_JQL, "fields": ["id"], "maxResults": 50}
    interactions = []
    for start, end in ((0, 50), (50, 100), (100, 120)):
        page: dict[str, Any] = {"startAt": start, "maxResults": 50, "total": 120}
        page["issues"] = [_key(n) for n in range(start + 1, end + 1)]
        interactions.append(_post(DC_SEARCH, body | {"startAt": start}, page))
    return {"interactions": interactions}


def _dc_search() -> dict[str, Any]:
    body: dict[str, Any] = {"jql": SEARCH_JQL, "fields": SEARCH_FIELDS, "maxResults": 50}
    body["expand"] = ["changelog"]
    first = {"startAt": 0, "maxResults": 50, "total": 3, "issues": [issue(1), issue(2)]}
    second = {"startAt": 2, "maxResults": 50, "total": 3, "issues": [issue(3)]}
    return {
        "interactions": [
            _post(DC_SEARCH, body | {"startAt": 0}, first),
            _post(DC_SEARCH, body | {"startAt": 2}, second),
        ]
    }


def _field(fid: str, name: str, *, custom: bool, kind: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": fid, "name": name, "custom": custom}
    if kind is not None:
        out["schema"] = {"type": kind}
    return out


def _fields() -> dict[str, Any]:
    listing = [
        _field("summary", "Summary", custom=False, kind="string"),
        _field("customfield_10016", "Story Points", custom=True, kind="number"),
        _field("customfield_10026", "Story point estimate", custom=True, kind="number"),
        _field("customfield_10001", "Team", custom=True, kind="team"),
        _field("customfield_10014", "Epic Link", custom=True, kind="any"),
        _field("customfield_10100", "Estimated cost USD", custom=True, kind="number"),
        _field("customfield_10101", "Budget", custom=True, kind="number"),
        _field("customfield_10102", "Budget owner", custom=True, kind="string"),
        _field("customfield_10103", "Cost center", custom=False, kind="number"),
        _field("customfield_10104", "Sprint", custom=True, kind=None),
    ]
    request = {"method": "GET", "path": "/rest/api/3/field"}
    return {"interactions": [{"request": request, "response": {"status": 200, "json": listing}}]}


# --- T01-18: complete changelogs and remote links ------------------------------------------

_STATUSES: Final = ("To Do", "In Progress", "In Review", "Done")


def history(n: int, k: int, *, field: str = "status") -> dict[str, Any]:
    """History ``k`` (0-based) of issue ``n``: id ``<10000 + n><k:03d>``, ``created`` one
    minute apart from 2026-08-01T10:00, an ``author`` (dropped by the projection) and one
    item; status items walk through ``_STATUSES``."""
    day, hour = divmod(10 + k // 60, 24)  # the hour carries into the day
    created = f"2026-08-{1 + day:02d}T{hour:02d}:{k % 60:02d}:00.000+0000"
    if field == "status":
        before, after = _STATUSES[k % 3], _STATUSES[k % 3 + 1]
        item = {"field": "status", "fieldtype": "jira", "from": str(k % 3 + 1)}
        item |= {"fromString": before, "to": str(k % 3 + 2), "toString": after}
    else:
        item = {"field": field, "fieldtype": "jira", "from": None, "fromString": None}
        item |= {"to": None, "toString": f"synthetic {k}"}
    author = {"accountId": f"synthetic-account-{k % 4}", "displayName": "Synthetic Person"}
    hid = f"{FIRST_ID - 1 + n}{k:03d}"
    return {"id": hid, "author": author, "created": created, "items": [item]}


def histories(n: int, count: int) -> list[dict[str, Any]]:
    """``count`` histories of issue ``n`` in ascending ``created`` order."""
    return [history(n, k) for k in range(count)]


def _search_body(fields: list[str]) -> dict[str, Any]:
    return {"jql": SEARCH_JQL, "fields": fields, "maxResults": 100}


def _bulk(ids: list[str], token: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"issueIdsOrKeys": ids, "maxResults": 1000}
    return body if token is None else body | {"nextPageToken": token}


def _logs(by_issue: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [{"issueId": str(FIRST_ID - 1 + n), "changeHistories": hs} for n, hs in by_issue.items()]


def _ids(*numbers: int) -> list[str]:
    return [str(FIRST_ID - 1 + n) for n in numbers]


def _cloud_changelog_bulk() -> dict[str, Any]:
    """One search page of 3 issues; the bulk changelog over two pages (``nextPageToken``),
    issue 1's histories split across them and given newest first; issue 3 has none."""
    page = {"issues": [issue(n) for n in (1, 2, 3)], "isLast": True}
    one = histories(1, 3)
    first = {"issueChangeLogs": _logs({1: [one[2], one[1]], 2: histories(2, 2)})}
    first["nextPageToken"] = BULK_TOKEN
    second = {"issueChangeLogs": _logs({1: [one[0]]})}
    return {
        "interactions": [
            _post(CLOUD_SEARCH, _search_body(SEARCH_FIELDS), page),
            _post(CLOUD_BULK, _bulk(_ids(1, 2, 3)), first),
            _post(CLOUD_BULK, _bulk(_ids(1, 2, 3), BULK_TOKEN), second),
        ]
    }


def _issue_changelog(
    n: int, start: int, page: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    path = f"/rest/api/3/issue/{FIRST_ID - 1 + n}/changelog"
    body: dict[str, Any] = {"self": f"{BASE_URL}{path}?startAt={start}&maxResults=100"}
    body |= {"startAt": start, "values": page} | extra
    return _get(path, body, {"startAt": str(start), "maxResults": "100"})


def _cloud_changelog_fallback() -> dict[str, Any]:
    """Two search pages. Page 1: the bulk changelog answers 404, so each issue is read from
    ``/issue/{id}/changelog``: issue 1 has 57 histories over two pages of 50 + 7 (the server
    caps ``maxResults`` at 50), issue 2 has 3 (``isLast`` absent: ``startAt + len >= total``).
    Page 2: no bulk call any more; issue 3 has no histories (``values`` empty)."""
    body = _search_body(SEARCH_FIELDS)
    first = {"issues": [issue(1), issue(2)], "nextPageToken": TOKEN_1, "isLast": False}
    second = {"issues": [issue(3)], "isLast": True}
    missing = {"request": {"method": "POST", "path": CLOUD_BULK, "json": _bulk(_ids(1, 2))}}
    missing["response"] = {"status": 404, "json": {"errorMessages": ["synthetic not found"]}}
    many = histories(1, 57)
    next_url = f"{BASE_URL}/rest/api/3/issue/{FIRST_ID}/changelog?startAt=50&maxResults=50"
    return {
        "interactions": [
            _post(CLOUD_SEARCH, body, first),
            missing,
            _issue_changelog(
                1, 0, many[:50], maxResults=50, total=57, isLast=False, nextPage=next_url
            ),
            _issue_changelog(1, 50, many[50:], maxResults=50, total=57, isLast=True),
            _issue_changelog(2, 0, histories(2, 3), maxResults=100, total=3),
            _post(CLOUD_SEARCH, body | {"nextPageToken": TOKEN_1}, second),
            _issue_changelog(3, 0, [], maxResults=100, total=0),
        ]
    }


def _dc_issue(n: int, count: int, total: int) -> dict[str, Any]:
    changelog = {"startAt": 0, "maxResults": count, "total": total}
    return issue(n) | {"changelog": changelog | {"histories": histories(n, count)}}


def _dc_changelog() -> dict[str, Any]:
    """Data Center search over two ``startAt`` pages with ``expand: changelog``; issue 1's
    search changelog holds 40 of ``total`` 60 histories, so it is refetched from
    ``/rest/api/2/issue/{id}?expand=changelog&fields=id``; issues 2 and 3 are complete."""
    body: dict[str, Any] = {"jql": SEARCH_JQL, "fields": SEARCH_FIELDS, "maxResults": 50}
    body["expand"] = ["changelog"]
    first = {"startAt": 0, "maxResults": 50, "total": 3}
    first["issues"] = [_dc_issue(1, 40, 60), _dc_issue(2, 2, 2)]
    second = {"startAt": 2, "maxResults": 50, "total": 3, "issues": [_dc_issue(3, 0, 0)]}
    full = {"startAt": 0, "maxResults": 60, "total": 60, "histories": histories(1, 60)}
    refetch = {"id": str(FIRST_ID), "key": "SYN-1", "changelog": full}
    params = {"expand": "changelog", "fields": "id"}
    return {
        "interactions": [
            _post(DC_SEARCH, body | {"startAt": 0}, first),
            _get(f"/rest/api/2/issue/{FIRST_ID}", refetch, params),
            _post(DC_SEARCH, body | {"startAt": 2}, second),
        ]
    }


def remote_link(n: int, k: int) -> dict[str, Any]:
    """Remote link ``k`` of issue ``n`` with the members the projection drops."""
    lid = 20000 + 10 * n + k
    ticket = f"INC{12340 + 10 * n + k:07d}"
    obj = {"url": f"https://servicedesk.example.test/incident?number={ticket}"}
    obj |= {"title": f"{ticket} synthetic outage", "icon": {"title": "Synthetic desk"}}
    link: dict[str, Any] = {"id": lid, "self": f"{BASE_URL}/rest/api/3/issue/SYN-{n}/remotelink"}
    link |= {"application": {"type": "com.example.desk", "name": "Synthetic desk"}}
    return link | {"relationship": "mentioned in", "object": obj}


def _remote_links(n: int, count: int, version: str = "3") -> dict[str, Any]:
    path = f"/rest/api/{version}/issue/{FIRST_ID - 1 + n}/remotelink"
    return _get(path, [remote_link(n, k) for k in range(count)])


def _cloud_remote_links() -> dict[str, Any]:
    """One search page of 3 issues, a one-page bulk changelog, then one remote-link call per
    issue (2, 1 and 0 links)."""
    page = {"issues": [issue(n) for n in (1, 2, 3)], "isLast": True}
    logs = {"issueChangeLogs": _logs({1: histories(1, 1), 2: [], 3: histories(3, 2)})}
    return {
        "interactions": [
            _post(CLOUD_SEARCH, _search_body(SEARCH_FIELDS), page),
            _post(CLOUD_BULK, _bulk(_ids(1, 2, 3)), logs),
            _remote_links(1, 2),
            _remote_links(2, 1),
            _remote_links(3, 0),
        ]
    }


def _it_issue(n: int) -> dict[str, Any]:
    """Issue ``n`` with the synth profile's custom fields; issue 2 blocks issue 1, issue 4 is
    still in progress."""
    raw = issue(n)
    fields = {k: v for k, v in raw["fields"].items() if k != "customfield_10014"}
    fields |= {"customfield_10016": float(n), "customfield_10050": 1000.0 * n}
    fields["customfield_10060"] = "Synthetic Team"
    if n == 2:
        link = {"id": "30001", "type": {"name": "Blocks"}, "outwardIssue": {"key": "SYN-1"}}
        fields["issuelinks"] = [link]
    if n == 4:
        fields["status"] = {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}
        fields["resolutiondate"] = None
    return raw | {"fields": fields}


def _cloud_sync_it() -> dict[str, Any]:
    """IT01-11: two search pages (synth custom fields); the bulk changelog of page 1 over two
    pages, of page 2 over one; one remote-link call per issue."""
    body = _search_body(["issuetype", "parent", "project", "components", "labels", "status",
                         "created", "resolutiondate", "summary", "description", "updated",
                         "issuelinks", *SYNTH_FIELD_IDS])  # fmt: skip
    first = {"issues": [_it_issue(n) for n in (1, 2, 3)], "nextPageToken": TOKEN_1}
    first["isLast"] = False
    second = {"issues": [_it_issue(4)], "isLast": True}
    one = histories(1, 3)
    bulk_1 = {"issueChangeLogs": _logs({1: one[1:], 2: histories(2, 2)})}
    bulk_1["nextPageToken"] = BULK_TOKEN
    bulk_2 = {"issueChangeLogs": _logs({1: one[:1], 3: [history(3, 0, field="labels")]})}
    bulk_3 = {"issueChangeLogs": _logs({4: histories(4, 1)})}
    return {
        "interactions": [
            _post(CLOUD_SEARCH, body, first),
            _post(CLOUD_BULK, _bulk(_ids(1, 2, 3)), bulk_1),
            _post(CLOUD_BULK, _bulk(_ids(1, 2, 3), BULK_TOKEN), bulk_2),
            _remote_links(1, 1),
            _remote_links(2, 0),
            _remote_links(3, 2),
            _post(CLOUD_SEARCH, body | {"nextPageToken": TOKEN_1}, second),
            _post(CLOUD_BULK, _bulk(_ids(4)), bulk_3),
            _remote_links(4, 0),
        ]
    }


FILES: Final = {
    "cloud_search.json": _cloud_search,
    "cloud_search_no_token.json": _cloud_search_no_token,
    "cloud_keys.json": _cloud_keys,
    "dc_keys.json": _dc_keys,
    "dc_search.json": _dc_search,
    "fields.json": _fields,
    "cloud_changelog_bulk.json": _cloud_changelog_bulk,
    "cloud_changelog_fallback.json": _cloud_changelog_fallback,
    "dc_changelog.json": _dc_changelog,
    "cloud_remote_links.json": _cloud_remote_links,
    "cloud_sync_it.json": _cloud_sync_it,
}


def render_all() -> dict[str, bytes]:
    """Every cassette's file name and exact bytes (UTF-8, LF, trailing newline)."""
    return {
        name: (json.dumps(build(), indent=2, ensure_ascii=False) + "\n").encode()
        for name, build in FILES.items()
    }


def write_jira_pages(out_dir: Path = CASSETTE_DIR) -> list[Path]:
    """Write every cassette into ``out_dir``; return the paths written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, data in render_all().items():
        path = out_dir / name
        path.write_bytes(data)
        written.append(path)
    return written


if __name__ == "__main__":
    for written_path in write_jira_pages():
        sys.stdout.write(f"{written_path}\n")
