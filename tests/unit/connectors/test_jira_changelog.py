"""Tests for the complete Jira changelog and remote links: herness.connectors.jira_changelog
``fetch_changelogs``, ``ChangelogState`` and ``fetch_remote_links`` through
``JiraConnector.sync`` (impl 01 U01-76, U01-77, U01-73; T01-18; TH01-05).

Pages are replayed from the committed cassettes (``tests.support.jira_pages``) through a
``SourceHttp`` over an ``httpx2.MockTransport`` client; every request (method, path, JSON
body, GET query) must match the recorded one, and no request beyond the cassette is allowed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest
from structlog.testing import capture_logs
from tests.support import jira_pages
from tests.unit.connectors._jira_data import (
    SINCE,
    UNTIL,
    Replay,
    cassette,
    connector,
    gets,
    source_http,
)

from herness.connectors.http import SourceNotFound
from herness.connectors.jira_changelog import (
    ChangelogState,
    fetch_changelogs,
    fetch_remote_links,
    project_history,
)
from herness.core.errors import SchemaViolation
from herness.core.resilience import ProcessState

pytestmark = pytest.mark.unit

BULK = "/rest/api/3/changelog/bulkfetch"
UNAVAILABLE = "connectors.jira.bulk_changelog_unavailable"


@pytest.fixture(autouse=True)
def _no_breaker(monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState) -> None:
    """`retry_page` runs without breaker or ops store; sleeps are no-ops."""
    import herness.core.resilience.retry as retry_module  # noqa: PLC0415 - test seam

    class _Breaker:
        def record_failure(self, err: Exception) -> None:
            del err

        def record_success(self) -> None:
            pass

    reset_process_state.sleep = lambda _s: None
    monkeypatch.setattr(retry_module, "guard", lambda _key: None)
    monkeypatch.setattr(retry_module, "breaker", lambda _key: _Breaker())


def _table(batches: Iterator[pa.RecordBatch]) -> pa.Table:
    return pa.Table.from_batches(list(batches))


def _column(table: pa.Table, name: str) -> dict[str, Any]:
    """``id`` -> decoded JSON value (or ``None``) of column ``name``."""
    ids = table.column("id").to_pylist()
    values = table.column(name).to_pylist()
    return {i: None if v is None else json.loads(v) for i, v in zip(ids, values, strict=True)}


def _expected(n: int, count: int) -> list[dict[str, object]]:
    """The projected histories of issue ``n`` (ascending, no ``author``)."""
    return [project_history(h) for h in jira_pages.histories(n, count)]


def _post(path: str, body: object, status: int = 200) -> dict[str, Any]:
    return {
        "request": {"method": "POST", "path": path},
        "response": {"status": status, "json": body},
    }


def _issue(n: int, **extra: Any) -> dict[str, Any]:
    return jira_pages.issue(n) | extra


# --- UT01-74: Cloud bulk changelog, 404 fallback ------------------------------------------


def test_ut01_74_bulk_404_falls_back_per_issue_57_histories() -> None:
    """UT01-74 the bulk changelog answers 404: the connector pages
    `/issue/{id}/changelog` by `startAt` (57 histories over 50 + 7), logs
    `connectors.jira.bulk_changelog_unavailable` once and makes no further bulk call on the
    next search page; `changelog` holds all 57 histories without `author`."""
    replay = Replay(cassette("cloud_changelog_fallback.json"))
    conn = connector(replay)
    with capture_logs() as logs:
        table = _table(conn.sync("issue", SINCE, UNTIL))
    assert not replay.interactions
    assert [(m, p) for m, p, _ in replay.seen] == [
        ("POST", jira_pages.CLOUD_SEARCH),
        ("POST", BULK),
        ("GET", "/rest/api/3/issue/10001/changelog"),
        ("GET", "/rest/api/3/issue/10001/changelog"),
        ("GET", "/rest/api/3/issue/10002/changelog"),
        ("POST", jira_pages.CLOUD_SEARCH),
        ("GET", "/rest/api/3/issue/10003/changelog"),
    ]
    assert replay.seen[1][2] == {"issueIdsOrKeys": ["10001", "10002"], "maxResults": 1000}
    starts = [q.get("startAt") for q in replay.queries]
    assert starts == [None, None, "0", "50", "0", None, "0"]
    assert {q.get("maxResults") for q in replay.queries if q} == {"100"}
    changelog = _column(table, "changelog")
    assert len(changelog["10001"]) == 57
    assert changelog["10001"] == _expected(1, 57)
    assert all("author" not in h for h in changelog["10001"])
    assert changelog["10002"] == _expected(2, 3)
    assert changelog["10003"] == []
    warnings = [e for e in logs if e["event"] == UNAVAILABLE]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert conn._cl_state.bulk_available is False


def test_ut01_74_bulk_pages_appended_per_issue() -> None:
    """UT01-74 bulk changelog pages follow the body `nextPageToken`; histories of one issue
    split across pages are appended and sorted by (created, id); an issue without
    histories gets `[]`; no warning while bulk works."""
    replay = Replay(cassette("cloud_changelog_bulk.json"))
    conn = connector(replay)
    with capture_logs() as logs:
        table = _table(conn.sync("issue", SINCE, UNTIL))
    assert not replay.interactions
    bodies = [b for _, p, b in replay.seen if p == BULK]
    ids = ["10001", "10002", "10003"]
    assert bodies == [
        {"issueIdsOrKeys": ids, "maxResults": 1000},
        {"issueIdsOrKeys": ids, "maxResults": 1000, "nextPageToken": jira_pages.BULK_TOKEN},
    ]
    changelog = _column(table, "changelog")
    assert changelog == {"10001": _expected(1, 3), "10002": _expected(2, 2), "10003": []}
    assert table.column("changelog")[2].as_py() == "[]"
    assert not [e for e in logs if e["event"] == UNAVAILABLE]
    assert conn._cl_state.bulk_available is True


def test_ut01_74_bulk_404_on_a_later_page_refetches_the_whole_page() -> None:
    """UT01-74 a 404 on the second bulk page discards the partial bulk result: every issue
    of the page is read per issue; the state flips once."""
    state = ChangelogState()
    first = {"issueChangeLogs": [{"issueId": "10001", "changeHistories": [{"id": "x"}]}]}
    first["nextPageToken"] = "synthetic-t1"
    one = {"startAt": 0, "total": 1, "values": jira_pages.histories(1, 1)}
    replay = Replay(
        [
            _post(BULK, first),
            _post(BULK, {"errorMessages": []}, status=404),
            *gets(one, path="/rest/api/3/issue/10001/changelog"),
        ]
    )
    replay.interactions[-1]["request"]["params"] = {"startAt": "0", "maxResults": "100"}
    got = fetch_changelogs(source_http(replay), flavor="cloud", issues=[_issue(1)], state=state)
    assert got == {"10001": _expected(1, 1)}
    assert state.bulk_available is False
    assert not replay.interactions


def test_ut01_74_state_off_skips_bulk_and_empty_page_makes_no_call() -> None:
    """UT01-74 with `bulk_available` false no bulk call is made; an empty issue list makes
    no call at all and returns `{}`."""
    replay = Replay([])
    http = source_http(replay)
    assert fetch_changelogs(http, flavor="cloud", issues=[], state=ChangelogState()) == {}
    assert fetch_changelogs(http, flavor="datacenter", issues=[], state=ChangelogState()) == {}
    page = {"startAt": 0, "total": 2, "isLast": True, "values": jira_pages.histories(2, 2)}
    replay.interactions = gets(page, path="/rest/api/3/issue/10002/changelog")
    replay.interactions[0]["request"]["params"] = {"startAt": "0", "maxResults": "100"}
    state = ChangelogState(bulk_available=False)
    got = fetch_changelogs(http, flavor="cloud", issues=[_issue(2)], state=state)
    assert got == {"10002": _expected(2, 2)}
    assert [p for _, p, _ in replay.seen] == ["/rest/api/3/issue/10002/changelog"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ([], "bad changelog page"),
        ({"values": []}, "bad changelog page"),
        ({"issueChangeLogs": [7]}, "bad changelog page"),
        ({"issueChangeLogs": [{"issueId": "99999", "changeHistories": []}]}, "bad changelog page"),
        (
            {"issueChangeLogs": [{"issueId": ["10001"], "changeHistories": []}]},
            "bad changelog page",
        ),
        ({"issueChangeLogs": [{"issueId": "10001", "changeHistories": {}}]}, "bad changelog page"),
        ({"issueChangeLogs": [], "nextPageToken": 3}, "bad changelog page"),
        (
            {"issueChangeLogs": [{"issueId": "10001", "changeHistories": [{"id": 1}]}]},
            "bad changelog history",
        ),
    ],
)
def test_ut01_74_bad_bulk_pages_are_schema_violations(body: object, message: str) -> None:
    """UT01-74 malformed bulk changelog pages raise `SchemaViolation` (TH01-05); the
    message holds no ticket text."""
    replay = Replay([_post(BULK, body)])
    with pytest.raises(SchemaViolation, match=f"^{message}$") as info:
        fetch_changelogs(
            source_http(replay), flavor="cloud", issues=[_issue(1)], state=ChangelogState()
        )
    assert info.value.context["source"] == "jira"


def test_ut01_74_repeated_bulk_token_is_refused() -> None:
    """UT01-74 a bulk changelog that repeats its `nextPageToken` cannot loop forever."""
    page = {"issueChangeLogs": [], "nextPageToken": "synthetic-same"}
    replay = Replay([_post(BULK, page), _post(BULK, page)])
    with pytest.raises(SchemaViolation, match="pagination cursor repeated"):
        fetch_changelogs(
            source_http(replay), flavor="cloud", issues=[_issue(1)], state=ChangelogState()
        )


@pytest.mark.parametrize(
    "page",
    [
        [],
        {"values": None, "total": 1},
        {"values": [3], "total": 1},
        {"values": [], "total": "1"},
        {"values": [], "total": True},
        {"values": [], "total": -1},
        {"values": [], "total": 0, "isLast": "yes"},
    ],
)
def test_ut01_74_bad_issue_changelog_pages(page: object) -> None:
    """UT01-74 a malformed `/issue/{id}/changelog` page is a `SchemaViolation` carrying
    the numeric issue id only."""
    replay = Replay(gets(page, path="/rest/api/3/issue/10001/changelog"))
    replay.interactions[0]["request"]["params"] = {"startAt": "0", "maxResults": "100"}
    state = ChangelogState(bulk_available=False)
    with pytest.raises(SchemaViolation, match=r"^bad changelog page$") as info:
        fetch_changelogs(source_http(replay), flavor="cloud", issues=[_issue(1)], state=state)
    assert info.value.context == {"source": "jira", "issue_id": "10001"}


def test_ut01_74_issue_changelog_404_is_not_swallowed() -> None:
    """UT01-74 only the bulk endpoint falls back: a 404 on the per-issue changelog is
    raised (entity failed, watermark unchanged)."""
    replay = Replay(gets({}, path="/rest/api/3/issue/10001/changelog", status=404))
    replay.interactions[0]["request"]["params"] = {"startAt": "0", "maxResults": "100"}
    state = ChangelogState(bulk_available=False)
    with pytest.raises(SourceNotFound):
        fetch_changelogs(source_http(replay), flavor="cloud", issues=[_issue(1)], state=state)


@pytest.mark.parametrize("bad", [{"key": "SYN-1"}, {"id": "1/../2"}, {"id": 10001}])
def test_ut01_74_bad_issue_id_is_refused_before_any_call(bad: dict[str, Any]) -> None:
    """UT01-74 an issue id that is not ASCII digits never reaches a request path."""
    replay = Replay([])
    with pytest.raises(SchemaViolation, match=r"^bad issue id$"):
        fetch_changelogs(source_http(replay), flavor="cloud", issues=[bad], state=ChangelogState())
    assert replay.seen == []


@pytest.mark.parametrize("bad", ["", "1/../2", "SYN-1", "\uff11"])
def test_ut01_76_bad_issue_id_is_refused_before_any_call(bad: str) -> None:
    """UT01-76 a remote-link issue id that is not ASCII digits never reaches a path."""
    replay = Replay([])
    with pytest.raises(SchemaViolation, match=r"^bad issue id$"):
        fetch_remote_links(source_http(replay), version="3", issue_ids=[bad])
    assert replay.seen == []


def test_ut01_74_bad_bulk_page_fails_closed_before_any_batch() -> None:
    """UT01-74 (TH01-05) a malformed changelog raises on the first `next()`, before any
    batch reaches the lake writer: the watermark cannot move."""
    search = cassette("cloud_changelog_bulk.json")[0]
    replay = Replay([search, _post(BULK, {"issueChangeLogs": "none"})])
    with pytest.raises(SchemaViolation, match="bad changelog page"):
        next(connector(replay).sync("issue", SINCE, UNTIL))
    assert len(replay.seen) == 2


# --- UT01-75: Data Center refetch -----------------------------------------------------------


def test_ut01_75_dc_refetch_when_total_exceeds_histories() -> None:
    """UT01-75 Data Center pages by `startAt`; an issue whose search changelog holds 40 of
    `total` 60 histories is refetched with `expand=changelog&fields=id`, the others are
    taken from the search: complete changelogs, ascending, without `author`."""
    replay = Replay(cassette("dc_changelog.json"))
    conn = connector(replay, flavor="datacenter", page_size=50)
    table = _table(conn.sync("issue", SINCE, UNTIL))
    assert not replay.interactions
    assert [(m, p) for m, p, _ in replay.seen] == [
        ("POST", jira_pages.DC_SEARCH),
        ("GET", "/rest/api/2/issue/10001"),
        ("POST", jira_pages.DC_SEARCH),
    ]
    assert replay.queries[1] == {"expand": "changelog", "fields": "id"}
    changelog = _column(table, "changelog")
    assert len(changelog["10001"]) == 60
    assert changelog == {"10001": _expected(1, 60), "10002": _expected(2, 2), "10003": []}
    assert all("author" not in h for hs in changelog.values() for h in hs)


def _dc(count: int, total: object, **extra: Any) -> dict[str, Any]:
    histories = jira_pages.histories(1, count)
    return _issue(1, changelog={"total": total, "histories": histories} | extra)


@pytest.mark.parametrize(
    "issue",
    [
        _issue(1),
        _issue(1, changelog=[]),
        _issue(1, changelog={"total": 1}),
        _dc(1, None),
        _dc(1, "2"),
    ],
)
def test_ut01_75_bad_dc_search_changelog(issue: dict[str, Any]) -> None:
    """UT01-75 a Data Center search issue without a `changelog` object holding a list
    `histories` and an integer `total` is refused (expand=changelog was requested)."""
    with pytest.raises(SchemaViolation, match=r"^bad changelog page$"):
        fetch_changelogs(
            source_http(Replay([])), flavor="datacenter", issues=[issue], state=ChangelogState()
        )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ([], "bad changelog page"),
        ({"id": "10001"}, "bad changelog page"),
        ({"id": "10001", "changelog": {"histories": "x"}}, "bad changelog page"),
        ({"id": "10002", "changelog": {"histories": jira_pages.histories(1, 3)}},
         "bad changelog page"),
        ({"id": "10001", "changelog": {"histories": jira_pages.histories(1, 2)}},
         "incomplete changelog"),
    ],
)  # fmt: skip
def test_ut01_75_bad_dc_refetch(body: object, message: str) -> None:
    """UT01-75 the refetch must be the same issue with at least `total` histories."""
    replay = Replay(gets(body, path="/rest/api/2/issue/10001"))
    replay.interactions[0]["request"]["params"] = {"expand": "changelog", "fields": "id"}
    with pytest.raises(SchemaViolation, match=f"^{message}$") as info:
        fetch_changelogs(
            source_http(replay), flavor="datacenter", issues=[_dc(1, 3)], state=ChangelogState()
        )
    assert info.value.context == {"source": "jira", "issue_id": "10001"}


# --- UT01-76: remote links --------------------------------------------------------------------


def _link(n: int, k: int) -> dict[str, object]:
    raw = jira_pages.remote_link(n, k)
    obj = raw["object"]
    return {"id": str(raw["id"]), "object": {"url": obj["url"], "title": obj["title"]}}


def test_ut01_76_remote_links_one_call_per_issue_projected() -> None:
    """UT01-76 with `fetch_remote_links` one `GET /rest/api/3/issue/{id}/remotelink` per
    issue; each link projected to `{id, object{url, title}}` (application, relationship,
    self and icon dropped; URLs stored, never fetched)."""
    replay = Replay(cassette("cloud_remote_links.json"))
    table = _table(connector(replay, fetch_remote_links=True).sync("issue", SINCE, UNTIL))
    assert not replay.interactions
    link_paths = [p for _, p, _ in replay.seen if p.endswith("/remotelink")]
    assert link_paths == [f"/rest/api/3/issue/1000{n}/remotelink" for n in (1, 2, 3)]
    links = _column(table, "remotelinks")
    assert links == {"10001": [_link(1, 0), _link(1, 1)], "10002": [_link(2, 0)], "10003": []}
    text = table.column("remotelinks")[0].as_py()
    assert all(word not in text for word in ("application", "relationship", "self", "icon"))
    assert text == json.dumps([_link(1, 0), _link(1, 1)], separators=(",", ":"))  # compact
    assert _column(table, "changelog")["10003"] == _expected(3, 2)


def test_ut01_76_remote_links_off_column_null_no_calls() -> None:
    """UT01-76 with `fetch_remote_links` false the column is NULL and no remote-link call
    is made (the cassette has none; an extra request would fail the replay)."""
    replay = Replay(cassette("cloud_changelog_bulk.json"))
    table = _table(connector(replay).sync("issue", SINCE, UNTIL))
    assert not replay.interactions
    assert not [p for _, p, _ in replay.seen if p.endswith("/remotelink")]
    assert table.column("remotelinks").to_pylist() == [None, None, None]


def test_ut01_76_data_center_uses_version_2() -> None:
    """UT01-76 the remote-link path carries the REST version (`2` on Data Center)."""
    raw = [jira_pages.remote_link(1, 0)]
    replay = Replay(gets(raw, path="/rest/api/2/issue/10001/remotelink"))
    got = fetch_remote_links(source_http(replay), version="2", issue_ids=["10001"])
    assert got == {"10001": [_link(1, 0)]}
    assert fetch_remote_links(source_http(Replay([])), version="2", issue_ids=[]) == {}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"links": []}, "bad remote link page"),
        (None, "bad remote link page"),
        (["x"], "bad remote link"),
        ([{"id": True, "object": {}}], "bad remote link"),
    ],
)
def test_ut01_76_bad_remote_link_bodies(body: object, message: str) -> None:
    """UT01-76 the remote-link body must be a list of link objects (else SchemaViolation)."""
    replay = Replay(gets(body, path="/rest/api/3/issue/10001/remotelink"))
    with pytest.raises(SchemaViolation, match=f"^{message}$") as info:
        fetch_remote_links(source_http(replay), version="3", issue_ids=["10001"])
    assert info.value.context["source"] == "jira"


def test_ut01_76_bad_remote_links_fail_closed_before_any_batch() -> None:
    """UT01-76 (TH01-05) a malformed remote-link body raises before any batch is yielded."""
    search, bulk, *_ = cassette("cloud_remote_links.json")
    bad = gets({"not": "a list"}, path="/rest/api/3/issue/10001/remotelink")
    replay = Replay([search, bulk, *bad])
    with pytest.raises(SchemaViolation, match="bad remote link page"):
        next(connector(replay, fetch_remote_links=True).sync("issue", SINCE, UNTIL))
    assert len(replay.seen) == 3
