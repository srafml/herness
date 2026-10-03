"""Deterministic Jira REST cassettes for the T01-17 tests (impl 01 §11, §13 O-16).

``tools.synth.api_pages`` (T11-15) is not in the tree, so this card-local generator writes
the Jira cassettes into ``tests/fixtures/connectors/jira/``; the files are committed and a
test checks that regenerating reproduces them byte for byte. Regenerate with
``uv run python -m tests.support.jira_pages``.

A cassette is ``{"interactions": [{"request": {...}, "response": {...}}]}`` replayed in
order: ``request`` holds ``method``, ``path`` and the JSON body (``json``, POST only);
``response`` holds ``status`` and ``json``. Hosts are synthetic (``jira.example.test``)
and every page token starts with ``synthetic`` (R-67). The JQL and field lists are written
as literals here, independent of ``herness.connectors.jira``, so the tests compare the
connector's requests against an oracle rather than against themselves.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final

__all__ = ["CASSETTE_DIR", "FILES", "issue", "render_all", "write_jira_pages"]

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


def _post(path: str, body: dict[str, Any], response: object) -> dict[str, Any]:
    return {
        "request": {"method": "POST", "path": path, "json": body},
        "response": {"status": 200, "json": response},
    }


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


FILES: Final = {
    "cloud_search.json": _cloud_search,
    "cloud_search_no_token.json": _cloud_search_no_token,
    "cloud_keys.json": _cloud_keys,
    "dc_keys.json": _dc_keys,
    "dc_search.json": _dc_search,
    "fields.json": _fields,
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
