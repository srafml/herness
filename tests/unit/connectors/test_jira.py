"""Tests for herness.connectors.jira: JQL, Cloud and DC search, key listing and field
discovery (impl 01 U01-71 to U01-75; T01-17; TH01-04, TH01-05).

Pages are replayed from the committed cassettes (``tests.support.jira_pages``) through a
``SourceHttp`` over an ``httpx2.MockTransport`` client. The changelog and remote-link calls
(``jira_changelog.fetch_changelogs`` / ``fetch_remote_links``, T01-18) are stubbed here where
a test is about search paging; ``test_jira_changelog.py`` replays them for real.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from tests.support import jira_pages
from tests.support.egress_mock import MockNet
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.support.sync_env import write_sync_config
from tests.unit.connectors._auth_data import store
from tests.unit.connectors._jira_data import (
    BASE,
    NOW,
    SINCE,
    UNTIL,
    Replay,
    Seams,
    cassette,
    connector,
    pages,
    settings,
)

from herness.connectors import jira_changelog
from herness.connectors.base import KEY_SCHEMA, METADATA_FIELDS
from herness.connectors.jira import (
    JIRA_FIELDS,
    JIRA_ISSUE_COLUMNS,
    FieldCandidate,
    JiraConnector,
    build_jql,
)
from herness.core import config as c
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.resilience import ProcessState

pytestmark = pytest.mark.unit

CUSTOM = list(jira_pages.CUSTOM_FIELD_IDS)
SEARCH_JQL = (
    'updated >= "2026/09/01 10:00" AND updated < "2026/09/02 09:59" AND (project = SYN) '
    "ORDER BY updated ASC, id ASC"
)


class _Spy:
    def __init__(self) -> None:
        self.failures: list[Exception] = []
        self.successes = 0

    def force_open(self, err: Exception) -> None:
        self.failures.append(err)

    def record_failure(self, err: Exception) -> None:
        self.failures.append(err)

    def record_success(self) -> None:
        self.successes += 1


@pytest.fixture(autouse=True)
def _no_breaker(monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState) -> _Spy:
    """`retry_page` guards and records on a spy breaker; sleeps are no-ops (no ops store)."""
    import herness.core.resilience.retry as retry_module  # noqa: PLC0415 - test seam

    reset_process_state.sleep = lambda _s: None
    spy = _Spy()
    monkeypatch.setattr(retry_module, "guard", lambda _key: None)
    monkeypatch.setattr(retry_module, "breaker", lambda _key: spy)
    monkeypatch.setattr("herness.connectors.http.breaker", lambda _key: spy)
    return spy


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> Seams:
    return Seams().install(monkeypatch)


def _rows(batches: Iterator[pa.RecordBatch]) -> pa.Table:
    return pa.Table.from_batches(list(batches))


def _page(issues: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"issues": issues} | extra


# --- UT01-72: JQL text --------------------------------------------------------------------


def test_ut01_72_build_jql_exact_text_minute_utc() -> None:
    """UT01-72 since/until are converted to UTC and floored to the minute; scope in brackets."""
    assert build_jql(since=SINCE, until=UNTIL, scope="project = SYN") == SEARCH_JQL
    assert SEARCH_JQL == jira_pages.SEARCH_JQL


@pytest.mark.parametrize(
    ("since", "until", "scope", "order", "expected"),
    [
        (None, None, None, "time", "ORDER BY updated ASC, id ASC"),
        (SINCE, None, None, "time", 'updated >= "2026/09/01 10:00" ORDER BY updated ASC, id ASC'),
        (None, UNTIL, "a = b", "time",
         'updated < "2026/09/02 09:59" AND (a = b) ORDER BY updated ASC, id ASC'),
        (None, None, "a = b", "time", "(a = b) ORDER BY updated ASC, id ASC"),
        (None, None, "project = SYN", "key", "(project = SYN) ORDER BY id ASC"),
        (None, None, None, "key", "ORDER BY id ASC"),
    ],
)  # fmt: skip
def test_ut01_72_build_jql_absent_clauses_omitted(
    since: datetime.datetime | None,
    until: datetime.datetime | None,
    scope: str | None,
    order: Any,
    expected: str,
) -> None:
    """UT01-72 absent clauses are omitted; `order="key"` gives `(<scope>) ORDER BY id ASC`."""
    assert build_jql(since=since, until=until, scope=scope, order=order) == expected


def test_ut01_72_build_jql_naive_datetime_is_config_error() -> None:
    """UT01-72 a naive bound is refused (precondition: aware)."""
    with pytest.raises(ConfigError, match="aware"):
        build_jql(since=datetime.datetime(2026, 9, 1), until=None, scope=None)  # noqa: DTZ001


# --- UT01-72: Cloud search pages ----------------------------------------------------------


def test_ut01_72_cloud_page_of_37_not_last_continues(seams: Seams) -> None:
    """UT01-72 a page of 37 with `isLast: false` continues with its token, then `isLast:
    true` ends: all 40 rows, exact request bodies (JQL in minutes, UTC), ascending order."""
    replay = Replay(cassette("cloud_search.json"))
    table = _rows(connector(replay).sync("issue", SINCE, UNTIL))
    assert not replay.interactions
    assert [body["jql"] for _, _, body in replay.seen] == [SEARCH_JQL, SEARCH_JQL]
    assert replay.seen[0][2] == {
        "jql": SEARCH_JQL,
        "fields": [*JIRA_FIELDS, *CUSTOM],
        "maxResults": 100,
    }
    assert replay.seen[1][2]["nextPageToken"] == jira_pages.TOKEN_1
    ids = [str(10000 + n) for n in range(1, 41)]
    assert table.column("_source_key").to_pylist() == ids
    assert table.column("id").to_pylist() == ids
    assert table.column("_record_id").to_pylist() == [f"jira:issue:{i}" for i in ids]
    assert table.column_names == [*METADATA_FIELDS, *JIRA_ISSUE_COLUMNS, *CUSTOM]
    updated = table.column("_source_updated_at").to_pylist()
    assert updated == sorted(updated)
    assert updated[0] == datetime.datetime(2026, 9, 1, 10, 6, tzinfo=datetime.UTC)
    assert set(table.column("_fetched_at").to_pylist()) == {NOW}
    payload = json.loads(table.column("_payload")[0].as_py())
    assert payload == jira_pages.issue(1)
    assert table.column("remotelinks").to_pylist() == [None] * 40  # fetch_remote_links off
    assert [flavor for flavor, _ in seams.changelog_calls] == ["cloud", "cloud"]
    assert [len(i) for _, i in seams.changelog_calls] == [37, 3]
    assert seams.states[0] is seams.states[1]  # one ChangelogState per connector instance
    assert seams.link_calls == []
    changelog = json.loads(table.column("changelog")[0].as_py())
    assert [h["id"] for h in changelog] == ["100011", "100012"]  # sorted by created
    assert all("author" not in h for h in changelog)


def test_ut01_72_remote_links_per_page_when_enabled(seams: Seams) -> None:
    """UT01-72 with `fetch_remote_links` the remote links of every issue are projected."""
    replay = Replay(cassette("cloud_search.json"))
    table = _rows(connector(replay, fetch_remote_links=True).sync("issue", SINCE, UNTIL))
    assert [(v, len(i)) for v, i in seams.link_calls] == [("3", 37), ("3", 3)]
    link = '[{"id":"1","object":{"url":"https://wiki.example.test/page","title":"Synthetic page"}}]'
    assert set(table.column("remotelinks").to_pylist()) == {link}


def test_ut01_72_dc_search_pages_by_start_at(seams: Seams) -> None:
    """UT01-72 Data Center: `startAt += len(issues)` until `startAt >= total`, expand
    changelog, version 2 remote links."""
    replay = Replay(cassette("dc_search.json"))
    conn = connector(replay, flavor="datacenter", page_size=50, fetch_remote_links=True)
    table = _rows(conn.sync("issue", SINCE, UNTIL))
    assert not replay.interactions
    assert table.column("_source_key").to_pylist() == ["10001", "10002", "10003"]
    assert [flavor for flavor, _ in seams.changelog_calls] == ["datacenter", "datacenter"]
    assert [v for v, _ in seams.link_calls] == ["2", "2"]


def test_ut01_72_rows_pass_through_bounded_batches(seams: Seams) -> None:
    """UT01-72 rows are emitted through `RowBatcher` in batches of at most `batch_rows`;
    an empty search yields no batch."""
    replay = Replay(cassette("cloud_search.json"))
    conn = connector(replay)
    conn._settings = conn._settings.model_copy(update={"batch_rows": 16})
    sizes = [b.num_rows for b in conn.sync("issue", SINCE, UNTIL)]
    assert sizes == [16, 16, 8]
    assert list(connector(pages(_page([], isLast=True))).sync("issue", None)) == []


def _refuse(*_args: object, **_kwargs: object) -> dict[str, list[dict[str, object]]]:
    msg = "bad changelog page"
    raise SchemaViolation(msg, source="jira")


def _refuse_links(*_args: object, **_kwargs: object) -> dict[str, list[dict[str, object]]]:
    msg = "bad remote link page"
    raise SchemaViolation(msg, source="jira")


def test_ut01_72_changelog_failure_fails_closed_before_any_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT01-72 a failing changelog fetch raises on the first `next()`, before any batch is
    yielded (no lake write, no watermark move; TH01-05)."""
    monkeypatch.setattr(jira_changelog, "fetch_changelogs", _refuse)
    replay = Replay(cassette("cloud_search.json"))
    with pytest.raises(SchemaViolation, match="bad changelog page"):
        next(connector(replay).sync("issue", SINCE, UNTIL))
    assert len(replay.seen) == 1


def test_ut01_72_remote_link_failure_fails_closed(
    seams: Seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-72 a failing remote-link fetch also raises before any batch."""
    monkeypatch.setattr(jira_changelog, "fetch_remote_links", _refuse_links)
    replay = Replay(cassette("cloud_search.json"))
    with pytest.raises(SchemaViolation, match="bad remote link page"):
        next(connector(replay, fetch_remote_links=True).sync("issue", SINCE, UNTIL))
    assert len(seams.changelog_calls) == 1


@pytest.mark.parametrize("missing", ["fetch_changelogs", "fetch_remote_links"])
def test_ut01_72_incomplete_changelog_is_schema_violation(
    seams: Seams, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """UT01-72 a page whose changelog or remote-link result misses an issue is refused."""
    monkeypatch.setattr(jira_changelog, missing, lambda *_a, **_k: {})
    replay = Replay(cassette("cloud_search.json"))
    with pytest.raises(SchemaViolation, match="incomplete changelog or remote links"):
        next(connector(replay, fetch_remote_links=True).sync("issue", SINCE, UNTIL))


def test_ut01_72_watermark_field_entities_and_unknown_entity() -> None:
    """UT01-72 `name`, `entities`, `watermark_field` and the `issue`-only entity rule."""
    conn = connector(Replay([]))
    assert (conn.name, conn.entities, conn.watermark_field("issue")) == (
        "jira",
        ("issue",),
        "updated",
    )
    with pytest.raises(ConfigError, match="unknown jira entity"):
        conn.watermark_field("epic")
    with pytest.raises(ConfigError, match="unknown jira entity"):
        next(conn.sync("epic", None))
    with pytest.raises(ConfigError, match="unknown jira entity"):
        next(conn.list_keys("epic"))


@pytest.mark.parametrize(("flavor", "version"), [("cloud", "3"), ("datacenter", "2")])
def test_ut01_72_check_reads_myself(flavor: str, version: str) -> None:
    """UT01-72 `check()` is one `GET /rest/api/{v}/myself`."""
    path = f"/rest/api/{version}/myself"
    replay = Replay(
        [{"request": {"method": "GET", "path": path}, "response": {"status": 200, "json": {}}}]
    )
    connector(replay, flavor=flavor).check()
    assert replay.seen == [("GET", path, None)]


# --- UT01-73: page shape --------------------------------------------------------------------


def test_ut01_73_not_last_without_token_is_schema_violation(seams: Seams) -> None:
    """UT01-73 `isLast: false` without `nextPageToken` raises before any batch."""
    replay = Replay(cassette("cloud_search_no_token.json"))
    with pytest.raises(SchemaViolation, match="missing nextPageToken"):
        next(connector(replay).sync("issue", SINCE, UNTIL))
    assert seams.changelog_calls == []


@pytest.mark.parametrize(
    ("bodies", "message"),
    [
        ([{"values": []}], "bad search page"),
        ([[]], "bad search page"),
        ([_page([], nextPageToken=7, isLast=False)], "bad search page"),
        ([_page([], nextPageToken="synthetic-t", isLast=False)] * 2, "pagination cursor repeated"),
        ([_page([{"id": "1x", "key": "A-1", "fields": {}}], isLast=True)], "bad issue id"),
    ],
)
def test_ut01_73_bad_cloud_pages(seams: Seams, bodies: list[Any], message: str) -> None:
    """UT01-73 shape violations of Cloud pages are SchemaViolation (TH01-04, TH01-05)."""
    with pytest.raises(SchemaViolation, match=message):
        list(connector(pages(*bodies)).sync("issue", None, None))


def test_ut01_73_issue_without_updated_is_schema_violation(seams: Seams) -> None:
    """UT01-73 an issue without `fields.updated` raises the typed SchemaViolation (not a
    KeyError) before any batch is yielded; the message names the field only (TH01-05)."""
    raw = jira_pages.issue(1)
    del raw["fields"]["updated"]
    batches = connector(pages(_page([raw], isLast=True))).sync("issue", None)
    with pytest.raises(SchemaViolation, match=r"^unparseable timestamp in updated$"):
        next(batches)


def test_ut01_73_absent_is_last_without_token_ends(seams: Seams) -> None:
    """UT01-73 `isLast` absent and no token: the page is the last one."""
    replay = pages(_page([jira_pages.issue(1)]))
    table = _rows(connector(replay).sync("issue", None, None))
    assert table.num_rows == 1
    assert replay.seen[0][2]["jql"] == "(project = SYN) ORDER BY updated ASC, id ASC"


@pytest.mark.parametrize(
    "page",
    [
        {"issues": [], "total": "3", "startAt": 0},
        {"issues": [{"id": "10001"}], "startAt": 0},
        {"issues": None, "total": 1},
    ],
)
def test_ut01_73_bad_dc_pages(page: dict[str, Any]) -> None:
    """UT01-73 a Data Center page without integer `total` or list `issues` is refused."""
    conn = connector(pages(page, path=jira_pages.DC_SEARCH), flavor="datacenter")
    with pytest.raises(SchemaViolation, match="bad search page"):
        list(conn.list_keys("issue"))


def test_ut01_73_dc_empty_page_ends() -> None:
    """UT01-73 Data Center stops on an empty `issues` list even below `total`."""
    page = {"issues": [], "total": 9, "startAt": 0}
    conn = connector(pages(page, path=jira_pages.DC_SEARCH), flavor="datacenter")
    assert list(conn.list_keys("issue")) == []


# --- UT01-77: field discovery ---------------------------------------------------------------


def _counts(db: Path) -> dict[str, int]:
    with sqlite3.connect(db) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {n: conn.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}  # noqa: S608 - table names from sqlite_master


def test_ut01_77_discover_fields_candidates_no_writes(ops_store: OpsStoreHandle) -> None:
    """UT01-77 the field list gives the expected candidates sorted by (key, name); nothing
    is written (ops store rows unchanged, no lake directory)."""
    before = _counts(ops_store.db_path)
    replay = Replay(cassette("fields.json"))
    found = connector(replay).discover_fields()
    assert found == [
        FieldCandidate("customfield_10014", "Epic Link", "any", "jira.epic_link"),
        FieldCandidate("customfield_10101", "Budget", "number", "jira.estimate_cost_usd"),
        FieldCandidate(
            "customfield_10100", "Estimated cost USD", "number", "jira.estimate_cost_usd"
        ),
        FieldCandidate("customfield_10016", "Story Points", "number", "jira.story_points"),
        FieldCandidate("customfield_10026", "Story point estimate", "number", "jira.story_points"),
        FieldCandidate("customfield_10001", "Team", "team", "jira.team"),
    ]
    assert replay.seen == [("GET", "/rest/api/3/field", None)]
    assert _counts(ops_store.db_path) == before
    assert not (ops_store.data_root / "raw").exists()


@pytest.mark.parametrize(
    "body", [{"fields": []}, [{"id": 1, "name": "x"}], ["summary"], [{"id": "a"}]]
)
def test_ut01_77_bad_field_list_is_schema_violation(body: Any) -> None:
    """UT01-77 a field list that is not a list of `{id, name}` objects is refused."""
    path = "/rest/api/2/field"
    replay = Replay(
        [{"request": {"method": "GET", "path": path}, "response": {"status": 200, "json": body}}]
    )
    with pytest.raises(SchemaViolation, match="bad field list"):
        connector(replay, flavor="datacenter").discover_fields()


# --- UT01-78: key listing -------------------------------------------------------------------


def test_ut01_78_cloud_key_listing_all_ids() -> None:
    """UT01-78 Cloud key listing: `fields=["id"]`, key-ordered JQL, all 155 ids as
    `KEY_SCHEMA` batches (one per page)."""
    replay = Replay(cassette("cloud_keys.json"))
    batches = list(connector(replay).list_keys("issue"))
    assert not replay.interactions
    assert all(b.schema == KEY_SCHEMA for b in batches)
    assert [b.num_rows for b in batches] == [100, 50, 5]
    keys = pa.Table.from_batches(batches).column("_source_key").to_pylist()
    assert keys == [str(10000 + n) for n in range(1, 156)]


def test_ut01_78_dc_key_listing_all_ids() -> None:
    """UT01-78 Data Center key listing pages by `startAt` without `expand`: all 120 ids."""
    replay = Replay(cassette("dc_keys.json"))
    conn = connector(replay, flavor="datacenter", page_size=50)
    keys = pa.Table.from_batches(list(conn.list_keys("issue"))).column("_source_key")
    assert keys.to_pylist() == [str(10000 + n) for n in range(1, 121)]
    assert all("expand" not in body for _, _, body in replay.seen)


# --- U01-71 construction (UT01-94 covers the registry row) ---------------------------------


def test_ut01_94_bad_custom_field_id_is_config_error() -> None:
    """UT01-94 a custom field id outside `^customfield_\\d{1,10}$` is a ConfigError."""
    with pytest.raises(ConfigError, match="bad custom field id cf_1"):
        JiraConnector(settings(), custom_field_ids=["cf_1"])


def test_ut01_94_default_http_needs_base_url_and_auth() -> None:
    """UT01-94 building the default fetcher without `base_url`/`auth` is a ConfigError."""
    conn = JiraConnector(settings().model_copy(update={"auth": None}))
    with pytest.raises(ConfigError, match="base_url and auth are required"):
        conn.check()


def test_ut01_94_default_http_is_built_lazily_through_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> None:
    """UT01-94 construction makes no call and resolves no secret; the first call builds the
    egress client with the `api_token` basic auth (synthetic credentials)."""
    del fake_keyring
    sources = f"""\
version: 1
sources:
  jira:
    enabled: true
    flavor: cloud
    base_url: {BASE}
    auth: {{method: api_token, credentials: "secret:jira_token"}}
"""
    cfg = c.init_config("local", config_dir=write_sync_config(tmp_path, sources), env={})
    section = cfg.sources.source("jira")
    assert isinstance(section, type(settings()))
    net = MockNet().install(monkeypatch)
    route = net.route("jira.example.test", "/rest/api/3/myself", "GET", json={})
    conn = JiraConnector(section, clock=lambda: NOW)
    assert route.call_count == 0
    store("jira_token", {"email": "synthetic-user-t0117", "token": "synthetic-token-t0117"})
    conn.check()
    conn.check()
    assert route.call_count == 2
    assert route.calls[0].headers["Authorization"].startswith("Basic ")
    c.reset_config()
