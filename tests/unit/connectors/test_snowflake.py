"""Tests for herness.connectors.snowflake.SnowflakeConnector (impl 01 U01-88, U01-89; T01-23).

`tests.support.fake_snowflake` stands in for the server through the `connect` parameter; the
key-pair credential comes from the in-memory keyring. `retry_page` runs for real with a spy
breaker and no-op sleeps.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

import pyarrow as pa
import pytest
from snowflake.connector import errors as sfe
from structlog.testing import capture_logs
from tests.support.fake_snowflake import FakeSnowflake
from tests.unit.connectors._mongo_data import SpyBreaker
from tests.unit.connectors._snowflake_data import (
    GB,
    SELECT_SQL,
    T0,
    UNLOCK,
    USER,
    connector,
    der,
    key_table,
    naive,
    pem,
    store_credential,
    table,
)

import herness.connectors.snowflake as sf_module
from herness.connectors.base import KEY_SCHEMA, METADATA_SCHEMA, Connector, SupportsKeyListing
from herness.connectors.snowflake import SnowflakeConnector, _map_sf_error
from herness.core.errors import (
    AuthError,
    ConfigError,
    RateLimited,
    SchemaViolation,
    SourceUnavailable,
)

pytestmark = pytest.mark.unit

UTC = datetime.UTC
SINCE = datetime.datetime(2026, 1, 1, 2, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
UNTIL = datetime.datetime(2026, 2, 1, tzinfo=UTC)
PARAMS = {"since": naive(2026, 1, 1), "until": naive(2026, 2, 1)}


def _rows(batches: list[pa.RecordBatch]) -> list[dict[str, Any]]:
    return [row for batch in batches for row in batch.to_pylist()]


def _server(**extra: Any) -> FakeSnowflake:
    return FakeSnowflake(tables=[table(0, 1500), table(1500, 700), table(2200, 2300)], **extra)


# --- UT01-86 sync and list_keys --------------------------------------------------------------


def test_ut01_86_sync_sql_bound_params_query_tag_and_batches(snowflake_env: SpyBreaker) -> None:
    """UT01-86 fake cursor with 3 Arrow tables: the SQL has quoted upper-case identifiers and
    bound `since`/`until` (naive UTC); `QUERY_TAG` is set first; the EXPLAIN guard runs the
    same SQL and parameters before the SELECT; batches ≤ `batch_rows`; Arrow types kept."""
    del snowflake_env
    server = _server()
    conn = connector(server, batch_rows=1000)
    batches = list(conn.sync("cost_center", SINCE, UNTIL))
    assert server.calls == [
        ("ALTER SESSION SET QUERY_TAG = %(tag)s", {"tag": "herness:cost_center"}),
        ("EXPLAIN USING JSON " + SELECT_SQL, PARAMS),
        (SELECT_SQL, PARAMS),
    ]
    assert [b.num_rows for b in batches] == [1000, 500, 700, 1000, 1000, 300]
    for batch in batches:
        assert batch.schema.names[:8] == METADATA_SCHEMA.names
        assert batch.schema.names[8:] == ["cc_id", "name", "amount", "updated_at"]
        for name in METADATA_SCHEMA.names:
            assert batch.schema.field(name).type == METADATA_SCHEMA.field(name).type
        assert batch.schema.field("cc_id").type == pa.int64()
        assert batch.schema.field("amount").type == pa.decimal128(12, 2)
        assert batch.schema.field("updated_at").type == pa.timestamp("ns")
    rows = _rows(batches)
    assert [r["cc_id"] for r in rows] == list(range(4500))
    first = rows[1]
    assert first["_record_id"] == "snowflake:cost_center:1"
    assert first["_source"] == "snowflake"
    assert first["_entity"] == "cost_center"
    assert first["_source_key"] == "1"
    assert first["_source_updated_at"] == datetime.datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    assert first["_fetched_at"] == datetime.datetime(2026, 3, 1, tzinfo=UTC)
    assert first["_deleted"] is False
    payload = json.loads(first["_payload"])
    assert payload == {
        "CC_ID": 1,
        "NAME": "cc1",
        "AMOUNT": "1.25",
        "UPDATED_AT": "2026-01-01 00:00:01",
    }


def test_ut01_86_open_bounds_filter_and_lower_case_identifiers(
    snowflake_env: SpyBreaker,
) -> None:
    """UT01-86 `since` None drops the lower bound, `until` None the upper one; the entity
    filter is appended in parentheses; configured names are upper-cased and quoted."""
    del snowflake_env
    server = FakeSnowflake(tables=[table(0, 3)])
    entity = {
        "table": "finance.public.cost_center",
        "key_field": "cc_id",
        "updated_field": "updated_at",
        "columns": ["cc_id", "name", "amount", "updated_at"],
        "filter": "NAME <> 'x'",
    }
    conn = connector(server, entity)
    rows = _rows(list(conn.sync("cost_center", None, UNTIL)))
    assert len(rows) == 3
    sql, params = server.statements("SELECT")[0], server.calls[-1][1]
    assert sql == (
        'SELECT "CC_ID", "NAME", "AMOUNT", "UPDATED_AT" FROM "FINANCE"."PUBLIC"."COST_CENTER"'
        """ WHERE "UPDATED_AT" < %(until)s AND (NAME <> 'x') ORDER BY "UPDATED_AT", "CC_ID\""""
    )
    assert params == {"until": naive(2026, 2, 1)}
    list(conn.sync("cost_center", SINCE))
    assert server.statements("SELECT")[-1] == (
        'SELECT "CC_ID", "NAME", "AMOUNT", "UPDATED_AT" FROM "FINANCE"."PUBLIC"."COST_CENTER"'
        """ WHERE "UPDATED_AT" >= %(since)s AND (NAME <> 'x') ORDER BY "UPDATED_AT", "CC_ID\""""
    )
    assert server.calls[-1][1] == {"since": naive(2026, 1, 1)}
    list(conn.sync("cost_center", None))
    assert server.statements("SELECT")[-1] == (
        'SELECT "CC_ID", "NAME", "AMOUNT", "UPDATED_AT" FROM "FINANCE"."PUBLIC"."COST_CENTER"'
        """ WHERE (NAME <> 'x') ORDER BY "UPDATED_AT", "CC_ID\""""
    )
    assert server.calls[-1][1] == {}


def test_ut01_86_no_rows_yields_nothing_and_connects_once_lazily(
    snowflake_env: SpyBreaker,
) -> None:
    """UT01-86 no Arrow table → no batch; `connect` runs on first use only, once."""
    del snowflake_env
    server = FakeSnowflake()
    conn = connector(server)
    assert server.connects == []
    assert list(conn.sync("cost_center", None)) == []
    assert list(conn.sync("cost_center", None)) == []
    assert len(server.connects) == 1


def test_ut01_86_connect_arguments(snowflake_env: SpyBreaker) -> None:
    """UT01-86 `connect` gets account, user, DER PKCS#8 key, warehouse, role, the session
    parameters, both timeouts, application and `ocsp_fail_open`."""
    del snowflake_env
    server = FakeSnowflake()
    list(connector(server, timeout_s=45, statement_timeout_s=600).sync("cost_center", None))
    (kwargs,) = server.connects
    assert kwargs.pop("private_key") == der()
    assert kwargs == {
        "account": "Acme-XY12345",
        "user": USER,
        "warehouse": "HERNESS_XS",
        "role": "HERNESS_READER",
        "session_parameters": {"STATEMENT_TIMEOUT_IN_SECONDS": 600, "TIMEZONE": "UTC"},
        "login_timeout": 45.0,
        "network_timeout": 45.0,
        "application": "herness",
        "ocsp_fail_open": True,
    }


def test_ut01_86_encrypted_key_with_passphrase(snowflake_env: SpyBreaker) -> None:
    """UT01-86 an encrypted PEM is opened with the credential's `passphrase`."""
    del snowflake_env
    store_credential(private_key=pem(UNLOCK), passphrase=UNLOCK)
    server = FakeSnowflake()
    list(connector(server).sync("cost_center", None))
    assert server.connects[0]["private_key"] == der()


def test_ut01_86_retried_select_reruns_the_same_statement(snowflake_env: SpyBreaker) -> None:
    """UT01-86 a network error on the SELECT is retried under `retry_page` with the same SQL
    and parameters; the breaker records one `SourceUnavailable`."""
    server = _server(errors={"SELECT": [sfe.OperationalError(msg="reset")]})
    rows = _rows(list(connector(server).sync("cost_center", None, UNTIL)))
    assert len(rows) == 4500
    first, second = [c for c in server.calls if c[0].startswith("SELECT")]
    assert first == second
    assert [type(e) for e in snowflake_env.failures] == [SourceUnavailable]


def test_ut01_86_null_key_or_timestamp_is_schema_violation(snowflake_env: SpyBreaker) -> None:
    """UT01-86 a null key → SchemaViolation; a null updated value → SchemaViolation."""
    del snowflake_env
    bad_key = table(0, 2).set_column(0, "CC_ID", pa.array([1, None], pa.int64()))
    with pytest.raises(SchemaViolation, match="null key"):
        list(connector(FakeSnowflake(tables=[bad_key])).sync("cost_center", None))
    bad_ts = table(0, 2).set_column(3, "UPDATED_AT", pa.array([T0, None], pa.timestamp("ns")))
    with pytest.raises(SchemaViolation, match="unparseable timestamp"):
        list(connector(FakeSnowflake(tables=[bad_ts])).sync("cost_center", None))


def test_ut01_86_unusable_key_or_colliding_columns(snowflake_env: SpyBreaker) -> None:
    """UT01-86 a key with a control character → SchemaViolation; two result columns with one
    lake name → SchemaViolation."""
    del snowflake_env
    bad = table(0, 1).set_column(1, "NAME", pa.array(["x"]))
    bad = pa.table({"CC_ID": ["a\x01"], "NAME": ["x"], "AMOUNT": bad["AMOUNT"], "UPDATED_AT": [T0]})
    with pytest.raises(SchemaViolation, match="invalid source key"):
        list(connector(FakeSnowflake(tables=[bad])).sync("cost_center", None))
    twin = table(0, 1).rename_columns(["CC_ID", "NAME", "Name", "UPDATED_AT"])
    with pytest.raises(SchemaViolation, match="column collision"):
        list(connector(FakeSnowflake(tables=[twin])).sync("cost_center", None))


def test_ut01_86_list_keys(snowflake_env: SpyBreaker) -> None:
    """UT01-86 `list_keys`: tag, guard, `SELECT <key> FROM <table> [WHERE (<filter>)] ORDER BY
    <key>`, keys cast to string as `KEY_SCHEMA` batches of ≤ `batch_rows`."""
    del snowflake_env
    server = FakeSnowflake(key_tables=[key_table(list(range(1500))), key_table([7, 8])])
    conn = connector(server, {"filter": "NAME IS NOT NULL"}, batch_rows=1000)
    batches = list(conn.list_keys("cost_center"))
    assert all(b.schema == KEY_SCHEMA for b in batches)
    assert [b.num_rows for b in batches] == [1000, 500, 2]
    keys = [k for b in batches for k in b.column("_source_key").to_pylist()]
    assert keys[:3] == ["0", "1", "2"]
    assert keys[-2:] == ["7", "8"]
    sql = (
        'SELECT "CC_ID" FROM "FINANCE"."PUBLIC"."COST_CENTER"'
        ' WHERE (NAME IS NOT NULL) ORDER BY "CC_ID"'
    )
    assert server.calls == [
        ("ALTER SESSION SET QUERY_TAG = %(tag)s", {"tag": "herness:cost_center"}),
        ("EXPLAIN USING JSON " + sql, {}),
        (sql, {}),
    ]
    no_filter = FakeSnowflake(key_tables=[key_table([None], pa.int64())])
    with pytest.raises(SchemaViolation, match="null key"):
        list(connector(no_filter).list_keys("cost_center"))
    assert no_filter.statements("SELECT") == [
        'SELECT "CC_ID" FROM "FINANCE"."PUBLIC"."COST_CENTER" ORDER BY "CC_ID"'
    ]


def test_ut01_86_protocols_names_and_watermark(snowflake_env: SpyBreaker) -> None:
    """UT01-86 name, entities, watermark field; Connector and SupportsKeyListing; an unknown
    entity → ConfigError."""
    del snowflake_env
    conn = connector(FakeSnowflake())
    assert conn.name == "snowflake"
    assert conn.entities == ("cost_center",)
    assert conn.watermark_field("cost_center") == "UPDATED_AT"
    assert isinstance(conn, SupportsKeyListing)
    typed: Connector = conn
    assert typed is conn
    with pytest.raises(ConfigError, match="unknown snowflake entity"):
        conn.watermark_field("nope")


def test_ut01_86_default_connect_is_the_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-86 without `connect` the driver's `snowflake.connector.connect` is used."""
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(sf_module.snowflake.connector, "connect", lambda **kw: seen.append(kw))
    conn = SnowflakeConnector(connector(FakeSnowflake())._settings)
    conn._connect(account="a")
    assert seen == [{"account": "a"}]


# --- UT01-87 scan guard and check -------------------------------------------------------------


def test_ut01_87_scan_guard_refuses_before_the_select(snowflake_env: SpyBreaker) -> None:
    """UT01-87 EXPLAIN reports 60 GB with `max_scan_gb` 50 → ConfigError before the SELECT."""
    del snowflake_env
    server = _server(bytes_assigned=60 * GB)
    with pytest.raises(ConfigError, match=r"^scan guard: 60\.0 GB exceeds max_scan_gb$"):
        list(connector(server, max_scan_gb=50).sync("cost_center", None))
    assert server.statements("SELECT") == []
    assert len(server.statements("EXPLAIN")) == 1


def test_ut01_87_scan_guard_limit_is_inclusive(snowflake_env: SpyBreaker) -> None:
    """UT01-87 exactly `max_scan_gb` passes; the key listing is guarded too."""
    del snowflake_env
    server = _server(bytes_assigned=50 * GB)
    assert len(_rows(list(connector(server, max_scan_gb=50).sync("cost_center", None)))) == 4500
    over = FakeSnowflake(key_tables=[key_table([1])], bytes_assigned=2 * GB)
    with pytest.raises(ConfigError, match="scan guard"):
        list(connector(over, max_scan_gb=1).list_keys("cost_center"))
    assert over.statements("SELECT") == []


@pytest.mark.parametrize(
    "explain",
    ['{"GlobalStats": {"partitionsTotal": 1}}', "[1, 2]", "not json", '{"GlobalStats": 3}'],
)
def test_ut01_87_explain_without_bytes_assigned(snowflake_env: SpyBreaker, explain: str) -> None:
    """UT01-87 an EXPLAIN plan without `GlobalStats.bytesAssigned` → SchemaViolation."""
    del snowflake_env
    server = _server(explain_json=explain)
    with pytest.raises(SchemaViolation, match="bytesAssigned"):
        list(connector(server).sync("cost_center", None))
    assert server.statements("SELECT") == []


def test_ut01_87_explain_with_no_row_is_schema_violation(
    snowflake_env: SpyBreaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-87 an EXPLAIN that returns no row → SchemaViolation."""
    del snowflake_env
    from tests.support import fake_snowflake  # noqa: PLC0415 - patched here only

    monkeypatch.setattr(fake_snowflake.FakeCursor, "fetchone", lambda _self: None)
    with pytest.raises(SchemaViolation, match="bytesAssigned"):
        list(connector(_server()).sync("cost_center", None))


def _warehouse(monitor: object = "HERNESS_RM", *, name: str = "HERNESS_XS") -> dict[str, Any]:
    row: dict[str, Any] = {"name": name, "state": "SUSPENDED", "size": "X-Small"}
    if monitor is not _ABSENT:
        row["resource_monitor"] = monitor
    return row


_ABSENT = object()


@pytest.mark.parametrize("monitor", ["", "null", None, _ABSENT, "NULL"])
def test_ut01_87_check_requires_a_resource_monitor(
    snowflake_env: SpyBreaker, monitor: object
) -> None:
    """UT01-87 `SHOW WAREHOUSES LIKE %(wh)s` without a monitor (empty, `null`, absent) →
    ConfigError("warehouse has no resource monitor")."""
    del snowflake_env
    server = FakeSnowflake(warehouses=[_warehouse(monitor)])
    with pytest.raises(ConfigError, match=r"^warehouse has no resource monitor$"):
        connector(server).check()
    assert server.calls == [("SHOW WAREHOUSES LIKE %(wh)s", {"wh": "HERNESS_XS"})]


def test_ut01_87_check_passes_with_a_monitor(snowflake_env: SpyBreaker) -> None:
    """UT01-87 a monitored warehouse passes; a `LIKE` wildcard match of another name is not
    taken for it; no warehouse row at all fails."""
    del snowflake_env
    server = FakeSnowflake(warehouses=[_warehouse(None, name="HERNESSAXS"), _warehouse()])
    connector(server).check()
    other = FakeSnowflake(warehouses=[_warehouse(name="HERNESSAXS")])
    with pytest.raises(ConfigError, match="resource monitor"):
        connector(other).check()
    with pytest.raises(ConfigError, match="resource monitor"):
        connector(FakeSnowflake()).check()


# --- UT01-88 error mapping --------------------------------------------------------------------


def test_ut01_88_auth_failure_on_connect(snowflake_env: SpyBreaker) -> None:
    """UT01-88 sqlstate 28000 at connect → AuthError; nothing is executed."""
    del snowflake_env
    error = sfe.DatabaseError(msg="JWT token is invalid", sqlstate="28000")
    server = FakeSnowflake(connect_error=error)
    with pytest.raises(AuthError, match="snowflake authentication failed") as info:
        list(connector(server).sync("cost_center", None))
    assert "JWT" not in str(info.value)
    assert info.value.__cause__ is None
    assert server.calls == []


def test_ut01_88_queue_timeout_is_rate_limited(snowflake_env: SpyBreaker) -> None:
    """UT01-88 sqlstate 57014 on the SELECT → RateLimited(retry_after=None) once retries stop."""
    errors = [sfe.ProgrammingError(msg="queued too long", sqlstate="57014") for _ in range(20)]
    server = _server(errors={"SELECT": errors})
    with pytest.raises(RateLimited) as info:
        list(connector(server).sync("cost_center", None))
    assert info.value.retry_after is None
    assert "queued" not in str(info.value)
    assert all(isinstance(e, RateLimited) for e in snowflake_env.failures)


def test_ut01_88_network_error_is_source_unavailable(snowflake_env: SpyBreaker) -> None:
    """UT01-88 a network error on every SELECT → SourceUnavailable once retries stop; a
    network error while reading Arrow batches → SourceUnavailable (not retried in place)."""
    del snowflake_env
    errors = [sfe.OperationalError(msg="socket reset") for _ in range(20)]
    with pytest.raises(SourceUnavailable, match="snowflake unavailable"):
        list(connector(_server(errors={"SELECT": errors})).sync("cost_center", None))
    mid = _server(fetch_error=sfe.InterfaceError(msg="connection reset"))
    batches: list[pa.RecordBatch] = []
    with pytest.raises(SourceUnavailable, match="snowflake unavailable"):
        batches.extend(connector(mid, batch_rows=1000).sync("cost_center", None))
    assert [b.num_rows for b in batches] == [1000, 500]
    assert len(mid.statements("SELECT")) == 1
    keys = FakeSnowflake(key_tables=[key_table([1]), key_table([2])])
    keys.fetch_error = sfe.OperationalError(msg="gone")
    with pytest.raises(SourceUnavailable):
        list(connector(keys).list_keys("cost_center"))


def test_ut01_88_errors_on_tag_explain_and_show_are_mapped(snowflake_env: SpyBreaker) -> None:
    """UT01-88 errors of the tag, EXPLAIN and SHOW statements are mapped (no driver text)."""
    del snowflake_env
    tag = FakeSnowflake(errors={"ALTER": [sfe.ProgrammingError(msg="bad", sqlstate="42000")]})
    with pytest.raises(ConfigError, match=r"^snowflake query rejected$"):
        list(connector(tag).sync("cost_center", None))
    explain = FakeSnowflake(errors={"EXPLAIN": [sfe.OperationalError(msg="x")]})
    with pytest.raises(SourceUnavailable):
        list(connector(explain).sync("cost_center", None))
    show = FakeSnowflake(errors={"SHOW": [sfe.DatabaseError(msg="x", sqlstate="28000")]})
    with pytest.raises(AuthError):
        connector(show).check()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (sfe.DatabaseError(msg="driver-said", sqlstate="28000"), AuthError),
        (sfe.ProgrammingError(msg="driver-said", sqlstate="28000"), AuthError),
        (sfe.OperationalError(msg="driver-said", sqlstate="28000"), AuthError),
        (sfe.OperationalError(msg="driver-said"), SourceUnavailable),
        (sfe.InterfaceError(msg="driver-said"), SourceUnavailable),
        (sfe.ProgrammingError(msg="driver-said", sqlstate="57014"), RateLimited),
        (sfe.ProgrammingError(msg="driver-said", sqlstate="42000"), ConfigError),
        (sfe.ProgrammingError(msg="driver-said"), ConfigError),
        (sfe.DatabaseError(msg="driver-said"), SourceUnavailable),
        (sfe.Error(msg="driver-said"), SourceUnavailable),
    ],
)
def test_ut01_88_error_mapping(exc: sfe.Error, expected: type[Exception]) -> None:
    """UT01-88 `_map_sf_error` per U01-88 step 3; the driver text never reaches the message."""
    mapped = _map_sf_error(exc)
    assert type(mapped) is expected
    assert "driver-said" not in str(mapped) + repr(mapped.context)
    assert mapped.context["source"] == "snowflake"
    if isinstance(mapped, RateLimited):
        assert mapped.retry_after is None


def test_ut01_88_missing_or_bad_credentials_are_config_errors(snowflake_env: SpyBreaker) -> None:
    """UT01-88 no `auth.credentials`, a credential without `user`/`private_key` or with an
    unreadable key → ConfigError; `connect` is never called."""
    del snowflake_env
    server = FakeSnowflake()
    section = connector(server)._settings.model_copy(update={"auth": None})
    with pytest.raises(ConfigError, match=r"snowflake auth\.credentials is required"):
        SnowflakeConnector(section, connect=server.connect).check()
    store_credential(user="")
    with pytest.raises(ConfigError, match="must hold user and private_key"):
        connector(server).check()
    armour = pem().splitlines()
    store_credential(private_key="\n".join([armour[0], "bm9wZQ==", armour[-1]]))
    with pytest.raises(ConfigError, match="private key cannot be read"):
        connector(server).check()
    store_credential(private_key=pem(UNLOCK))  # encrypted, no passphrase
    with pytest.raises(ConfigError, match="private key cannot be read"):
        connector(server).check()
    assert server.connects == []


def test_ut01_88_connect_logs_no_credential(snowflake_env: SpyBreaker) -> None:
    """UT01-88 connecting and syncing log no user key material."""
    del snowflake_env
    with capture_logs() as logs:
        list(connector(_server()).sync("cost_center", None))
    assert "PRIVATE KEY" not in repr(logs)


def test_ut01_86_key_not_castable_to_text_is_schema_violation(snowflake_env: SpyBreaker) -> None:
    """UT01-86 a binary key that is not UTF-8 → SchemaViolation, not a raw Arrow error."""
    del snowflake_env
    server = FakeSnowflake(key_tables=[key_table([b"\xff\xfe"], pa.binary())])
    with pytest.raises(SchemaViolation, match="invalid source key"):
        list(connector(server).list_keys("cost_center"))
