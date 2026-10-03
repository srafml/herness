"""Security tests of the Snowflake connector (impl 01 ST01-16; TH01-17, TH01-03, TH01-07;
T01-23).

The EXPLAIN scan guard stops a query that would scan more than `max_scan_gb` before the main
SELECT is sent (the fake cursor's call log is the evidence). The key-pair credential is
resolved when the connection is first needed and reaches only `connect`: no connector
attribute, log event, error message, context or chained cause carries it.
"""

from __future__ import annotations

import datetime

import pytest
from snowflake.connector import errors as sfe
from structlog.testing import capture_logs
from tests.support.fake_snowflake import FakeSnowflake
from tests.unit.connectors._mongo_data import SpyBreaker
from tests.unit.connectors._snowflake_data import GB, connector, naive, pem, store_credential, table

from herness.core.errors import AuthError, ConfigError, HernessError

pytestmark = pytest.mark.unit

_PEM_MARK = "PRIVATE KEY"


def test_st01_16_explain_at_ten_times_the_limit_never_runs_the_select(
    snowflake_env: SpyBreaker,
) -> None:
    """ST01-16 EXPLAIN reporting 10 times `max_scan_gb` → ConfigError; the main SELECT is never
    executed (fake cursor call log: tag and EXPLAIN only)."""
    del snowflake_env
    server = FakeSnowflake(tables=[table(0, 10)], bytes_assigned=10 * 5 * GB)
    with pytest.raises(ConfigError, match="scan guard"):
        list(connector(server, max_scan_gb=5).sync("cost_center", None))
    kinds = [sql.split(" ", 1)[0] for sql, _ in server.calls]
    assert kinds == ["ALTER", "EXPLAIN"]
    assert server.statements("SELECT") == []


def test_st01_16_filter_is_config_and_values_are_bound(snowflake_env: SpyBreaker) -> None:
    """ST01-16 (TH01-07) window values never appear in the SQL text; they are bound."""
    del snowflake_env
    server = FakeSnowflake(tables=[table(0, 1)])
    since = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    list(connector(server).sync("cost_center", since))
    for sql, params in server.calls:
        assert "2026" not in sql
        assert params is not None
    assert server.calls[-1][1] == {"since": naive(2026, 1, 1)}


def _no_key_in(error: HernessError, marker: str) -> None:
    text = str(error) + repr(error.context) + str(error.hint) + repr(error.details)
    assert marker not in text
    assert error.__cause__ is None


def test_st01_16_credential_not_kept_on_the_connector_or_logged(
    snowflake_env: SpyBreaker,
) -> None:
    """ST01-16 (TH01-03) after `check` and `sync`, no connector attribute other than the
    driver connection holds the user key and no captured log event carries it."""
    del snowflake_env
    server = FakeSnowflake(tables=[table(0, 3)], warehouses=[{"name": "HERNESS_XS"}])
    server.warehouses[0]["resource_monitor"] = "RM"
    conn = connector(server)
    with capture_logs() as logs:
        conn.check()
        list(conn.sync("cost_center", None))
    assert len(server.connects) == 1
    own = {k: v for k, v in vars(conn).items() if k != "_conn"}
    body = pem().splitlines()[1]
    assert body not in repr(own)
    assert _PEM_MARK not in repr(own)
    assert server.connects[0]["private_key"] not in repr(own).encode()
    assert body not in repr(logs)


def test_st01_16_errors_carry_no_key_material(snowflake_env: SpyBreaker) -> None:
    """ST01-16 (TH01-03) a driver error whose text echoes the key and an unreadable key both
    raise errors without the key text and without a chained cause."""
    del snowflake_env
    body = pem().splitlines()[1]
    echo = sfe.DatabaseError(msg=f"bad key {body}", sqlstate="28000")
    with pytest.raises(AuthError) as auth:
        connector(FakeSnowflake(connect_error=echo)).check()
    _no_key_in(auth.value, body)
    broken = pem().replace(body, body[::-1])
    store_credential(private_key_pem=broken)
    with pytest.raises(ConfigError) as bad:
        connector(FakeSnowflake()).check()
    _no_key_in(bad.value, body[::-1])
    assert bad.value.__suppress_context__


def test_st01_16_scan_guard_explains_the_percent_filter_as_sent(snowflake_env: SpyBreaker) -> None:
    """ST01-16 (TH01-17) with a `%` literal in the filter and bound window values, the EXPLAIN
    guard reaches the server (no raw driver formatting error) and explains exactly the SELECT
    text the server then receives."""
    del snowflake_env
    server = FakeSnowflake(tables=[table(0, 1)])
    since = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    list(connector(server, {"filter": "NAME LIKE '%A%'"}).sync("cost_center", since))
    explain, select = server.sent[1:]
    assert explain == "EXPLAIN USING JSON " + select
    assert "AND (NAME LIKE '%A%')" in select
