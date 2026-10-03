"""Cassette replay, settings and seam stubs for the Jira tests (T01-17).

The cassettes come from ``tests.support.jira_pages`` (committed under
``tests/fixtures/connectors/jira/``). ``Replay`` answers them in order through an
``httpx2.MockTransport`` client handed to ``SourceHttp`` (connectors never build clients;
tests may, T01-14 pattern) and records every request; a request that differs from the
recorded one (method, path or JSON body) fails the test.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest
from tests.support import jira_pages
from tests.unit.connectors._http_data import mock_client

from herness.connectors import jira as jira_module
from herness.connectors.http import SourceHttp
from herness.connectors.jira import JiraConnector
from herness.connectors.settings import JiraSettings

BASE = jira_pages.BASE_URL
NOW = datetime.datetime(2026, 9, 2, 10, 0, tzinfo=datetime.UTC)
SINCE = datetime.datetime(
    2026, 9, 1, 12, 0, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=2))
)
UNTIL = datetime.datetime(2026, 9, 2, 9, 59, 59, 900_000, tzinfo=datetime.UTC)


def cassette(name: str) -> list[dict[str, Any]]:
    """The interactions of the committed cassette ``name``."""
    text = (jira_pages.CASSETTE_DIR / name).read_text(encoding="utf-8")
    interactions: list[dict[str, Any]] = json.loads(text)["interactions"]
    return interactions


@dataclass
class Replay:
    """Answers ``interactions`` in order; ``seen`` holds ``(method, path, json body)``."""

    interactions: list[dict[str, Any]]
    seen: list[tuple[str, str, Any]] = field(default_factory=list)
    headers: list[httpx2.Headers] = field(default_factory=list)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path, body))
        self.headers.append(request.headers)
        assert self.interactions, "unexpected extra request"
        want = self.interactions.pop(0)
        recorded = want["request"]
        assert (request.method, request.url.path) == (recorded["method"], recorded["path"])
        if "json" in recorded or request.method == "GET":
            assert body == recorded.get("json")
        reply = want["response"]
        content = json.dumps(reply["json"]).encode()
        return httpx2.Response(reply["status"], content=iter([content]))


def pages(*bodies: object, path: str = jira_pages.CLOUD_SEARCH) -> Replay:
    """A replay answering ``bodies`` in order to POSTs on ``path`` (request bodies unchecked)."""
    return Replay(
        [
            {"request": {"method": "POST", "path": path}, "response": {"status": 200, "json": b}}
            for b in bodies
        ]
    )


def settings(**extra: Any) -> JiraSettings:
    """Cloud settings on the synthetic host, scoped to project SYN, unless overridden."""
    data: dict[str, Any] = {
        "enabled": True,
        "flavor": "cloud",
        "base_url": BASE,
        "auth": {"method": "api_token", "credentials": "secret:jira_token"},
        "jql_scope": jira_pages.SCOPE,
    }
    if extra.get("flavor") == "datacenter":
        data["auth"] = {"method": "pat", "credentials": "secret:jira_token"}
    return JiraSettings.model_validate(data | extra)


def connector(
    replay: Replay, *, custom: Sequence[str] = jira_pages.CUSTOM_FIELD_IDS, **extra: Any
) -> JiraConnector:
    """A connector whose ``SourceHttp`` replays ``replay`` (no auth, fixed clock)."""
    http = SourceHttp(mock_client(replay, BASE), breaker_key="jira", auth=None, clock=lambda: NOW)
    return JiraConnector(settings(**extra), custom_field_ids=custom, http=http, clock=lambda: NOW)


def history(hid: str, created: str, **extra: Any) -> dict[str, Any]:
    """A raw changelog history with an ``author`` (dropped by the projection)."""
    item = {"field": "status", "fieldtype": "jira", "from": "1", "fromString": "To Do"}
    item |= {"to": "3", "toString": "Done"}
    author = {"accountId": "synthetic-account", "displayName": "Synthetic Person"}
    return {"id": hid, "created": created, "author": author, "items": [item]} | extra


@dataclass
class Seams:
    """Stand-ins for the T01-18 seams: two histories per issue, one remote link per issue."""

    changelog_calls: list[tuple[str, list[str]]] = field(default_factory=list)
    link_calls: list[tuple[str, list[str]]] = field(default_factory=list)

    def changelogs(
        self, http: object, *, flavor: str, issues: Sequence[Mapping[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        del http
        ids = [str(i["id"]) for i in issues]
        self.changelog_calls.append((flavor, ids))
        late, early = "2026-09-01T09:00:00.000+0000", "2026-08-02T09:00:00.000+0000"
        return {i: [history(f"{i}2", late), history(f"{i}1", early)] for i in ids}

    def remote_links(
        self, http: object, *, version: str, issue_ids: Iterable[str]
    ) -> dict[str, list[dict[str, Any]]]:
        del http
        ids = list(issue_ids)
        self.link_calls.append((version, ids))
        obj = {"url": "https://wiki.example.test/page", "title": "Synthetic page", "icon": {}}
        return {i: [{"id": 1, "object": obj, "relationship": "mentions"}] for i in ids}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Seams:
        monkeypatch.setattr(jira_module, "_fetch_changelogs", self.changelogs)
        monkeypatch.setattr(jira_module, "_fetch_remote_links", self.remote_links)
        return self
