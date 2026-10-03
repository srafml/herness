"""Tests of herness.connectors.servicenow (impl 01 U01-66 to U01-70; UT01-67 to UT01-71; T01-16).

The connector reads the seeded cassettes of ``tests.support.sn_cassettes`` through the real
``SourceHttp`` page fetch on a mock transport; ``retry_page`` guards on a spy breaker.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pyarrow as pa
import pytest
from structlog.testing import capture_logs
from tests.support import sn_cassettes as sc
from tests.support.sn_cassettes import T0, FakeServiceNow, Replay
from tests.unit.connectors import _servicenow_data as d
from tests.unit.connectors._mongo_data import SpyBreaker

import herness.core.resilience.retry as retry_module
from herness.connectors import servicenow as sn
from herness.connectors.base import KEY_SCHEMA, METADATA_SCHEMA, Connector, SupportsKeyListing
from herness.connectors.servicenow import ServiceNowConnector, build_sn_query, merge_by_time
from herness.connectors.settings_base import EntitySettings
from herness.core.errors import AuthError, ConfigError, SchemaViolation
from herness.core.resilience import ProcessState

pytestmark = pytest.mark.unit

UTC = datetime.UTC
H = datetime.timedelta(hours=1)
INCIDENT = sc.TABLE_PATH + "incident"
AUDIT = sc.TABLE_PATH + "sys_audit_delete"
COLUMNS = [f"{f}{s}" for f in ("sys_id", "sys_updated_on", *d.FIELDS) for s in ("", "_display")]


@pytest.fixture(autouse=True)
def spy_breaker(monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState) -> SpyBreaker:
    """`retry_page` guards and records on a spy breaker; sleeps are no-ops."""
    del reset_process_state
    spy = SpyBreaker()
    monkeypatch.setattr(retry_module, "guard", lambda _key: None)
    monkeypatch.setattr(retry_module, "breaker", lambda _key: spy)
    return spy


def _sync(conn: ServiceNowConnector, since: datetime.datetime, until: datetime.datetime) -> list:
    return d.rows(conn.sync("incident", since, until))


def _queries(fake: FakeServiceNow, table: str = "incident") -> list[str]:
    return [d.params(r).get("sysparm_query", "") for r in fake.table_requests(table)]


# --- UT01-67: query strings and windows -------------------------------------------------------


def test_ut01_67_query_floors_sub_seconds_and_orders_clauses() -> None:
    """UT01-67 since/until with sub-second parts are floored to whole seconds in UTC; the
    clauses come in the U01-67 order, absent ones omitted."""
    since = datetime.datetime(2026, 2, 20, 1, 2, 3, 999_999, tzinfo=UTC)
    tz = datetime.timezone(datetime.timedelta(hours=2))
    until = datetime.datetime(2026, 2, 20, 6, 5, 5, 500_000, tzinfo=tz)  # 04:05:05 UTC
    assert build_sn_query(since=since, until=until) == (
        "sys_updated_on>=2026-02-20 01:02:03^sys_updated_on<2026-02-20 04:05:05"
        "^ORDERBYsys_updated_on^ORDERBYsys_id"
    )
    assert build_sn_query(
        since=since, until=until, classes=["cmdb_ci_server", "cmdb_ci_linux"], filter="active=true"
    ) == (
        "sys_updated_on>=2026-02-20 01:02:03^sys_updated_on<2026-02-20 04:05:05"
        "^sys_class_nameINcmdb_ci_server,cmdb_ci_linux^active=true"
        "^ORDERBYsys_updated_on^ORDERBYsys_id"
    )
    assert build_sn_query(since=None, until=None, order="key") == "ORDERBYsys_id"
    assert build_sn_query(since=None, until=None, classes=["a"], filter="x=1", order="key") == (
        "sys_class_nameINa^x=1^ORDERBYsys_id"
    )
    assert build_sn_query(since=since, until=None, classes=[], filter="") == (
        "sys_updated_on>=2026-02-20 01:02:03^ORDERBYsys_updated_on^ORDERBYsys_id"
    )


def test_ut01_67_sixty_hours_in_three_windows() -> None:
    """UT01-67 a 60 h range with `window_hours` 24 is fetched in 3 windows, each with its
    own exact query; every record of the range is read once."""
    fake = FakeServiceNow.from_cassette("incident_table")
    since = T0 + datetime.timedelta(microseconds=250_000)
    out = _sync(d.connector(fake), since, T0 + 60 * H)
    assert _queries(fake) == [
        "sys_updated_on>=2026-02-20 00:00:00^sys_updated_on<2026-02-21 00:00:00"
        "^ORDERBYsys_updated_on^ORDERBYsys_id",
        "sys_updated_on>=2026-02-21 00:00:00^sys_updated_on<2026-02-22 00:00:00"
        "^ORDERBYsys_updated_on^ORDERBYsys_id",
        "sys_updated_on>=2026-02-22 00:00:00^sys_updated_on<2026-02-22 12:00:00"
        "^ORDERBYsys_updated_on^ORDERBYsys_id",
    ]
    live = [r for r in out if not r["_deleted"]]
    assert len(live) == 240
    assert len({r["_source_key"] for r in live}) == 240
    audit = _queries(fake, "sys_audit_delete")
    assert audit[0] == ""  # the probe carries no query
    assert audit[1:] == [
        "tablename=incident^sys_created_on>=2026-02-20 00:00:00"
        "^sys_created_on<2026-02-21 00:00:00^ORDERBYsys_created_on^ORDERBYdocumentkey",
        "tablename=incident^sys_created_on>=2026-02-21 00:00:00"
        "^sys_created_on<2026-02-22 00:00:00^ORDERBYsys_created_on^ORDERBYdocumentkey",
        "tablename=incident^sys_created_on>=2026-02-22 00:00:00"
        "^sys_created_on<2026-02-22 12:00:00^ORDERBYsys_created_on^ORDERBYdocumentkey",
    ]


def test_ut01_67_classes_and_filter_from_settings() -> None:
    """UT01-67 `cmdb_ci`: `sys_class_name` is fetched and the configured classes and filter
    join the query; a missing `since` starts at the backfill start of `until`."""
    entities = {
        "cmdb_ci": {"fields": ["name"], "classes": ["cmdb_ci_server"], "filter": "active=true"}
    }
    cfg = d.settings(entities=entities, backfill={"start": datetime.date(2026, 2, 24)})
    fake = FakeServiceNow({"cmdb_ci": []}, audit_status=403)
    assert d.rows(d.connector(fake, cfg).sync("cmdb_ci", None)) == []
    (first, *rest) = fake.table_requests("cmdb_ci")
    assert d.params(first)["sysparm_fields"] == "sys_id,sys_updated_on,sys_class_name,name"
    assert d.params(first)["sysparm_query"] == (
        "sys_updated_on>=2026-02-24 00:00:00^sys_updated_on<2026-02-25 00:00:00"
        "^sys_class_nameINcmdb_ci_server^active=true^ORDERBYsys_updated_on^ORDERBYsys_id"
    )
    assert rest == []  # backfill start 2026-02-24 to NOW (2026-02-25): one 24 h window


# --- UT01-68: paging -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected", "offsets"),
    [
        ("pages_empty", 0, ["0"]),
        ("pages_one_full_then_empty", 100, ["0", "100"]),
        ("pages_short_last", 137, ["0", "100"]),
        ("pages_link_next", 212, ["0", "100", "200"]),
    ],
)
def test_ut01_68_row_counts_equal_the_cassette(
    name: str, expected: int, offsets: list[str]
) -> None:
    """UT01-68 empty result, one full page then empty, short last page, `Link rel=next`:
    the rows equal the cassette's records, no page is read twice or skipped."""
    replay = Replay.from_cassette(name)
    out = _sync(d.connector(replay), T0, T0 + 24 * H)
    assert replay.done
    data = [r for r in replay.seen if r.url.path == INCIDENT]
    assert [d.params(r)["sysparm_offset"] for r in data] == offsets
    want = [
        rec["sys_id"]["value"]
        for i in replay.interactions
        if i["request"]["path"] == INCIDENT
        for rec in i["response"]["body"]["result"]
    ]
    assert [r["_source_key"] for r in out] == want
    assert len(out) == expected
    first = d.params(data[0])
    assert first == {
        "sysparm_query": build_sn_query(since=T0, until=T0 + 24 * H),
        "sysparm_fields": ",".join(["sys_id", "sys_updated_on", *d.FIELDS]),
        "sysparm_limit": "100",
        "sysparm_offset": "0",
        "sysparm_display_value": "all",
        "sysparm_exclude_reference_link": "true",
        "sysparm_no_count": "true",
    }


def test_ut01_68_next_link_is_followed_without_params() -> None:
    """UT01-68 a same-host `Link rel=next` URL is requested as given (absolute, its own
    query only); offset paging is used when a full page carries no link."""
    replay = Replay.from_cassette("pages_link_next")
    _sync(d.connector(replay), T0, T0 + 24 * H)
    later = [r for r in replay.seen if r.url.path == INCIDENT][1:]
    assert [str(r.url) for r in later] == [
        f"{sc.BASE_URL}{INCIDENT}?sysparm_offset=100&sysparm_limit=100",
        f"{sc.BASE_URL}{INCIDENT}?sysparm_offset=200&sysparm_limit=100",
    ]


def test_ut01_68_batches_have_metadata_then_configured_columns() -> None:
    """UT01-68 every batch starts with the metadata columns, then `<f>`/`<f>_display` of each
    fetched field; `batch_rows` bounds each batch."""
    fake = FakeServiceNow.from_cassette("incident_table", audit_status=403)
    cfg = d.settings().model_copy(update={"batch_rows": 50})
    batches = list(d.connector(fake, cfg).sync("incident", T0, T0 + 24 * H))
    assert [b.num_rows for b in batches] == [50, 46]
    for b in batches:
        assert b.schema.names == [*METADATA_SCHEMA.names, *COLUMNS]
        assert all(b.schema.field(c).type == pa.string() for c in COLUMNS)


def test_ut01_68_repeated_next_link_stops_with_schema_violation() -> None:
    """UT01-68 a next link that repeats a cursor raises SchemaViolation (TH01-04)."""
    link = {"Link": f'<{sc.BASE_URL}{INCIDENT}?sysparm_offset=100>;rel="next"'}
    pages: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == AUDIT:
            return httpx2.Response(403, json={})
        pages.append(len(pages))
        at = f"2026-02-20 00:0{len(pages)}:00"  # ascending pages: only the cursor repeats
        full = {"result": [_record(f"{i:032x}", at) for i in range(100)]}
        return httpx2.Response(200, headers=link, json=full)

    with pytest.raises(SchemaViolation, match="pagination cursor repeated"):
        _sync(d.connector(handler), T0, T0 + H)
    assert len(pages) == 2  # the first page and the link; the repeat is never requested


def test_ut01_68_committed_cassettes_equal_the_generator() -> None:
    """UT01-68 the committed cassettes are byte-identical to the seeded generator output, and
    every value on them is synthetic (host, ids)."""
    for name in sc.CASSETTES:
        text = (sc.CASSETTE_DIR / f"{name}.json").read_text("utf-8")
        assert text == sc.render(name), name
        assert "service-now.com" not in text


def test_ut01_68_write_all_writes_every_cassette(tmp_path: Path) -> None:
    """UT01-68 `write_all` writes one JSON file per cassette."""
    paths = sc.write_all(tmp_path)
    assert sorted(p.name for p in paths) == sorted(f"{n}.json" for n in sc.CASSETTES)
    assert json.loads(paths[0].read_text("utf-8"))["kind"] in {"table", "pages"}


# --- UT01-69: display pairs ------------------------------------------------------------------


def test_ut01_69_reference_field_gives_value_and_display_columns() -> None:
    """UT01-69 a reference field `{value, display_value}` fills `<f>` and `<f>_display`;
    `_payload` keeps the record as compact JSON."""
    fake = FakeServiceNow.from_cassette("incident_table", audit_status=403)
    first = _sync(d.connector(fake), T0, T0 + H)[0]
    rec = next(sc.records_of("incident_table"))
    assert first["assignment_group"] == rec["assignment_group"]["value"]
    assert first["assignment_group_display"].startswith("Synthetic Group ")
    assert first["priority"] == rec["priority"]["value"]
    assert first["priority_display"] == rec["priority"]["display_value"]
    assert first["sys_id"] == first["_source_key"] == rec["sys_id"]["value"]
    assert first["_record_id"] == f"servicenow:incident:{first['_source_key']}"
    assert first["_source_updated_at"] == T0
    payload = json.loads(first["_payload"])
    assert payload["assignment_group"] == rec["assignment_group"]
    assert ", " not in first["_payload"]


def test_ut01_69_missing_field_and_plain_value_are_kept() -> None:
    """UT01-69 a field the record lacks is null; a plain (non-pair) value fills `<f>` only."""
    rec = _record("a" * 32, "2026-02-20 01:00:00")
    rec["number"] = "INC1"
    out = _sync(d.connector(_one_page([rec])), T0, T0 + H)
    assert (out[0]["number"], out[0]["number_display"]) == ("INC1", None)
    assert (out[0]["state"], out[0]["state_display"]) == (None, None)


# --- UT01-70: delete tombstones --------------------------------------------------------------


def test_ut01_70_audit_deletes_merge_ascending_with_records() -> None:
    """UT01-70 audited deletes of the table interleave with the records: the output is
    ascending by (`_source_updated_at`, `_source_key`) with the tombstones in place."""
    fake = FakeServiceNow.from_cassette("incident_table")
    out = _sync(d.connector(fake), T0, T0 + 60 * H)
    order = [(r["_source_updated_at"], r["_source_key"]) for r in out]
    assert order == sorted(order)
    dead = [r for r in out if r["_deleted"]]
    want = [a for a in fake.audit if a["tablename"] == "incident"]
    assert [r["_source_key"] for r in dead] == [a["documentkey"] for a in want]
    assert [r["_source_updated_at"] for r in dead] == [T0 + h * H for h in (1, 25, 25, 49)]
    assert all(r["_payload"] is None and r["number"] is None for r in dead)
    assert len(out) == 244
    (probe, *window_reads) = fake.table_requests("sys_audit_delete")
    assert d.params(probe) == {"sysparm_limit": "1", "sysparm_fields": "sys_id"}
    assert {d.params(r)["sysparm_display_value"] for r in window_reads} == {"false"}
    assert {d.params(r)["sysparm_fields"] for r in window_reads} == {"documentkey,sys_created_on"}


def test_ut01_70_forbidden_audit_table_gives_no_tombstones_and_logs_once() -> None:
    """UT01-70 a 403 probe of `sys_audit_delete`: no tombstones, the probe is never repeated
    and `connectors.servicenow.audit_delete_unreadable` is logged once at INFO."""
    fake = FakeServiceNow.from_cassette("incident_table", audit_status=403)
    conn = d.connector(fake)
    with capture_logs() as logs:
        out = _sync(conn, T0, T0 + 60 * H)
        out += _sync(conn, T0, T0 + 24 * H)
    assert not any(r["_deleted"] for r in out)
    assert len(fake.table_requests("sys_audit_delete")) == 1
    events = [e for e in logs if e["event"] == "connectors.servicenow.audit_delete_unreadable"]
    assert events == [
        {
            "event": "connectors.servicenow.audit_delete_unreadable",
            "log_level": "info",
            "component": "connectors.servicenow",
            "source": "servicenow",
        }
    ]


def test_ut01_70_too_many_deletes_in_a_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-70 more deletes in one window than the limit raises SchemaViolation."""
    monkeypatch.setattr(sn, "_MAX_DELETES", 1)
    fake = FakeServiceNow.from_cassette("incident_table")
    with pytest.raises(SchemaViolation, match="too many deletes in window"):
        _sync(d.connector(fake), T0 + 24 * H, T0 + 48 * H)


def test_ut01_70_delete_without_documentkey() -> None:
    """UT01-70 an audit row without `documentkey` raises SchemaViolation."""
    fake = FakeServiceNow({"incident": []}, [{"tablename": "incident", "sys_created_on": "x"}])
    fake.audit[0]["sys_created_on"] = "2026-02-20 00:30:00"
    with pytest.raises(SchemaViolation, match="missing documentkey"):
        _sync(d.connector(fake), T0, T0 + H)


def _items(*pairs: tuple[int, str]) -> Iterator[tuple[datetime.datetime, str, str]]:
    return iter([(T0 + s * H, k, f"rec-{k}") for s, k in pairs])


def test_ut01_70_merge_by_time_order_and_ties() -> None:
    """UT01-70 `merge_by_time`: ascending by (ts, key); on an equal (ts, key) the record
    comes first; deletes after the last record are kept."""
    deletes = [(T0, "a"), (T0 + H, "b"), (T0 + H, "c"), (T0 + 3 * H, "z")]
    merged = list(merge_by_time(_items((0, "b"), (1, "b"), (2, "a")), deletes))
    assert merged == [
        (T0, "a", None),
        (T0, "b", "rec-b"),
        (T0 + H, "b", "rec-b"),
        (T0 + H, "b", None),
        (T0 + H, "c", None),
        (T0 + 2 * H, "a", "rec-a"),
        (T0 + 3 * H, "z", None),
    ]
    assert list(merge_by_time(_items(), [])) == []
    assert list(merge_by_time(_items((0, "a")), [])) == [(T0, "a", "rec-a")]


@pytest.mark.parametrize(
    ("records", "deletes"),
    [
        ([(1, "a"), (0, "b")], []),
        ([(0, "b"), (0, "a")], []),
        ([], [(T0 + H, "a"), (T0, "a")]),
    ],
)
def test_ut01_70_merge_by_time_rejects_unordered_input(
    records: list[tuple[int, str]], deletes: list[tuple[datetime.datetime, str]]
) -> None:
    """UT01-70 records or deletes out of order raise SchemaViolation("source order violated")."""
    with pytest.raises(SchemaViolation, match="source order violated"):
        list(merge_by_time(_items(*records), deletes))


# --- UT01-71: key listing --------------------------------------------------------------------


def test_ut01_71_two_pages_of_ids_in_key_schema_batches() -> None:
    """UT01-71 `list_keys` over 2 pages of ids: `KEY_SCHEMA` batches of `batch_rows`, the
    key-only query (`sysparm_fields=sys_id`, display values off, ordered by `sys_id`)."""
    fake = FakeServiceNow.from_cassette("incident_table")
    fake.tables["incident"] = fake.tables["incident"][:150]
    cfg = d.settings().model_copy(update={"batch_rows": 120})
    conn = d.connector(fake, cfg)
    assert isinstance(conn, SupportsKeyListing)
    batches = list(conn.list_keys("incident"))
    assert [b.num_rows for b in batches] == [120, 30]
    assert all(b.schema == KEY_SCHEMA for b in batches)
    keys = [k for b in batches for k in b.column(0).to_pylist()]
    assert keys == sorted(r["sys_id"]["value"] for r in fake.tables["incident"])
    reads = fake.table_requests("incident")
    assert [d.params(r)["sysparm_offset"] for r in reads] == ["0", "100"]
    assert {d.params(r)["sysparm_fields"] for r in reads} == {"sys_id"}
    assert {d.params(r)["sysparm_display_value"] for r in reads} == {"false"}
    assert {d.params(r)["sysparm_query"] for r in reads} == {"ORDERBYsys_id"}
    assert fake.table_requests("sys_audit_delete") == []


def test_ut01_71_empty_listing_and_bad_key() -> None:
    """UT01-71 an empty table yields no batch; a record without `sys_id` raises."""
    assert list(d.connector(FakeServiceNow({"incident": []})).list_keys("incident")) == []
    with pytest.raises(SchemaViolation, match="missing sys_id"):
        list(d.connector(_one_page([{"sys_id": ""}])).list_keys("incident"))


# --- U01-66 members --------------------------------------------------------------------------


def test_ut01_67_connector_members_and_check() -> None:
    """UT01-67 (U01-66) name, entities and watermark field; `check` is one GET of the first
    entity with `sysparm_limit=1` and `sysparm_fields=sys_id`; errors propagate."""
    fake = FakeServiceNow.from_cassette("incident_table")
    conn: Connector = d.connector(fake)
    assert (conn.name, conn.entities) == ("servicenow", ("incident",))
    assert conn.watermark_field("incident") == "sys_updated_on"
    conn.check()
    (req,) = fake.requests
    assert (req.url.path, d.params(req)) == (
        INCIDENT,
        {"sysparm_limit": "1", "sysparm_fields": "sys_id"},
    )
    fake.fail[INCIDENT] = 401
    with pytest.raises(AuthError):
        conn.check()


def test_ut01_67_unknown_entity_and_missing_connection_settings() -> None:
    """UT01-67 (U01-66) an unknown or non-ServiceNow entity → ConfigError; a section without
    `auth` cannot build its HTTP client (no network call is made)."""
    conn = d.connector(FakeServiceNow())
    with pytest.raises(ConfigError, match="unknown entity"):
        list(conn.sync("problem", T0, T0 + H))
    cfg = d.settings()
    odd = cfg.model_copy(update={"entities": {"incident": EntitySettings()}})
    with pytest.raises(ConfigError, match="not a ServiceNow entity"):
        list(ServiceNowConnector(odd).list_keys("incident"))
    bare = ServiceNowConnector(cfg.model_copy(update={"auth": None}))
    with pytest.raises(ConfigError, match="needs base_url and auth"):
        bare.check()


# --- shape errors (TH01-05) ------------------------------------------------------------------


def _record(key: str, updated: str) -> dict[str, Any]:
    return {"sys_id": sc.pair(key), "sys_updated_on": sc.pair(updated)}


def _one_page(result: object) -> d.Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == AUDIT:
            return httpx2.Response(403, json={})
        return httpx2.Response(200, json={"result": result})

    return handler


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"sys_id": "a"}, "result is not a list"),
        (["not-an-object"], "unexpected record shape"),
        ([{"sys_updated_on": sc.pair("2026-02-20 01:00:00")}], "missing sys_id"),
        ([{"sys_id": sc.pair("a")}], "unparseable timestamp in sys_updated_on"),
        ([_record("a", "20/02/2026")], "unparseable timestamp in sys_updated_on"),
    ],
)
def test_ut01_68_shape_errors_raise_schema_violation(result: object, message: str) -> None:
    """UT01-68 `result` not a list, a non-object record, no `sys_id`, no or a bad
    `sys_updated_on`: SchemaViolation naming the problem, never the value."""
    with pytest.raises(SchemaViolation, match=message) as caught:
        _sync(d.connector(_one_page(result)), T0, T0 + H)
    assert "20/02/2026" not in str(caught.value)


def test_ut01_68_body_not_an_object() -> None:
    """UT01-68 a JSON array body raises SchemaViolation."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        status = 403 if request.url.path == AUDIT else 200
        return httpx2.Response(status, json=[])

    with pytest.raises(SchemaViolation, match="result is not a list"):
        _sync(d.connector(handler), T0, T0 + H)
