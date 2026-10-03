"""Tests for herness.connectors.dataverse.DataverseConnector (impl 01 U01-90, U01-91; T01-24;
TH01-02, TH01-16).

Pages come from an ``httpx2.MockTransport`` client (respx patches only ``httpx``) wrapped in
the real ``SourceHttp``; ``retry_page`` runs for real with a spy breaker and no-op sleeps.
The default HTTP layer is exercised over ``MockNet`` with a faked MSAL app.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx2
import pyarrow as pa
import pytest
from tests.support.config_tree import write_full_config
from tests.support.egress_mock import MockNet
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.connectors._auth_data import ACCESS, FakeMsalApp, MsalFactory, msal_token, store
from tests.unit.connectors._dataverse_data import (
    BASE,
    ENTITYSET_PATH,
    PREFER,
    ROW_TEXT,
    TENANT,
    Server,
    connector,
    next_link,
    page,
    row,
    settings,
)
from tests.unit.connectors._http_data import SYNTHETIC_TOKEN
from tests.unit.connectors._mongo_data import SpyBreaker

import herness.core.resilience.retry as retry_module
from herness.connectors import auth as auth_module
from herness.connectors.base import KEY_SCHEMA, METADATA_SCHEMA, Connector, SupportsKeyListing
from herness.connectors.dataverse import DataverseConnector
from herness.connectors.http import ForeignHostError
from herness.core import config as c
from herness.core.errors import ConfigError, HernessError, RateLimited, SchemaViolation
from herness.core.resilience import ProcessState

pytestmark = pytest.mark.unit

UTC = datetime.UTC
SINCE = datetime.datetime(2026, 8, 1, 12, 0, 5, 900_000, tzinfo=datetime.UTC)
UNTIL = datetime.datetime(2026, 8, 2, 2, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
SELECT = "cr123_projectid,modifiedon,cr123_name,statuscode"
ORDERBY = "modifiedon asc,cr123_projectid asc"
FILTER = "modifiedon ge 2026-08-01T12:00:05Z and modifiedon lt 2026-08-02T00:00:00Z"
COLUMNS = [
    "cr123_projectid",
    "cr123_projectid_display",
    "modifiedon",
    "modifiedon_display",
    "cr123_name",
    "cr123_name_display",
    "statuscode",
    "statuscode_display",
]


@pytest.fixture(autouse=True)
def spy(monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState) -> SpyBreaker:
    """`retry_page` guards and records on a spy breaker; sleeps are no-ops."""
    del reset_process_state
    breaker = SpyBreaker()
    monkeypatch.setattr(retry_module, "guard", lambda _key: None)
    monkeypatch.setattr(retry_module, "breaker", lambda _key: breaker)
    return breaker


def _params(request: httpx2.Request) -> dict[str, str]:
    return dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))


def _shown(err: HernessError) -> str:
    """Everything an error exposes: message, context, hint and details."""
    return repr((err.message, dict(err.context), err.hint, dict(err.details), str(err)))


def _rows(batches: list[pa.RecordBatch]) -> list[dict[str, Any]]:
    return [r for batch in batches for r in batch.to_pylist()]


def _three_pages() -> Server:
    return Server.of(
        page([row(1), row(2)], next_link(2)),
        page([row(3, minute=1)], next_link(3)),
        page([row(4, minute=2)]),
    )


# --- UT01-89 sync ----------------------------------------------------------------------------


def test_ut01_89_prefer_on_every_page_and_next_link_unmodified() -> None:
    """UT01-89 3 pages with `@odata.nextLink`: `Prefer` (page size and FormattedValue
    annotations) and the OData version headers are on every page; each next link is
    requested unmodified with no added parameters; never `$skip` or `$top`."""
    server = _three_pages()
    list(connector(server).sync("project", SINCE, UNTIL))
    assert len(server.requests) == 3
    for request in server.requests:
        assert request.method == "GET"
        assert request.headers["Prefer"] == PREFER
        assert request.headers["OData-MaxVersion"] == "4.0"
        assert request.headers["OData-Version"] == "4.0"
        assert request.headers["Authorization"] == f"Bearer {SYNTHETIC_TOKEN}"
        assert "$skip=" not in str(request.url)
        assert "$top" not in str(request.url)
    first, second, third = server.requests
    assert str(first.url).startswith(f"{BASE}{ENTITYSET_PATH}?")
    assert _params(first) == {"$select": SELECT, "$filter": FILTER, "$orderby": ORDERBY}
    assert str(second.url) == next_link(2)
    assert str(third.url) == next_link(3)


def test_ut01_89_display_columns_payload_and_order() -> None:
    """UT01-89 each selected field `f` gives `f` and `f_display` (the FormattedValue
    annotation, null when absent) after the metadata columns; `_payload` is the row as
    JSON; rows keep the source order (`modifiedon`, key ascending)."""
    batches = list(connector(_three_pages()).sync("project", SINCE, UNTIL))
    assert len(batches) == 1
    assert batches[0].schema.names[:8] == METADATA_SCHEMA.names
    assert batches[0].schema.names[8:] == COLUMNS
    rows = _rows(batches)
    assert [r["_source_key"] for r in rows] == [row(n)["cr123_projectid"] for n in (1, 2, 3, 4)]
    first = rows[0]
    assert first["_record_id"] == f"dataverse:project:{row(1)['cr123_projectid']}"
    assert first["_source"] == "dataverse"
    assert first["_entity"] == "project"
    assert first["_source_updated_at"] == datetime.datetime(2026, 8, 1, 10, 0, 1, tzinfo=UTC)
    assert first["_fetched_at"] == datetime.datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert first["_deleted"] is False
    assert json.loads(first["_payload"]) == row(1)
    assert first["statuscode"] == "1"
    assert first["statuscode_display"] == "Active"
    assert first["modifiedon_display"] == "8/1/2026 10:00 AM"
    assert first["cr123_name"] == "Project 1"
    assert first["cr123_name_display"] is None
    assert first["cr123_projectid_display"] is None


def test_ut01_89_batches_bounded_by_batch_rows() -> None:
    """UT01-89 1,200 rows over three pages with `batch_rows` 1,000 give batches of 1,000 and
    200 rows."""
    rows = [row(n, minute=n // 60 % 60) for n in range(1200)]
    server = Server.of(
        page(rows[:500], next_link(2)), page(rows[500:1000], next_link(3)), page(rows[1000:])
    )
    batches = list(connector(server, batch_rows=1000).sync("project", None))
    assert [b.num_rows for b in batches] == [1000, 200]


@pytest.mark.parametrize(
    ("since", "until", "expected"),
    [
        (None, None, None),
        (SINCE, None, "modifiedon ge 2026-08-01T12:00:05Z"),
        (None, UNTIL, "modifiedon lt 2026-08-02T00:00:00Z"),
    ],
)
def test_ut01_89_filter_bounds(
    since: datetime.datetime | None, until: datetime.datetime | None, expected: str | None
) -> None:
    """UT01-89 `$filter` has only the given bounds (UTC, floored to seconds); none → no
    `$filter`; `$select` and `$orderby` are always sent."""
    server = Server.of(page([]))
    assert list(connector(server).sync("project", since, until)) == []
    params = _params(server.requests[0])
    assert params.get("$filter") == expected
    assert params["$select"] == SELECT
    assert params["$orderby"] == ORDERBY


def test_ut01_89_select_deduplicated_key_and_updated_first() -> None:
    """UT01-89 `$select` is key, updated, then the configured fields without repeats."""
    server = Server.of(page([]))
    entity = {
        "entityset": "cr123_projects",
        "key_field": "cr123_projectid",
        "updated_field": "cr123_changed",
        "select": ["cr123_changed", "cr123_projectid", "cr123_name"],
    }
    list(connector(server, entities={"project": entity}).sync("project", None))
    assert _params(server.requests[0])["$select"] == "cr123_projectid,cr123_changed,cr123_name"
    assert _params(server.requests[0])["$orderby"] == "cr123_changed asc,cr123_projectid asc"


def test_ut01_89_foreign_next_link_is_refused() -> None:
    """UT01-89 a next link on another host raises ForeignHostError (TH01-02); no request is
    sent to it."""
    server = Server.of(page([row(1)], "https://evil.example/api/data/v9.2/cr123_projects"))
    with pytest.raises(ForeignHostError):
        list(connector(server).sync("project", None))
    assert [r.url.host for r in server.requests] == ["org.example.com"]


def test_ut01_89_repeated_next_link_stops() -> None:
    """UT01-89 the same next link twice is a repeated cursor: SchemaViolation."""
    server = Server.of(page([row(1)], next_link(2)), page([row(2)], next_link(2)))
    with pytest.raises(SchemaViolation, match="pagination cursor repeated"):
        list(connector(server).sync("project", None))
    assert len(server.requests) == 2


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"value": {"a": 1}},
        {"no_value": []},
        {"value": ["not an object"]},
        {"value": [], "@odata.nextLink": 7},
    ],
)
def test_ut01_89_bad_response_shape(body: Any) -> None:
    """UT01-89 a body that is not an object with a list of objects `value`, or a next link
    that is not a string, raises SchemaViolation."""
    with pytest.raises(SchemaViolation, match="bad dataverse"):
        list(connector(Server.of(body)).sync("project", None))


@pytest.mark.parametrize("key", [None, "", 42])
def test_ut01_89_bad_key_is_schema_violation(key: Any) -> None:
    """UT01-89 a missing, empty or non-string key raises SchemaViolation without the row."""
    bad = row(1) | {"cr123_projectid": key, "cr123_name": ROW_TEXT}
    if key is None:
        del bad["cr123_projectid"]
    with pytest.raises(SchemaViolation, match="missing or empty key field") as info:
        list(connector(Server.of(page([bad]))).sync("project", None))
    assert ROW_TEXT not in _shown(info.value)


def test_ut01_89_bad_timestamp_is_schema_violation() -> None:
    """UT01-89 an unparseable `modifiedon` raises SchemaViolation naming the field only."""
    bad = row(1) | {"modifiedon": ROW_TEXT}
    with pytest.raises(SchemaViolation, match="unparseable timestamp in modifiedon") as info:
        list(connector(Server.of(page([bad]))).sync("project", None))
    assert ROW_TEXT not in _shown(info.value)


def test_ut01_89_rate_limited_carries_retry_after(spy: SpyBreaker) -> None:
    """UT01-89 HTTP 429 with `Retry-After: 7` surfaces as RateLimited(retry_after=7) after
    the page retries; no URL query or host is in the message."""
    server = Server([(429, {}, {"Retry-After": "7"})])
    with pytest.raises(RateLimited) as info:
        list(connector(server).sync("project", SINCE))
    assert info.value.retry_after == 7
    text = _shown(info.value)
    assert "org.example.com" not in text
    assert "$filter" not in text
    assert len(server.requests) > 1
    assert all(isinstance(err, RateLimited) for err in spy.failures)


def test_ut01_89_naive_bound_and_duplicate_display_column_are_config_errors() -> None:
    """UT01-89 a naive `since` and a select that collides with a `_display` column raise
    ConfigError before any request."""
    server = Server.of(page([]))
    naive = datetime.datetime(2026, 8, 1)  # noqa: DTZ001 - the rejected input
    with pytest.raises(ConfigError, match="timezone-aware"):
        list(connector(server).sync("project", naive))
    entity = {
        "entityset": "cr123_projects",
        "key_field": "cr123_projectid",
        "select": ["statuscode", "statuscode_display"],
    }
    with pytest.raises(ConfigError, match="duplicate _display column"):
        list(connector(server, entities={"project": entity}).sync("project", None))
    assert server.requests == []


# --- UT01-91 list_keys -----------------------------------------------------------------------


def test_ut01_91_list_keys_all_pages_select_key_only() -> None:
    """UT01-91 key listing pages: every key as `KEY_SCHEMA` batches (an empty page yields
    none); the first request has `$select=<key>` only (no `$filter`/`$orderby`); next links
    are followed unmodified with `Prefer` on every page."""
    keys = [{"cr123_projectid": row(n)["cr123_projectid"]} for n in range(5)]
    server = Server.of(
        page(keys[:2], next_link(2)), page([], next_link(3)), page(keys[2:], next_link(4)), page([])
    )
    batches = list(connector(server).list_keys("project"))
    assert all(batch.schema == KEY_SCHEMA for batch in batches)
    assert [b.num_rows for b in batches] == [2, 3]
    listed = [r["_source_key"] for r in _rows(batches)]
    assert listed == [k["cr123_projectid"] for k in keys]
    assert _params(server.requests[0]) == {"$select": "cr123_projectid"}
    assert [str(r.url) for r in server.requests[1:]] == [next_link(n) for n in (2, 3, 4)]
    assert all(r.headers["Prefer"] == PREFER for r in server.requests)


def test_ut01_91_list_keys_raises_never_ends_early() -> None:
    """UT01-91 a bad key or a failing later page raises instead of ending the listing."""
    bad = Server.of(page([{"cr123_projectid": ""}]))
    with pytest.raises(SchemaViolation, match="missing or empty key field"):
        list(connector(bad).list_keys("project"))
    failing = Server([(200, page([{"cr123_projectid": "k1"}], next_link(2)), {}), (403, {}, {})])
    listing = connector(failing).list_keys("project")
    assert next(listing).num_rows == 1
    with pytest.raises(HernessError, match="refused the credentials"):
        next(listing)


# --- UT01-90 members and the default HTTP layer ----------------------------------------------


def test_ut01_90_members_and_protocols() -> None:
    """UT01-90 `name`, configured `entities`, `watermark_field`, both protocols; an unknown
    entity raises ConfigError."""
    conn = connector(Server.of(page([])))
    assert conn.name == "dataverse"
    assert conn.entities == ("project",)
    assert conn.watermark_field("project") == "modifiedon"
    assert isinstance(conn, SupportsKeyListing)
    typed: Connector = conn
    assert typed is conn
    with pytest.raises(ConfigError, match="unknown dataverse entity"):
        conn.watermark_field("nope")
    with pytest.raises(ConfigError, match="unknown dataverse entity"):
        list(conn.list_keys("nope"))


def test_ut01_90_check_reads_who_am_i() -> None:
    """UT01-90 `check()` is one `GET /api/data/v9.2/WhoAmI`."""
    server = Server.of({"UserId": "00000000-0000-4000-8000-000000000001"})
    connector(server).check()
    assert [(r.method, r.url.path) for r in server.requests] == [("GET", "/api/data/v9.2/WhoAmI")]
    assert server.requests[0].headers["OData-Version"] == "4.0"


SOURCES_YAML = f"""\
version: 1
sources:
  dataverse:
    enabled: true
    base_url: {BASE}
    hosts: [login.microsoftonline.com]
    auth:
      method: msal_client_credentials
      tenant_id: "{TENANT}"
      credentials: "secret:dataverse_app"
    entities:
      project: {{entityset: cr123_projects, key_field: cr123_projectid, select: [cr123_name]}}
"""


@pytest.fixture
def live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> Iterator[tuple[MockNet, MsalFactory]]:
    """A loaded config with the Dataverse section, `MockNet` below the egress client, a
    faked MSAL app and the app secret in the in-memory keyring."""
    del fake_keyring
    cfg_dir = write_full_config(tmp_path)
    (cfg_dir / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    c.init_config("hybrid", config_dir=cfg_dir, env={})
    secret = "synthetic-client-secret-t0124"  # noqa: S105  # pragma: allowlist secret
    store("dataverse_app", {"client_id": "synthetic-client-id-t0124", "client_secret": secret})
    factory = MsalFactory(FakeMsalApp([msal_token(1)]))
    monkeypatch.setattr(auth_module, "_msal_app", factory)
    yield MockNet().install(monkeypatch), factory
    c.reset_config()


def test_ut01_90_default_http_needs_base_url_and_auth() -> None:
    """UT01-90 a section without `auth` (bypassing validation) raises ConfigError on first
    use instead of building a client."""
    section = settings().model_copy(update={"auth": None})
    with pytest.raises(ConfigError, match="base_url and auth are required"):
        DataverseConnector(section).check()


def test_ut01_90_default_http_uses_msal_scope_and_bearer(
    live: tuple[MockNet, MsalFactory],
) -> None:
    """UT01-90 without `http`, the connector builds nothing until first use, then the egress
    client with an MSAL token for scope `f"{base_url}/.default"` sent as a bearer (TH01-16)."""
    net, factory = live
    who = net.route("org.example.com", "/api/data/v9.2/WhoAmI", json={"UserId": "u"})
    section = c.get_config().sources.source("dataverse")
    assert isinstance(section, type(settings()))
    conn = DataverseConnector(section)
    assert factory.built == []
    assert who.call_count == 0
    conn.check()
    conn.check()
    assert factory.app.calls == [[f"{BASE}/.default"]]
    assert factory.built[0]["authority"] == f"https://login.microsoftonline.com/{TENANT}"
    assert [r.headers["Authorization"] for r in who.calls] == [f"Bearer {ACCESS}1"] * 2
